"""REAL display never substitutes strategy estimates or partial wins for account P/L."""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import server
from core.dashboard_evidence import annotate_history
from core.ticker_performance import build_ticker_performance
from engine.bot_engine import ChocoletraBot


class StrictRealDisplayTests(unittest.TestCase):
    def test_real_candle_profit_does_not_enter_real_session_total(self):
        pos = dict(ticker="TSLA", mode="REAL", status="OPEN", floating_pnl=999,
                   valuation_available=True)
        row = build_ticker_performance(["TSLA"], positions=[pos],
            execution_rows=[dict(ticker="TSLA", mode="REAL")])[0]
        self.assertIsNone(row["unrealized_pnl"])
        self.assertIsNone(row["total_pnl"])
        self.assertEqual(row["unknown_open_pnl"], 1)
        self.assertIsNone(server._position_pnl_summary([pos])["real_unrealized_pnl"])

    def test_paper_candle_profit_remains_explicit_simulation(self):
        row = build_ticker_performance(["TSLA"], positions=[dict(
            ticker="TSLA", mode="PAPER", status="OPEN", floating_pnl=999,
            valuation_available=True)], execution_rows=[dict(ticker="TSLA", mode="PAPER")])[0]
        self.assertEqual(row["total_pnl"], 999)
        self.assertEqual(row["pnl_source"], "PAPER_SIMULATION")

    def test_public_snapshot_masks_real_estimate_without_mutating_engine_or_risk_inputs(self):
        raw = [dict(ticker="TSLA", mode="REAL", floating_pnl=999, status="OPEN")]
        original = copy.deepcopy(raw)
        with patch.object(server, "_ENGINE", SimpleNamespace(dashboard_snapshot=lambda: raw)):
            result = server._engine_positions_snapshot()
        self.assertIsNone(result[0]["floating_pnl"])
        self.assertEqual(result[0]["local_estimated_floating_pnl"], 999)
        self.assertEqual(raw, original)

    def test_manual_event_cannot_invent_broker_results_from_current_real_setting(self):
        with patch.dict(server.BOT_STATE, mode="real", trades=[], active_positions={},
                        stats={"trades_closed": 0, "trades_open": 0, "pnl_total": 0}):
            server._register_trade("SELL", "TSLA", 105, 3, "CLOSED", 999)
            event = server.BOT_STATE["trades"][-1]
            self.assertEqual(event["mode"], "UNKNOWN")
            self.assertIsNone(event["pnl"])
            self.assertIsNone(event["precio"])
            self.assertFalse(event["pnl_verified"])

    def test_engine_event_carries_real_mode_even_if_dashboard_global_mode_is_paper(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot._hooks = {}
        bot.set_hooks(on_trade_event=server._hook_trade_event)
        with patch.dict(server.BOT_STATE, mode="paper", trades=[], active_positions={},
                        stats={"trades_closed": 0, "trades_open": 0, "pnl_total": 0}):
            bot._emit_trade("BUY", "TSLA", 98, 2, "CLOSED", -3,
                execution_mode="REAL", price_verified=True, accounting_verified=True)
            event = server.BOT_STATE["trades"][-1]
            self.assertEqual(event["mode"], "REAL")
            self.assertEqual(event["pnl"], -3)
            self.assertEqual(event["pnl_source"], "LOCAL_RECORDED_FILLS_GROSS")
            self.assertFalse(event["is_broker_reported"])

    def test_unverified_engine_close_remains_unknown_not_zero(self):
        with patch.dict(server.BOT_STATE, trades=[], active_positions={},
                        stats={"trades_closed": 0, "trades_open": 0, "pnl_total": 0}):
            server._hook_trade_event(dict(tipo="BUY", ticker="TSLA", precio=None,
                cantidad=3, estado="CLOSED", pnl=None, mode="REAL",
                price_verified=False, accounting_verified=False))
            event = server.BOT_STATE["trades"][-1]
            self.assertIsNone(event["pnl"])
            self.assertFalse(event["pnl_verified"])

    def test_legacy_six_argument_hook_still_receives_one_event(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot._hooks = {"trade": Mock()}
        bot._emit_trade("BUY", "TSLA", 100, 3, "OPEN", 0)
        bot._hooks["trade"].assert_called_once_with("BUY", "TSLA", 100, 3, "OPEN", 0)

    def test_partial_profit_and_larger_final_loss_resolve_to_loss_only_with_all_fill_proof(self):
        common = dict(ticker="TSLA", modo="REAL", position_id="p", execution_id="e", position_side="LONG")
        row = dict(common, evento="POSITION_CLOSE", parent_order_id="entry", order_id="exit",
                   entrada=100, salida=99, cantidad=3, pnl_usd=-3)
        audit = [dict(common, evento="ENTRY_FILLED", estado="FILLED", order_id="entry",
                      cantidad=3, precio=100, detalle={"broker_price_verified": True}),
                 dict(common, evento="TP1_FILLED_70_PERCENT", estado="FILLED", order_id="partial",
                      cantidad=1, precio=101, detalle={"fill_price_verified": True}),
                 dict(common, evento="POSITION_CLOSED", order_id="exit", cantidad=3, precio=99,
                      pnl_usd=-3, detalle={"broker_confirmed": True, "fill_price_verified": True,
                                         "salida_real": 98, "tp1_ejecutado": True})]
        checked = annotate_history([row], audit)[0]
        self.assertTrue(checked["performance_verified"])
        self.assertEqual(checked["display_pnl_usd"], -3)
        audit[1]["detalle"] = {}
        self.assertFalse(annotate_history([row], audit)[0]["performance_verified"])
        self.assertIsNone(annotate_history([row], audit)[0]["display_pnl_usd"])

    def test_old_filled_partial_marker_without_price_proof_is_not_a_broker_fill_marker(self):
        start = datetime(2026, 10, 22, tzinfo=timezone.utc)
        row = dict(ticker="TSLA", modo="REAL", evento="TP1_FILLED_70_PERCENT", estado="FILLED",
                   timestamp_utc="2026-10-22T15:00:00Z", cantidad=1, precio=101,
                   pnl_usd="", detalle="{}")
        with patch("core.order_audit.read_order_audit", return_value=[row]):
            self.assertEqual(server._chart_trade_events("TSLA", start, start + timedelta(days=1)), [])
            row["detalle"] = json.dumps({"fill_price_verified": True})
            marker = server._chart_trade_events("TSLA", start, start + timedelta(days=1))[0]
            self.assertEqual(marker["price"], 101)
            self.assertIsNone(marker["pnl_usd"])


if __name__ == "__main__":
    unittest.main()
