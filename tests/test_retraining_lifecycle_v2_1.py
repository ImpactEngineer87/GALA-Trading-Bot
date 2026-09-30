import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.retraining_lifecycle import RetrainingLifecycle
from agents.training import EntrenadorAgentes


class RetrainingLifecycleTests(unittest.TestCase):
    def _write_feedback(self, path: Path, ticker: str, count: int):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["ticker", "ganadora"])
            writer.writeheader()
            for index in range(count):
                writer.writerow({"ticker": ticker, "ganadora": index % 2})

    def _append_feedback(self, path: Path, ticker: str, count: int):
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["ticker", "ganadora"])
            for index in range(count):
                writer.writerow({"ticker": ticker, "ganadora": index % 2})

    def test_existing_history_becomes_installation_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feedback = root / "data" / "agents_feedback.csv"
            state = root / "data" / "retraining_state.json"
            models = root / "data" / "modelos_shadow"
            models.mkdir(parents=True)
            (models / "modelo_AMD.meta.json").write_text(
                json.dumps({"muestras": 80}), encoding="utf-8"
            )
            self._write_feedback(feedback, "AMD", 100)

            lifecycle = RetrainingLifecycle(
                feedback_path=str(feedback),
                state_path=str(state),
                shadow_models_dir=str(models),
                warning_samples=30,
                retrain_samples=50,
            )
            row = lifecycle.snapshot(["AMD"])[0]
            self.assertEqual(row["new_observations"], 0)
            self.assertEqual(row["last_attempt_count"], 100)
            self.assertEqual(row["status"], "COLLECTING")

    def test_30_is_review_and_50_is_pending_per_ticker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feedback = root / "feedback.csv"
            state = root / "state.json"
            self._write_feedback(feedback, "AMD", 10)
            lifecycle = RetrainingLifecycle(
                feedback_path=str(feedback),
                state_path=str(state),
                shadow_models_dir=str(root / "models"),
                warning_samples=30,
                retrain_samples=50,
            )
            lifecycle.refresh(["AMD", "AMZN"])

            self._append_feedback(feedback, "AMD", 30)
            row = lifecycle.snapshot(["AMD"])[0]
            self.assertEqual(row["new_observations"], 30)
            self.assertEqual(row["status"], "READY_REVIEW")
            self.assertEqual(lifecycle.eligible_tickers(["AMD", "AMZN"]), [])

            self._append_feedback(feedback, "AMD", 20)
            row = lifecycle.snapshot(["AMD"])[0]
            self.assertEqual(row["new_observations"], 50)
            self.assertEqual(row["status"], "PENDING_RETRAIN")
            self.assertEqual(lifecycle.eligible_tickers(["AMD", "AMZN"]), ["AMD"])

    def test_completed_attempt_resets_only_that_ticker_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feedback = root / "feedback.csv"
            state = root / "state.json"
            self._write_feedback(feedback, "AMD", 1)
            lifecycle = RetrainingLifecycle(
                feedback_path=str(feedback),
                state_path=str(state),
                shadow_models_dir=str(root / "models"),
                warning_samples=30,
                retrain_samples=50,
            )
            lifecycle.refresh(["AMD"])
            self._append_feedback(feedback, "AMD", 50)
            self.assertEqual(lifecycle.eligible_tickers(["AMD"]), ["AMD"])
            lifecycle.mark_training("AMD", 51)
            lifecycle.mark_result(
                "AMD",
                total_count=51,
                accepted=False,
                reason="candidate did not improve incumbent",
                metrics={"balanced_accuracy": 51.2, "shadow_model_saved": False},
            )
            row = lifecycle.snapshot(["AMD"])[0]
            self.assertEqual(row["new_observations"], 0)
            self.assertEqual(row["status"], "COLLECTING")
            self.assertEqual(row["last_result"], "REJECTED")
            self.assertEqual(row["last_balanced_accuracy"], 51.2)

    def test_runtime_error_keeps_batch_pending_for_next_session_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feedback = root / "feedback.csv"
            state = root / "state.json"
            self._write_feedback(feedback, "AMD", 1)
            lifecycle = RetrainingLifecycle(
                feedback_path=str(feedback),
                state_path=str(state),
                shadow_models_dir=str(root / "models"),
                warning_samples=30,
                retrain_samples=50,
            )
            lifecycle.refresh(["AMD"])
            self._append_feedback(feedback, "AMD", 50)
            lifecycle.mark_training("AMD", 51)
            lifecycle.mark_result(
                "AMD",
                total_count=51,
                accepted=False,
                error="temporary xgboost failure",
                consume_batch=False,
            )
            row = lifecycle.snapshot(["AMD"])[0]
            self.assertEqual(row["new_observations"], 50)
            self.assertEqual(row["status"], "PENDING_RETRAIN")
            self.assertEqual(row["last_result"], "ERROR")
            self.assertEqual(lifecycle.eligible_tickers(["AMD"]), ["AMD"])


class TrainerSchedulingTests(unittest.TestCase):
    def test_end_of_session_policy_never_schedules_on_trade_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {
                "agents": {
                    "shadow_mode": {
                        "enabled": True,
                        "tickers": ["AMD"],
                        "modelos_dir": str(root / "models"),
                    },
                    "entrenamiento": {
                        "activo": False,
                        "feedback_path": str(root / "feedback.csv"),
                        "modelos_dir": str(root / "prod"),
                        "automatic_retraining": {
                            "enabled": True,
                            "warning_samples": 30,
                            "retrain_samples": 50,
                            "only_after_session": True,
                            "state_path": str(root / "state.json"),
                            "auto_activate_production": False,
                        },
                    },
                }
            }
            trainer = EntrenadorAgentes(config)
            with patch.object(trainer, "_schedule_retrain_ticker") as schedule:
                progress = trainer.quizas_reentrenar_ticker("AMD", 50)
            schedule.assert_not_called()
            self.assertEqual(progress["new_observations"], 50)
            self.assertEqual(progress["status"], "PENDING_RETRAIN")


class PostSessionSafetyTests(unittest.TestCase):
    def _engine(self, *, paper=True, positions=None):
        from engine.bot_engine import ChocoletraBot
        import threading
        engine = ChocoletraBot.__new__(ChocoletraBot)
        engine._lock = threading.RLock()
        engine._posiciones = positions or {}
        engine.paper_trading = paper
        engine.account_number = "acct" if not paper else ""
        engine.tickers = ["AMD", "AMZN"]
        engine._asegurar_token_trading = lambda: True
        return engine

    def test_paper_requires_internal_positions_to_be_flat(self):
        engine = self._engine(paper=True)
        self.assertTrue(engine._post_session_training_safe())
        engine._posiciones["AMD"] = {"reservado": False}
        self.assertFalse(engine._post_session_training_safe())

    def test_real_requires_broker_positions_and_orders_to_be_flat(self):
        engine = self._engine(paper=False)

        class Broker:
            def obtener_posiciones(self, account):
                return []

            def obtener_ordenes_abiertas(self, account):
                return []

        engine.schwab = Broker()
        self.assertTrue(engine._post_session_training_safe())
        engine.schwab.obtener_ordenes_abiertas = lambda account: [
            {
                "orderLegCollection": [
                    {"instrument": {"symbol": "AMD"}}
                ]
            }
        ]
        self.assertFalse(engine._post_session_training_safe())


if __name__ == "__main__":
    unittest.main()
