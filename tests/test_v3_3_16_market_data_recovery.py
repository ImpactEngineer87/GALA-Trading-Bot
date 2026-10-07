"""Offline production-method tests; no credentials, broker HTTP or orders.

Run: python -m unittest discover -s tests -p test_v3_3_16_market_data_recovery.py
Real pandas and the production transport/provider/engine method bodies are used.
Unavailable application dependencies are excluded by the existing AST harness.
"""
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
from agents.contracts import DatosMultiTemporal
from test_schwab_request_budget_offline import (
    Clock, Deferred, GateFixture, Paused, Response, load_methods,
)
from test_v3_3_15_exit_control import ticker_bot

HISTORY = "https://api.schwabapi.com/marketdata/v1/pricehistory"
DAY = 86400000


def candles(end, frequency=1):
    return {"candles": [dict(datetime=end - (99 - i) * frequency * 60000,
                             open=100, high=101, low=99, close=100 + i / 1000,
                             volume=1000) for i in range(100)]}


def make_client(clock):
    time = SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.mono)
    cls = load_methods("api/schwab_client.py", "SchwabClient", {
        "_pedir_pricehistory", "_fetch_minutos_chunked", "_resample",
        "obtener_historico", "obtener_1m_reciente", "obtener_multitemporal",
    }, {"pd": pd, "json": json, "time": time,
        "BASE_URL": "https://api.schwabapi.com/marketdata/v1", "_log": lambda *a: None})
    client = cls()
    client._asegurar_token = Mock(return_value=True)
    client.include_extended_hours_data = True
    client._market_cache_lock = threading.RLock()
    client._history_cache, client._mtf_frame_cache = {}, {}
    client._get_autorizado = Mock(side_effect=lambda url, params, **kw:
        Response(payload=candles(params.get("endDate", int(clock.wall * 1000)), params.get("frequency", 1))))
    return client


def real_frame(clock):
    frame = pd.DataFrame(candles(int(clock.wall * 1000))["candles"])
    frame.index = pd.to_datetime(frame.pop("datetime"), unit="ms")
    return frame.astype(float)


class HistoryAdmissionTests(GateFixture):
    def setUp(self):
        super().setUp()
        self.gate = self.make_gate(wait=self.clock.advance)

    def test_seventeen_history_reads_drain_burst_without_extra_requests(self):
        for _ in range(17):
            self.gate.request(self.send(), HISTORY, admission_wait_seconds=5)
        self.assertEqual(len(self.calls), 17)
        self.assertEqual(self.clock.mono, 104)
        self.assertEqual(self.gate.snapshot()["request_metrics"]["sent_total"], 17)

    def test_minute_exhaustion_waits_at_most_five_seconds(self):
        self.gate = self.make_gate(wait=self.clock.advance, requests_per_minute=5,
                                   control_reserve=2, burst_per_second=100)
        for _ in range(3):
            self.gate.request(self.send(), HISTORY)
        with self.assertRaises(Deferred):
            self.gate.request(self.send(), HISTORY, admission_wait_seconds=500)
        self.assertEqual(self.clock.mono, 105)
        self.assertEqual(len(self.calls), 3)

    def test_http_pause_never_waited_out(self):
        for status in (403, 429):
            with self.subTest(status=status):
                gate = self.make_gate(wait=self.clock.advance)
                gate.request(self.send(Response(status)), HISTORY)
                started = self.clock.mono
                with self.assertRaises(Paused):
                    gate.request(self.send(), HISTORY, admission_wait_seconds=5)
                self.assertEqual(self.clock.mono, started)
                self.clock.advance(3601)

    def test_order_write_is_not_queued_or_replayed(self):
        for _ in range(6):
            self.gate.request(self.send(method="post"), HISTORY, recovery_safe=False)
        with self.assertRaises(Deferred):
            self.gate.request(self.send(method="post"), HISTORY,
                              recovery_safe=False, admission_wait_seconds=5)
        self.assertEqual(self.clock.mono, 100)
        self.assertEqual(len(self.calls), 6)

    def test_history_wait_releases_gate_lock_and_preserves_control_capacity(self):
        for _ in range(4):
            self.gate.request(self.send(), HISTORY)
        def wait(seconds):
            self.assertFalse(self.gate._lock._is_owned())
            self.gate.request(self.send(method="delete"),
                              "https://api.schwabapi.com/trader/v1/accounts/test/orders/test",
                              recovery_safe=False)
            self.clock.advance(seconds)
        self.gate._wait = wait
        self.gate.request(self.send(), HISTORY, admission_wait_seconds=5)
        self.assertEqual(len(self.calls), 6)


class ProviderRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.client = make_client(self.clock)

    def test_auth_failure_returns_empty_waiting_without_demo_or_http(self):
        self.client._asegurar_token.return_value = False
        result = self.client.obtener_multitemporal("AAPL")
        self.assertEqual(result.estado_datos, "WAITING")
        self.assertFalse(result.sintetico)
        self.assertEqual(result.velas, {})
        self.client._get_autorizado.assert_not_called()

    def test_deferred_warmup_keeps_successful_sources_and_fetches_micro_last(self):
        original = self.client._get_autorizado.side_effect
        failed = False
        def send(url, params, **kwargs):
            nonlocal failed
            if params.get("frequencyType") == "minute" and not failed:
                failed = True
                raise Deferred(7, "control_capacity_reserved")
            return original(url, params, **kwargs)
        self.client._get_autorizado.side_effect = send
        result = self.client.obtener_multitemporal("AAPL")
        self.assertEqual((result.estado_datos, result.reintento_seg), ("WAITING", 7))
        self.assertEqual(result.velas, {})
        self.clock.advance(60)
        result = self.client.obtener_multitemporal("AAPL")
        self.assertEqual(result.estado_datos, "READY")
        self.assertEqual(set(result.velas), {"1W", "1D", "4H", "1H", "15m", "5m", "1m"})
        calls = self.client._get_autorizado.call_args_list
        self.assertEqual(sum(c.kwargs["params"].get("frequencyType") == "daily" for c in calls), 1)
        self.assertEqual(sum(c.kwargs["params"].get("frequencyType") == "weekly" for c in calls), 1)
        self.assertEqual(calls[-1].kwargs["params"]["frequency"], 1)

    def test_old_successful_pages_reused_after_interrupted_download(self):
        original = self.client._get_autorizado.side_effect
        count = 0
        def send(url, params, **kwargs):
            nonlocal count
            count += 1
            if count == 4:
                raise Deferred(1, "request_burst")
            return original(url, params, **kwargs)
        self.client._get_autorizado.side_effect = send
        with self.assertRaises(Deferred):
            self.client._fetch_minutos_chunked("AAPL", 30, 60)
        cached = set(self.client._history_cache)
        self.assertEqual(len(cached), 2)
        self.clock.advance(60)
        result = self.client._fetch_minutos_chunked("AAPL", 30, 60)
        self.assertIsNotNone(result)
        requested = [json.dumps(c.kwargs["params"], sort_keys=True)
                     for c in self.client._get_autorizado.call_args_list[4:]]
        self.assertTrue(cached.isdisjoint(requested))

    def test_history_cache_copies_and_expires_but_never_caches_empty(self):
        today = int(self.clock.wall * 1000) // DAY * DAY
        params = dict(symbol="AAPL", startDate=today - 2 * DAY, endDate=today - 1,
                      frequencyType="minute", frequency=5)
        frame = self.client._pedir_pricehistory(params)
        frame.iloc[0, 0] = -999
        self.assertEqual(self.client._pedir_pricehistory(params).iloc[0, 0], 100)
        self.assertEqual(self.client._get_autorizado.call_count, 1)
        self.clock.advance(86400)
        self.client._pedir_pricehistory(params)
        self.assertEqual(self.client._get_autorizado.call_count, 2)
        params["symbol"] = "AMD"
        self.client._get_autorizado.side_effect = None
        self.client._get_autorizado.return_value = Response(payload={"candles": []})
        self.assertIsNone(self.client._pedir_pricehistory(params))
        self.assertIsNone(self.client._pedir_pricehistory(params))
        self.assertEqual(self.client._get_autorizado.call_count, 4)

    def test_failed_http_not_cached_or_turned_into_synthetic(self):
        self.client._get_autorizado.side_effect = None
        self.client._get_autorizado.return_value = Response(400)
        for _ in range(2):
            result = self.client.obtener_multitemporal("AAPL")
            self.assertEqual(result.estado_datos, "WAITING")
            self.assertFalse(result.sintetico)
            self.assertEqual(result.velas, {})
        self.assertEqual(self.client._get_autorizado.call_count, 2)

    def test_chunk_bounds_cover_depth_and_remain_stable_during_utc_day(self):
        self.client._fetch_minutos_chunked("AAPL", 30, 60)
        bounds = [c.kwargs["params"] for c in self.client._get_autorizado.call_args_list]
        today = int(self.clock.wall * 1000) // DAY * DAY
        self.assertEqual(bounds[-1]["startDate"], today - 60 * DAY)
        for i, params in enumerate(bounds):
            self.assertLessEqual(params["endDate"] - params["startDate"], 10 * DAY)
            if i:
                self.assertEqual(params["endDate"], bounds[i - 1]["startDate"] - 1)
        count = len(bounds)
        self.clock.advance(120)
        self.client._fetch_minutos_chunked("AAPL", 30, 60)
        self.assertEqual(self.client._get_autorizado.call_count, count + 1)
        self.clock.advance(86400)
        self.client._fetch_minutos_chunked("AAPL", 30, 60)
        self.assertGreater(self.client._get_autorizado.call_count, count + 2)

    def test_completed_sources_expire_without_extending_ttl_on_read(self):
        self.client.obtener_multitemporal("AAPL")
        initial = self.client._get_autorizado.call_count
        self.clock.advance(299)
        self.client.obtener_multitemporal("AAPL")
        self.assertEqual(self.client._get_autorizado.call_count, initial + 1)
        self.clock.advance(2)
        self.client.obtener_multitemporal("AAPL")
        self.assertGreater(self.client._get_autorizado.call_count, initial + 3)

    def test_authorized_get_applies_wait_only_to_history_including_401_retry(self):
        request = Mock(side_effect=[Response(401), Response(), Response()])
        cls = load_methods("api/schwab_client.py", "SchwabClient", {"_get_autorizado"},
                           {"schwab_request": request})
        client = cls()
        client._asegurar_token = Mock(return_value=True)
        client._headers = Mock(return_value={"Authorization": "Bearer offline-test"})
        client._recuperar_401 = Mock(return_value=True)
        client._session = SimpleNamespace(get=Mock())
        client._get_autorizado(HISTORY)
        client._get_autorizado("https://api.schwabapi.com/marketdata/v1/quotes")
        self.assertEqual([c.kwargs["admission_wait_seconds"] for c in request.call_args_list], [5, 5, 0])


class EngineRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        cls = load_methods("engine/bot_engine.py", "ChocoletraBot",
                           {"_obtener_mtf", "_mtf_listo", "_mtf_refresco_activo"}, {
            "DatosMultiTemporal": DatosMultiTemporal,
            "time": SimpleNamespace(time=lambda: self.clock.wall)})
        self.bot = cls()
        self.bot.config = {}
        self.bot._mtf_cache = {}
        self.bot._mtf_ttl, self.bot._mtf_ttl_macro = 30, 300
        # v3.3.17: no background refresher here, so the cycle path downloads.
        self.bot._mtf_macro_max_stale = 600
        self.bot._mtf_lock, self.bot._mtf_refresco_hilo = threading.Lock(), None
        self.bot._emit_log = Mock()
        self.client = make_client(self.clock)
        self.bot.schwab = self.client

    def test_pending_full_download_not_cached_then_recovers(self):
        self.client._get_autorizado.side_effect = Deferred(2, "request_burst")
        self.assertEqual(self.bot._obtener_mtf("AAPL").estado_datos, "WAITING")
        self.assertEqual(self.bot._mtf_cache, {})
        self.client._get_autorizado.side_effect = lambda url, params, **kw: Response(
            payload=candles(params.get("endDate", int(self.clock.wall * 1000))))
        self.assertEqual(self.bot._obtener_mtf("AAPL").estado_datos, "READY")
        self.assertIn("AAPL", self.bot._mtf_cache)

    def test_full_cache_freshness_uses_completion_time(self):
        def fetch(*args):
            self.clock.advance(120)
            return DatosMultiTemporal("AAPL", {"1m": real_frame(self.clock)})
        self.client.obtener_multitemporal = fetch
        self.bot._obtener_mtf("AAPL")
        self.assertEqual(self.bot._mtf_cache["AAPL"][0], self.clock.wall)
        self.assertEqual(self.bot._mtf_cache["AAPL"][2], self.clock.wall)

    def test_legacy_synthetic_cache_discarded(self):
        self.bot._mtf_cache["AAPL"] = (self.clock.wall, DatosMultiTemporal(
            "AAPL", {"1m": real_frame(self.clock)}, sintetico=True), self.clock.wall)
        self.client.obtener_multitemporal = Mock(return_value=DatosMultiTemporal("AAPL"))
        self.bot._obtener_mtf("AAPL")
        self.client.obtener_multitemporal.assert_called_once()
        self.assertFalse(self.bot._mtf_cache["AAPL"][1].sintetico)

    def test_micro_failure_preserves_macro_but_does_not_publish_stale_price(self):
        previous = DatosMultiTemporal("AAPL", {"1m": real_frame(self.clock)})
        self.bot._mtf_cache["AAPL"] = (self.clock.wall, previous, self.clock.wall - 60)
        self.client.obtener_1m_reciente = Mock(return_value=None)
        result = self.bot._obtener_mtf("AAPL")
        self.assertEqual(result.estado_datos, "WAITING")
        self.assertEqual(result.velas, {})
        self.assertIs(self.bot._mtf_cache["AAPL"][1], previous)
        self.client.obtener_1m_reciente.return_value = real_frame(self.clock)
        self.assertEqual(self.bot._obtener_mtf("AAPL").estado_datos, "READY")
        self.assertEqual(self.bot._mtf_cache["AAPL"][2], self.clock.wall)

    def test_micro_local_pause_retains_good_cache(self):
        self.bot._mtf_cache["AAPL"] = (self.clock.wall, DatosMultiTemporal("AAPL"), self.clock.wall - 60)
        self.client.obtener_1m_reciente = Mock(side_effect=Paused(600, reason="http_403"))
        result = self.bot._obtener_mtf("AAPL")
        self.assertEqual((result.estado_datos, result.reintento_seg), ("WAITING", 600))
        self.assertIn("AAPL", self.bot._mtf_cache)

    def test_pending_data_never_valued_or_used_to_manage_or_enter_positions(self):
        bot = ticker_bot()
        bot._obtener_mtf = Mock(return_value=DatosMultiTemporal("META", estado_datos="WAITING",
                                                             motivo_datos="request_burst"))
        bot._datos_micro_confirmados = Mock(side_effect=AssertionError("pending data must stop pipeline"))
        bot._procesar_ticker("META")
        self.assertEqual(bot._last_prices, {})
        bot._gestionar_posiciones.assert_not_called()
        bot._cerrar_posicion.assert_not_called()
        bot._abrir_o_revertir.assert_not_called()
        self.assertEqual(bot._set_ticker_processing.call_args.args[1], "WAITING")


class MultiTickerRecoverySimulation(GateFixture):
    def test_seventeen_tickers_recover_under_real_shared_gate(self):
        self.gate = self.make_gate(wait=self.clock.advance)
        client = make_client(self.clock)
        sent = []
        def get(url, params, **kwargs):
            sent.append(self.clock.mono)
            return Response(payload=candles(params.get("endDate", int(self.clock.wall * 1000)),
                                           params.get("frequency", 1)))
        client._get_autorizado = lambda url, **kw: self.gate.request(
            get, url, admission_wait_seconds=5, **kw)
        tickers = [f"TEST{i}" for i in range(17)]
        ready = set()
        for cycle in range(30):
            for ticker in tickers:
                result = client.obtener_multitemporal(ticker)
                self.assertFalse(result.sintetico)
                if result.estado_datos == "READY":
                    ready.add(ticker)
                else:
                    self.assertEqual(result.velas, {})
            if len(ready) == 17:
                break
            self.clock.advance(60)
        self.assertEqual(ready, set(tickers), "warmup must make progress after local deferrals")
        for timestamp in sent:
            self.assertLessEqual(sum(timestamp - 60 < t <= timestamp for t in sent), 60)
            self.assertLessEqual(sum(timestamp - 1 < t <= timestamp for t in sent), 4)
        self.assertEqual(self.gate.snapshot()["request_metrics"]["sent_total"], len(sent))


if __name__ == "__main__":
    unittest.main()
