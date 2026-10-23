"""Manual AI training must work when automatic retraining is OFF.

All worker starts are mocked. Never touches a real broker or model directory.
"""
import unittest
import threading
from unittest.mock import MagicMock, patch

from agents.training import EntrenadorAgentes


class ManualAiTrainControlsTests(unittest.TestCase):
    @staticmethod
    def trainer(auto_enabled=False, eligible=("RCL", "MSFT", "ADBE")):
        trainer = EntrenadorAgentes.__new__(EntrenadorAgentes)
        trainer.auto_retrain_enabled = auto_enabled
        trainer._lock = threading.RLock()
        trainer._entrenando = set()
        trainer.activo = False
        trainer.shadow_enabled = True
        trainer.retraining = MagicMock()
        trainer.retraining.eligible_tickers.return_value = list(eligible)
        trainer.entrenar_todos = MagicMock()
        return trainer

    @patch("agents.training.threading.Thread")
    def test_manual_pending_only_works_with_auto_off(self, thread_cls):
        trainer = self.trainer(False)
        launched = trainer.solicitar_reentreno_todos(
            ["RCL", "MSFT", "ADBE"], solo_elegibles=True,
            manual_request=True,
        )
        self.assertTrue(launched)
        trainer.retraining.eligible_tickers.assert_called_once_with(
            ["RCL", "MSFT", "ADBE"]
        )
        kwargs = thread_cls.call_args.kwargs
        self.assertEqual(kwargs["target"], trainer._retrain_eligible_batch)
        self.assertEqual(kwargs["args"], (["RCL", "MSFT", "ADBE"], True))
        thread_cls.return_value.start.assert_called_once()
        self.assertFalse(trainer.auto_retrain_enabled)

    @patch("agents.training.threading.Thread")
    def test_unattended_scheduler_stays_off_when_auto_off(self, thread_cls):
        trainer = self.trainer(False)
        launched = trainer.solicitar_reentreno_todos(
            ["RCL"], solo_elegibles=True
        )
        self.assertFalse(launched)
        thread_cls.assert_not_called()

    @patch("agents.training.threading.Thread")
    def test_manual_cannot_train_without_new_50_eligible_closes(self, thread_cls):
        trainer = self.trainer(False, eligible=())
        launched = trainer.solicitar_reentreno_todos(
            ["BA"], solo_elegibles=True, manual_request=True
        )
        self.assertFalse(launched)
        thread_cls.assert_not_called()
        trainer.retraining.eligible_tickers.assert_called_once_with(["BA"])

    def test_manual_batch_trains_shadow_only_and_persists_candidate_result(self):
        trainer = self.trainer(False)
        trainer._lock = threading.RLock()
        trainer._entrenando = set()
        trainer.auto_activate_production = True
        trainer.retraining.register_closed.return_value = {
            "total_observations": 511
        }
        metrics = {
            "shadow_candidate_accepted": False,
            "shadow_candidate_reason": "candidate did not pass normal TEST gates",
        }
        trainer.entrenar_ticker = MagicMock(return_value=metrics)
        result = trainer._retrain_eligible_batch(["RCL"], manual_request=True)
        self.assertEqual(result["RCL"], metrics)
        trainer.entrenar_ticker.assert_called_once_with(
            "RCL", allow_production_activation=False, automatic=True,
        )
        self.assertEqual(
            trainer.retraining.mark_result.call_args.kwargs["consume_batch"], True
        )
        self.assertEqual(
            trainer.retraining.mark_result.call_args.kwargs["accepted"], False
        )
        self.assertEqual(trainer._entrenando, set())

    def test_missing_training_data_keeps_50_trades_pending(self):
        trainer = self.trainer(False)
        trainer._lock = threading.RLock()
        trainer._entrenando = set()
        trainer.auto_activate_production = False
        trainer.retraining.register_closed.return_value = {
            "total_observations": 511
        }
        trainer.entrenar_ticker = MagicMock(return_value=None)
        trainer._retrain_eligible_batch(["RCL"], manual_request=True)
        kwargs = trainer.retraining.mark_result.call_args.kwargs
        self.assertFalse(kwargs["consume_batch"])
        self.assertTrue(kwargs["deferred"])
        self.assertFalse(kwargs["accepted"])

    @patch("agents.training.threading.Thread")
    def test_auto_enabled_keeps_existing_scheduler_path(self, thread_cls):
        trainer = self.trainer(True, eligible=("MSFT",))
        launched = trainer.solicitar_reentreno_todos(
            ["RCL", "MSFT"], solo_elegibles=True
        )
        self.assertTrue(launched)
        kwargs = thread_cls.call_args.kwargs
        self.assertEqual(kwargs["args"], (["MSFT"],))
        thread_cls.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()

