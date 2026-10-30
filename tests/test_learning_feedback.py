"""Offline learning evidence durability, identity and counter regressions."""

import csv
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from agents.contracts import DecisionFinal
from agents.feedback_store import FeedbackStore, COLUMNAS, V2_9_COLUMNAS
from agents.learning_dataset import (
    eligible_closed_rows, eligible_closed_trades, summarize_closed_trades,
)
from agents.retraining_lifecycle import RetrainingLifecycle


def open_context(identifier="position-1", mode="REAL", side="BUY"):
    position = "LONG" if side == "BUY" else "SHORT"
    return {
        "execution_id": "execution-" + identifier, "position_id": identifier,
        "signal_id": "signal-" + identifier, "correlation_id": "correlation-" + identifier,
        "modo": mode, "entry_order_id": "entry-" + identifier,
        "position_side": position, "signal_side": side,
        "entry_action": "BUY" if side == "BUY" else "SELL_SHORT",
        "state_before": "OUT", "state_after": position,
        "entry_confirmed": True, "audit_linked": True,
        "history_linked": True, "slippage_linked": True,
    }


def close_context(identifier="position-1", mode="REAL", side="BUY"):
    return {
        "modo": mode, "exit_order_id": "exit-" + identifier,
        "exit_action": "SELL" if side == "BUY" else "BUY_TO_COVER",
        "state_before": "LONG" if side == "BUY" else "SHORT", "state_after": "OUT",
        "exit_confirmed": True, "audit_linked": True, "history_linked": True,
        "closed_utc": "2026-10-16T15:30:00+00:00",
    }


class LearningDatasetTests(unittest.TestCase):
    def test_strict_integrity_values_and_mode_match_dataframe_and_row_readers(self):
        rows = [
            {"ticker": "aapl", "modo": " real ", "execution_valid": "1.0", "position_id": "one"},
            {"ticker": "AAPL", "modo": "PAPER", "execution_valid": 1, "position_id": "two"},
            {"ticker": "AAPL", "modo": "REAL", "execution_valid": "", "position_id": "three"},
            {"ticker": "AAPL", "modo": "REAL", "execution_valid": None, "position_id": "four"},
            {"ticker": "AAPL", "modo": "UNKNOWN", "execution_valid": 1, "position_id": "five"},
            {"ticker": "AAPL", "modo": "PAPER", "execution_valid": 0, "position_id": "six"},
        ]
        self.assertEqual(len(eligible_closed_rows(rows)), 2)
        frame = eligible_closed_trades(pd.DataFrame(rows))
        self.assertEqual(list(frame["ticker"]), ["AAPL", "AAPL"])
        self.assertEqual(list(frame["modo"]), ["REAL", "PAPER"])
        counts = summarize_closed_trades(rows)["AAPL"]
        self.assertEqual(counts["eligible_observations"], 2)
        self.assertEqual(counts["eligible_closed_real"], 1)
        self.assertEqual(counts["eligible_closed_paper"], 1)
        self.assertEqual(counts["quarantined_observations"], 4)

    def test_identity_dedup_preserves_distinct_positions_with_identical_prices(self):
        base = {"ticker": "AMD", "modo": "REAL", "execution_valid": 1,
                "timestamp": "2026-10-15 10:00:00", "tipo": "BUY",
                "precio_entrada": 100, "precio_salida": 101}
        rows = [{**base, "position_id": "p1"}, {**base, "position_id": "p2"},
                {**base, "position_id": "p1", "timestamp": "2026-10-15 10:00:01"},
                {**base, "position_id": "p1", "modo": "PAPER"}]
        self.assertEqual(len(eligible_closed_trades(pd.DataFrame(rows))), 3)
        summary = summarize_closed_trades(rows)["AMD"]
        self.assertEqual(summary["eligible_closed_real"], 2)
        self.assertEqual(summary["eligible_closed_paper"], 1)
        self.assertEqual(summary["duplicate_closed_rows"], 1)

    def test_later_quarantine_supersedes_previous_eligible_duplicate(self):
        first = {"ticker": "NVDA", "modo": "REAL", "position_id": "same", "execution_valid": 1}
        rows = [first, {**first, "execution_valid": 0}]
        self.assertEqual(eligible_closed_rows(rows), [])
        self.assertTrue(eligible_closed_trades(pd.DataFrame(rows)).empty)
        summary = summarize_closed_trades(rows)["NVDA"]
        self.assertEqual(summary["quarantined_observations"], 1)
        self.assertEqual(summary["duplicate_closed_rows"], 1)

    def test_legacy_compatibility_is_explicit_and_dedups_numeric_price_forms(self):
        row = {"ticker": "AAPL", "timestamp": "2026-10-15 10:00:00", "tipo": "BUY",
               "precio_entrada": "100.0", "precio_salida": "101.00"}
        duplicate = {**row, "precio_entrada": 100, "precio_salida": 101}
        self.assertEqual(len(eligible_closed_rows([row, duplicate], allow_legacy=True)), 1)
        self.assertEqual(eligible_closed_rows([row], allow_legacy=False), [])
        self.assertEqual(eligible_closed_rows([{**row, "execution_valid": ""}]), [])
        self.assertEqual(len(eligible_closed_rows([{"ticker": "AAPL"}] * 3)), 3)


class FeedbackDurabilityTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.csv_path = self.root / "feedback.csv"
        self.store = FeedbackStore(str(self.csv_path))
        self.decision = DecisionFinal(
            ticker="AAPL", operar=True, tipo="BUY",
            timestamp=datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc),
        )

    def open(self, identifier="position-1", mode="REAL"):
        self.store.abrir(self.decision, 100, 99, lifecycle=open_context(identifier, mode))

    def rows(self):
        with self.csv_path.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_restart_recovers_open_features_and_saves_weighted_exit_label(self):
        self.open()
        restarted = FeedbackStore(str(self.csv_path))
        row = restarted.cerrar("aapl", 100.7, lifecycle=close_context())
        self.assertEqual(row["execution_valid"], 1)
        self.assertEqual(row["r_multiple"], 0.7)
        self.assertEqual(row["risk_per_share"], 1)
        self.assertEqual(row["closed_utc"], "2026-10-16T15:30:00+00:00")
        self.assertEqual(restarted.total_cerradas_ticker("AAPL"), 1)
        self.assertFalse(restarted.tiene_abierta("AAPL"))

    def test_unknown_real_exit_is_saved_without_price_win_or_r_label(self):
        self.open()
        closing = {**close_context(), "exit_confirmed": False}
        result = self.store.cerrar("AAPL", None, lifecycle=closing)
        self.assertEqual(result["execution_valid"], 0)
        for key in ("precio_salida", "r_multiple", "ganadora"):
            self.assertEqual(result[key], "")
        self.assertEqual(self.store.total_cerradas_ticker("AAPL"), 0)
        restarted = FeedbackStore(str(self.csv_path))
        self.assertEqual(restarted.total_cerradas_ticker("AAPL"), 0)
        self.assertEqual(self.rows()[-1]["precio_salida"], "")
        self.assertFalse(restarted.tiene_abierta("AAPL"))

    def test_failed_append_preserves_persistent_open_for_retry(self):
        self.open()
        with patch.object(self.store, "_persistir", side_effect=OSError("disk unavailable")):
            with self.assertRaisesRegex(OSError, "disk unavailable"):
                self.store.cerrar("AAPL", 101, lifecycle=close_context())
        self.assertTrue(self.store.tiene_abierta("AAPL"))
        intent = json.loads(self.store._open_state_path.read_text(encoding="utf-8"))["open"]["AAPL"]
        self.assertEqual(intent["closing_row"]["position_id"], "position-1")
        self.assertEqual(intent["closing_row"]["execution_valid"], 1)
        restarted = FeedbackStore(str(self.csv_path))
        self.assertFalse(restarted.tiene_abierta("AAPL"))
        self.assertIsNone(restarted.cerrar("AAPL", 101, lifecycle=close_context()))
        self.assertEqual(restarted.total_cerradas(), 1)

    def test_crash_after_csv_commit_does_not_replay_stale_open_sidecar(self):
        self.open()
        stale_open = self.store._open_state_path.read_bytes()
        self.store.cerrar("AAPL", 101, lifecycle=close_context())
        self.store._open_state_path.write_bytes(stale_open)
        restarted = FeedbackStore(str(self.csv_path))
        self.assertFalse(restarted.tiene_abierta("AAPL"))
        self.assertIsNone(restarted.cerrar("AAPL", 101, lifecycle=close_context()))
        self.assertEqual(restarted.total_cerradas(), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_sidecar_cleanup_failure_retry_does_not_append_second_close(self):
        self.open()
        real_persist = self.store._persistir_abiertas_locked
        attempts = []

        def fail_cleanup():
            attempts.append(1)
            if len(attempts) == 2:
                raise OSError("sidecar denied")
            return real_persist()

        with patch.object(self.store, "_persistir_abiertas_locked", side_effect=fail_cleanup):
            with self.assertRaisesRegex(OSError, "sidecar denied"):
                self.store.cerrar("AAPL", 101, lifecycle=close_context())
        self.assertTrue(self.store.tiene_abierta("AAPL"))
        self.assertEqual(len(self.rows()), 1)
        self.store.cerrar("AAPL", 101, lifecycle=close_context())
        self.assertEqual(self.store.total_cerradas(), 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse(self.store.tiene_abierta("AAPL"))

    def test_fsync_failure_rolls_back_partial_append_and_keeps_open(self):
        self.open()
        real_fsync = os.fsync
        attempts = []

        def fail_csv_flush(descriptor):
            attempts.append(1)
            if len(attempts) == 2:
                raise OSError("flush denied")
            return real_fsync(descriptor)

        with patch("agents.feedback_store.os.fsync", side_effect=fail_csv_flush):
            with self.assertRaisesRegex(OSError, "flush denied"):
                self.store.cerrar("AAPL", 101, lifecycle=close_context())
        self.assertTrue(self.store.tiene_abierta("AAPL"))
        self.assertEqual(self.csv_path.stat().st_size, 0)
        self.store.cerrar("AAPL", 101, lifecycle=close_context())
        self.assertEqual(len(self.rows()), 1)

    def test_next_open_replays_pending_close_before_recording_new_position(self):
        self.open("first")
        with patch.object(self.store, "_persistir", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.store.cerrar("AAPL", 101, lifecycle=close_context("first"))
        self.open("second")
        self.assertEqual(self.store.total_cerradas(), 1)
        self.assertEqual(self.rows()[0]["position_id"], "first")
        self.assertEqual(self.rows()[0]["precio_salida"], "101.0")
        self.assertTrue(self.store.tiene_abierta("AAPL"))
        self.store.cerrar("AAPL", 99, lifecycle=close_context("second"))
        self.assertEqual(self.store.total_cerradas(), 2)

    def test_next_open_preserves_old_intent_when_storage_still_fails(self):
        self.open("first")
        with patch.object(self.store, "_persistir", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.store.cerrar("AAPL", 101, lifecycle=close_context("first"))
            with self.assertRaises(OSError):
                self.open("second")
        pending = json.loads(self.store._open_state_path.read_text(encoding="utf-8"))["open"]["AAPL"]
        self.assertEqual(pending["lifecycle"]["position_id"], "first")
        self.assertEqual(pending["closing_row"]["position_id"], "first")

    def test_concurrent_duplicate_close_records_one_observation(self):
        self.open()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: self.store.cerrar("AAPL", 101, lifecycle=close_context()), range(2)
            ))
        self.assertEqual(sum(row is not None for row in results), 1)
        self.assertEqual(self.store.total_cerradas(), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_mixed_real_and_paper_closes_count_together_without_reopening_duplicate(self):
        self.open("real", "REAL")
        self.store.cerrar("AAPL", 101, lifecycle=close_context("real", "REAL"))
        self.open("real", "REAL")
        self.assertFalse(self.store.tiene_abierta("AAPL"))
        self.open("paper", "PAPER")
        self.store.cerrar("AAPL", 99, lifecycle=close_context("paper", "PAPER"))
        self.assertEqual(self.store.total_cerradas_ticker("AAPL"), 2)
        self.assertEqual(self.store.total_cerradas(), 2)

    def test_pending_open_cannot_be_overwritten_by_another_position(self):
        self.open("first")
        with self.assertRaisesRegex(ValueError, "no se sobrescribe"):
            self.open("second")
        row = self.store.cerrar("AAPL", 101, lifecycle=close_context("first"))
        self.assertEqual(row["position_id"], "first")

    def test_unknown_real_exit_and_invalid_risk_are_quarantined(self):
        self.open()
        closing = {**close_context(), "exit_order_id": "", "exit_confirmed": False}
        row = self.store.cerrar("AAPL", 101, lifecycle=closing)
        self.assertEqual(row["execution_valid"], 0)
        self.assertIn("BROKER_CONFIRMATION_MISSING", row["exclusion_reason"])
        self.assertIn("ORDER_ID_MISSING", row["exclusion_reason"])
        self.store.abrir(self.decision, 100, 100, lifecycle=open_context("zero-risk"))
        row = self.store.cerrar("AAPL", 101, lifecycle=close_context("zero-risk"))
        self.assertEqual(row["execution_valid"], 0)
        self.assertIn("INVALID_PRICE_OR_RISK", row["exclusion_reason"])
        self.assertEqual(self.store.total_cerradas(), 0)

    def test_replayed_unknown_real_exit_preserves_quarantine(self):
        self.open()
        closing = {**close_context(), "exit_order_id": "", "exit_confirmed": False}
        with patch.object(self.store, "_persistir", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.store.cerrar("AAPL", 101, lifecycle=closing)
        restarted = FeedbackStore(str(self.csv_path))
        self.assertEqual(restarted.total_cerradas(), 0)
        self.assertEqual(self.rows()[0]["execution_valid"], "0")
        self.assertIn("BROKER_CONFIRMATION_MISSING", self.rows()[0]["exclusion_reason"])

    def test_schema_upgrade_preserves_linked_history_and_unknown_close_time(self):
        old = {field: "" for field in V2_9_COLUMNAS}
        old.update({"ticker": "AAPL", "modo": "REAL", "execution_valid": 1,
                    "position_id": "historic", "r_multiple": "0.5", "ganadora": 1})
        with self.csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=V2_9_COLUMNAS)
            writer.writeheader()
            writer.writerow(old)
        upgraded = FeedbackStore(str(self.csv_path))
        self.assertEqual(upgraded.total_cerradas(), 1)
        self.assertEqual(self.rows()[0]["closed_utc"], "")
        self.assertEqual(self.rows()[0]["r_multiple"], "0.5")
        self.assertTrue(Path(str(self.csv_path) + ".before_closed_utc").exists())
        with self.csv_path.open(encoding="utf-8", newline="") as handle:
            self.assertEqual(next(csv.reader(handle)), COLUMNAS)


class LearningLifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.feedback = self.root / "feedback.csv"
        self.state = self.root / "retraining.json"

    def lifecycle(self):
        return RetrainingLifecycle(
            feedback_path=str(self.feedback), state_path=str(self.state),
            shadow_models_dir=str(self.root / "models"), warning_samples=2, retrain_samples=3,
        )

    def write_rows(self, rows):
        with self.feedback.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["ticker", "modo", "position_id", "execution_valid"])
            writer.writeheader()
            writer.writerows(rows)

    def test_snapshot_counts_sources_and_duplicates_without_saving(self):
        first = {"ticker": "AAPL", "modo": "REAL", "position_id": "one", "execution_valid": 1}
        self.write_rows([first, first,
            {**first, "position_id": "two", "modo": "PAPER"},
            {**first, "position_id": "three", "execution_valid": ""}])
        row = self.lifecycle().snapshot(["AAPL"])[0]
        self.assertEqual(row["eligible_observations"], 2)
        self.assertEqual(row["eligible_closed_real"], 1)
        self.assertEqual(row["eligible_closed_paper"], 1)
        self.assertEqual(row["duplicate_closed_rows"], 1)
        self.assertEqual(row["quarantined_observations"], 1)
        self.assertEqual(row["status"], "READY_REVIEW")
        self.assertFalse(self.state.exists())

    def test_restarted_owner_is_recovered_and_live_owner_is_preserved(self):
        self.write_rows([
            {"ticker": "AAPL", "modo": "REAL", "position_id": str(index), "execution_valid": 1}
            for index in range(3)
        ])
        lifecycle = self.lifecycle()
        lifecycle.mark_training("AAPL", 3)
        current = lifecycle.snapshot(["AAPL"])[0]
        self.assertEqual(current["status"], "TRAINING")
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        payload["tickers"]["AAPL"]["training_owner_token"] = "previous-process"
        self.state.write_text(json.dumps(payload), encoding="utf-8")
        original = self.state.read_bytes()
        recovered = self.lifecycle().snapshot(["AAPL"])[0]
        self.assertEqual(recovered["status"], "PENDING_RETRAIN")
        self.assertEqual(recovered["last_result"], "INTERRUPTED")
        self.assertEqual(recovered["new_observations"], 3)
        self.assertEqual(self.state.read_bytes(), original)

    def test_legacy_training_waits_for_abandonment_window(self):
        record = {"status": "TRAINING", "training_started_utc": datetime.now(timezone.utc).isoformat()}
        RetrainingLifecycle._recover_interrupted_training(record)
        self.assertEqual(record["status"], "TRAINING")
        record["training_started_utc"] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        RetrainingLifecycle._recover_interrupted_training(record)
        self.assertEqual(record["last_result"], "INTERRUPTED")

    def test_rebase_of_old_raw_counter_allows_new_unique_close_progress(self):
        first = {"ticker": "AAPL", "modo": "REAL", "position_id": "one", "execution_valid": 1}
        self.write_rows([first, first, {**first, "position_id": "two"}])
        self.state.write_text(json.dumps({"schema_version": 3, "tickers": {"AAPL": {
            "total_observations": 3, "last_attempt_count": 3, "last_success_count": 3,
            "last_attempt_utc": "2026-10-16T14:00:00+00:00", "counting_basis": "execution_valid",
        }}}), encoding="utf-8")
        lifecycle = self.lifecycle()
        row = lifecycle.register_closed("AAPL")
        self.assertEqual(row["last_attempt_count"], 2)
        self.assertEqual(row["counter_rebase_reason"], "UNIQUE_ELIGIBLE_CLOSES")
        self.write_rows([first, first, {**first, "position_id": "two"},
                         {**first, "position_id": "three"}])
        row = lifecycle.snapshot(["AAPL"])[0]
        self.assertEqual(row["total_observations"], 3)
        self.assertEqual(row["new_observations"], 1)

    def test_economic_and_source_metrics_belong_to_latest_evaluation(self):
        lifecycle = self.lifecycle()
        lifecycle.mark_result("AAPL", total_count=3, accepted=True, metrics={
            "evaluation_id": "run-one", "economic_test": {"expectancy_r": 0.3},
            "economic_gate_passed": True, "economic_reasons": [],
            "muestras_fit_reales": 10, "muestras_fit_suplementarias": 4,
            "validation_test_actual_only": True, "temporal_integrity_passed": True,
        })
        row = lifecycle.snapshot(["AAPL"])[0]
        self.assertEqual(row["economic_test"]["expectancy_r"], 0.3)
        self.assertEqual(row["last_evaluation"]["muestras_fit_suplementarias"], 4)
        lifecycle.mark_result("AAPL", total_count=4, accepted=False,
                              metrics={"evaluation_id": "run-two", "balanced_accuracy": 55})
        row = lifecycle.snapshot(["AAPL"])[0]
        self.assertEqual(row["last_evaluation"]["evaluation_id"], "run-two")
        self.assertEqual(row["economic_test"], {})
        self.assertIsNone(row["economic_gate_passed"])
        self.assertEqual(row["muestras_fit_suplementarias"], 0)


if __name__ == "__main__":
    unittest.main()

