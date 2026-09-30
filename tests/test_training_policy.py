import tempfile
import unittest
import json
from types import SimpleNamespace
from pathlib import Path

import numpy as np

from agents.training import (
    EntrenadorAgentes,
    FEATURES,
    FEATURE_VERSION,
    TRAINING_POLICY_VERSION,
)


class _PersistedModel:
    n_features_in_ = len(FEATURES)

    def predict_proba(self, values):
        return np.array([[0.25, 0.75] for _ in range(len(values))])


def _trainer(tmpdir):
    return EntrenadorAgentes({
        "agents": {
            "entrenamiento": {
                "modelos_dir": tmpdir,
                "feedback_path": f"{tmpdir}/feedback.csv",
                "min_balanced_accuracy_activacion": 0.55,
                "min_recall_ganadoras": 0.20,
                "min_precision_lift_pct": 5.0,
                "min_approval_rate_pct": 10.0,
                "max_approval_rate_pct": 80.0,
                "min_perdedoras_filtradas_pct": 55.0,
            }
        }
    })


class TrainingPolicyTests(unittest.TestCase):
    def test_cold_start_passes_in_paper_but_fails_closed_when_required(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trainer = _trainer(tmpdir)
            decision = SimpleNamespace(ticker="MSFT")

            self.assertEqual(trainer.evaluar(decision), (None, True))
            self.assertEqual(
                trainer.evaluar(decision, requerir_modelo=True),
                (None, False),
            )

    def test_incompatible_model_is_archived_not_left_active(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            active = Path(tmpdir) / "modelo_MSFT.pkl"
            active.write_bytes(b"legacy")

            trainer = _trainer(tmpdir)

            self.assertFalse(active.exists())
            self.assertFalse(trainer.modelo_disponible("MSFT"))
            self.assertEqual(
                len(list(Path(tmpdir).glob(
                    "modelo_MSFT.pkl.incompatible_*"))),
                1,
            )

    def test_always_veto_accuracy_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trainer = _trainer(tmpdir)
            y = np.array([0] * 100 + [1] * 50)
            probabilities = np.full(len(y), 0.10)
            metrics = trainer._metricas_probabilidades(
                y, probabilities, umbral=0.50)

            self.assertAlmostEqual(metrics["accuracy"], 2 / 3)
            self.assertAlmostEqual(metrics["baseline_accuracy"], 2 / 3)
            self.assertAlmostEqual(metrics["balanced_accuracy"], 0.50)
            self.assertEqual(metrics["recall"], 0.0)
            self.assertTrue(trainer._razones_rechazo(metrics))

    def test_validation_selects_a_non_degenerate_threshold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trainer = _trainer(tmpdir)
            y = np.array([0, 1] * 50)
            probabilities = np.array(
                [value for _ in range(50) for value in (0.20, 0.80)])
            threshold, metrics = trainer._seleccionar_umbral(
                y, probabilities)

            self.assertGreater(threshold, 0.20)
            self.assertLessEqual(threshold, 0.80)
            self.assertEqual(metrics["balanced_accuracy"], 1.0)
            self.assertEqual(metrics["recall"], 1.0)
            self.assertEqual(metrics["specificity"], 1.0)

    def test_good_balanced_filter_passes_activation_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trainer = _trainer(tmpdir)
            y = np.array([0, 1] * 50)
            probabilities = np.array(
                [value for _ in range(50) for value in (0.25, 0.75)])
            metrics = trainer._metricas_probabilidades(
                y, probabilities, umbral=0.50)

            self.assertEqual(trainer._razones_rechazo(metrics), [])

    def test_approved_model_survives_a_new_trainer_instance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trainer = _trainer(tmpdir)
            metadata = {
                "ticker": "MSFT",
                "muestras": 321,
                "decision_threshold": 0.61,
                "features": list(FEATURES),
                "feature_version": FEATURE_VERSION,
                "training_policy_version": TRAINING_POLICY_VERSION,
                "activado": True,
            }

            self.assertTrue(
                trainer._guardar("MSFT", _PersistedModel(), metadata)
            )
            self.assertTrue(
                json.loads(
                    (Path(tmpdir) / "modelo_MSFT.meta.json").read_text(
                        encoding="utf-8"
                    )
                )["activado"]
            )

            restarted = _trainer(tmpdir)

            self.assertTrue(restarted.modelo_disponible("MSFT"))
            self.assertEqual(restarted._umbrales["MSFT"], 0.61)
            self.assertEqual(restarted._ultimo_reentreno["MSFT"], 321)


if __name__ == "__main__":
    unittest.main()
