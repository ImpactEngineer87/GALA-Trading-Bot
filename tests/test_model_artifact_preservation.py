"""Offline checks: production model paths are stable and artifacts never renamed."""
import json
import pickle
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agents.training import EntrenadorAgentes, FEATURES, FEATURE_VERSION, TRAINING_POLICY_VERSION
from core.project_paths import resolve_runtime_path


class ModelPreservationTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.models = self.root / "models"
        self.models.mkdir()
        self.config = {
            "agents": {
                "entrenamiento": {
                    "modelos_dir": str(self.models),
                    "feedback_path": str(self.root / "agentes_feedback.csv"),
                    "automatic_retraining": {
                        "state_path": str(self.root / "retraining_state.json")
                    },
                },
                "shadow_mode": {
                    "enabled": False,
                    "modelos_dir": str(self.root / "shadow"),
                },
            }
        }

    def test_relative_paths_resolve_against_project_root(self):
        result = resolve_runtime_path("data/modelos", project_root=self.root)
        self.assertEqual(Path(result), self.root / "data" / "modelos")
        absolute = str(self.root / "another_folder")
        self.assertEqual(resolve_runtime_path(absolute, project_root=Path("/tmp")), absolute)

    def test_legacy_missing_metadata_keeps_original_model_untouched(self):
        original = b"original model binary bytes"
        path = self.models / "modelo_AMD.pkl"
        path.write_bytes(original)
        trainer = EntrenadorAgentes(self.config)
        self.assertEqual(trainer.modelos_faltantes(["AMD"]), ["AMD"])
        self.assertEqual(path.read_bytes(), original)
        self.assertIn("metadata", trainer.model_load_errors["AMD"])
        self.assertEqual(sorted(p.name for p in self.models.iterdir()), ["modelo_AMD.pkl"])

    def test_schema_mismatch_does_not_move_model_or_metadata(self):
        model = self.models / "modelo_UAL.pkl"
        meta = self.models / "modelo_UAL.meta.json"
        model.write_bytes(b"untouched")
        original_meta = b'{"feature_version": 1, "features": ["wrong"], "training_policy_version": 1}'
        meta.write_bytes(original_meta)
        trainer = EntrenadorAgentes(self.config)
        self.assertFalse(trainer.modelo_disponible("UAL"))
        self.assertEqual(model.read_bytes(), b"untouched")
        self.assertEqual(meta.read_bytes(), original_meta)
        self.assertEqual(
            sorted(path.name for path in self.models.iterdir()),
            ["modelo_UAL.meta.json", "modelo_UAL.pkl"],
        )

    def test_compatible_model_still_loads_with_unchanged_threshold(self):
        model = self.models / "modelo_RCL.pkl"
        meta = self.models / "modelo_RCL.meta.json"
        with model.open("wb") as handle:
            pickle.dump(SimpleNamespace(n_features_in_=len(FEATURES)), handle)
        original_meta = json.dumps({
            "feature_version": FEATURE_VERSION,
            "training_policy_version": TRAINING_POLICY_VERSION,
            "features": FEATURES,
            "decision_threshold": 0.77,
            "muestras": 91,
        }).encode("utf-8")
        meta.write_bytes(original_meta)
        trainer = EntrenadorAgentes(self.config)
        self.assertTrue(trainer.modelo_disponible("RCL"))
        self.assertAlmostEqual(trainer._umbrales["RCL"], 0.77)
        self.assertEqual(meta.read_bytes(), original_meta)
        self.assertTrue(trainer.production_snapshot("RCL").get("loaded"))


if __name__ == "__main__":
    unittest.main()
