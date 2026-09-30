import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from agents.feature_audit import auditar_features
from agents.training import FEATURES


class FeatureAuditTests(unittest.TestCase):
    def test_audit_writes_latest_reports_for_current_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            rows = 50
            data = {
                "timestamp": pd.date_range(
                    "2026-01-01", periods=rows, freq="h"),
                "ticker": ["MSFT"] * rows,
                "tipo": ["BUY" if i % 2 else "SELL" for i in range(rows)],
                "ganadora": [i % 2 for i in range(rows)],
            }
            for offset, feature in enumerate(FEATURES, start=1):
                if feature == "tipo_num":
                    continue
                data[feature] = np.arange(rows, dtype=float) + offset
            feedback = root / "feedback.csv"
            reports = root / "reports"
            pd.DataFrame(data).to_csv(feedback, index=False)

            paths = auditar_features(str(feedback), str(reports))

            self.assertTrue(Path(paths["csv"]).exists())
            self.assertTrue(Path(paths["json"]).exists())
            latest = pd.read_csv(reports / "auditoria_features_latest.csv")
            self.assertEqual(set(latest["feature"]), set(FEATURES))
            # The deliberately increasing synthetic series changes between the
            # first 80% and final 20%, so the audit must surface chronological drift.
            self.assertIn("DRIFT", set(latest["status"]))
            self.assertNotIn("MISSING", set(latest["status"]))
            self.assertNotIn("CONSTANT", set(latest["status"]))


if __name__ == "__main__":
    unittest.main()
