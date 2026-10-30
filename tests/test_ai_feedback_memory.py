"""Regressions for memory-safe 291k-row AI feedback polling.

The tests check both exact identity/eligibility parity and that a single
feedback CSV refresh serves concurrent dashboard requests.
"""
import csv
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agents.learning_dataset import (
    _unique_indices, closed_row_is_eligible, eligible_closed_trades,
    summarize_closed_trades,
)


def reference_summary(rows, fields):
    """Historic non-streaming semantics for comparison in small fixtures."""
    unique = set(_unique_indices(rows))
    summary = {}
    for index, row in enumerate(rows):
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        record = summary.setdefault(ticker, {
            "eligible_observations": 0, "eligible_closed_real": 0,
            "eligible_closed_paper": 0, "eligible_closed_legacy": 0,
            "quarantined_observations": 0,
            "quarantined_closed_real": 0, "quarantined_closed_paper": 0,
            "duplicate_closed_rows": 0,
            "lifetime_observations": 0,
        })
        record["lifetime_observations"] += 1
        if index not in unique:
            record["duplicate_closed_rows"] += 1
            continue
        mode = str(row.get("modo") or "").strip().lower()
        if not closed_row_is_eligible(row, fieldnames=fields):
            record["quarantined_observations"] += 1
            if mode in ("real", "paper"):
                record[f"quarantined_closed_{mode}"] += 1
            continue
        record["eligible_observations"] += 1
        record[f"eligible_closed_{mode if mode in ('real', 'paper') else 'legacy'}"] += 1
    return summary


class StreamingFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            dict(ticker="AAPL", modo="REAL", execution_valid="1", position_id="p1",
                 timestamp="2026-10-19T10:01:00", tipo="BUY",
                 precio_entrada="100.00", precio_salida="101.00"),
            dict(ticker="aapl", modo="REAL", execution_valid="0", position_id="p1",
                 timestamp="2026-10-19T10:01:00", tipo="BUY",
                 precio_entrada="100.00", precio_salida="101.00"),
            dict(ticker="AAPL", modo="PAPER", execution_valid="1", position_id="p1",
                 timestamp="2026-10-19T10:01:00", tipo="BUY",
                 precio_entrada="100.00", precio_salida="101.00"),
            dict(ticker="META", modo="PAPER", execution_valid="1", position_id="",
                 timestamp="2026-10-19T10:01:00", tipo="SELL",
                 precio_entrada="101.0", precio_salida="100"),
            dict(ticker="META", modo="PAPER", execution_valid="1", position_id="",
                 timestamp="2026-10-19T10:01:00", tipo="SELL",
                 precio_entrada="101.00", precio_salida="100.00"),
            dict(ticker="BA", modo="REAL", execution_valid="1", position_id="",
                 timestamp="", tipo="SELL", precio_entrada="", precio_salida=""),
            dict(ticker="BA", modo="REAL", execution_valid="0", position_id="",
                 timestamp="", tipo="SELL", precio_entrada="", precio_salida=""),
        ]
        self.fields = list(self.rows[0])

    def test_streaming_summary_preserves_latest_quarantine_and_counts(self):
        expected = reference_summary(self.rows, self.fields)
        iterator = (row for row in self.rows)
        actual = summarize_closed_trades(iterator, fieldnames=self.fields)
        self.assertEqual(actual, expected)
        self.assertEqual(actual["AAPL"]["duplicate_closed_rows"], 1)
        self.assertEqual(actual["AAPL"]["eligible_closed_real"], 0)
        self.assertEqual(actual["AAPL"]["eligible_closed_paper"], 1)

    def test_dataframe_eligibility_avoids_wide_row_dictionaries(self):
        try:
            import pandas as pd
            from unittest.mock import patch
        except ImportError as exc:
            self.skipTest(str(exc))
        df = pd.DataFrame(self.rows)
        # Extra columns emulate a 60+ column, 291k-line feedback CSV.
        for n in range(55):
            df[f"unused_feature_{n}"] = float(n)
        reference = [i for i in _unique_indices(self.rows)
                     if closed_row_is_eligible(self.rows[i], fieldnames=self.fields)]
        with patch.object(pd.DataFrame, "to_dict", side_effect=AssertionError(
            "Do not expand every wide CSV row to Python dicts"
        )):
            result = eligible_closed_trades(df)
        self.assertEqual(list(result.index), reference)
        self.assertEqual(len(result.columns), len(df.columns))

    def test_concurrent_ai_status_reuses_one_cache_refresh(self):
        try:
            import pandas as pd
            from agents.training import EntrenadorAgentes
        except ImportError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feedback.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=self.fields)
                writer.writeheader()
                writer.writerows(self.rows)
            trainer = object.__new__(EntrenadorAgentes)
            trainer.feedback_path = str(path)
            trainer._lock = threading.Lock()
            trainer._status_feedback_load_lock = threading.Lock()
            trainer._status_feedback_signature = None
            trainer._status_feedback_rows = pd.DataFrame()
            touched = []
            calls_lock = threading.Lock()

            def prepare(frame):
                with calls_lock:
                    touched.append(1)
                time.sleep(0.05)
                return frame

            trainer._prepare_training_rows = prepare
            barrier = threading.Barrier(5)

            def run():
                barrier.wait(timeout=5)
                return trainer._training_rows_for_status()

            with ThreadPoolExecutor(max_workers=5) as pool:
                frames = list(pool.map(lambda _: run(), range(5)))
            self.assertEqual(len(touched), 1, "Only one full CSV load per changed file")
            self.assertTrue(all(len(frame) == len(self.rows) for frame in frames))


if __name__ == "__main__":
    unittest.main()
