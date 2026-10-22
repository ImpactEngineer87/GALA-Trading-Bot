"""Read-only AI APPROVED is not equivalent to a brokerage trade."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.audit_signal_order_funnel import audit


class SignalOrderFunnelTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.signals = Path(self.temp) / "signal.jsonl"
        self.orders = Path(self.temp) / "orders.csv"

    def test_approved_signal_without_broker_event_is_not_called_a_fill(self):
        with self.signals.open("w", encoding="utf-8") as f:
            f.write(json.dumps({
                "date": "2026-10-22", "timestamp": "14:26:00",
                "ticker": "BA", "tipo": "SELL", "ai_decision": "APPROVED",
                "execution_mode": "REAL", "signal_id": "signal-ba",
            }) + "\n")
        rows, missing = audit(
            self.signals, self.orders, "2026-10-22", ["BA"]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "APPROVED")
        self.assertFalse(rows[0]["has_submission"])
        self.assertFalse(rows[0]["has_fill"])
        self.assertEqual(rows[0]["broker_events"], [])

    def test_missed_signal_reason_from_learning_ledger_without_broker_fill(self):
        with self.signals.open("w", encoding="utf-8") as f:
            f.write(json.dumps({
                "date": "2026-10-22", "timestamp": "10:51:11",
                "ticker": "TSLA", "tipo": "SELL", "ai_decision": "APPROVED",
                "execution_mode": "REAL", "signal_id": "tsla-sign",
            }) + "\n")
        learning = Path(self.temp) / "learning.csv"
        with learning.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "signal_id", "ticker", "reason", "event",
            ])
            writer.writeheader()
            writer.writerow({
                "signal_id": "tsla-sign", "ticker": "TSLA",
                "reason": "ENTRY_SLOT_NOT_AVAILABLE", "event": "OPEN",
            })
        rows, _ = audit(
            self.signals, self.orders, "2026-10-22", ["TSLA"], learning
        )
        self.assertEqual(rows[0]["learning_skip_reason"], "ENTRY_SLOT_NOT_AVAILABLE")
        self.assertFalse(rows[0]["has_submission"])
        self.assertFalse(rows[0]["has_fill"])

    def test_structured_signal_skip_audit_is_distinct_from_broker_order(self):
        with self.signals.open("w", encoding="utf-8") as f:
            f.write(json.dumps({
                "date": "2026-10-22", "timestamp": "14:26:05",
                "ticker": "BA", "tipo": "SELL", "ai_decision": "APPROVED",
                "execution_mode": "REAL", "signal_id": "ba-new",
            }) + "\n")
        with self.orders.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "fecha", "ticker", "evento", "signal_id", "motivo",
            ])
            writer.writeheader()
            writer.writerow({
                "fecha": "2026-10-22", "ticker": "BA",
                "evento": "SIGNAL_EXECUTION_SKIPPED", "signal_id": "ba-new",
                "motivo": "ALREADY_SAME_SIDE",
            })
        rows, _ = audit(
            self.signals, self.orders, "2026-10-22", ["BA"]
        )
        self.assertEqual(rows[0]["execution_skip_reason"], "ALREADY_SAME_SIDE")
        self.assertIn("SIGNAL_EXECUTION_SKIPPED", rows[0]["broker_events"])
        self.assertFalse(rows[0]["has_submission"])
        self.assertFalse(rows[0]["has_fill"])

    def test_order_submission_and_execution_are_distinct(self):
        with self.signals.open("w", encoding="utf-8") as f:
            f.write(json.dumps({
                "date": "2026-10-22", "timestamp": "12:28:00",
                "ticker": "NKE", "tipo": "SELL", "ai_decision": "APPROVED",
                "execution_mode": "REAL", "signal_id": "signal-nke",
            }) + "\n")
        with self.orders.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "fecha", "ticker", "evento", "signal_id",
            ])
            writer.writeheader()
            writer.writerow({
                "fecha": "2026-10-22", "ticker": "NKE",
                "evento": "ENTRY_SUBMITTED", "signal_id": "signal-nke",
            })
        rows, _ = audit(self.signals, self.orders, "2026-10-22", ["NKE"])
        self.assertTrue(rows[0]["has_submission"])
        self.assertFalse(rows[0]["has_fill"])
        self.assertEqual(rows[0]["broker_events"], ["ENTRY_SUBMITTED"])


if __name__ == "__main__":
    unittest.main()
