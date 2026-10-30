"""Regression: keep the original feedback ledger without loading it fully in RAM."""
import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agents.feedback_store import FeedbackStore, COLUMNAS


def row(ticker, position_id, feedback_id, *, valid="1"):
    record = {field: "" for field in COLUMNAS}
    record.update({
        "ticker": ticker, "modo": "PAPER", "tipo": "BUY",
        "feedback_id": feedback_id, "position_id": position_id,
        "execution_id": "exe-" + position_id,
        "execution_valid": valid, "timestamp": "2026-10-19T12:00:00Z",
        "precio_entrada": "100", "precio_salida": "101",
        "ganadora": "1",
    })
    return record


class FeedbackStreamingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.csv_path = self.root / "agentes_feedback.csv"
        with self.csv_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=COLUMNAS)
            writer.writeheader()
            for idx in range(1000):
                writer.writerow(row("AAPL" if idx % 2 else "AMD", f"pos-{idx}", f"fb-{idx}"))
            # A newer quarantine must supersede the original valid close.
            writer.writerow(row("AAPL", "pos-1", "fb-retracted", valid="0"))

    def test_counts_without_retaining_full_history_or_rewriting_csv(self):
        before = hashlib.sha256(self.csv_path.read_bytes()).digest()
        store = FeedbackStore(str(self.csv_path))
        self.assertEqual(store.total_cerradas(), 999)
        self.assertEqual(store.total_cerradas_ticker("AMD"), 500)
        self.assertEqual(store.total_cerradas_ticker("AAPL"), 499)
        self.assertEqual(store._closed_rows, [])
        self.assertEqual(store._closed_by_identity, {})
        self.assertEqual(store._closed_by_feedback_id, {})
        self.assertEqual(before, hashlib.sha256(self.csv_path.read_bytes()).digest())

    def test_pending_close_identity_index_and_restart(self):
        sidecar = Path(str(self.csv_path) + ".open.json")
        pending = {
            "feedback_id": "fb-123", "features": {}, "precio_entrada": 100,
            "stop_loss": 99, "riesgo_planificado": 1,
            "lifecycle": {"position_id": "pos-123", "modo": "PAPER"},
            "tipo": "BUY", "timestamp": "2026-10-19T12:00:00Z",
        }
        sidecar.write_text(json.dumps({"open": {"AAPL": pending}}), encoding="utf-8")
        before = hashlib.sha256(self.csv_path.read_bytes()).digest()
        store = FeedbackStore(str(self.csv_path))
        self.assertEqual(store._abiertas, {})
        self.assertEqual(store.total_cerradas(), 999)
        self.assertEqual(before, hashlib.sha256(self.csv_path.read_bytes()).digest())

    def test_unknown_schema_does_not_discard_original_training_data(self):
        path = self.root / "bad-schema.csv"
        original = b"unexpected,columns\nkeep,this-row\n"
        path.write_bytes(original)
        with self.assertRaisesRegex(RuntimeError, "CSV ORIGINAL conservado"):
            FeedbackStore(str(path))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.root.glob("bad-schema.csv*")),
                         ["bad-schema.csv"])


if __name__ == "__main__":
    unittest.main()
