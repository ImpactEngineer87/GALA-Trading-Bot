"""Offline production-method tests; no credentials, broker HTTP or orders.

Run: python -m unittest discover -s tests -p test_v3_3_19_reliable_real_exits.py
Reproduces October 8 (v3.3.18): Schwab's single-order and full order-list
reads fail for the account, the open-position monitor polled the full list,
and the shared list pause then refused the WORKING-list reads that closes and
the end-of-session kill switch need ("cierre REAL no confirmado").
"""
import threading
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

from test_schwab_request_budget_offline import (
    TRANSPORT, Clock, Deferred, GateFixture, Response, control_requests, load_methods,
)
from test_v3_3_15_exit_control import ticker_bot

ACCOUNT = "private-account"
LIST = f"https://api.schwabapi.com/trader/v1/accounts/{ACCOUNT}/orders"
ORDER = LIST + "/private-order"
BROKEN = Response(400, {"errors": [{"status": 500, "id": "15561df6-0ab3-dc65-a391-302798a9dde5"}]})
WORKING = {"status": "WORKING"}


def order(oid, status="WORKING", symbol="NVDA"):
    return {"orderId": oid, "status": status,
            "orderLegCollection": [{"instrument": {"symbol": symbol}}]}


class IsolatedPauseTests(GateFixture):
    def test_failing_full_list_does_not_pause_the_working_list(self):
        self.gate.request(self.send(BROKEN), LIST)
        with self.assertRaises(Deferred):
            self.gate.request(self.send(), LIST)
        self.gate.request(self.send(), LIST, params=WORKING)
        self.gate.request(self.send(BROKEN), LIST, params=WORKING)
        self.gate.request(self.send(), ORDER)
        self.assertEqual(len(self.calls), 4)

    def test_background_poll_failures_never_pause_order_action_reads(self):
        for _ in range(3):
            self.gate.request(self.send(BROKEN), LIST, params=WORKING)
            self.clock.advance(61)
        self.gate.request(self.send(BROKEN), LIST, params=WORKING)
        with self.assertRaises(Deferred) as raised:
            self.gate.request(self.send(), LIST, params=WORKING)
        self.assertEqual(raised.exception.reason, "order_read_backoff")
        with control_requests():
            self.gate.request(self.send(), LIST, params=WORKING)
            self.gate.request(self.send(BROKEN), LIST, params=WORKING)
            with self.assertRaises(Deferred):
                self.gate.request(self.send(), LIST, params=WORKING)
        self.assertEqual(len(self.calls), 6)

    def test_error_log_names_which_order_list_failed(self):
        self.gate.request(self.send(BROKEN), LIST)
        self.gate.request(self.send(BROKEN), LIST, params=WORKING)
        self.gate.request(self.send(BROKEN), ORDER)
        self.assertEqual([e.get("orders_query") for e in self.events],
                         ["status=ALL", "status=WORKING", None])
        self.assertNotIn(ACCOUNT, str(self.events))


class MonitorStatusFixture(GateFixture):
    def setUp(self):
        super().setUp()
        Executor = load_methods("api/hybrid_order_executor.py", "EjecutorHibridoDeOrdenes", {
            "estado_orden_vigilada", "_working_snapshot_reciente", "_working_orders_snapshot",
            "_iter_order_nodes", "_buscar_orden_en_listado", "_listado_ordenes", "_orden_en",
            "orden_trabajando"},
            {"schwab_request": self.gate.request, "nullcontext": nullcontext, "_ps": Mock(),
             "time": SimpleNamespace(monotonic=lambda: self.clock.mono)})
        Executor._iter_order_nodes.__globals__["EjecutorHibridoDeOrdenes"] = Executor
        self.executor = ex = Executor()
        ex.TRADER_URL, ex.request_timeout_s, ex.paper_trading = "https://api.schwabapi.com/trader/v1", 5, False
        ex._order_list_cache, ex._order_list_lock = None, threading.Lock()
        ex._working_cache, ex._working_lock = None, threading.Lock()
        self.working = [order("tp1"), order("sl")]
        self.counts = {"working": 0, "single": 0, "full": 0}

        def working_list(account):                 # SchwabClient provider, status=WORKING
            self.counts["working"] += 1
            return self.gate.request(self.list_get, LIST, params=WORKING).payload
        ex._working_orders_provider = working_list

        def get(url, **kwargs):                    # single order and full list: broken
            self.counts["single" if url.endswith("-order") or "/orders/" in url else "full"] += 1
            return BROKEN
        get.__name__ = "get"
        ex._http_session = lambda: SimpleNamespace(get=get)

    def list_get(self, url, **kwargs):
        return Response(200, list(self.working))
    list_get.__name__ = "get"


class MonitorStatusTests(MonitorStatusFixture):
    def test_working_orders_share_one_recent_list_read(self):
        for _ in range(3):
            for oid in ("tp1", "sl"):
                self.assertEqual(self.executor.estado_orden_vigilada(ACCOUNT, oid, 5), "WORKING")
            self.clock.advance(2)
        self.assertEqual(self.counts, {"working": 1, "single": 0, "full": 0})

    def test_order_gone_without_readable_detail_is_not_working(self):
        self.working = [order("tp1")]
        self.assertEqual(self.executor.estado_orden_vigilada(ACCOUNT, "sl", 5), "NOT_WORKING")
        self.assertEqual((self.counts["single"], self.counts["full"]), (1, 1))

    def test_order_gone_with_readable_detail_reports_it(self):
        self.working = []
        self.executor._buscar_orden_en_listado = lambda account, oid, *a: order(oid, "FILLED")
        self.assertEqual(self.executor.estado_orden_vigilada(ACCOUNT, "sl", 5), "FILLED")

    def test_unreadable_working_list_is_unknown(self):
        self.executor._working_orders_provider = lambda account: None
        self.assertEqual(self.executor.estado_orden_vigilada(ACCOUNT, "sl", 5), "DESCONOCIDO")


class CancelPriorityTests(unittest.TestCase):
    def test_cancellation_and_its_confirmation_use_the_order_action_reserve(self):
        Executor = load_methods("api/hybrid_order_executor.py", "EjecutorHibridoDeOrdenes",
                                {"cancelar_orden_confirmada"}, {})
        executor = Executor()
        seen = []
        executor._cancelar_orden_confirmada_impl = lambda *args: seen.append(
            getattr(TRANSPORT["_TRAFFIC_CONTEXT"], "control", False)) or {"confirmado": True}
        self.assertEqual(executor.cancelar_orden_confirmada(ACCOUNT, "sl", ticker="NVDA"),
                         {"confirmado": True})
        self.assertEqual(seen, [True])
        self.assertFalse(getattr(TRANSPORT["_TRAFFIC_CONTEXT"], "control", False))


def monitor_bot(statuses, broker_qty):
    Bot = load_methods("engine/bot_engine.py", "ChocoletraBot", {"_gestionar_real"}, {})
    bot = Bot()
    bot._activo, bot._lock, bot.account_number = True, threading.RLock(), ACCOUNT
    pos = {"tipo": "BUY", "salida_parcial": True, "tp1_hit": False, "tp1_order_id": "tp1",
           "sl_order_id": "sl", "sl_reconcile_pending": False, "tp_final": 0,
           "cantidad_resto": 3, "sl_actual": 95.0}
    bot._posiciones = {"NVDA": pos}
    bot._asegurar_token_trading = lambda: True
    bot.ejecutor_hibrido = SimpleNamespace(
        monitor_status_max_age_s=5.0,
        estado_orden_vigilada=Mock(side_effect=lambda account, oid, age: statuses[oid]))
    bot._cantidad_real_broker = Mock(return_value=broker_qty)
    bot._ejecutar_parcial = Mock()
    bot._cerrar_posicion = Mock()
    bot._gestionar_real("NVDA", pos, 100.0)
    return bot, pos


class MonitorDecisionTests(unittest.TestCase):
    def test_tp1_gone_and_position_reduced_to_remainder_runs_the_partial(self):
        bot, pos = monitor_bot({"tp1": "NOT_WORKING", "sl": "WORKING"}, broker_qty=3)
        bot._ejecutar_parcial.assert_called_once_with("NVDA", pos, tp1_por_posicion=True)
        bot._cerrar_posicion.assert_not_called()

    def test_stop_gone_and_broker_flat_closes_as_stop(self):
        bot, pos = monitor_bot({"tp1": "NOT_WORKING", "sl": "NOT_WORKING"}, broker_qty=0)
        bot._ejecutar_parcial.assert_not_called()
        bot._cerrar_posicion.assert_called_once_with("NVDA", 95.0, motivo="SL(real)")

    def test_gone_orders_with_unchanged_position_do_nothing(self):
        bot, _ = monitor_bot({"tp1": "NOT_WORKING", "sl": "NOT_WORKING"}, broker_qty=10)
        bot._ejecutar_parcial.assert_not_called()
        bot._cerrar_posicion.assert_not_called()

    def test_unreadable_position_does_nothing(self):
        bot, _ = monitor_bot({"tp1": "NOT_WORKING", "sl": "NOT_WORKING"}, broker_qty=None)
        bot._ejecutar_parcial.assert_not_called()
        bot._cerrar_posicion.assert_not_called()

    def test_working_or_unknown_orders_never_read_positions(self):
        for status in ("WORKING", "DESCONOCIDO"):
            with self.subTest(status=status):
                bot, _ = monitor_bot({"tp1": status, "sl": status}, broker_qty=0)
                bot._cantidad_real_broker.assert_not_called()
                bot._ejecutar_parcial.assert_not_called()
                bot._cerrar_posicion.assert_not_called()

    def test_reported_fills_act_without_position_reads(self):
        bot, pos = monitor_bot({"tp1": "FILLED", "sl": "WORKING"}, broker_qty=None)
        bot._ejecutar_parcial.assert_called_once_with("NVDA", pos, tp1_por_posicion=False)
        bot, _ = monitor_bot({"tp1": "WORKING", "sl": "FILLED"}, broker_qty=None)
        bot._cerrar_posicion.assert_called_once_with("NVDA", 95.0, motivo="SL(real)")
        bot._cantidad_real_broker.assert_not_called()


class PartialByPositionTests(unittest.TestCase):
    def make(self, detail_status):
        Bot = load_methods("engine/bot_engine.py", "ChocoletraBot", {"_ejecutar_parcial_impl"}, {})
        bot = Bot()
        bot._lock, bot.account_number = threading.RLock(), ACCOUNT
        pos = {"tipo": "BUY", "orden": SimpleNamespace(cantidad_parcial=7), "tp1": 110.0,
               "entrada": 100.0, "modo_resto": "breakeven", "sl": 95.0, "sl_order_id": "sl",
               "cantidad_resto": 3, "tp1_order_id": "tp1", "tp1_hit": False, "pnl_realizado": 0.0}
        bot._posiciones = {"NVDA": pos}
        bot._is_paper_ticker = lambda ticker, pos=None: False
        bot.breakeven_offset_pct = 0.0
        bot._asegurar_token_trading = lambda: True
        bot._ahora_et = lambda: "now"
        bot._audit_event = Mock()
        bot._cerrar_posicion = Mock()
        bot.ejecutor_hibrido = SimpleNamespace(
            detalle_orden=Mock(return_value={"status": detail_status, "avg_price": 0.0}),
            mover_stop_breakeven=Mock(return_value={"status": "OK", "order_id": "sl-3"}))
        return bot, pos

    def test_position_evidence_reprotects_once_without_inventing_pnl(self):
        bot, pos = self.make("DESCONOCIDO")
        bot._ejecutar_parcial_impl("NVDA", pos, tp1_por_posicion=True)
        bot.ejecutor_hibrido.mover_stop_breakeven.assert_called_once_with(
            "NVDA", "BUY", 3, 100.0, "sl", ACCOUNT)
        self.assertTrue(pos["tp1_hit"])
        self.assertTrue(pos["partial_price_pending"])
        self.assertEqual((pos["sl_order_id"], pos["sl_actual"], pos["pnl_realizado"]), ("sl-3", 100.0, 0.0))
        self.assertEqual(bot._audit_event.call_args.args[0], "TP1_PRICE_RECONCILIATION_REQUIRED")
        bot._ejecutar_parcial_impl("NVDA", pos, tp1_por_posicion=True)       # already handled
        bot.ejecutor_hibrido.mover_stop_breakeven.assert_called_once()

    def test_without_position_evidence_an_unknown_detail_changes_nothing(self):
        bot, pos = self.make("DESCONOCIDO")
        bot._ejecutar_parcial_impl("NVDA", pos)
        bot.ejecutor_hibrido.mover_stop_breakeven.assert_not_called()
        self.assertFalse(pos["tp1_hit"])


class StopCloseConfirmationTests(unittest.TestCase):
    def close(self, still_working, broker_qty):
        bot = ticker_bot()
        bot.account_number = ACCOUNT
        bot._posiciones["META"].update(entrada=100, orden=SimpleNamespace(cantidad=10),
                                       cantidad_resto=3, tp1_hit=True, sl_order_id="sl")
        bot._position_lifecycle_context = lambda *args: {}
        bot._asegurar_token_trading = lambda: True
        bot._broker_retry_settings = lambda: (1, 0)
        bot.schwab = SimpleNamespace(obtener_ordenes_abiertas=lambda account: [])
        bot._iter_broker_orders = lambda rows: iter(rows)
        bot._symbol_from_broker_order = lambda o: o["symbol"]
        bot._cantidad_real_broker = Mock(return_value=broker_qty)
        bot.ejecutor_hibrido.cancelar_orden_confirmada = Mock(
            return_value={"confirmado": True, "broker_status": "NOT_WORKING"})
        bot.ejecutor_hibrido.detalle_orden = Mock(return_value={"status": "DESCONOCIDO"})
        bot.ejecutor_hibrido.orden_trabajando = Mock(return_value=still_working)
        bot.ejecutor_hibrido.liquidar_posicion = Mock(side_effect=AssertionError("no second close"))
        return bot

    def test_stop_gone_and_broker_flat_confirms_without_order_detail(self):
        bot = self.close(still_working=False, broker_qty=0)
        with self.assertRaises(KeyError):          # 'pnl_realizado': past broker confirmation
            bot._cerrar_posicion_impl("META", 95.0, "SL(real)")
        failed = [c for c in bot._audit_event.call_args_list if c.args[0] == "POSITION_CLOSE_FAILED"]
        self.assertEqual(failed, [])

    def test_stop_still_working_or_unreadable_is_not_a_close(self):
        for still_working in (True, None):
            with self.subTest(still_working=still_working):
                bot = self.close(still_working=still_working, broker_qty=0)
                self.assertFalse(bot._cerrar_posicion_impl("META", 95.0, "SL(real)"))
                self.assertIn("META", bot._posiciones)

    def test_broker_not_flat_is_not_a_close(self):
        bot = self.close(still_working=False, broker_qty=3)
        self.assertFalse(bot._cerrar_posicion_impl("META", 95.0, "SL(real)"))


class SessionScenario(MonitorStatusFixture):
    """The October 8 failure mode for 30 minutes: single-order and full-list
    reads always fail, the WORKING list works. The monitor polls TP1 + STOP
    every 2s; the STOP fills at minute 20; order-action reads (closes, kill
    switch, entry checks) read the WORKING list every 30s."""

    def test_stop_fill_is_detected_and_order_action_reads_never_refused(self):
        Bot = load_methods("engine/bot_engine.py", "ChocoletraBot", {"_gestionar_real"}, {})
        bot = Bot()
        bot._activo, bot._lock, bot.account_number = True, threading.RLock(), ACCOUNT
        pos = {"tipo": "BUY", "salida_parcial": True, "tp1_hit": False, "tp1_order_id": "tp1",
               "sl_order_id": "sl", "sl_reconcile_pending": False, "tp_final": 0,
               "cantidad_resto": 3, "sl_actual": 95.0}
        bot._posiciones = {"NVDA": pos}
        bot._asegurar_token_trading = lambda: True
        self.executor.monitor_status_max_age_s = 5.0
        bot.ejecutor_hibrido = self.executor
        broker = {"qty": 10}
        bot._cantidad_real_broker = lambda ticker, side: broker["qty"]
        bot._ejecutar_parcial = Mock()
        closed_at = []
        bot._cerrar_posicion = Mock(side_effect=lambda *a, **k: closed_at.append(self.clock.mono))
        refused = 0
        start = self.clock.mono
        for second in range(0, 1800, 2):
            if second == 1200:                     # STOP fills at Schwab
                self.working, broker["qty"] = [order("tp1")], 0
            if not closed_at:
                bot._gestionar_real("NVDA", pos, 100.0)
            if second % 30 == 0:
                try:
                    with control_requests():
                        self.gate.request(self.list_get, LIST, params=WORKING)
                except Deferred:
                    refused += 1
            self.clock.advance(2)
        self.assertEqual(refused, 0)
        self.assertEqual(len(closed_at), 1)
        self.assertLessEqual(closed_at[0] - start - 1200, 8)
        bot._cerrar_posicion.assert_called_once_with("NVDA", 95.0, motivo="SL(real)")
        bot._ejecutar_parcial.assert_not_called()
        self.assertLessEqual(self.counts["working"], 1200 // 5 + 2)
        self.assertLessEqual(self.counts["single"] + self.counts["full"], 4)


if __name__ == "__main__":
    unittest.main()
