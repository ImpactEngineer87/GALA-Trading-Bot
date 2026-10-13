"""No-network tests of safe market-data pacing and read-only x/50 audit."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.schwab_rate_guard import (
    MarketDataGuard,
    SchwabMarketBackoff,
    guarded_market_get,
)


class Response:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


class MarketDataGuardTests(unittest.TestCase):
    def test_403_enters_cooldown_without_oauth_action(self):
        guard = MarketDataGuard(min_interval=0)
        with patch("api.schwab_rate_guard._setting", return_value=1.0):
            guard.observe(Response(403, {"x-request-id": "test-id"}),
                          "https://api.schwabapi.com/marketdata/v1/quotes?symbols=NVDA")
        result = guard.snapshot()
        self.assertEqual(result["status"], 403)
        self.assertEqual(result["request_id"], "test-id")
        self.assertNotIn("NVDA", result["endpoint"])
        with self.assertRaises(SchwabMarketBackoff):
            guard.acquire()

    def test_429_obeys_retry_after(self):
        guard = MarketDataGuard(min_interval=0)
        with patch("api.schwab_rate_guard._setting", return_value=1.0):
            guard.observe(Response(429, {"Retry-After": "45"}),
                          "https://api.schwabapi.com/marketdata/v1/pricehistory")
        self.assertGreaterEqual(guard.snapshot()["cooldown_remaining_sec"], 40)

    def test_success_does_not_enter_cooldown(self):
        guard = MarketDataGuard(min_interval=0)
        guard.observe(Response(200), "https://api.schwabapi.com/marketdata/v1/quotes")
        guard.acquire()
        self.assertEqual(guard.snapshot()["cooldown_remaining_sec"], 0)

    def test_dashboard_and_client_share_guard(self):
        guard = MarketDataGuard(min_interval=0)
        with patch("api.schwab_rate_guard.MARKET_DATA_GUARD", guard), \
             patch("api.schwab_rate_guard.requests.get", return_value=Response(403)) as http:
            response = guarded_market_get("https://api.schwabapi.com/marketdata/v1/quotes")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(http.call_count, 1)
            with self.assertRaises(SchwabMarketBackoff):
                guarded_market_get("https://api.schwabapi.com/marketdata/v1/quotes")
            self.assertEqual(http.call_count, 1)


class CounterAuditTests(unittest.TestCase):
    def test_counter_does_not_advance_on_excluded_rows(self):
        from AUDITAR_CONTADOR_IA import report
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            feedback = root / "feedback.csv"
            state = root / "state.json"
            with feedback.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=[
                        "ticker", "timestamp", "modo", "execution_valid",
                        "exclusion_reason", "position_id"
                    ]
                )
                writer.writeheader()
                writer.writerow({"ticker": "NVDA", "execution_valid": "1"})
                writer.writerow({
                    "ticker": "NVDA", "execution_valid": "0",
                    "exclusion_reason": "BROKER_CONFIRMATION_MISSING"
                })
            state.write_text(json.dumps({
                "tickers": {"NVDA": {"last_attempt_count": 0}}
            }), encoding="utf-8")
            data = report("NVDA", feedback, state)
            self.assertEqual(data["new_observations_x50"], 1)
            self.assertEqual(data["excluded_closed"], 1)
            self.assertEqual(data["exclusion_reasons"]["BROKER_CONFIRMATION_MISSING"], 1)


if __name__ == "__main__":
    unittest.main()
