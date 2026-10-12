"""Operator per-ticker AI threshold (v3.3.19); offline, no broker or models.

Run: python -m unittest discover -s tests -p test_v3_3_19_ai_threshold_override.py
Client request (October 8): the AI is too restrictive (META threshold 69%),
so the operator needs to lower a ticker's threshold from the dashboard.
"""
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from flask import Flask

from agents import training as training_mod
from agents.training import EntrenadorAgentes
from api import v330_routes
from core.ai_threshold_overrides import AIThresholdOverrides, validate_threshold
from test_live_money_guard import _FixedModel


class _Decision:
    ticker = "META"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "overrides.json"

    def test_bounds_and_types(self):
        for value in (0.30, 0.6, "0.95"):
            self.assertGreaterEqual(validate_threshold(value), 0.30)
        for value in (0.29, 0.96, 60, True, None, "x", float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_threshold(value)

    def test_set_persists_and_clear_removes(self):
        store = AIThresholdOverrides(self.path)
        with self.assertRaises(ValueError):
            store.set("META", 0.6, reason="")
        row = store.set("meta", 0.6, actor="admin", reason="client request", model_threshold=0.69)
        self.assertEqual((row["threshold"], row["model_threshold"], row["updated_by"]), (0.6, 0.69, "admin"))
        self.assertEqual(AIThresholdOverrides(self.path).get("META"), 0.6)
        self.assertEqual(store.clear("META")["threshold"], 0.6)
        self.assertIsNone(AIThresholdOverrides(self.path).get("META"))
        self.assertIsNone(store.clear("META"))

    def test_unreadable_or_invalid_file_falls_back_to_model_thresholds(self):
        self.path.write_text("{not json", encoding="utf-8")
        store = AIThresholdOverrides(self.path)
        self.assertIsNone(store.get("META"))
        self.assertIn("No se pudo leer", store.load_error)
        self.path.write_text(json.dumps({"META": {"threshold": 0.05}, "BA": {"threshold": 0.65}}),
                             encoding="utf-8")
        store = AIThresholdOverrides(self.path)
        self.assertIsNone(store.get("META"))
        self.assertEqual(store.get("BA"), 0.65)

    def test_failed_write_keeps_previous_value(self):
        store = AIThresholdOverrides(self.path)
        store.set("META", 0.6, reason="first")
        with patch.object(store, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.set("META", 0.5, reason="second")
        self.assertEqual(store.get("META"), 0.6)


class TrainerDecisionTests(unittest.TestCase):
    def trainer(self, tmp, probability, model_threshold=0.69):
        from test_live_money_guard import LiveInferenceGateTests
        trainer = EntrenadorAgentes(LiveInferenceGateTests._config(None, tmp))
        trainer.umbrales_operador = AIThresholdOverrides(Path(tmp) / "overrides.json")
        trainer._modelos["META"] = _FixedModel(probability)
        trainer._umbrales["META"] = model_threshold
        trainer._production_snapshots["META"] = {"threshold": model_threshold, "model_version": "v1"}
        return trainer

    def evaluate(self, trainer, real=True):
        decision = _Decision()
        with patch.object(training_mod, "construir_features", return_value={}):
            return decision, trainer.evaluar(decision, requerir_modelo=real)

    def test_lower_operator_threshold_approves_and_is_reported(self):
        with TemporaryDirectory() as tmp:
            trainer = self.trainer(tmp, 0.62)
            _, (prob, approved) = self.evaluate(trainer)
            self.assertEqual((prob, approved), (0.62, False))
            trainer.umbrales_operador.set("META", 0.60, reason="client")
            decision, (prob, approved) = self.evaluate(trainer)
            self.assertEqual((prob, approved), (0.62, True))
            self.assertEqual(decision.ai_model_snapshot["threshold"], 0.60)
            self.assertEqual(decision.ai_model_snapshot["model_threshold"], 0.69)
            self.assertEqual(decision.ai_model_snapshot["threshold_source"], "OPERATOR")
            self.assertEqual(trainer.umbral_decision("META"), 0.60)
            self.assertEqual(trainer._umbrales["META"], 0.69, "model threshold untouched")

    def test_operator_threshold_can_also_be_stricter(self):
        with TemporaryDirectory() as tmp:
            trainer = self.trainer(tmp, 0.72)
            trainer.umbrales_operador.set("META", 0.80, reason="tighten")
            _, (_, approved) = self.evaluate(trainer)
            self.assertFalse(approved)

    def test_no_model_still_blocks_real_and_paper_observation_is_unchanged(self):
        with TemporaryDirectory() as tmp:
            trainer = self.trainer(tmp, 0.62)
            trainer.umbrales_operador.set("META", 0.30, reason="test")
            trainer._modelos.pop("META")
            decision, result = self.evaluate(trainer)
            self.assertEqual(result, (None, False))
            self.assertNotIn("threshold_source", decision.ai_model_snapshot)
            trainer = self.trainer(tmp, 0.10)
            trainer.umbrales_operador.set("META", 0.60, reason="test")
            _, (_, approved) = self.evaluate(trainer, real=False)
            self.assertTrue(approved, "PAPER with umbral_meta_filtro=0 only observes")


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from test_live_money_guard import LiveInferenceGateTests
        self.trainer = EntrenadorAgentes(LiveInferenceGateTests._config(None, self.tmp.name))
        self.trainer.umbrales_operador = AIThresholdOverrides(Path(self.tmp.name) / "overrides.json")
        self.trainer._modelos["META"] = _FixedModel(0.5)
        self.trainer._umbrales["META"] = 0.69
        self.audit = Path(self.tmp.name) / "audit.jsonl"
        self.state = {"tickers": ["META"], "running": False}
        app = Flask(__name__)
        app.secret_key = "offline-test"
        v330_routes.register(app, {
            "BOT_STATE": self.state, "_ENGINE_CONTROL_LOCK": threading.RLock(),
            "_ai_trainer_for_status": lambda: self.trainer,
            "_read_config_runtime": lambda: {"live_trading": {"authorization_audit_path": str(self.audit)}},
        })
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = "admin"

    def test_get_set_and_clear(self):
        data = self.client.get("/api/ai/threshold/META").get_json()
        self.assertEqual((data["model_threshold"], data["applied_threshold"], data["override"]), (0.69, 0.69, None))
        data = self.client.post("/api/ai/threshold/META", json={"threshold": 0.6, "reason": "client"}).get_json()
        self.assertEqual((data["applied_threshold"], data["override"]["updated_by"]), (0.6, "admin"))
        data = self.client.delete("/api/ai/threshold/META").get_json()
        self.assertEqual((data["applied_threshold"], data["override"]), (0.69, None))
        events = [json.loads(line)["event"] for line in self.audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events, ["AI_THRESHOLD_OVERRIDE_SET", "AI_THRESHOLD_OVERRIDE_CLEARED"])

    def test_rejections_change_nothing(self):
        # v3.3.25: a running engine and a missing reason are no longer refusals.
        cases = [({"threshold": 0.1, "reason": "x"}, 400), ({"threshold": 0.96}, 400),
                 ({"threshold": "x"}, 400), ({}, 400)]
        for body, status in cases:
            with self.subTest(body=body):
                response = self.client.post("/api/ai/threshold/META", json=body)
                self.assertEqual(response.status_code, status)
        self.assertIsNone(self.trainer.umbrales_operador.get("META"))
        self.assertFalse(self.audit.exists())
        self.assertEqual(self.client.get("/api/ai/threshold/NOPE").status_code, 404)


if __name__ == "__main__":
    unittest.main()
