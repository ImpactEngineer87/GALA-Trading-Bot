"""Offline checks: bounded entry pricing, short side, and independent TTL.

This suite does not submit/cancel any brokerage orders.
"""
import unittest
from types import SimpleNamespace

from api.hybrid_order_executor import (
    EjecutorHibridoDeOrdenes, _instruccion_apertura,
)
from engine.bot_engine import ChocoletraBot


class SafeEntryExecutionTests(unittest.TestCase):
    def executor(self, tolerance, *, paper=False):
        x = EjecutorHibridoDeOrdenes.__new__(EjecutorHibridoDeOrdenes)
        x.paper_trading = paper
        x.execution_v2_config = {
            "enabled": True, "entry_limit_tolerance_usd": tolerance,
        }
        x.max_slippage_pct = 0
        return x

    @staticmethod
    def order(side, price=100.0):
        return SimpleNamespace(tipo=side, precio_limite=price)

    def test_sell_signal_uses_schwab_sell_short_not_sell(self):
        self.assertEqual(_instruccion_apertura("SELL"), "SELL_SHORT")
        self.assertEqual(_instruccion_apertura("BUY"), "BUY")
        with self.assertRaises(ValueError):
            _instruccion_apertura("OM-R")
        with self.assertRaises(ValueError):
            _instruccion_apertura("")

    def test_bounded_tolerance_is_in_correct_direction(self):
        executor = self.executor(0.05)
        self.assertEqual(executor._submitted_limit_price(self.order("BUY"), "LIMIT"), 100.05)
        self.assertEqual(executor._submitted_limit_price(self.order("SELL"), "LIMIT"), 99.95)
        executor.execution_v2_config["entry_limit_tolerance_usd"] = 0.10
        self.assertEqual(executor._submitted_limit_price(self.order("BUY"), "LIMIT"), 100.10)
        self.assertEqual(executor._submitted_limit_price(self.order("SELL"), "LIMIT"), 99.90)

    def test_tolerance_off_preserves_original_limit(self):
        executor = self.executor(0)
        self.assertEqual(executor._submitted_limit_price(self.order("BUY"), "LIMIT"), 100.0)

    def test_paper_does_not_gain_real_execution_slippage(self):
        executor = self.executor(0.10, paper=True)
        self.assertEqual(executor._submitted_limit_price(self.order("SELL"), "LIMIT"), 100.0)

    def test_rejects_unsafe_price_buffer(self):
        executor = self.executor(0.2)
        with self.assertRaises(ValueError):
            executor._submitted_limit_price(self.order("SELL"), "LIMIT")
        executor.execution_v2_config["entry_limit_tolerance_usd"] = -1
        with self.assertRaises(ValueError):
            executor._submitted_limit_price(self.order("BUY"), "LIMIT")
        executor.execution_v2_config["entry_limit_tolerance_usd"] = "NaN"
        with self.assertRaises(ValueError):
            executor._submitted_limit_price(self.order("BUY"), "LIMIT")

    def test_twenty_second_limit_only_not_paper_market(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.entrada_timeout_seg = 180
        bot.entrada_limit_timeout_seg = 20.0
        bot.config = {"execution_engine": {"enabled": True}}
        bot._is_paper_ticker = lambda ticker, pos=None: bool(pos.get("paper"))
        self.assertEqual(bot._pending_entry_timeout(
            "NKE", {"paper": False, "execution_context": {
                "submitted_order_type": "LIMIT",
            }}
        ), 20.0)
        self.assertEqual(bot._pending_entry_timeout(
            "NKE", {"paper": False, "execution_context": {
                "submitted_order_type": "MARKET",
            }}
        ), 180)
        self.assertEqual(bot._pending_entry_timeout(
            "NKE", {"paper": True, "execution_context": {
                "submitted_order_type": "LIMIT",
            }}
        ), 180)


if __name__ == "__main__":
    unittest.main()
