"""No false REAL learning progress: verified vs quarantined broker closes."""
import unittest

from agents.learning_dataset import summarize_closed_trades


class RealLearningExclusionTests(unittest.TestCase):
    def test_real_and_paper_valid_invalid_counts_are_separated(self):
        fields = ("ticker", "modo", "position_id", "execution_valid")
        rows = [
            {"ticker": "NVDA", "modo": "REAL", "position_id": "real-1", "execution_valid": 1},
            {"ticker": "NVDA", "modo": "REAL", "position_id": "real-2", "execution_valid": 0},
            {"ticker": "NVDA", "modo": "PAPER", "position_id": "paper-1", "execution_valid": 1},
            {"ticker": "NVDA", "modo": "PAPER", "position_id": "paper-2", "execution_valid": 0},
        ]
        result = summarize_closed_trades(iter(rows), fieldnames=fields)["NVDA"]
        self.assertEqual(result["eligible_closed_real"], 1)
        self.assertEqual(result["quarantined_closed_real"], 1)
        self.assertEqual(result["eligible_closed_paper"], 1)
        self.assertEqual(result["quarantined_closed_paper"], 1)
        self.assertEqual(result["eligible_observations"], 2)
        self.assertEqual(result["quarantined_observations"], 2)

    def test_later_exclusion_supersedes_old_valid_real_close(self):
        fields = ("ticker", "modo", "position_id", "execution_valid")
        rows = [
            {"ticker": "META", "modo": "REAL", "position_id": "same", "execution_valid": 1},
            {"ticker": "META", "modo": "REAL", "position_id": "same", "execution_valid": 0},
            {"ticker": "META", "modo": "REAL", "position_id": "different", "execution_valid": 1},
        ]
        result = summarize_closed_trades(iter(rows), fieldnames=fields)["META"]
        self.assertEqual(result["eligible_closed_real"], 1)
        self.assertEqual(result["quarantined_closed_real"], 1)
        self.assertEqual(result["duplicate_closed_rows"], 1)

    def test_mode_unknown_exclusions_do_not_count_as_real(self):
        fields = ("ticker", "modo", "position_id", "execution_valid")
        rows = [
            {"ticker": "BA", "modo": "UNKNOWN", "position_id": "broken", "execution_valid": 0},
        ]
        result = summarize_closed_trades(iter(rows), fieldnames=fields)["BA"]
        self.assertEqual(result["quarantined_observations"], 1)
        self.assertEqual(result["quarantined_closed_real"], 0)
        self.assertEqual(result["quarantined_closed_paper"], 0)


if __name__ == "__main__":
    unittest.main()
