"""Price observation freshness for mixed REAL/PAPER dashboard valuations."""
import json
import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from engine.bot_engine import ChocoletraBot


class DashboardPriceFreshnessTests(unittest.TestCase):
    now = 1793372400.0

    def engine(self, *, paper=True, observed_age=5, bar_age=600):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot._lock = threading.RLock()
        bot._is_paper_ticker = Mock(return_value=paper)
        bot._last_prices = {"META": 98.0}
        bot._last_price_times = {"META": self.now - observed_age}
        bot._last_bar_times = {"META": datetime.fromtimestamp(
            self.now - bar_age, timezone.utc).isoformat()}
        bot._max_risk_price_age_sec = 120
        bot._posiciones = {"META": {
            "tipo": "SELL", "cantidad": 5, "entrada": 100.0,
            "orden": SimpleNamespace(cantidad=5),
            "entry_integrity": {"entry_confirmed": not paper},
            "execution_mode": "PAPER" if paper else "REAL",
        }}
        return bot

    def snapshot(self, bot):
        with patch("engine.bot_engine.time.time", return_value=self.now):
            result = bot.dashboard_snapshot()[0]
        json.dumps(result, allow_nan=False)
        return result

    def test_fresh_observed_price_from_older_confirmed_bar_has_pnl(self):
        for paper in (True, False):
            with self.subTest(paper=paper):
                bot = self.engine(paper=paper)
                original_position = dict(bot._posiciones["META"])
                row = self.snapshot(bot)
                self.assertTrue(row["valuation_available"])
                self.assertEqual(row["floating_pnl"], 10.0)
                self.assertEqual(row["price_age_sec"], 5.0)
                self.assertEqual(row["market_bar_at"], bot._last_bar_times["META"])
                self.assertTrue(row["last_price_at"])
                self.assertEqual(bot._posiciones["META"], original_position)
                self.assertFalse(row["is_broker_reported"])

    def test_fresh_bar_cannot_hide_stale_price_observation(self):
        row = self.snapshot(self.engine(observed_age=121, bar_age=1))
        self.assertFalse(row["valuation_available"])
        self.assertIsNone(row["floating_pnl"])
        self.assertEqual(row["price_age_sec"], 121.0)

    def test_observation_age_obeys_configured_risk_price_limit(self):
        bot = self.engine(observed_age=45)
        bot._max_risk_price_age_sec = 30
        self.assertFalse(self.snapshot(bot)["valuation_available"])
        bot._max_risk_price_age_sec = 60
        self.assertTrue(self.snapshot(bot)["valuation_available"])

    def test_unknown_nonfinite_or_future_observation_cannot_value_position(self):
        for timestamp in (None, "bad", True, 0, -1, float("nan"),
                          float("inf"), self.now + 1, 1e100):
            with self.subTest(timestamp=timestamp):
                bot = self.engine()
                bot._last_price_times["META"] = timestamp
                row = self.snapshot(bot)
                self.assertFalse(row["valuation_available"])
                self.assertIsNone(row["floating_pnl"])
                self.assertIsNone(row["price_age_sec"])
                self.assertEqual(row["last_price_at"], "")
        bot = self.engine()
        del bot._last_price_times
        self.assertFalse(self.snapshot(bot)["valuation_available"])

    def test_price_timestamp_and_value_are_one_locked_snapshot(self):
        bot = self.engine()
        replacement = dict(bot._last_price_times)
        replacement["META"] = self.now - 500

        def change_live_timestamp(*args, **kwargs):
            bot._last_price_times = replacement
            return True

        bot._is_paper_ticker.side_effect = change_live_timestamp
        self.assertTrue(self.snapshot(bot)["valuation_available"])

    def test_fresh_timestamp_does_not_make_invalid_entry_or_price_a_valuation(self):
        for value in (float("nan"), float("inf"), 0, -1):
            with self.subTest(entry=value):
                bot = self.engine()
                bot._posiciones["META"]["entrada"] = value
                self.assertFalse(self.snapshot(bot)["valuation_available"])
            with self.subTest(price=value):
                bot = self.engine()
                bot._last_prices["META"] = value
                self.assertFalse(self.snapshot(bot)["valuation_available"])


if __name__ == "__main__":
    unittest.main()
