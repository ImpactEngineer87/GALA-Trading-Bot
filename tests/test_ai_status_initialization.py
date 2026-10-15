"""Dashboard polling initializes one read-only AI status trainer."""

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server
from agents.training import EntrenadorAgentes


class AIStatusInitializationTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.state = self.root / "retraining_state.json"
        self.config = {
            "agents": {
                "entrenamiento": {
                    "modelos_dir": str(self.root / "models"),
                    "feedback_path": str(self.root / "feedback.csv"),
                    "automatic_retraining": {"state_path": str(self.state)},
                },
                "shadow_mode": {
                    "modelos_dir": str(self.root / "shadow"),
                    "tickers": ["AAPL"],
                },
            },
        }
        for name in ("_AI_STATUS_TRAINER", "_AI_STATUS_SIGNATURE", "_ENGINE"):
            self.enterContext(patch.object(server, name, None))
        self.enterContext(patch.dict(server.BOT_STATE, {"running": False}))
        self.enterContext(patch.object(server, "_read_config_runtime", return_value=self.config))
        self.network = self.enterContext(patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Unexpected HTTP request"),
        ))

    def tearDown(self):
        self.network.assert_not_called()

    def test_concurrent_dashboard_requests_share_one_initialized_trainer(self):
        start = threading.Barrier(8)
        entered = threading.Event()
        duplicate = threading.Event()
        release = threading.Event()
        counter_lock = threading.Lock()
        constructions = []
        trainer = object()

        def construct(config):
            with counter_lock:
                constructions.append(config)
                if len(constructions) > 1:
                    duplicate.set()
            entered.set()
            if not release.wait(3):
                raise TimeoutError("Test constructor was not released")
            return trainer

        def request_status():
            start.wait(timeout=3)
            return server._ai_trainer_for_status()

        with patch("agents.training.EntrenadorAgentes", side_effect=construct):
            with ThreadPoolExecutor(max_workers=8) as workers:
                futures = [workers.submit(request_status) for _ in range(8)]
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertFalse(duplicate.wait(0.1), "Concurrent trainer construction")
                finally:
                    release.set()
                results = [future.result(timeout=3) for future in futures]
        self.assertEqual(len(constructions), 1)
        self.assertTrue(all(result is trainer for result in results))

    def test_changed_model_artifact_refreshes_cache_once(self):
        first, second = object(), object()
        with patch("agents.training.EntrenadorAgentes", side_effect=[first, second]) as factory:
            self.assertIs(server._ai_trainer_for_status(), first)
            self.assertIs(server._ai_trainer_for_status(), first)
            models = self.root / "models"
            models.mkdir()
            (models / "modelo_AAPL.meta.json").write_text("{}", encoding="utf-8")
            self.assertIs(server._ai_trainer_for_status(), second)
            self.assertIs(server._ai_trainer_for_status(), second)
        self.assertEqual(factory.call_count, 2)

    def test_running_engine_uses_its_own_trainer(self):
        trainer = object()
        server._ENGINE = SimpleNamespace(orquestador=SimpleNamespace(entrenador=trainer))
        server.BOT_STATE["running"] = True
        with patch("agents.training.EntrenadorAgentes") as factory:
            self.assertIs(server._ai_trainer_for_status(), trainer)
        factory.assert_not_called()

    def test_failed_initialization_can_be_retried_without_publishing_bad_cache(self):
        trainer = object()
        with patch("agents.training.EntrenadorAgentes", side_effect=[RuntimeError("load failed"), trainer]):
            with self.assertRaisesRegex(RuntimeError, "load failed"):
                server._ai_trainer_for_status()
            self.assertIsNone(server._AI_STATUS_TRAINER)
            self.assertIsNone(server._AI_STATUS_SIGNATURE)
            self.assertIs(server._ai_trainer_for_status(), trainer)

    def test_actual_status_initialization_does_not_create_lifecycle_file(self):
        trainer = server._ai_trainer_for_status()
        rows = trainer.retraining_status(["AAPL"])
        self.assertEqual(rows[0]["ticker"], "AAPL")
        self.assertFalse(self.state.exists())
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_actual_trainer_load_preserves_persisted_progress_bytes_and_mtime(self):
        original = json.dumps({"schema_version": 3, "tickers": {"AAPL": {
            "total_observations": 100,
            "last_attempt_count": 100,
            "last_success_count": 100,
            "last_result": "ACCEPTED",
            "last_attempt_utc": "2026-10-16T00:00:00+00:00",
            "counting_basis": "execution_valid",
        }}}).encode("utf-8")
        self.state.write_bytes(original)
        original_mtime = self.state.stat().st_mtime_ns
        trainer = EntrenadorAgentes(self.config)
        self.assertEqual(trainer.retraining_status(["AAPL"])[0]["last_result"], "ACCEPTED")
        self.assertEqual(self.state.read_bytes(), original)
        self.assertEqual(self.state.stat().st_mtime_ns, original_mtime)
        self.assertEqual(list(self.root.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
