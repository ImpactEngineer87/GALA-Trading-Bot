"""Unit tests for read-only REAL historical exclusion audit."""
import csv
import tempfile
import unittest
from pathlib import Path

from scripts.audit_historical_real_feedback import audit


class HistoricalRealFeedbackAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.feedback = Path(self.tmp) / "feedback.csv"
        self.executions = Path(self.tmp) / "orders.csv"

    def write(self, path, rows):
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    def test_only_independently_linked_real_filled_order_is_candidate(self):
        self.write(self.feedback, [
            {"ticker": "META", "modo": "REAL", "execution_valid": "0",
             "exclusion_reason": "BROKER_CONFIRMATION_MISSING", "exit_order_id": "exit-a",
             "position_id": "position-a", "execution_id": "execution-a"},
            {"ticker": "META", "modo": "REAL", "execution_valid": "0",
             "exclusion_reason": "ORDER_ID_MISSING", "exit_order_id": "",
             "position_id": "position-b", "execution_id": "execution-b"},
            {"ticker": "META", "modo": "REAL", "execution_valid": "1",
             "exclusion_reason": "", "exit_order_id": "exit-c",
             "position_id": "position-c", "execution_id": "execution-c"},
            {"ticker": "META", "modo": "PAPER", "execution_valid": "0",
             "exclusion_reason": "", "exit_order_id": "exit-paper",
             "position_id": "position-paper", "execution_id": "execution-paper"},
        ])
        self.write(self.executions, [
            {"ticker": "META", "modo": "REAL", "evento": "EXIT_FILLED",
             "estado": "FILLED", "order_id": "exit-a", "precio": "101.5",
             "cantidad": "5", "position_id": "position-a", "execution_id": "execution-a"},
            {"ticker": "META", "modo": "PAPER", "evento": "EXIT_FILLED",
             "estado": "FILLED", "order_id": "exit-paper", "precio": "101.5",
             "cantidad": "5", "position_id": "position-paper", "execution_id": "execution-paper"},
        ])
        before = self.feedback.read_bytes(), self.executions.read_bytes()
        rows = audit(self.feedback, self.executions)
        self.assertEqual(rows["META"]["already_valid"], 1)
        self.assertEqual(rows["META"]["excluded"], 2)
        self.assertEqual(rows["META"]["local_fill_link_candidate"], 1)
        self.assertEqual(rows["META"]["missing_exit_id"], 1)
        self.assertEqual(rows["META"]["audit_exit_filled_missing_price"], 0)
        self.assertEqual(rows["META"]["audit_exit_filled_missing_quantity"], 0)
        self.assertEqual(rows["META"]["audit_exit_filled_missing_id"], 0)
        self.assertEqual(before, (self.feedback.read_bytes(), self.executions.read_bytes()))

    def test_broker_fill_must_match_real_ticker_and_position_identity(self):
        self.write(self.feedback, [
            {"ticker": "NKE", "modo": "REAL", "execution_valid": "0",
             "exclusion_reason": "BROKER_CONFIRMATION_MISSING",
             "exit_order_id": "known", "position_id": "position-a",
             "execution_id": "exec-a"},
        ])
        self.write(self.executions, [
            {"ticker": "NKE", "modo": "REAL", "evento": "EXIT_FILLED",
             "estado": "FILLED", "order_id": "known", "precio": "10", "cantidad": "1",
             "position_id": "wrong", "execution_id": "wrong"},
        ])
        rows = audit(self.feedback, self.executions)
        self.assertEqual(rows["NKE"]["local_fill_link_candidate"], 0)
        self.assertEqual(rows["NKE"]["ambiguous_or_unlinked_local_fill"], 1)

    def test_reports_incomplete_broker_audit_fields_separately(self):
        self.write(self.feedback, [
            {"ticker": "NVDA", "modo": "REAL", "execution_valid": "0",
             "exclusion_reason": "BROKER_CONFIRMATION_MISSING",
             "exit_order_id": "oid-9", "position_id": "p-9", "execution_id": "e-9"},
        ])
        self.write(self.executions, [
            {"ticker": "NVDA", "modo": "REAL", "evento": "EXIT_FILLED",
             "estado": "FILLED", "order_id": "oid-9", "precio": "",
             "cantidad": "", "position_id": "", "execution_id": ""},
        ])
        stats = audit(self.feedback, self.executions)["NVDA"]
        self.assertEqual(stats["audit_exit_filled_all_modes"], 1)
        self.assertEqual(stats["audit_exit_filled_incomplete"], 1)
        self.assertEqual(stats["audit_exit_filled_missing_price"], 1)
        self.assertEqual(stats["audit_exit_filled_missing_quantity"], 1)
        self.assertEqual(stats["audit_exit_filled_missing_position_id"], 1)
        self.assertEqual(stats["audit_exit_filled_missing_execution_id"], 1)
        self.assertEqual(stats["local_fill_link_candidate"], 0)

    def test_missing_audit_ledger_leaves_feedback_untouched(self):
        self.write(self.feedback, [
            {"ticker": "BA", "modo": "REAL", "execution_valid": "0",
             "exclusion_reason": "ORDER_ID_MISSING", "exit_order_id": "",
             "position_id": "id", "execution_id": "eid"},
        ])
        original = self.feedback.read_bytes()
        result = audit(self.feedback, self.executions)
        self.assertEqual(result["BA"]["missing_exit_id"], 1)
        self.assertEqual(self.feedback.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
