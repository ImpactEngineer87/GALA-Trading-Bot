"""Offline production-method tests; no credentials, broker HTTP or orders.

Run: python -m unittest discover -s tests -p test_v3_3_17_warmup_admission.py
The concurrent simulation runs every ticker worker, the background history
refresher and a status poll in real threads under a cooperative scheduler with
a virtual clock, so the shared gate sees the engine's interleaving (the v3.3.16
simulation was sequential and could not show cross-ticker starvation).
"""
import ast
import heapq
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from agents.contracts import DatosMultiTemporal
from test_schwab_request_budget_offline import (
    MARKET, REQUESTS, ROOT, TRANSPORT, Clock, Deferred, GateFixture, Paused, Response, load_methods,
)
from test_v3_3_16_market_data_recovery import candles, make_client, real_frame

HISTORY = "https://api.schwabapi.com/marketdata/v1/pricehistory"
warmup_requests = TRANSPORT["schwab_warmup_requests"]
ENGINE_METHODS = {"_obtener_mtf", "_mtf_listo", "_mtf_refresco_activo", "_resumen_historicos_mtf",
                  "_siguiente_refresco_mtf", "_refrescar_mtf_fondo", "_refresco_mtf_loop"}


def make_bot(clock, client, *, tickers=("AAPL",), real=(), background=False, sleep=None):
    Bot = load_methods("engine/bot_engine.py", "ChocoletraBot", ENGINE_METHODS, {
        "DatosMultiTemporal": DatosMultiTemporal, "schwab_warmup_requests": warmup_requests,
        "time": SimpleNamespace(time=lambda: clock.wall, sleep=sleep or clock.advance)})
    bot = Bot()
    bot.config, bot.tickers, bot._activo = {}, list(tickers), True
    bot._mtf_cache, bot._mtf_ttl, bot._mtf_ttl_macro, bot._mtf_macro_max_stale = {}, 0, 300, 600
    bot._mtf_lock, bot._mtf_ciclo_libre = threading.Lock(), threading.Event()
    bot._mtf_ciclo_libre.set()
    bot._mtf_fallos, bot._mtf_reintento = {}, {}
    bot._mtf_refresco_hilo = SimpleNamespace(is_alive=lambda: bot._activo) if background else None
    bot._real_tickers = lambda names: [t for t in names if t in set(real)]
    bot._emit_log = Mock()
    bot.schwab = client
    return bot


def bundle(clock, ticker="AAPL"):
    return DatosMultiTemporal(ticker, {"1m": real_frame(clock), "1D": real_frame(clock)})


class WarmupGateTests(GateFixture):
    def test_warmup_leaves_headroom_for_cycle_reads(self):
        with warmup_requests(10):
            for _ in range(50):
                self.gate.request(self.send(), HISTORY)
                self.clock.advance(0.3)
            with self.assertRaises(Deferred) as raised:
                self.gate.request(self.send(), HISTORY)
        self.assertEqual(raised.exception.reason, "warmup_headroom_reserved")
        for _ in range(10):                         # ordinary background reads still fit
            self.gate.request(self.send(), HISTORY)
            self.clock.advance(0.3)
        with self.assertRaises(Deferred) as raised:
            self.gate.request(self.send(), HISTORY)
        self.assertEqual(raised.exception.reason, "control_capacity_reserved")

    def test_warmup_pauses_while_cycle_runs_but_oauth_does_not(self):
        allowed = threading.Event()
        with warmup_requests(0, allowed):
            with self.assertRaises(Deferred) as raised:
                self.gate.request(self.send(), HISTORY, admission_wait_seconds=5)
            self.assertEqual((raised.exception.reason, self.clock.mono), ("warmup_paused", 100))
            self.gate.request(self.send(method="post"), "https://api.schwabapi.com/v1/oauth/token")
            allowed.set()
            self.gate.request(self.send(), HISTORY)
        self.assertEqual(len(self.calls), 2)

    def test_headroom_wait_is_bounded_and_context_is_thread_local(self):
        self.gate = self.make_gate(wait=self.clock.advance)
        with warmup_requests(59):
            self.gate.request(self.send(), HISTORY)
            with self.assertRaises(Deferred):
                self.gate.request(self.send(), HISTORY, admission_wait_seconds=500)
            self.assertEqual(self.clock.mono, 105)
            other = []
            thread = threading.Thread(target=lambda: other.append(
                self.gate.request(self.send(), HISTORY)))
            thread.start()
            thread.join()
        self.assertEqual(len(other), 1)


class RefresherScheduleTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.bot = make_bot(self.clock, Mock(), tickers=["AAPL", "AMD", "BA", "META", "TSLA"],
                            real=["BA", "TSLA"], background=True)

    def test_cold_real_tickers_first_then_configured_order(self):
        order = []
        while (ticker := self.bot._siguiente_refresco_mtf()) is not None:
            order.append(ticker)
            self.bot._mtf_cache[ticker] = (self.clock.wall, bundle(self.clock, ticker), self.clock.wall)
        self.assertEqual(order, ["BA", "TSLA", "AAPL", "AMD", "META"])

    def test_aging_macro_preempts_cold_and_due_macro_follows_cold(self):
        for ticker, age in (("AAPL", 100), ("AMD", 400), ("META", 460), ("TSLA", 500)):
            self.bot._mtf_cache[ticker] = (self.clock.wall - age, bundle(self.clock, ticker), self.clock.wall)
        order = []
        while (ticker := self.bot._siguiente_refresco_mtf()) is not None:
            order.append(ticker)
            self.bot._mtf_cache[ticker] = (self.clock.wall, bundle(self.clock, ticker), self.clock.wall)
        # Past halfway to the 600s limit (oldest first), cold, then merely due.
        self.assertEqual(order, ["TSLA", "META", "BA", "AMD"])

    def test_backed_off_ticker_is_skipped(self):
        self.bot._mtf_reintento["BA"] = self.clock.wall + 30
        self.assertEqual(self.bot._siguiente_refresco_mtf(), "TSLA")
        self.clock.advance(31)
        self.assertEqual(self.bot._siguiente_refresco_mtf(), "BA")

    def test_summary_lists_pending_tickers_once_per_cycle(self):
        self.bot._mtf_cache["BA"] = (self.clock.wall, bundle(self.clock, "BA"), self.clock.wall)
        self.bot._resumen_historicos_mtf()
        self.assertIn("1/5 tickers listos | pendientes: AAPL, AMD, META, TSLA",
                      self.bot._emit_log.call_args.args[1])


class RefresherStepTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.client = make_client(self.clock)
        self.bot = make_bot(self.clock, self.client, tickers=["AAPL", "BA"], background=True)

    def test_success_stores_bundle_and_reports_progress(self):
        self.bot._mtf_fallos["AAPL"], self.bot._mtf_reintento["AAPL"] = 2, 0
        self.assertEqual(self.bot._refrescar_mtf_fondo("AAPL"), 0)
        self.assertTrue(self.bot._mtf_listo("AAPL"))
        self.assertEqual(self.bot._mtf_cache["AAPL"][0], self.clock.wall)
        self.assertEqual((self.bot._mtf_fallos, self.bot._mtf_reintento), ({}, {}))
        self.assertIn("Históricos completos (1/2", self.bot._emit_log.call_args.args[1])

    def test_downloads_run_inside_warmup_context(self):
        seen = []
        original = self.client._get_autorizado.side_effect
        def send(url, params, **kwargs):
            seen.append(TRANSPORT["_TRAFFIC_CONTEXT"].warmup)
            return original(url, params, **kwargs)
        self.client._get_autorizado.side_effect = send
        self.bot._mtf_cache["BA"] = (self.clock.wall, bundle(self.clock, "BA"), self.clock.wall)
        self.bot._refrescar_mtf_fondo("AAPL")
        self.assertTrue(seen)
        self.assertTrue(all(item == (8, self.bot._mtf_ciclo_libre) for item in seen))
        self.assertIsNone(getattr(TRANSPORT["_TRAFFIC_CONTEXT"], "warmup", None))

    def test_local_wait_retries_soon_without_counting_a_failure(self):
        for exc, expected in ((Deferred(9, "warmup_headroom_reserved"), 9),
                              (Deferred(1, "warmup_paused"), 1), (Paused(600, reason="http_403"), 15)):
            with self.subTest(exc=type(exc).__name__):
                self.client._get_autorizado.side_effect = exc
                self.assertEqual(self.bot._refrescar_mtf_fondo("AAPL"), expected)
                self.assertEqual((self.bot._mtf_fallos, self.bot._mtf_reintento), ({}, {}))
                self.assertNotIn("AAPL", self.bot._mtf_cache)

    def test_ticker_failures_back_off_exponentially(self):
        self.client._get_autorizado.side_effect = None
        self.client._get_autorizado.return_value = Response(400)
        for delay in (30, 60, 120, 240, 300, 300):
            self.assertEqual(self.bot._refrescar_mtf_fondo("AAPL"), 0)
            self.assertEqual(self.bot._mtf_reintento["AAPL"], self.clock.wall + delay)
        self.assertNotIn("AAPL", self.bot._mtf_cache)

    def test_unexpected_error_or_synthetic_never_kills_loop_or_caches(self):
        self.client.obtener_multitemporal = Mock(side_effect=RuntimeError("offline"))
        self.assertEqual(self.bot._refrescar_mtf_fondo("AAPL"), 0)
        self.client.obtener_multitemporal = Mock(return_value=DatosMultiTemporal(
            "AAPL", {"1m": real_frame(self.clock)}, sintetico=True))
        self.assertEqual(self.bot._refrescar_mtf_fondo("AAPL"), 0)
        self.assertEqual(self.bot._mtf_fallos["AAPL"], 2)
        self.assertNotIn("AAPL", self.bot._mtf_cache)

    def test_auth_unavailable_waits_without_backoff(self):
        self.client._asegurar_token.return_value = False
        self.assertEqual(self.bot._refrescar_mtf_fondo("AAPL"), 15)
        self.assertEqual(self.bot._mtf_fallos, {})

    def test_loop_pauses_during_cycle_and_stops_with_engine(self):
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                self.bot._activo = False
        bot = make_bot(self.clock, self.client, background=True, sleep=sleep)
        self.bot = bot
        bot._mtf_ciclo_libre.clear()
        bot._siguiente_refresco_mtf = Mock(side_effect=AssertionError("must stay paused"))
        bot._refresco_mtf_loop()
        self.assertEqual(sleeps, [0.25, 0.25, 0.25])


class CycleReadTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.client = make_client(self.clock)
        self.bot = make_bot(self.clock, self.client, background=True)

    def warm(self, age):
        self.bot._mtf_cache["AAPL"] = (self.clock.wall - age, bundle(self.clock), self.clock.wall - 60)
        return self.bot._mtf_cache["AAPL"]

    def test_cold_ticker_waits_for_refresher_without_http(self):
        result = self.bot._obtener_mtf("AAPL")
        self.assertEqual((result.estado_datos, result.motivo_datos), ("WAITING", "WARMUP_PENDING"))
        self.assertEqual(result.velas, {})
        self.client._get_autorizado.assert_not_called()

    def test_due_macro_is_left_to_refresher_and_cycle_reads_only_1m(self):
        entry = self.warm(age=450)
        result = self.bot._obtener_mtf("AAPL")
        self.assertEqual(result.estado_datos, "READY")
        params = [c.kwargs["params"] for c in self.client._get_autorizado.call_args_list]
        self.assertEqual([(p["frequency"], p["frequencyType"]) for p in params], [(1, "minute")])
        self.assertEqual(self.bot._mtf_cache["AAPL"][0], entry[0])
        self.assertEqual(self.bot._mtf_cache["AAPL"][2], self.clock.wall)

    def test_macro_beyond_max_age_blocks_ticker_without_http(self):
        self.warm(age=601)
        result = self.bot._obtener_mtf("AAPL")
        self.assertEqual((result.estado_datos, result.motivo_datos, result.velas),
                         ("WAITING", "MACRO_STALE", {}))
        self.client._get_autorizado.assert_not_called()

    def test_micro_update_never_overwrites_a_newer_refresher_bundle(self):
        self.warm(age=30)
        newer = (self.clock.wall, bundle(self.clock), self.clock.wall)
        original = self.client.obtener_1m_reciente
        def refresh_during_read(ticker):
            self.bot._mtf_cache[ticker] = newer
            return original(ticker)
        self.client.obtener_1m_reciente = refresh_during_read
        self.assertEqual(self.bot._obtener_mtf("AAPL").estado_datos, "READY")
        self.assertIs(self.bot._mtf_cache["AAPL"], newer)


class SynchronousFallbackTests(unittest.TestCase):
    """Without a live refresher thread the cycle still downloads, as in 3.3.16."""

    def setUp(self):
        self.clock = Clock()
        self.client = make_client(self.clock)
        self.bot = make_bot(self.clock, self.client)
        self.assertEqual(self.bot._obtener_mtf("AAPL").estado_datos, "READY")
        self.started, self.previous = self.bot._mtf_cache["AAPL"][0], self.bot._mtf_cache["AAPL"][1]
        self.client._get_autorizado.reset_mock()

    def defer_macro_only(self):
        original = self.client._get_autorizado.side_effect
        def send(url, params, **kwargs):
            if params.get("frequencyType") == "weekly":
                raise Deferred(14, "control_capacity_reserved")
            return original(url, params, **kwargs)
        self.client._get_autorizado.side_effect = send

    def test_dead_refresher_thread_falls_back_to_cycle_download(self):
        self.bot._mtf_refresco_hilo = SimpleNamespace(is_alive=lambda: False)
        for cache in (self.bot._mtf_cache, self.client._mtf_frame_cache, self.client._history_cache):
            cache.clear()
        self.assertEqual(self.bot._obtener_mtf("AAPL").estado_datos, "READY")
        self.assertGreater(self.client._get_autorizado.call_count, 10)

    def test_deferred_macro_refresh_keeps_macro_and_refreshes_1m(self):
        self.clock.advance(301)
        self.defer_macro_only()
        result = self.bot._obtener_mtf("AAPL")
        self.assertEqual(result.estado_datos, "READY")
        self.assertIs(result, self.previous)
        entry = self.bot._mtf_cache["AAPL"]
        self.assertEqual((entry[0], entry[2]), (self.started, self.clock.wall))
        self.assertIn("Refresco macro pospuesto", self.bot._emit_log.call_args.args[1])

    def test_fallback_still_requires_a_fresh_1m(self):
        self.clock.advance(301)
        self.client._get_autorizado.side_effect = Deferred(14, "control_capacity_reserved")
        result = self.bot._obtener_mtf("AAPL")
        self.assertEqual((result.estado_datos, result.velas), ("WAITING", {}))
        self.assertIs(self.bot._mtf_cache["AAPL"][1], self.previous)

    def test_no_fallback_beyond_max_stale_age(self):
        self.clock.advance(601)
        self.defer_macro_only()
        result = self.bot._obtener_mtf("AAPL")
        self.assertEqual((result.estado_datos, result.motivo_datos), ("WAITING", "control_capacity_reserved"))
        self.assertEqual(self.bot._mtf_cache["AAPL"][0], self.started)


class Scheduler:
    """One thread runs at a time; virtual time advances when all are asleep."""

    def __init__(self, clock):
        self.clock, self.lock, self.heap, self.seq, self.active = clock, threading.Lock(), [], 0, 0
        self.yielded = threading.Event()

    def _push(self, wake, event):
        heapq.heappush(self.heap, (wake, self.seq, event))
        self.seq += 1

    def spawn(self, job):
        start, done = threading.Event(), threading.Event()
        with self.lock:
            self.active += 1
            self._push(self.clock.mono, start)
        def body():
            start.wait()
            try:
                job()
            finally:
                done.set()
                with self.lock:
                    self.active -= 1
                self.yielded.set()
        threading.Thread(target=body, daemon=True).start()
        return done

    def sleep(self, seconds):
        event = threading.Event()
        with self.lock:
            self._push(self.clock.mono + max(0.0, seconds), event)
        self.yielded.set()
        event.wait()

    def run(self):
        while True:
            with self.lock:
                if not self.active:
                    return
                wake, _, event = heapq.heappop(self.heap)
            self.clock.advance(max(0.0, wake - self.clock.mono))
            self.yielded.clear()
            event.set()
            self.yielded.wait()


def quote(url, **kwargs):
    return Response()


quote.__name__ = "get"


class ConcurrentSessionSimulation(GateFixture):
    """17 tickers (5 REAL), LiveGuard preflight, engine start 17s before the
    first bar, cycle workers in parallel, background refresher, status poll."""

    def run_session(self, cycles=12):
        sched = Scheduler(self.clock)
        gate = self.make_gate(wait=sched.sleep)
        client = make_client(self.clock)
        history = []

        def get(url, params=None, **kwargs):
            def send(target, **kw):
                history.append(self.clock.mono)
                sched.sleep(0.3)
                end = params.get("endDate", int(self.clock.wall * 1000))
                return Response(payload=candles(end, params.get("frequency", 1)))
            send.__name__ = "get"
            return gate.request(send, url, params=params, admission_wait_seconds=5)
        client._get_autorizado = get
        tickers = [f"T{i:02d}" for i in range(17)]
        real = tickers[::3][:5]
        bot = make_bot(self.clock, client, tickers=tickers, real=real, background=True,
                       sleep=sched.sleep)
        self.clock.advance(60 - self.clock.wall % 60 + 20)
        for _ in range(20):                          # read-only LiveGuard preflight
            gate.request(quote, MARKET)
            self.clock.advance(0.35)                 # engine starts ~17s before a bar
        first_ready, misses, status = {}, [], {"sent": 0, "deferred": 0}

        def driver():
            for cycle in range(1, cycles + 1):
                sched.sleep((self.clock.wall // 60 + 1) * 60 + 0.1 - self.clock.wall)
                bot._mtf_ciclo_libre.clear()
                warm, outcome = {t for t in tickers if bot._mtf_listo(t)}, {}
                done = [sched.spawn(lambda t=t: outcome.__setitem__(t, bot._obtener_mtf(t)))
                        for t in tickers]
                while not all(flag.is_set() for flag in done):
                    sched.sleep(0.05)
                bot._mtf_ciclo_libre.set()
                for ticker, data in outcome.items():
                    self.assertFalse(data.sintetico)
                    if data.estado_datos == "READY":
                        first_ready.setdefault(ticker, cycle)
                    elif ticker in warm:
                        misses.append((cycle, ticker, data.motivo_datos))
            bot._activo = False

        def poll_status():
            while bot._activo:
                try:
                    gate.request(quote, MARKET)
                    status["sent"] += 1
                except Deferred:
                    status["deferred"] += 1
                sched.sleep(20)

        for job in (driver, bot._refresco_mtf_loop, poll_status):
            sched.spawn(job)
        sched.run()
        return history, first_ready, real, misses, status

    def test_real_first_all_ready_and_no_warm_ticker_misses_a_bar(self):
        history, first_ready, real, misses, status = self.run_session()
        self.assertTrue(all(first_ready.get(t, 99) <= 3 for t in real), first_ready)
        self.assertEqual(len(first_ready), 17)
        self.assertLessEqual(max(first_ready.values()), 10)
        self.assertEqual(misses, [])
        self.assertEqual(status["deferred"], 0)
        for timestamp in history:
            self.assertLessEqual(sum(timestamp - 60 < t <= timestamp for t in history), 60)
            self.assertLessEqual(sum(timestamp - 1 < t <= timestamp for t in history), 4)


# ---------------------------------------------------------------- server.py

HTTP = SimpleNamespace(RequestException=REQUESTS.RequestException, get=Mock(), post=Mock(), request=Mock())


class FakeResponse(dict):
    def __init__(self, payload):
        super().__init__(payload)
        self.status_code, self.headers = 200, {}


def load_server(names, **namespace):
    tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    scope = {"requests": HTTP, "SchwabRequestDeferred": Deferred, "SchwabRequestPaused": Paused,
             "SchwabOAuthFailure": type("SchwabOAuthFailure", (Exception,), {}),
             "datetime": datetime, "timedelta": timedelta, "timezone": timezone,
             "time": SimpleNamespace(monotonic=lambda: 1000.0, time=lambda: 0.0),
             "SCHWAB_STATUS_CACHE_SECONDS": 20, "SCHWAB_DEFERRED_VERIFY_GRACE_SECONDS": 300,
             "jsonify": FakeResponse, "_log": Mock()}
    scope.update(namespace)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "server.py", "exec"), scope)
    return scope


class ServerDeferralTests(unittest.TestCase):
    def verify_scope(self, state):
        bot_state = {"schwab_token": "offline-access", **state}
        scope = load_server(
            {"_verify_schwab_connection", "_set_schwab_auth_state", "_schwab_verified_age_seconds"},
            BOT_STATE=bot_state, _SCHWAB_AUTH_LOCK=threading.RLock(),
            schwab_request_pause=lambda: {"paused": False},
            _schwab_auth_snapshot=lambda: dict(bot_state), _adopt_schwab_process_tokens=lambda: False,
            _current_schwab_access_token=lambda: "offline-access", get_env=lambda key: "",
            SCHWAB_MKT_URL="https://api.schwabapi.com/marketdata/v1",
            schwab_request=Mock(side_effect=Deferred(16, "control_capacity_reserved")))
        return scope, bot_state

    def test_recent_connected_status_survives_local_deferral(self):
        verified = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(timespec="seconds")
        scope, state = self.verify_scope({"schwab_auth_state": "connected", "schwab_auth_verified": True,
                                          "schwab_auth_verified_at": verified})
        result = scope["_verify_schwab_connection"]()
        self.assertEqual(result["schwab_auth_state"], "connected")
        self.assertEqual(result["schwab_auth_verified_at"], verified)
        self.assertEqual(state["schwab_auth_next_check_monotonic"], 1020.0)
        scope["_log"].assert_not_called()

    def test_old_or_unknown_status_reports_postponed_not_network_failure(self):
        verified = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat(timespec="seconds")
        for state in ({"schwab_auth_state": "connected", "schwab_auth_verified_at": verified},
                      {"schwab_auth_state": "unknown"}):
            with self.subTest(state=state["schwab_auth_state"]):
                scope, bot_state = self.verify_scope(state)
                scope["_verify_schwab_connection"]()
                self.assertEqual(bot_state["schwab_auth_state"], "unavailable")
                self.assertIn("pospuesta", bot_state["schwab_auth_message"])
                self.assertNotIn("error temporal de red", bot_state["schwab_auth_message"])
                self.assertEqual(bot_state["schwab_auth_next_check_monotonic"], 1020.0)
                scope["_log"].assert_not_called()

    def test_refresh_deferral_does_not_escalate_network_backoff(self):
        state = {"schwab_refresh_backoff_seconds": 30}
        scope = load_server({"_schwab_refresh_locked", "_set_schwab_auth_state"},
                            BOT_STATE=state, OAUTH_ROTATION_LOCK=threading.RLock(),
                            get_env=lambda key: "offline", SCHWAB_REDIRECT_URI="https://127.0.0.1",
                            refresh_oauth_token=Mock(side_effect=Deferred(3, "request_burst")))
        self.assertFalse(scope["_schwab_refresh_locked"]("offline-refresh"))
        self.assertEqual(state["schwab_refresh_backoff_seconds"], 30)
        self.assertNotIn("schwab_refresh_retry_after_monotonic", state, "not a Schwab pause")
        self.assertEqual(state["schwab_auth_next_check_monotonic"], 1003.0)
        self.assertIn("pospuesta", state["schwab_auth_message"])

    def diagnostics(self, send):
        socket = SimpleNamespace(getaddrinfo=lambda *a, **k: [(0, 0, 0, "", ("192.0.2.1", 443))],
                                 SOCK_STREAM=1, gethostname=lambda: "offline-host")
        return load_server(
            {"_probe_schwab_route", "schwab_network_diagnostics"},
            BOT_STATE={}, socket=socket, get_env=lambda key: "offline-client",
            schwab_request=send, build_safe_diagnostic=lambda *a, **k: {},
            OAUTH_USER_AGENT="offline", SCHWAB_REDIRECT_URI="https://127.0.0.1",
            SCHWAB_AUTH_URL="https://api.schwabapi.com/v1/oauth/authorize",
            SCHWAB_TOKEN_URL="https://api.schwabapi.com/v1/oauth/token",
            SCHWAB_TRADE_URL="https://api.schwabapi.com/trader/v1",
            APP_VERSION="3.3.17", _schwab_retry_remaining=lambda: 0)

    def test_deferred_probes_are_local_budget_not_network_failure(self):
        def send(_send, url, **kwargs):
            if url.endswith("/token"):
                return Response(401, {"error": "invalid_client"})
            raise Deferred(16, "control_capacity_reserved")
        result = self.diagnostics(send)["schwab_network_diagnostics"]()
        self.assertEqual(result["conclusion"], "LOCAL_BUDGET_BUSY")
        self.assertEqual(result["local_retry_after_seconds"], 16)
        self.assertEqual(result["retry_after_seconds"], 0)
        trader = result["probes"]["trader"]
        self.assertEqual((trader["deferred"], trader["request_sent"]), (True, False))
        self.assertNotIn("network_error", trader)
        self.assertTrue(result["probes"]["token_endpoint"]["oauth_response"])

    def test_real_transport_error_is_still_a_network_failure(self):
        def send(_send, url, **kwargs):
            raise REQUESTS.RequestException("offline connection reset")
        result = self.diagnostics(send)["schwab_network_diagnostics"]()
        self.assertEqual(result["conclusion"], "NETWORK_PROBE_FAILED")

    def test_deferred_code_exchange_keeps_code_unused(self):
        fake = ModuleType("api.schwab_oauth")
        fake.parse_authorization_input = lambda raw, **kw: {"code": "offline-code", "state": ""}
        state = {"schwab_oauth_last_code_fingerprint": "earlier-code"}
        scope = load_server(
            {"schwab_connect"}, BOT_STATE=state,
            request=SimpleNamespace(get_json=lambda: {"auth_code": "offline-code"}),
            session={}, get_env=lambda key: "offline", SCHWAB_REDIRECT_URI="https://127.0.0.1",
            code_fingerprint=lambda code: "fingerprint-" + code, _SCHWAB_AUTH_LOCK=threading.RLock(),
            _schwab_retry_remaining=lambda: 0, OAUTH_ROTATION_LOCK=threading.RLock(),
            exchange_authorization_code=Mock(side_effect=Deferred(2, "request_budget")),
            _set_schwab_auth_state=Mock())
        with patch.dict(sys.modules, {"api": ModuleType("api"), "api.schwab_oauth": fake}):
            response = scope["schwab_connect"]()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response["request_sent"])
        self.assertEqual(response.headers["Retry-After"], "2")
        self.assertEqual(state["schwab_oauth_last_code_fingerprint"], "earlier-code")
        scope["_set_schwab_auth_state"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
