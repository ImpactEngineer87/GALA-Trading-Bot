"""Offline regression tests for model-pinned manual probability thresholds."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.ai_threshold_override import AIThresholdOverrides, validate_threshold


class ThresholdStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state_path = root / "override.json"
        self.audit_path = root / "audit.jsonl"
        self.policy = AIThresholdOverrides({
            "state_path": str(self.state_path),
            "audit_path": str(self.audit_path),
        })

    def test_persists_and_expires_on_model_change(self):
        self.policy.set("aapl", "model-v1", 0.60, "operator")
        self.assertEqual(self.policy.effective("AAPL", "model-v1", 0.72), 0.60)
        # A restarted trainer reads the same JSON; no migration or model change.
        fresh = AIThresholdOverrides({
            "state_path": str(self.state_path),
            "audit_path": str(self.audit_path),
        })
        self.assertEqual(fresh.effective("AAPL", "model-v1", 0.72), 0.60)
        self.assertEqual(fresh.effective("AAPL", "model-v2", 0.69), 0.69)
        self.policy.reset("AAPL", "operator")
        self.assertIsNone(fresh.active("AAPL", "model-v1"))
        events = [json.loads(line)["event"] for line in self.audit_path.read_text().splitlines()]
        self.assertEqual(events, [
            "AI_THRESHOLD_CHANGE_REQUESTED", "AI_THRESHOLD_RESET_REQUESTED"
        ])

    def test_rejects_invalid_and_non_finite_values_without_state_change(self):
        for value in [False, True, "0.55", 0.29, 0.96, 1.0, float("nan"), float("inf")]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_threshold(value)
        self.assertFalse(self.state_path.exists())
        self.assertEqual(validate_threshold(0.30), 0.30)
        self.assertEqual(validate_threshold(0.95), 0.95)

    def test_unreadable_state_raises_instead_of_ignoring_policy(self):
        self.policy.set("TSLA", "model-v1", 0.58, "operator")
        self.state_path.write_text("{corrupted", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.policy.effective("TSLA", "model-v1", 0.75)

    def test_operator_and_version_are_required(self):
        with self.assertRaises(ValueError):
            self.policy.set("AAPL", "", 0.60, "operator")
        with self.assertRaises(ValueError):
            self.policy.set("AAPL", "v1", 0.60, "")
        with self.assertRaises(ValueError):
            self.policy.set("../AAPL", "v1", 0.60, "operator")


class ThresholdInferenceTests(unittest.TestCase):
    def test_manual_threshold_applies_to_next_real_decision(self):
        try:
            from agents.training import EntrenadorAgentes
            from agents import training
            import numpy as np
        except ImportError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as root:
            overrides = AIThresholdOverrides({
                "state_path": str(Path(root) / "overrides.json"),
                "audit_path": str(Path(root) / "overrides.audit.jsonl"),
            })
            class Model:
                def predict_proba(self, features):
                    return np.array([[0.35, 0.65]])

            trainer = object.__new__(EntrenadorAgentes)
            trainer._lock = threading.Lock()
            trainer._modelos = {"AAPL": Model()}
            trainer._umbrales = {"AAPL": 0.72}
            trainer._production_snapshots = {
                "AAPL": {"model_version": "model-v1", "threshold": 0.72}
            }
            trainer.umbral_meta = 0.5
            trainer.activo = True
            trainer.manual_real_authorization = None
            trainer.manual_threshold_overrides = overrides
            with patch.object(training, "construir_features", return_value={}):
                first = SimpleNamespace(ticker="AAPL")
                self.assertEqual(trainer.evaluar(first, requerir_modelo=True), (0.65, False))
                overrides.set("AAPL", "model-v1", 0.60, "operator")
                second = SimpleNamespace(ticker="AAPL")
                self.assertEqual(trainer.evaluar(second, requerir_modelo=True), (0.65, True))
                self.assertEqual(second.ai_model_snapshot["threshold"], 0.60)
                self.assertEqual(second.ai_model_snapshot["model_threshold"], 0.72)
                self.assertEqual(second.ai_model_snapshot["threshold_source"], "MANUAL")
                # Corrupt policy must NOT open a REAL entry.
                Path(root, "overrides.json").write_text("not json", encoding="utf-8")
                third = SimpleNamespace(ticker="AAPL")
                self.assertEqual(trainer.evaluar(third, requerir_modelo=True), (None, False))
                self.assertTrue(third.ai_inference_error)


if __name__ == "__main__":
    unittest.main()
