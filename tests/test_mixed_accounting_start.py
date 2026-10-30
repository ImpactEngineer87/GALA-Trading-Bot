"""Mixed startup must continue PAPER while never enabling REAL with an accounting hold."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.mixed_accounting_start import (
    broker_confirmed_flat,
    unresolved_real_accounting,
)


class MixedAccountingStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        self.guard = self.root / "data/real_accounting_reconciliation.json"
        self.config = {
            "execution_engine": {
                "accounting_state_path": "data/real_accounting_reconciliation.json"
            }
        }

    def _write(self, rows):
        self.guard.write_text(json.dumps({"unresolved": rows}),
                              encoding="utf-8")

    def test_unapproved_hold_quarantines_real(self):
        self._write({"OPERATOR_HOLD": {"ticker": "META"}})
        blocked, reason = unresolved_real_accounting(
            self.config, self.root, "EXAMPLE_ACCOUNT")
        self.assertTrue(blocked)
        self.assertEqual(reason, "REAL_ACCOUNTING_UNRESOLVED")

    def test_clean_state_allows_normal_preflight(self):
        self._write({})
        blocked, reason = unresolved_real_accounting(
            self.config, self.root, "EXAMPLE_ACCOUNT")
        self.assertFalse(blocked)
        self.assertEqual(reason, "ACCOUNTING_CLEAN")

    def test_corrupt_or_missing_guard_fails_closed(self):
        self.assertTrue(unresolved_real_accounting(
            self.config, self.root, "EXAMPLE_ACCOUNT")[0])
        self.guard.write_text("{invalid", encoding="utf-8")
        self.assertTrue(unresolved_real_accounting(
            self.config, self.root, "EXAMPLE_ACCOUNT")[0])

    def test_unapproved_exception_still_blocks(self):
        self._write({"OPERATOR_HOLD": {"ticker": "META"}})
        with patch("core.historical_manual_resume.exception_status",
                   return_value={
                       "historical_resume_active": False,
                       "entry_blocked": True,
                   }):
            self.assertTrue(unresolved_real_accounting(
                self.config, self.root, "EXAMPLE_ACCOUNT")[0])

    def test_explicit_valid_exception_requires_real_preflight(self):
        self._write({"OPERATOR_HOLD": {"ticker": "META"}})
        with patch("core.historical_manual_resume.exception_status",
                   return_value={
                       "historical_resume_active": True,
                       "entry_blocked": False,
                   }):
            blocked, reason = unresolved_real_accounting(
                self.config, self.root, "EXAMPLE_ACCOUNT")
        self.assertFalse(blocked)
        self.assertEqual(reason, "EXACT_HISTORICAL_EXCEPTION_APPROVED")

    def test_broker_flat_requires_zero_positions_and_orders(self):
        class Response:
            status_code = 200

            def __init__(self, orders):
                self.orders = orders

            def json(self):
                return self.orders

        class Broker:
            positions = []
            orders = []
            all_orders = [{"status": "FILLED"}]

            def __init__(self, config):
                pass

            def _asegurar_token(self):
                return True

            def obtener_account_hash(self, account):
                return "EXAMPLE_HASH"

            def obtener_posiciones(self, account_hash):
                return self.positions

            def obtener_ordenes_abiertas(self, account_hash):
                return self.orders

            def _get_autorizado(self, url, params=None, timeout=None):
                return Response(self.all_orders)

        self.assertTrue(broker_confirmed_flat(
            self.config, "EXAMPLE_ACCOUNT", Broker))
        Broker.positions = [{"symbol": "BA"}]
        self.assertFalse(broker_confirmed_flat(
            self.config, "EXAMPLE_ACCOUNT", Broker))
        Broker.positions = []
        Broker.orders = [{"status": "WORKING"}]
        self.assertFalse(broker_confirmed_flat(
            self.config, "EXAMPLE_ACCOUNT", Broker))
        Broker.orders = []
        Broker.all_orders = [{"status": "PENDING_ACTIVATION"}]
        self.assertFalse(broker_confirmed_flat(
            self.config, "EXAMPLE_ACCOUNT", Broker))
        Broker.all_orders = [{"status": "FILLED", "childOrderStrategies": [
            {"status": "WORKING"}]}]
        self.assertFalse(broker_confirmed_flat(
            self.config, "EXAMPLE_ACCOUNT", Broker))
        Broker.all_orders = [{"status": "FILLED"}] * 500
        self.assertFalse(broker_confirmed_flat(
            self.config, "EXAMPLE_ACCOUNT", Broker))
        Broker.orders = None
        self.assertFalse(broker_confirmed_flat(
            self.config, "EXAMPLE_ACCOUNT", Broker))

    def test_broker_errors_block_paper_fallback_start(self):
        class BadBroker:
            def __init__(self, config):
                raise RuntimeError("no auth")
        self.assertFalse(broker_confirmed_flat(
            self.config, "EXAMPLE_ACCOUNT", BadBroker))
        self.assertFalse(broker_confirmed_flat(
            self.config, "", BadBroker))

    def test_hard_accounting_quarantine_survives_data_recovery(self):
        from engine.bot_engine import ChocoletraBot

        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.config = {"live_trading": {"runtime_isolate_ticker_failures": True}}
        bot._ticker_quarantine = {"BA": "REAL_ACCOUNTING_UNRESOLVED"}
        bot._ticker_quarantine_health = {}
        bot.execution_mode_for = lambda ticker: "REAL"
        bot._audit_event = lambda *args, **kwargs: None
        bot._emit_log = lambda *args, **kwargs: None

        bot._quarantine_real_ticker("BA", "Synthetic market data")
        self.assertEqual(bot._ticker_quarantine["BA"],
                         "REAL_ACCOUNTING_UNRESOLVED")
        self.assertFalse(bot._recover_ticker_quarantine("BA", "market_data"))
        self.assertFalse(bot._recover_ticker_quarantine("BA", "model"))
        self.assertFalse(bot._recover_ticker_quarantine("BA", "inference"))
        self.assertEqual(bot._ticker_quarantine["BA"],
                         "REAL_ACCOUNTING_UNRESOLVED")


if __name__ == "__main__":
    unittest.main()
