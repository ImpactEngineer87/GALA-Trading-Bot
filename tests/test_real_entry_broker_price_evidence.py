"""Offline tests: REAL position may be protected but never get false AI fill."""
import unittest
from engine.bot_engine import ChocoletraBot


class BrokerEntryEvidenceTests(unittest.TestCase):
    def test_positive_finite_broker_execution_price_only(self):
        positive = ChocoletraBot._positive_broker_fill_price
        self.assertTrue(positive("188.43"))
        for invalid in (None, "", 0, "0", -1, float("nan"),
                        float("inf"), "bad"):
            with self.subTest(invalid=repr(invalid)):
                self.assertFalse(positive(invalid))

    def test_state_transition_is_not_broker_execution_evidence(self):
        valid = ChocoletraBot._verified_entry_confirmation
        self.assertFalse(valid(True, False, "order-a", {}))
        self.assertFalse(valid(True, False, "order-a", {
            "fill_price": 188.43
        }))
        self.assertFalse(valid(True, False, "order-a", {
            "broker_price_verified": False
        }))
        self.assertFalse(valid(True, False, "order-a", {
            "broker_price_verified": 1
        }))
        self.assertFalse(valid(True, False, "", {
            "broker_price_verified": True
        }))
        self.assertFalse(valid(False, False, "order-a", {
            "broker_price_verified": True
        }))
        self.assertTrue(valid(True, False, "order-a", {
            "broker_price_verified": True
        }))

    def test_simulated_paper_entry_validation_preserves_prior_behavior(self):
        valid = ChocoletraBot._verified_entry_confirmation
        self.assertTrue(valid(True, True, "SIM-ENTRY", {}))
        self.assertTrue(valid(True, True, "", {}))
        self.assertFalse(valid(False, True, "SIM-ENTRY", {}))


if __name__ == "__main__":
    unittest.main()
