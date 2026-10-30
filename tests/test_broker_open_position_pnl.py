"""Broker-native open-position P/L must remain separate from local bot P/L."""
import unittest

from core.broker_pnl import open_position_pnl_snapshot


class BrokerPositionPnlTests(unittest.TestCase):
    def test_unavailable_broker_is_never_zero_pnl(self):
        result = open_position_pnl_snapshot(None)
        self.assertFalse(result["available"])
        self.assertIsNone(result["position_day_pnl"])

    def test_sums_only_when_all_positions_have_broker_numbers(self):
        result = open_position_pnl_snapshot([
            {"symbol": "NKE", "tipo": "BUY", "cantidad": 10,
             "broker_position_day_pnl": 6.50},
            {"symbol": "META", "tipo": "SELL", "cantidad": 2,
             "broker_position_day_pnl": "-3.25"},
        ])
        self.assertTrue(result["available"])
        self.assertEqual(result["position_day_pnl"], 3.25)
        self.assertEqual(result["unknown_count"], 0)
        self.assertEqual(result["source"], "SCHWAB_OPEN_POSITIONS")

    def test_missing_broker_value_never_invented(self):
        result = open_position_pnl_snapshot([
            {"symbol": "NKE", "tipo": "BUY", "cantidad": 10,
             "broker_position_day_pnl": 6.50},
            {"symbol": "META", "tipo": "BUY", "cantidad": 3,
             "broker_position_day_pnl": None},
        ])
        self.assertIsNone(result["position_day_pnl"])
        self.assertEqual(result["partial_known_pnl"], 6.5)
        self.assertEqual(result["unknown_count"], 1)
        self.assertIsNone(result["positions"][1]["broker_position_day_pnl"])

    def test_no_position_is_not_an_account_daily_realized_pnl(self):
        result = open_position_pnl_snapshot([])
        self.assertTrue(result["available"])
        self.assertEqual(result["positions"], [])
        self.assertEqual(result["position_day_pnl"], 0)
        # This is mathematically zero on open positions, not proof that
        # historical day P/L or all brokerage account P/L equals zero.


if __name__ == "__main__":
    unittest.main()
