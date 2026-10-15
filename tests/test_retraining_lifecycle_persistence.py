"""Offline regressions for atomic lifecycle writes and read-only snapshots."""

import json
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agents import retraining_lifecycle
from agents.retraining_lifecycle import RetrainingLifecycle, STATE_SCHEMA_VERSION


class RetrainingLifecyclePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.state_path = self.root / "retraining_state.json"

    def make_lifecycle(self, state_path=None):
        return RetrainingLifecycle(
            feedback_path=str(self.root / "feedback.csv"),
            state_path=str(state_path or self.state_path),
            shadow_models_dir=str(self.root / "models"),
            warning_samples=30,
            retrain_samples=50,
        )

    def seed_state(self):
        writer = self.make_lifecycle()
        writer.register_closed("AAPL", 50)
        writer.mark_result(
            "AAPL", total_count=50, accepted=True, reason="accepted result",
            metrics={"evaluation_id": "persisted-evaluation", "balanced_accuracy": 72.5},
        )
        return writer

    def test_independent_writers_replace_their_own_complete_temp_files(self):
        writers = [self.make_lifecycle(), self.make_lifecycle()]
        rendezvous = threading.Barrier(2)
        real_replace = os.replace
        observed = []
        observation_lock = threading.Lock()

        def replace_together(source, destination):
            source_path = Path(source)
            self.assertEqual(source_path.parent.resolve(), self.root.resolve())
            self.assertEqual(Path(destination), self.state_path)
            # Both JSON files must be fully serialized before either rename.
            payload = json.loads(source_path.read_text(encoding="utf-8"))
            with observation_lock:
                observed.append((source_path, payload))
            rendezvous.wait(timeout=5)
            # Serialize the actual system calls for Windows, which can reject
            # concurrent destination handles. Both writers have already reached
            # rename with complete files; the former shared temp still fails.
            with observation_lock:
                return real_replace(source, destination)

        with patch.object(retraining_lifecycle.os, "replace", side_effect=replace_together):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(writer.register_closed, ticker, count)
                    for writer, ticker, count in zip(writers, ("AAPL", "AMD"), (12, 34))
                ]
                results = [future.result(timeout=10) for future in futures]

        self.assertEqual([result["total_observations"] for result in results], [12, 34])
        self.assertEqual(len(observed), 2)
        self.assertNotEqual(observed[0][0], observed[1][0])
        expected_tickers = {frozenset(("AAPL",)), frozenset(("AMD",))}
        self.assertEqual({frozenset(payload["tickers"]) for _, payload in observed}, expected_tickers)
        persisted = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["schema_version"], STATE_SCHEMA_VERSION)
        self.assertIn(frozenset(persisted["tickers"]), expected_tickers)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_serialization_failure_preserves_state_and_cleans_only_owned_temp(self):
        writer = self.seed_state()
        original = self.state_path.read_bytes()
        unrelated = self.root / "retraining_state.json.tmp"
        unrelated.write_text("another writer owns this file", encoding="utf-8")
        writer._state["AAPL"]["invalid_value"] = object()

        with self.assertRaises(TypeError):
            writer.refresh()

        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "another writer owns this file")
        self.assertEqual(list(self.root.glob("*.tmp")), [unrelated])

    def test_replace_failure_preserves_state_and_cleans_only_owned_temp(self):
        writer = self.seed_state()
        original = self.state_path.read_bytes()
        unrelated = self.root / "unrelated.tmp"
        unrelated.write_text("keep me", encoding="utf-8")

        with patch.object(
            retraining_lifecycle.os, "replace", side_effect=PermissionError("replacement denied")
        ), self.assertRaisesRegex(PermissionError, "replacement denied"):
            writer.register_closed("AMD", 10)

        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep me")
        self.assertEqual(list(self.root.glob("*.tmp")), [unrelated])

    def test_initial_write_creates_missing_directories_and_valid_state(self):
        nested = self.root / "missing" / "nested" / "state.json"
        writer = self.make_lifecycle(nested)
        writer.register_closed("AAPL", 7)

        persisted = json.loads(nested.read_text(encoding="utf-8"))
        self.assertEqual(persisted["schema_version"], STATE_SCHEMA_VERSION)
        self.assertEqual(persisted["warning_samples"], 30)
        self.assertEqual(persisted["retrain_samples"], 50)
        self.assertEqual(persisted["tickers"]["AAPL"]["total_observations"], 7)
        self.assertEqual(list(nested.parent.glob("*.tmp")), [])

    @unittest.skipUnless(os.name == "posix", "POSIX state-file permissions")
    def test_existing_file_permissions_survive_replacement(self):
        writer = self.seed_state()
        self.state_path.chmod(0o640)
        writer.register_closed("AMD", 5)
        self.assertEqual(stat.S_IMODE(self.state_path.stat().st_mode), 0o640)

    def test_snapshot_reloads_completed_result_without_writing_state(self):
        cached_reader = self.make_lifecycle()
        self.seed_state()
        (self.root / "feedback.csv").write_text(
            "ticker,execution_valid\n" + "AAPL,1\n" * 60, encoding="utf-8"
        )
        original = self.state_path.read_bytes()
        modified_ns = self.state_path.stat().st_mtime_ns

        with patch.object(
            cached_reader, "_save_state_locked", side_effect=AssertionError("Status must not write")
        ):
            rows = cached_reader.snapshot(["AAPL", "AMD"])

        aapl = next(row for row in rows if row["ticker"] == "AAPL")
        self.assertEqual(aapl["last_result"], "ACCEPTED")
        self.assertEqual(aapl["last_evaluation_id"], "persisted-evaluation")
        self.assertEqual(aapl["last_balanced_accuracy"], 72.5)
        self.assertEqual(aapl["total_observations"], 60)
        self.assertEqual(aapl["new_observations"], 10)
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual(self.state_path.stat().st_mtime_ns, modified_ns)
        self.assertEqual(list(self.root.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
