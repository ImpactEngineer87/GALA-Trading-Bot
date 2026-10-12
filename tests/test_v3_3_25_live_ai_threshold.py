"""Per-ticker AI threshold editable while the engine runs (v3.3.25); offline.

Client request (October 12): some tickers have a very high AI threshold and
block signals; the client must be able to set each ticker's threshold. Until
now a change required stopping the engine, which runs the kill switch.
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
from agents.veto_observation_store import VetoObservationStore
from api import v330_routes
from core.ai_threshold_overrides import AIThresholdOverrides
from test_live_money_guard import _FixedModel


class _Decision:
    ticker = "META"


class LiveThresholdRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from test_live_money_guard import LiveInferenceGateTests
        self.trainer = EntrenadorAgentes(LiveInferenceGateTests._config(None, self.tmp.name))
        self.trainer.umbrales_operador = AIThresholdOverrides(Path(self.tmp.name) / "overrides.json")
        self.trainer._modelos["META"] = _FixedModel(0.62)
        self.trainer._umbrales["META"] = 0.69
        self.audit = Path(self.tmp.name) / "audit.jsonl"
        self.logs = []
        self.state = {"tickers": ["META"], "running": True}
        app = Flask(__name__)
        app.secret_key = "offline-test"
        v330_routes.register(app, {
            "BOT_STATE": self.state, "_ENGINE_CONTROL_LOCK": threading.RLock(),
            "_ai_trainer_for_status": lambda: self.trainer,
            "_read_config_runtime": lambda: {"live_trading": {"authorization_audit_path": str(self.audit)}},
            "_log": lambda level, message: self.logs.append((level, message)),
        })
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = "admin"

    def evaluate(self):
        decision = _Decision()
        with patch.object(training_mod, "construir_features", return_value={}):
            return decision, self.trainer.evaluar(decision, requerir_modelo=True)

    def test_running_engine_applies_a_change_to_the_next_decision(self):
        _, (_, approved) = self.evaluate()
        self.assertFalse(approved, "0.62 is below the model's 69%")
        response = self.client.post("/api/ai/threshold/META", json={"threshold": 0.6})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["applied_threshold"], 0.6)
        decision, (_, approved) = self.evaluate()
        self.assertTrue(approved)
        self.assertEqual(decision.ai_model_snapshot["threshold_source"], "OPERATOR")
        self.assertEqual(self.trainer._umbrales["META"], 0.69, "model threshold untouched")

    def test_reason_is_optional_and_every_change_is_audited_and_logged(self):
        self.client.post("/api/ai/threshold/META", json={"threshold": 0.55})
        self.assertEqual(self.trainer.umbrales_operador.row("META")["reason"],
                         "Ajuste desde el dashboard")
        self.client.delete("/api/ai/threshold/META")
        events = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["event"] for e in events],
                         ["AI_THRESHOLD_OVERRIDE_SET", "AI_THRESHOLD_OVERRIDE_CLEARED"])
        self.assertEqual(events[0]["actor"], "admin")
        self.assertEqual([level for level, _ in self.logs], ["WARN", "INFO"])
        self.assertIn("55%", self.logs[0][1])
        _, (_, approved) = self.evaluate()
        self.assertFalse(approved, "back on the model's 69%")

    def test_out_of_range_is_refused_while_running(self):
        for value in (0.29, 0.96):
            with self.subTest(value=value):
                response = self.client.post("/api/ai/threshold/META", json={"threshold": value})
                self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.trainer.umbrales_operador.get("META"))
        self.assertFalse(self.audit.exists())


class ThresholdVetoCountTests(unittest.TestCase):
    def test_only_threshold_vetoes_are_counted_as_changeable(self):
        with TemporaryDirectory() as tmp:
            store = VetoObservationStore(str(Path(tmp) / "vetoes.csv"))
            for index, reason in enumerate(
                    ("AI_THRESHOLD", "AI_THRESHOLD", "HARD_VETO:LATERAL_RANGE", "REGIME_VETO")):
                store.record_veto(
                    ticker="META", tipo="BUY", probability=0.5, threshold=0.69,
                    entry_price=100., stop_loss=99., take_profit=102.,
                    execution_mode="REAL", veto_reason=reason, signal_id=f"s{index}")
            row = {r["ticker"]: r for r in store.summary_by_ticker(["META", "TSLA"])}
            self.assertEqual((row["META"]["veto_total"], row["META"]["veto_threshold"]), (4, 2))
            self.assertEqual(row["TSLA"]["veto_threshold"], 0)
            reloaded = VetoObservationStore(str(Path(tmp) / "vetoes.csv"))
            self.assertEqual(reloaded.summary_by_ticker(["META"])[0]["veto_threshold"], 2)


if __name__ == "__main__":
    unittest.main()
