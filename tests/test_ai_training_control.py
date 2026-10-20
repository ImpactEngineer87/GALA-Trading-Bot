"""Offline tests for audited, operator-activated SHADOW retraining."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.ai_training_control import AITrainingControl


class AITrainingControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temporary)
        self.cfg = {
            "state_path": str(self.root / "control.json"),
            "audit_path": str(self.root / "audit.jsonl"),
        }
        self.control = AITrainingControl(self.cfg)

    def test_original_configuration_is_unchanged_until_operator_opt_in(self):
        p = self.control.policy(
            configured_enabled=False, configured_only_after_session=True,
            configured_auto_activate_production=False,
        )
        self.assertFalse(p["enabled"])
        self.assertEqual(p["source"], "CONFIG")
        self.assertFalse((self.root / "control.json").exists())

    def test_persisted_opt_in_survives_restart_without_touching_training_data(self):
        original = self.root / "historical-feedback.csv"
        original.write_bytes(b"do not modify historical evidence")
        saved = self.control.set_enabled(True, "chris")
        self.assertTrue(saved["enabled"])
        self.assertTrue(saved["only_after_session"])
        self.assertFalse(saved["auto_activate_production"])
        fresh = AITrainingControl(self.cfg)
        p = fresh.policy(configured_enabled=False)
        self.assertEqual(p["source"], "OPERATOR")
        self.assertTrue(p["enabled"])
        self.assertTrue(p["only_after_session"])
        self.assertFalse(p["auto_activate_production"])
        self.assertEqual(original.read_bytes(), b"do not modify historical evidence")
        self.assertEqual(
            json.loads((self.root / "audit.jsonl").read_text().splitlines()[0])["actor"],
            "chris",
        )

    def test_disable_does_not_delete_histories_or_model_files(self):
        saved_model = self.root / "modelo_BA.pkl"
        saved_model.write_bytes(b"real production model")
        self.control.set_enabled(True, "operator")
        self.control.set_enabled(False, "operator")
        self.assertFalse(self.control.policy(configured_enabled=True)["enabled"])
        self.assertEqual(saved_model.read_bytes(), b"real production model")

    def test_rejects_invalid_toggle_and_unauthenticated_actor(self):
        for v in (1, 0, None, "true", [], {}):
            with self.subTest(v=v), self.assertRaises(ValueError):
                self.control.set_enabled(v, "operator")
        with self.assertRaises(ValueError):
            self.control.set_enabled(True, "")
        self.assertFalse((self.root / "control.json").exists())

    def test_corrupt_operator_state_fails_closed(self):
        self.control.set_enabled(True, "operator")
        (self.root / "control.json").write_text("{bad-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.control.policy(configured_enabled=True)

    def test_audit_failure_prevents_policy_change(self):
        with patch.object(self.control.history, "append", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.control.set_enabled(True, "operator")
        self.assertFalse((self.root / "control.json").exists())


class TrainerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temporary)
        self.config = {
            "agents": {
                "entrenamiento": {
                    "activo": False,
                    "modelos_dir": str(self.root / "production"),
                    "feedback_path": str(self.root / "agentes_feedback.csv"),
                    "automatic_retraining": {
                        "enabled": False,
                        "only_after_session": True,
                        "auto_activate_production": False,
                        "state_path": str(self.root / "retraining_state.json"),
                    },
                    "ai_training_control": {
                        "state_path": str(self.root / "control.json"),
                        "audit_path": str(self.root / "control_audit.jsonl"),
                    },
                },
                "shadow_mode": {
                    "enabled": True,
                    "modelos_dir": str(self.root / "shadow"),
                    "tickers": ["AAPL", "BA"],
                },
            },
        }

    def trainer(self):
        from agents.training import EntrenadorAgentes
        return EntrenadorAgentes(self.config)

    def test_refresh_without_restart_and_no_intraday_auto_training(self):
        trainer = self.trainer()
        self.assertFalse(trainer.auto_retrain_enabled)
        trainer.ai_training_control.set_enabled(True, "operator")
        self.assertTrue(trainer.refresh_automatic_retraining_policy()["enabled"])
        self.assertTrue(trainer.auto_retrain_only_after_session)
        self.assertFalse(trainer.auto_activate_production)
        with patch.object(trainer, "_schedule_retrain_ticker",
                          side_effect=AssertionError("Intraday XGBoost forbidden")):
            trainer.quizas_reentrenar_ticker("AAPL", 0)
        trainer.ai_training_control.set_enabled(False, "operator")
        self.assertFalse(trainer.refresh_automatic_retraining_policy()["enabled"])

    def test_manual_eligible_retraining_does_not_require_auto_enabled(self):
        trainer = self.trainer()
        with patch.object(trainer.retraining, "eligible_tickers", return_value=["AAPL"]) as elig, \
             patch("agents.training.threading.Thread") as background:
            self.assertTrue(trainer.solicitar_reentreno_todos(
                ["AAPL"], solo_elegibles=True, manual=True
            ))
            elig.assert_called_once_with(["AAPL"])
            background.return_value.start.assert_called_once()
            self.assertFalse(trainer.solicitar_reentreno_todos(
                ["AAPL"], solo_elegibles=True
            ))

    def test_operator_policy_overrides_unsafe_legacy_automatic_settings(self):
        self.config["agents"]["entrenamiento"]["automatic_retraining"] = {
            "enabled": False, "only_after_session": False,
            "auto_activate_production": True,
            "state_path": str(self.root / "retraining_state.json"),
        }
        trainer = self.trainer()
        trainer.ai_training_control.set_enabled(True, "operator")
        policy = trainer.refresh_automatic_retraining_policy()
        self.assertTrue(policy["only_after_session"])
        self.assertFalse(policy["auto_activate_production"])
        self.assertFalse(trainer.auto_activate_production)


class DashboardTrainingControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temporary)
        from agents.training import EntrenadorAgentes
        self.trainer = EntrenadorAgentes({
            "agents": {
                "entrenamiento": {
                    "activo": False,
                    "modelos_dir": str(self.root / "models"),
                    "feedback_path": str(self.root / "feedback.csv"),
                    "automatic_retraining": {
                        "enabled": False, "only_after_session": True,
                        "state_path": str(self.root / "lifecycle.json"),
                    },
                    "ai_training_control": {
                        "state_path": str(self.root / "control.json"),
                        "audit_path": str(self.root / "audit.jsonl"),
                    },
                },
                "shadow_mode": {"enabled": True, "modelos_dir": str(self.root / "shadow")},
            },
        })

    def test_authenticated_csrf_checked_api_toggles_without_broker_io(self):
        import server
        client = server.app.test_client()
        with client.session_transaction() as ses:
            ses["logged_in"] = True
            ses["username"] = "operator"
            ses["csrf_token"] = "csrf-test-token"
        with patch.object(server, "_ai_trainer_for_status", return_value=self.trainer), \
             patch.object(server, "_read_config_runtime", return_value={
                 "agents": {"entrenamiento": {"reentrenar_fin_sesion": True}}
             }), \
             patch.object(server, "_log"), \
             patch("requests.sessions.Session.request", side_effect=AssertionError("broker network")):
            denied = client.post("/api/ai/retraining-control", json={"enabled": True})
            self.assertEqual(denied.status_code, 403)
            res = client.post("/api/ai/retraining-control", json={"enabled": True},
                              headers={"X-CSRF-Token": "csrf-test-token"})
            self.assertEqual(res.status_code, 200, res.get_json())
            state = res.get_json()
            self.assertTrue(state["automatic_retraining"])
            self.assertFalse(state["auto_activate_production"])
            self.assertFalse(state["training_started"])
            self.assertFalse(state["broker_orders_sent"])
            self.assertTrue(self.trainer.auto_retrain_enabled)
            off = client.post("/api/ai/retraining-control", json={"enabled": False},
                              headers={"X-CSRF-Token": "csrf-test-token"})
            self.assertEqual(off.status_code, 200)
            self.assertFalse(self.trainer.auto_retrain_enabled)


if __name__ == "__main__":
    unittest.main()
