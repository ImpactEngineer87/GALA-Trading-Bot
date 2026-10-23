"""Exercise actual engine close accounting offline against confirmed/unknown fills."""
import csv
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from api.hybrid_order_executor import EjecutorHibridoDeOrdenes
from core.daily_loss_guard import DailyLossGuard
from core.order_audit import OrderAuditLedger
from core.real_accounting import RealAccountingGuard
from core.risk_manager import OrdenTradingCalculada
from core.trade_history import TradeHistoryLedger
from engine.bot_engine import ChocoletraBot


class RealCloseAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def engine(self, *, paper=False, response=None, entry_verified=True, partial=False):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.config = {"kill_switch": {"daily_loss": {"state_file": str(self.root / "risk.json")}}}
        bot._lock = threading.RLock()
        bot.account_number = "offline-account"
        bot._is_paper_ticker = Mock(return_value=paper)
        bot._has_real_tickers = Mock(return_value=not paper)
        bot._position_lifecycle_context = Mock(return_value={
            "position_id": "position-1", "execution_id": "execution-1",
            "signal_id": "signal-1", "correlation_id": "correlation-1"})
        bot._state_machine = Mock(return_value=Mock())
        bot._asegurar_token_trading = Mock(return_value=True)
        bot._asegurar_ticker_limpio = Mock(return_value=True)
        bot._cantidad_real_broker = Mock(return_value=10)
        bot._recuperar_cierre_stop_llenado = Mock(return_value={})
        bot._marcar_liquidado = Mock()
        bot.motor_señales = Mock()
        bot.orquestador = Mock()
        bot._emit_log = Mock()
        bot._emit_trade = Mock()
        bot._evaluar_limite_perdida_diaria = Mock(return_value=False)
        bot.real_accounting_guard = RealAccountingGuard(self.root / "accounting.json")
        history = TradeHistoryLedger(str(self.root / "history.csv"))
        audit = OrderAuditLedger(str(self.root / "audit.csv"))
        bot._history_event = lambda event, **fields: history.record(event, **fields)
        bot._audit_event = lambda event, **fields: audit.record(event, modo="PAPER" if paper else "REAL", **fields)
        bot.daily_loss_guard = DailyLossGuard(bot.config, str(history.path))
        bot.ejecutor_hibrido = Mock()
        bot.ejecutor_hibrido.has_pending_exit.return_value = False
        bot.ejecutor_hibrido.liquidar_posicion.return_value = response or {
            "status": "OK", "posicion_plana": True, "order_id": "exit-1",
            "cantidad": 10, "avg_price": 37.595, "fill_price_verified": True}
        bot._posiciones = {"NKE": {
            "tipo": "BUY", "entrada": 37.66, "orden": SimpleNamespace(cantidad=10),
            "entry_order_id": "entry-1", "entry_integrity": {"entry_confirmed": entry_verified},
            "position_id": "position-1", "tp1_hit": partial, "cantidad_resto": 3 if partial else 10,
            "pnl_realizado": 0.7 if partial else 0.0,
        }}
        return bot

    def last_close(self):
        with (self.root / "history.csv").open(newline="") as handle:
            return list(csv.DictReader(handle))[-1]

    def test_broker_fill_replaces_requested_price_and_daily_pnl(self):
        bot = self.engine()
        self.assertTrue(bot._cerrar_posicion_impl("NKE", 37.69, "offline test"))
        row = self.last_close()
        self.assertEqual(float(row["salida"]), 37.595)
        self.assertAlmostEqual(float(row["pnl_usd"]), -0.65)
        self.assertAlmostEqual(bot.daily_loss_guard.realized_pnl(False), -0.65)
        self.assertFalse(bot._real_accounting_status()["blocked"])

    def test_flat_without_price_is_unknown_not_zero_and_reversal_is_blocked(self):
        bot = self.engine(response={"status": "OK", "posicion_plana": True,
            "order_id": "exit-1", "cantidad": 10, "avg_price": 0, "fill_price_verified": False})
        self.assertTrue(bot._cerrar_posicion_impl("NKE", 100, "opposite signal"))
        self.assertNotIn("NKE", bot._posiciones)
        row = self.last_close()
        self.assertEqual(row["salida"], "")
        self.assertEqual(row["pnl_usd"], "")
        bot._emit_trade.assert_called_once_with("BUY", "NKE", None, 10, "CLOSED", None)
        self.assertIsNone(bot.orquestador.al_cerrar.call_args.args[1])
        self.assertFalse(bot.orquestador.al_cerrar.call_args.kwargs["lifecycle"]["exit_confirmed"])
        decision = SimpleNamespace(propuesta=SimpleNamespace(ticker="NKE", tipo="SELL"))
        self.assertFalse(bot._abrir_operacion(decision, 100, None))
        self.assertEqual(decision.execution_skip_reason, "REAL_ACCOUNTING_UNRESOLVED")
        self.assertTrue(RealAccountingGuard(self.root / "accounting.json").snapshot()["blocked"])

    def test_flat_without_any_exit_id_does_not_erase_the_tracked_quantity(self):
        bot = self.engine()
        bot._cantidad_real_broker.return_value = 0
        self.assertTrue(bot._cerrar_posicion_impl("NKE", 200, "external flat"))
        bot.ejecutor_hibrido.liquidar_posicion.assert_not_called()
        self.assertEqual(self.last_close()["cantidad"], "10")
        self.assertEqual(self.last_close()["pnl_usd"], "")
        self.assertTrue(bot._real_accounting_status()["blocked"])

    def test_positive_unverified_price_or_unverified_entry_cannot_make_pnl(self):
        for flag in (False, None):
            bot = self.engine(response={"status": "OK", "posicion_plana": True,
                "order_id": "exit-1", "cantidad": 10, "avg_price": 99, "fill_price_verified": flag})
            bot._cerrar_posicion_impl("NKE", 100, "test")
            self.assertEqual(self.last_close()["pnl_usd"], "")
        bot = self.engine(entry_verified=False)
        bot._cerrar_posicion_impl("NKE", 100, "test")
        self.assertEqual(self.last_close()["pnl_usd"], "")

    def test_incomplete_close_quantity_cannot_price_the_entire_position(self):
        bot = self.engine(response={"status": "OK", "posicion_plana": True,
            "order_id": "exit-1", "cantidad": 5, "avg_price": 38, "fill_price_verified": True})
        bot._cerrar_posicion_impl("NKE", 99, "test")
        self.assertEqual(self.last_close()["pnl_usd"], "")
        self.assertTrue(bot._real_accounting_status()["blocked"])

    def test_partial_and_final_fill_pnl_are_weighted_without_double_counting(self):
        bot = self.engine(partial=True, response={"status": "OK", "posicion_plana": True,
            "order_id": "exit-1", "cantidad": 3, "avg_price": 37.56, "fill_price_verified": True})
        bot._cerrar_posicion_impl("NKE", 99, "test")
        self.assertAlmostEqual(float(self.last_close()["pnl_usd"]), 0.4)
        self.assertAlmostEqual(float(self.last_close()["salida"]), 37.7)
        self.assertAlmostEqual(bot.daily_loss_guard.realized_pnl(False), -0.3)

    def test_unknown_partial_price_keeps_remainder_protected_and_pnl_unknown(self):
        bot = self.engine()
        pos = bot._posiciones["NKE"]
        pos.update(orden=SimpleNamespace(cantidad=10, cantidad_parcial=7), cantidad_resto=3,
                   tp1=38, modo_resto="breakeven", sl=37.3,
                   sl_order_id="old-stop", tp1_order_id="tp1")
        bot.breakeven_offset_pct = 0.01
        bot.ejecutor_hibrido.detalle_orden.return_value = {
            "status": "FILLED", "filled": 7, "avg_price": 0, "fill_price_verified": False}
        bot.ejecutor_hibrido.mover_stop_breakeven.return_value = {"status": "OK", "order_id": "new-stop"}
        bot._ejecutar_parcial_impl("NKE", pos)
        self.assertEqual(pos["sl_order_id"], "new-stop")
        self.assertTrue(pos["partial_price_pending"])
        self.assertEqual(bot.ejecutor_hibrido.mover_stop_breakeven.call_args.args[2], 3)
        self.assertTrue(bot._real_accounting_status()["blocked"])
        bot._emit_trade.assert_not_called()
        self.assertIn("NKE", bot._posiciones)

    def test_broker_not_flat_preserves_tracking_and_writes_no_close(self):
        bot = self.engine(response={"status": "OK", "posicion_plana": False,
            "order_id": "exit-1", "cantidad": 10, "avg_price": 38, "fill_price_verified": True})
        self.assertFalse(bot._cerrar_posicion_impl("NKE", 100, "test"))
        self.assertIn("NKE", bot._posiciones)
        self.assertFalse((self.root / "history.csv").exists())

    def test_incomplete_accounting_is_unknown_in_dashboard_but_keeps_daily_loss_guard(self):
        bot = self.engine()
        bot.real_accounting_guard.require_reconciliation("p", ticker="NKE", reason="Missing fill")
        bot.daily_loss_guard.add_realized(False, -210)
        status = bot.daily_risk_snapshot()
        self.assertIsNone(status["net_pnl"])
        self.assertIsNone(status["remaining_to_limit_usd"])
        self.assertTrue(status["real_accounting"]["blocked"])
        bot._activar_kill_switch_diario = Mock()
        self.assertTrue(ChocoletraBot._evaluar_limite_perdida_diaria(bot))
        self.assertEqual(bot._activar_kill_switch_diario.call_args.args[0]["net_pnl"], -210)

    def test_paper_remains_explicitly_simulated(self):
        bot = self.engine(paper=True)
        bot._cerrar_posicion_impl("NKE", 37.69, "test")
        self.assertEqual(self.last_close()["modo"], "PAPER")
        self.assertAlmostEqual(float(self.last_close()["pnl_usd"]), 0.3)
        bot.ejecutor_hibrido.liquidar_posicion.assert_not_called()

    def test_fill_accounting_keeps_subcent_precision_with_tick_rounded_protection(self):
        bot = self.engine()
        original = OrdenTradingCalculada("NKE", "BUY", 38.31, 38.21, 0.2, 10, 1, 0.1)
        adjusted = bot._ajustar_orden_llenada(original, 10, 38.315)
        self.assertEqual(adjusted.precio_limite, 38.315)
        self.assertEqual(adjusted.stop_loss, round(adjusted.stop_loss, 2))


class AccountingPersistenceTests(unittest.TestCase):
    def test_unknown_shadow_exit_does_not_create_a_win_loss_label(self):
        from agents.shadow_store import ShadowDecisionStore
        with tempfile.TemporaryDirectory() as tmp:
            store = ShadowDecisionStore(str(Path(tmp) / "shadow.csv"))
            store._open["NKE"] = {"ticker": "NKE", "tipo": "BUY", "precio_entrada": 37.66}
            row = store.cerrar("NKE", None)
            for key in ("precio_salida", "r_multiple", "ganadora", "shadow_correct"):
                self.assertEqual(row[key], "")
            self.assertNotIn("NKE", store._open)

    def test_corrupt_state_blocks_entries_without_touching_models_or_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("invalid json")
            self.assertTrue(RealAccountingGuard(path).snapshot()["blocked"])
            self.assertEqual(path.read_text(), "invalid json")

    def test_current_session_unverified_real_history_blocks_even_with_numeric_old_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history.csv"
            history.write_text("fecha,modo,ticker,evento,pnl_usd\n2026-10-22,REAL,NKE,POSITION_CLOSE,999\n")
            before = history.read_bytes()
            guard = RealAccountingGuard(root / "state.json")
            guard.inspect_session(history, root / "missing.csv", "2026-10-22")
            self.assertTrue(guard.snapshot()["blocked"])
            self.assertEqual(history.read_bytes(), before)
            self.assertTrue(RealAccountingGuard(root / "state.json").snapshot()["blocked"])

    def test_old_session_does_not_become_todays_realized_risk_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "history.csv").write_text("fecha,modo,ticker,pnl_usd\n2026-10-22,REAL,NKE,999\n")
            guard = RealAccountingGuard(root / "state.json")
            guard.inspect_session(root / "history.csv", root / "missing.csv", "2026-10-23")
            self.assertFalse(guard.snapshot()["blocked"])

    def test_live_parser_rejects_partial_price_coverage_and_retains_filled_quantity(self):
        for price in (100, 0, float("nan")):
            detail = EjecutorHibridoDeOrdenes._detalle_desde_orden({"status": "FILLED", "filledQuantity": 10,
                "price": 999, "orderActivityCollection": [{"executionLegs": [{"price": price, "quantity": 5}]}]})
            self.assertEqual(detail["filled"], 10)
            self.assertEqual(detail["avg_price"], 0)
            self.assertFalse(detail["fill_price_verified"])

    def test_live_parser_retains_actual_weighted_subcent_fill(self):
        detail = EjecutorHibridoDeOrdenes._detalle_desde_orden({"status": "FILLED", "filledQuantity": 10,
            "price": 999, "orderActivityCollection": [{"executionLegs": [
                {"price": 38.31, "quantity": 5}, {"price": 38.32, "quantity": 5}]}]})
        self.assertAlmostEqual(detail["avg_price"], 38.315)
        self.assertTrue(detail["fill_price_verified"])


if __name__ == "__main__":
    unittest.main()
