"""Production-code regression tests without dependencies, credentials or HTTP.

Run: python -m unittest discover -s tests -p test_schwab_request_budget_offline.py
Only the unavailable requests dependency is replaced with exception types. The
transport definitions and selected application methods come from actual source.
This does not replace full application/deployment integration tests.
"""
import ast
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]


class RequestException(Exception):
    def __init__(self, message="", response=None):
        super().__init__(message)
        self.response = response


REQUESTS = SimpleNamespace(RequestException=RequestException)
tree = ast.parse((ROOT / "api/schwab_transport.py").read_text())
tree.body = [node for node in tree.body
             if not (isinstance(node, ast.Import) and any(a.name == "requests" for a in node.names))
             and not (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_GATE" for t in node.targets))]
TRANSPORT = {"requests": REQUESTS, "__file__": str(ROOT / "api/schwab_transport.py"), "__name__": "offline_transport"}
exec(compile(tree, str(ROOT / "api/schwab_transport.py"), "exec"), TRANSPORT)
Gate = TRANSPORT["SchwabRequestGate"]
Deferred = TRANSPORT["SchwabRequestDeferred"]
Paused = TRANSPORT["SchwabRequestPaused"]
control_requests = TRANSPORT["schwab_control_requests"]


class Clock:
    def __init__(self):
        self.wall, self.mono = 1791316800.0, 100.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class Response:
    def __init__(self, status=200, payload=None, headers=None, text=None):
        self.status_code = status
        self.payload = {} if payload is None else payload
        self.headers = headers or {}
        self.text = json.dumps(self.payload) if text is None else text

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RequestException("HTTP error with private URL/body", response=self)


def load_methods(path, class_name, names, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    cls.bases, cls.decorator_list = [], []
    scope = {"Optional": Optional, "datetime": datetime, "timedelta": timedelta,
             "threading": threading, "requests": REQUESTS,
             "SchwabRequestDeferred": Deferred, "SchwabRequestPaused": Paused,
             "schwab_control_requests": control_requests}
    scope.update(namespace)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), path, "exec"), scope)
    return scope[class_name]


MARKET = "https://api.schwabapi.com/marketdata/v1/quotes"
ORDER = "https://api.schwabapi.com/trader/v1/accounts/private-account/orders/private-order"


class GateFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock, self.events = Clock(), []
        self.gate = self.make_gate()
        self.calls = []

    def make_gate(self, **kwargs):
        return Gate(Path(self.temp.name) / "pause.json", clock=lambda: self.clock.wall,
                    monotonic=lambda: self.clock.mono, diagnostic_sink=self.events.append, **kwargs)

    def send(self, response=None, method="get"):
        def callback(url, **kwargs):
            self.calls.append((url, kwargs))
            return response or Response()
        callback.__name__ = method
        return callback


class TransportTests(GateFixture):
    def test_concurrent_workers_cannot_exceed_burst(self):
        send = self.send()
        def worker(_):
            try:
                self.gate.request(send, MARKET)
                return True
            except Deferred as exc:
                self.assertFalse(exc.request_sent)
                return False
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(worker, range(100)))
        self.assertEqual(sum(results), 4)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.gate.snapshot()["request_metrics"]["sent_total"], 4)

    def test_burst_reserves_two_places_for_control(self):
        send = self.send()
        for _ in range(4):
            self.gate.request(send, MARKET)
        with self.assertRaises(Deferred):
            self.gate.request(send, MARKET)
        for _ in range(2):
            self.gate.request(self.send(method="delete"), ORDER, recovery_safe=False)
        with self.assertRaises(Deferred):
            self.gate.request(self.send(method="post"), ORDER, recovery_safe=False)
        self.assertEqual(len(self.calls), 6)

    def test_rolling_minute_limit_includes_control_and_retries(self):
        self.gate = self.make_gate(requests_per_minute=5, control_reserve=2, burst_per_second=100)
        send = self.send()
        for _ in range(3):
            self.gate.request(send, MARKET)
            self.clock.advance(1)
        with self.assertRaises(Deferred) as ctx:
            self.gate.request(send, MARKET)
        self.assertEqual(ctx.exception.reason, "control_capacity_reserved")
        with control_requests():
            self.gate.request(send, ORDER)
            self.gate.request(send, ORDER)
            with self.assertRaises(Deferred):
                self.gate.request(send, ORDER)
        self.assertEqual(self.gate.snapshot()["request_metrics"]["sent_last_60_seconds"], 5)
        self.clock.advance(60)
        self.gate.request(send, MARKET)
        self.assertEqual(self.gate.snapshot()["request_metrics"]["sent_last_60_seconds"], 1)

    def test_context_restores_background_priority(self):
        with self.assertRaises(ValueError):
            with control_requests():
                raise ValueError()
        self.gate = self.make_gate(requests_per_minute=2, control_reserve=1)
        self.gate.request(self.send(), MARKET)
        with self.assertRaises(Deferred):
            self.gate.request(self.send(), MARKET)

    def test_http_400_with_body_500_is_logged_and_backs_off_account_reads(self):
        response = Response(400, {"errors": [{"status": 500, "id": "15561df6-0ab3-dc65-a391-302798a9dde5",
                                             "detail": "secret-content"}]})
        self.gate.request(self.send(response), ORDER)
        with self.assertRaises(Deferred) as ctx:
            self.gate.request(self.send(), ORDER.replace("private-order", "another-order"))
        self.assertEqual(ctx.exception.reason, "order_read_backoff")
        self.assertEqual(self.events[0]["http_status"], 400)
        self.assertEqual(self.events[0]["broker_statuses"], [500])
        self.assertEqual(len(self.calls), 1)
        serialized = json.dumps(self.events)
        for secret in ("private-account", "private-order", "secret-content"):
            self.assertNotIn(secret, serialized)
        self.clock.advance(5)
        self.gate.request(self.send(response), ORDER)
        self.clock.advance(9)
        with self.assertRaises(Deferred):
            self.gate.request(self.send(), ORDER)
        self.clock.advance(1)
        self.gate.request(self.send(), ORDER)

    def test_success_resets_order_read_backoff(self):
        self.gate.request(self.send(Response(503)), ORDER)
        self.clock.advance(5)
        self.gate.request(self.send(), ORDER)
        self.gate.request(self.send(Response(503)), ORDER)
        self.clock.advance(5)
        self.gate.request(self.send(), ORDER)

    def test_backoff_is_specific_to_account_and_reads(self):
        self.gate.request(self.send(Response(400)), ORDER)
        self.gate.request(self.send(), ORDER.replace("private-account", "other-account"))
        self.gate.request(self.send(method="delete"), ORDER, recovery_safe=False)
        self.assertEqual(len(self.calls), 3)

    def test_request_exception_is_counted_and_redacted(self):
        def get(url, **kwargs):
            raise RequestException("secret-token private URL")
        with self.assertRaises(RequestException):
            self.gate.request(get, ORDER)
        with self.assertRaises(Deferred):
            self.gate.request(self.send(), ORDER)
        self.assertEqual(self.gate.snapshot()["request_metrics"]["sent_total"], 1)
        self.assertNotIn("secret-token", json.dumps(self.events))

    def test_403_pause_persists_and_uses_one_safe_recovery(self):
        self.gate.request(self.send(Response(403)), MARKET)
        self.clock.advance(300)
        restarted = self.make_gate()
        with self.assertRaises(Paused) as ctx:
            restarted.request(self.send(), ORDER)
        self.assertEqual(ctx.exception.retry_after_seconds, 300)
        self.clock.advance(300)
        with self.assertRaises(Paused):
            restarted.request(self.send(method="post"), ORDER, recovery_safe=False)
        restarted.request(self.send(), MARKET)
        self.assertFalse(restarted.snapshot()["recovery_pending"])

    def test_429_honors_http_date_and_survives_restart(self):
        from email.utils import format_datetime
        from datetime import timezone
        retry = format_datetime(datetime.fromtimestamp(self.clock.wall + 120, timezone.utc), usegmt=True)
        self.gate.request(self.send(Response(429, headers={"Retry-After": retry})), MARKET)
        self.assertEqual(self.gate.remaining(), 120)
        self.clock.advance(30)
        restarted = self.make_gate()
        self.assertEqual(restarted.remaining(), 90)
        self.assertEqual(restarted.snapshot()["pause_reason"], "http_429")
        with self.assertRaises(Paused) as ctx:
            restarted.request(self.send(), MARKET)
        self.assertIn("http_429", str(ctx.exception))

    def test_failed_recovery_keeps_ten_minute_pause(self):
        self.gate.request(self.send(Response(403)), MARKET)
        self.clock.advance(600)
        self.gate.request(self.send(Response(429)), MARKET)
        self.assertGreaterEqual(self.gate.remaining(), 600)

    def test_probe_serialization_and_late_denial(self):
        entered, release = threading.Event(), threading.Event()
        def get(url, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return Response(403)
        with ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(self.gate.request, get, MARKET)
            self.assertTrue(entered.wait(2))
            self.gate.request(self.send(Response(403)), MARKET)
            self.clock.advance(600)
            def recovery(url, **kwargs):
                with self.assertRaises(Paused):
                    self.gate.request(self.send(), MARKET)
                release.set()
                old.result(timeout=2)
                return Response()
            self.gate.request(recovery, MARKET)
        self.assertEqual(self.gate.remaining(), 1200)

    def test_no_credentials_or_ids_in_403_diagnostics_or_state(self):
        text = '<HTML>Reference&#32;&#35;18&#46;2a0ed217&#46;1790894070&#46;382bce1d secret</HTML>'
        self.gate.request(self.send(Response(403, text=text)), ORDER + "?token=secret",
                          headers={"Authorization": "secret"}, data={"refresh_token": "secret"})
        self.assertEqual(self.events[0]["akamai_reference"], "18.2a0ed217.1790894070.382bce1d")
        for text in (json.dumps(self.events), self.gate.state_path.read_text()):
            for secret in ("secret", "private-account", "private-order"):
                self.assertNotIn(secret, text)

    def test_other_hosts_are_not_budgeted_or_counted(self):
        self.gate.request(self.send(), "https://example.test/health")
        self.assertEqual(self.gate.snapshot()["request_metrics"]["sent_total"], 0)

    def test_snapshot_does_not_send_or_count_requests(self):
        for _ in range(100):
            self.gate.snapshot()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.gate.snapshot()["request_metrics"]["sent_total"], 0)

    def test_refused_mutation_is_not_queued_or_replayed(self):
        self.gate = self.make_gate(requests_per_minute=1, control_reserve=0)
        self.gate.request(self.send(), MARKET)
        post = Mock(return_value=Response(201))
        with self.assertRaises(Deferred) as ctx:
            self.gate.request(post, ORDER, recovery_safe=False)
        self.assertFalse(ctx.exception.request_sent)
        self.clock.advance(60)
        self.gate.snapshot()
        post.assert_not_called()
        self.gate.request(post, ORDER, recovery_safe=False)
        post.assert_called_once()

    def test_shared_wrapper_disables_unbudgeted_redirects(self):
        TRANSPORT["_GATE"] = self.gate
        TRANSPORT["schwab_request"](self.send(), MARKET)
        self.assertFalse(self.calls[0][1]["allow_redirects"])


class ExecutorTests(GateFixture):
    def setUp(self):
        super().setUp()
        self.Executor = load_methods("api/hybrid_order_executor.py", "EjecutorHibridoDeOrdenes",
            {"_buscar_orden_en_listado", "_working_orders_snapshot", "detalle_orden", "esperar_estado_orden",
             "orden_trabajando", "_iter_order_nodes", "_orden_en", "_listado_ordenes"},
            {"schwab_request": self.gate.request, "_ps": Mock(), "nullcontext": nullcontext,
             "time": SimpleNamespace(monotonic=lambda: self.clock.mono)})
        self.Executor._iter_order_nodes.__globals__["EjecutorHibridoDeOrdenes"] = self.Executor
        self.executor = self.Executor()
        self.executor._order_list_cache, self.executor._order_list_lock = None, threading.Lock()
        self.executor.TRADER_URL = "https://api.schwabapi.com/trader/v1"
        self.executor.request_timeout_s = 5
        self.executor.paper_trading = False
        self.executor._working_orders_provider = None
        self.results = []
        def get(url, **kwargs):
            self.calls.append((url, kwargs))
            return self.results.pop(0)
        self.executor._http_session = lambda: SimpleNamespace(get=get)

    def test_auth_or_access_failure_does_not_fan_out_to_listing(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.gate = self.make_gate()
                # Previous cases may have persisted a cooldown; start a fresh file.
                self.gate.state_path.unlink(missing_ok=True)
                self.gate = self.make_gate()
                self.setUp_executor_gate()
                self.results = [Response(status)]
                before = len(self.calls)
                self.assertIsNone(self.executor._buscar_orden_en_listado("private-account", "private-order"))
                self.assertEqual(len(self.calls) - before, 1)

    def setUp_executor_gate(self):
        self.executor._buscar_orden_en_listado.__func__.__globals__["schwab_request"] = self.gate.request

    def test_failed_detail_plus_working_snapshot_stays_unknown(self):
        # v3.3.19: the WORKING list has its own pause, so it is still asked
        # after the single-order and full-list reads fail; all three failing
        # leaves the order unknown.
        self.results = [Response(400), Response(400), Response(400)]
        result = self.executor.detalle_orden("private-account", "private-order")
        self.assertEqual(result["status"], "DESCONOCIDO")
        self.assertEqual(result["filled"], 0)
        self.assertEqual(len(self.calls), 3)

    def test_server_failure_has_one_recovery_fallback(self):
        self.results = [Response(503), Response(200, [{"orderId": "private-order", "status": "WORKING"}])]
        result = self.executor._buscar_orden_en_listado("private-account", "private-order")
        self.assertEqual(result["status"], "WORKING")
        self.assertEqual(len(self.calls), 2)

    def test_repeated_failed_fallback_does_not_bypass_backoff(self):
        self.results = [Response(400), Response(400)]
        for _ in range(50):
            self.assertIsNone(self.executor._buscar_orden_en_listado("private-account", "private-order"))
        self.assertEqual(len(self.calls), 2)

    def test_404_allows_one_listing_fallback(self):
        self.results = [Response(404), Response(200, [{"orderId": "private-order", "status": "WORKING"}])]
        result = self.executor._buscar_orden_en_listado("private-account", "private-order")
        self.assertEqual(result["status"], "WORKING")
        self.assertEqual(len(self.calls), 2)

    def test_wrong_order_response_is_not_accepted(self):
        self.results = [Response(200, {"orderId": "wrong", "status": "FILLED"}), Response(200, [])]
        self.assertIsNone(self.executor._buscar_orden_en_listado("private-account", "private-order"))

    def test_malformed_working_orders_provider_is_unknown(self):
        self.executor._working_orders_provider = lambda account: {"errors": []}
        self.assertIsNone(self.executor._working_orders_snapshot("private-account"))
        self.assertEqual(self.calls, [])

    def test_malformed_http_working_orders_is_unknown(self):
        self.results = [Response(200, {"errors": []})]
        self.assertIsNone(self.executor._working_orders_snapshot("private-account"))

    def test_bounded_confirmation_fetches_in_control_worker_context(self):
        self.executor.ack_timeout_s = 0.75
        self.gate = self.make_gate(requests_per_minute=2, control_reserve=1)
        self.gate.request(self.send(), MARKET)
        self.executor.detalle_orden = lambda *args: self.gate.request(self.send(), ORDER) and {"status": "WORKING"}
        observed = []
        def run_order(fetch, **kwargs):
            with ThreadPoolExecutor(max_workers=1) as pool:
                observed.append(pool.submit(fetch).result())
            return SimpleNamespace(confirmed=True, status="WORKING", filled=0, avg_price=0,
                                   elapsed_ms=0, timed_out=False, error="")
        self.executor._sync = SimpleNamespace(run_order=run_order)
        result = self.executor.esperar_estado_orden("private-account", "private-order", ["WORKING"])
        self.assertTrue(result["confirmado"])
        self.assertEqual(observed, [{"status": "WORKING"}])


class ApplicationTests(unittest.TestCase):
    def test_reconciliation_does_not_retry_a_local_pause(self):
        Bot = load_methods("engine/bot_engine.py", "ChocoletraBot", {"_broker_read_with_retry"},
                           {"time": SimpleNamespace(sleep=Mock()), "schwab_control_requests": nullcontext})
        bot = Bot()
        bot._broker_retry_settings = lambda: (3, 0.75)
        bot._emit_log = Mock()
        callback = Mock(side_effect=Deferred(30, "request_budget"))
        self.assertEqual(bot._broker_read_with_retry("positions", callback)[0], False)
        callback.assert_called_once()

    def test_metrics_route_has_no_auth_refresh_or_http_side_effect(self):
        tree = ast.parse((ROOT / "server.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "schwab_request_metrics")
        node.decorator_list = []
        snapshot = Mock(return_value={"request_metrics": {"sent_total": 7}})
        scope = {"schwab_request_pause": snapshot, "jsonify": lambda x: x, "APP_VERSION": "3.3.15"}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "server.py", "exec"), scope)
        result = scope["schwab_request_metrics"]()
        self.assertEqual(result["request_metrics"]["sent_total"], 7)
        self.assertEqual(result["version"], "3.3.15")
        snapshot.assert_called_once()


if __name__ == "__main__":
    unittest.main()
