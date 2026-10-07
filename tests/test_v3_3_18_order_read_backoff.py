"""Offline production-method tests; no credentials, broker HTTP or orders.

Run: python -m unittest discover -s tests -p test_v3_3_18_order_read_backoff.py
Reproduces the October 7 VPS evidence: Schwab's single-order endpoint
returned HTTP 400 (broker status 500) on every call while one REAL position
was monitored, and the shared backoff then refused every other REAL ticker's
order-list read before entry (`order_read_backoff`).
"""
import threading
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

from test_schwab_request_budget_offline import (
    Deferred, GateFixture, Response, control_requests, load_methods,
)

ACCOUNT = "private-account"
LIST = f"https://api.schwabapi.com/trader/v1/accounts/{ACCOUNT}/orders"
ORDER = LIST + "/private-order"
BROKEN = Response(400, {"errors": [{"status": 500, "id": "15561df6-0ab3-dc65-a391-302798a9dde5"}]})


def order(oid, status="WORKING", symbol="META"):
    return {"orderId": oid, "status": status,
            "orderLegCollection": [{"instrument": {"symbol": symbol}}]}


class SeparateBackoffTests(GateFixture):
    def test_failing_single_order_reads_do_not_pause_the_order_list(self):
        self.gate.request(self.send(BROKEN), ORDER)
        with self.assertRaises(Deferred) as raised:
            self.gate.request(self.send(), ORDER.replace("private-order", "another-order"))
        self.assertEqual(raised.exception.reason, "order_read_backoff")
        self.gate.request(self.send(), LIST)
        with control_requests():
            self.gate.request(self.send(), LIST)
        self.assertEqual(len(self.calls), 3)

    def test_refused_list_read_cannot_leave_the_list_paused(self):
        # v3.3.17 sequence on the VPS: the single-order read fails, the one
        # allowed list fallback is refused locally (budget busy), and every
        # later list read stayed in `order_read_backoff` for minutes.
        self.gate.request(self.send(BROKEN), ORDER)
        for _ in range(3):
            self.gate.request(self.send(), "https://api.schwabapi.com/marketdata/v1/pricehistory")
        with self.assertRaises(Deferred) as raised:
            self.gate.request(self.send(), LIST)
        self.assertEqual(raised.exception.reason, "control_burst_reserved")
        self.clock.advance(1.1)
        for minute in range(5):
            with control_requests():
                self.gate.request(self.send(), LIST, params={"status": "WORKING"})
            self.gate.request(self.send(), LIST)
            self.clock.advance(61)
            self.gate.request(self.send(BROKEN), ORDER)    # still broken every minute
        self.assertEqual(sum(url == LIST for url, _ in self.calls), 10)

    def test_a_failing_order_list_still_backs_off_on_its_own(self):
        self.gate.request(self.send(BROKEN), LIST)
        with self.assertRaises(Deferred) as raised:
            self.gate.request(self.send(), LIST)
        self.assertEqual(raised.exception.reason, "order_read_backoff")
        self.gate.request(self.send(), ORDER)
        self.clock.advance(5)
        self.gate.request(self.send(), LIST)
        self.assertEqual(len(self.calls), 3)


class ExecutorFixture(GateFixture):
    def setUp(self):
        super().setUp()
        Executor = load_methods("api/hybrid_order_executor.py", "EjecutorHibridoDeOrdenes",
            {"_buscar_orden_en_listado", "_listado_ordenes", "_orden_en", "_estado_orden"},
            {"schwab_request": self.gate.request, "nullcontext": nullcontext,
             "time": SimpleNamespace(monotonic=lambda: self.clock.mono)})
        self.executor = Executor()
        self.executor.TRADER_URL = "https://api.schwabapi.com/trader/v1"
        self.executor.request_timeout_s = 5
        self.executor._order_list_cache, self.executor._order_list_lock = None, threading.Lock()
        self.orders = [order("tp1"), order("sl")]
        self.single_order_calls = self.list_calls = 0

        def get(url, **kwargs):
            if url.rstrip("/").endswith("/orders"):
                self.list_calls += 1
                return Response(200, list(self.orders))
            self.single_order_calls += 1
            return BROKEN                          # as on the VPS: always 400/500
        get.__name__ = "get"
        self.executor._http_session = lambda: SimpleNamespace(get=get)


class OrderLookupTests(ExecutorFixture):
    def test_lookups_use_the_list_while_single_order_reads_back_off(self):
        self.assertEqual(self.executor._estado_orden(ACCOUNT, "tp1"), "WORKING")
        self.assertEqual((self.single_order_calls, self.list_calls), (1, 1))
        for _ in range(5):
            self.clock.advance(0.5)
            self.assertEqual(self.executor._estado_orden(ACCOUNT, "sl"), "WORKING")
        self.assertEqual((self.single_order_calls, self.list_calls), (1, 6),
                         "no single-order request while it backs off; the list answers")

    def test_monitor_reuses_one_list_for_every_order_within_max_age(self):
        for _ in range(4):                         # 2s monitor ticks at 0, 2, 4, 6s
            for oid in ("tp1", "sl"):
                self.assertEqual(self.executor._estado_orden(ACCOUNT, oid, 5), "WORKING")
            self.clock.advance(2)
        self.assertEqual(self.list_calls, 2, "lists read at 0s and 6s only")
        self.orders[1] = order("sl", "FILLED")
        self.assertEqual(self.executor._estado_orden(ACCOUNT, "sl", 5), "WORKING")  # 2s old
        self.clock.advance(3.1)
        self.assertEqual(self.executor._estado_orden(ACCOUNT, "sl", 5), "FILLED")

    def test_confirmations_and_unknown_orders_always_read_fresh(self):
        self.executor._estado_orden(ACCOUNT, "tp1", 5)
        self.orders[0] = order("tp1", "CANCELED")
        self.assertEqual(self.executor._estado_orden(ACCOUNT, "tp1"), "CANCELED")
        self.orders.append(order("new-entry"))
        self.assertEqual(self.executor._estado_orden(ACCOUNT, "new-entry", 5), "WORKING")
        self.assertEqual(self.list_calls, 3)

    def test_concurrent_monitor_threads_share_one_list_read(self):
        started = threading.Barrier(4)
        def poll(oid):
            started.wait()
            self.executor._estado_orden(ACCOUNT, oid, 5)
        self.executor._buscar_orden_en_listado(ACCOUNT, "tp1")   # per-order now backing off
        self.clock.advance(6)
        before = self.list_calls
        threads = [threading.Thread(target=poll, args=(oid,)) for oid in ("tp1", "sl", "tp1", "sl")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.list_calls - before, 1)

    def test_list_failure_stays_unknown(self):
        self.executor._http_session = lambda: SimpleNamespace(get=self.send(BROKEN))
        self.assertEqual(self.executor._estado_orden(ACCOUNT, "tp1", 5), "DESCONOCIDO")


class SessionScenario(ExecutorFixture):
    """Ten minutes with one open REAL position (TP1 + STOP polled every 2s by
    the monitor) while other REAL tickers read the order list before entry."""

    def test_entry_checks_succeed_while_a_position_is_monitored(self):
        refused, statuses = 0, []
        for second in range(0, 600, 2):
            for oid in ("tp1", "sl"):
                statuses.append(self.executor._estado_orden(ACCOUNT, oid, 5))
            if second % 20 == 0:                   # another REAL ticker's pre-entry read
                try:
                    with control_requests():
                        self.gate.request(self.send(Response(200, [])), LIST, params={"status": "WORKING"})
                except Deferred:
                    refused += 1
            self.clock.advance(2)
        self.assertEqual(refused, 0)
        self.assertNotIn("DESCONOCIDO", statuses)
        self.assertLessEqual(self.list_calls, 600 // 5 + 1)
        self.assertLessEqual(self.single_order_calls, 15)


class RefresherHeadroomTests(unittest.TestCase):
    def setUp(self):
        from test_schwab_request_budget_offline import Clock, TRANSPORT
        from test_v3_3_16_market_data_recovery import make_client
        from test_v3_3_17_warmup_admission import make_bot
        self.context = TRANSPORT["_TRAFFIC_CONTEXT"]
        self.clock = Clock()
        self.client = make_client(self.clock)
        self.bot = make_bot(self.clock, self.client, tickers=["AAPL", "META"], background=True)
        self.bot._is_paper_ticker = lambda ticker, pos=None: (pos or {}).get("execution_mode") == "PAPER"
        self.bot.ejecutor_hibrido = SimpleNamespace(monitor_status_max_age_s=5.0)
        self.seen = []
        original = self.client._get_autorizado.side_effect
        def send(url, params, **kwargs):
            self.seen.append(self.context.warmup[0])
            return original(url, params, **kwargs)
        self.client._get_autorizado.side_effect = send

    def headroom(self, ticker):
        self.seen.clear()
        self.bot._refrescar_mtf_fondo(ticker)
        self.assertTrue(self.seen)
        return set(self.seen)

    def test_cold_download_leaves_room_for_the_order_monitor_with_real_exposure(self):
        self.assertEqual(self.headroom("AAPL"), {8})
        self.bot._posiciones = {"META": {"execution_mode": "PAPER"}}
        self.assertEqual(self.headroom("META"), {8})
        self.bot._mtf_cache.pop("META")
        self.bot._posiciones = {"META": {"execution_mode": "REAL"}}
        self.assertEqual(self.headroom("META"), {8 + 12 + 2})
        self.clock.advance(301)                    # a warm macro refresh is not greedy
        self.assertEqual(self.headroom("AAPL"), {8})


class EngineTests(unittest.TestCase):
    def make_bot(self, read):
        Bot = load_methods("engine/bot_engine.py", "ChocoletraBot", {
            "_asegurar_ticker_limpio", "_iter_broker_orders", "_symbol_from_broker_order"}, {})
        Bot._symbol_from_broker_order.__globals__["ChocoletraBot"] = Bot
        bot = Bot()
        bot.account_number = ACCOUNT
        bot._is_paper_ticker = lambda ticker: False
        bot._broker_read_with_retry = Mock(side_effect=read)
        bot.schwab = SimpleNamespace(obtener_ordenes_abiertas=Mock())
        bot.ejecutor_hibrido = SimpleNamespace(cancelar_orden_confirmada=Mock())
        bot._emit_log = Mock()
        return bot

    def test_unreadable_orders_are_reported_as_such(self):
        bot = self.make_bot(lambda *a, **k: (False, None, "SchwabRequestDeferred: order_read_backoff; espere 31s"))
        detalle = {}
        self.assertFalse(bot._asegurar_ticker_limpio("NVDA", detalle))
        self.assertEqual(detalle["estado"], "ORDERS_UNREADABLE")
        self.assertIn("No se pudieron leer las órdenes de Schwab", detalle["motivo"])
        self.assertIn("order_read_backoff", detalle["motivo"])
        bot.ejecutor_hibrido.cancelar_orden_confirmada.assert_not_called()

    def test_real_leftover_orders_keep_their_reason(self):
        bot = self.make_bot(lambda *a, **k: (True, [order("old-stop", symbol="NVDA"),
                                                   order("other", symbol="META")], ""))
        detalle = {}
        self.assertFalse(bot._asegurar_ticker_limpio("NVDA", detalle))
        self.assertEqual(detalle["estado"], "SYMBOL_NOT_CLEAN")
        self.assertIn("Persisten 1 orden", detalle["motivo"])
        self.assertEqual(bot.ejecutor_hibrido.cancelar_orden_confirmada.call_count, 4)

    def test_clean_symbol_and_legacy_callers(self):
        bot = self.make_bot(lambda *a, **k: (True, [order("other", symbol="META")], ""))
        self.assertTrue(bot._asegurar_ticker_limpio("NVDA"))

    def test_position_monitor_polls_with_the_shared_list_age(self):
        Bot = load_methods("engine/bot_engine.py", "ChocoletraBot", {"_gestionar_real"}, {})
        bot = Bot()
        bot._activo, bot._lock, bot.account_number = True, threading.RLock(), ACCOUNT
        pos = {"tipo": "BUY", "salida_parcial": True, "tp1_hit": False, "tp1_order_id": "tp1",
               "sl_order_id": "sl", "sl_reconcile_pending": False, "tp_final": 0}
        bot._posiciones = {"META": pos}
        bot._asegurar_token_trading = lambda: True
        bot.ejecutor_hibrido = SimpleNamespace(monitor_status_max_age_s=5.0,
                                               _estado_orden=Mock(return_value="WORKING"))
        bot._gestionar_real("META", pos, 100.0)
        self.assertEqual([c.args for c in bot.ejecutor_hibrido._estado_orden.call_args_list],
                         [(ACCOUNT, "tp1", 5.0), (ACCOUNT, "sl", 5.0)])


if __name__ == "__main__":
    unittest.main()
