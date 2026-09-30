import unittest
import sys
import types

# The repository declares these runtime dependencies in requirements.txt.  The
# isolated CI used for these pure unit tests may not have them installed, so
# provide the minimum import surface; no network request is made in this suite.
try:
    import requests  # noqa: F401
except ImportError:
    requests_stub = types.ModuleType("requests")

    class _StubSession:
        def __init__(self):
            self.headers = {}

    class _RequestException(Exception):
        pass

    requests_stub.Session = _StubSession
    requests_stub.exceptions = types.SimpleNamespace(
        RequestException=_RequestException,
    )
    sys.modules["requests"] = requests_stub

try:
    import dotenv  # noqa: F401
except ImportError:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub

from api.hybrid_order_executor import (
    ContextoMercado,
    EjecutorHibridoDeOrdenes,
)
from api.order_sync import AsyncOrderSynchronizer
from core.risk_manager import OrdenTradingCalculada


class SequenceDetail:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.index = 0

    def __call__(self):
        idx = min(self.index, len(self.statuses) - 1)
        self.index += 1
        return {"status": self.statuses[idx], "filled": 0, "avg_price": 0}


class FakeResponse:
    def __init__(self, order_id="1001"):
        self.headers = {
            "Location": f"https://api.schwabapi.com/trader/v1/accounts/x/orders/{order_id}"
        }

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.posts = []
        self.deletes = []

    def post(self, url, json, timeout):
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse(str(1000 + len(self.posts)))

    def delete(self, url, timeout):
        self.deletes.append({"url": url, "timeout": timeout})
        return FakeResponse()


def make_order():
    return OrdenTradingCalculada(
        ticker="AAPL",
        tipo="BUY",
        precio_limite=200.0,
        stop_loss=198.0,
        trailing_stop_pct=1.0,
        cantidad=10,
        riesgo_usd=20.0,
        atr_actual=1.0,
        take_profit=204.0,
        take_profit_parcial=202.0,
        cantidad_parcial=7,
        cantidad_resto=3,
        modo_resto="breakeven",
        salida_parcial_activa=True,
    )


class AsyncPollingTests(unittest.TestCase):
    def test_waits_through_pending_cancel_until_canceled(self):
        sync = AsyncOrderSynchronizer(poll_interval_ms=25)
        detail = SequenceDetail(["WORKING", "PENDING_CANCEL", "CANCELED"])
        result = sync.run_order(
            detail,
            desired_statuses={"CANCELED"},
            failure_statuses={"FILLED"},
            timeout_s=0.3,
        )
        self.assertTrue(result.confirmed)
        self.assertEqual(result.status, "CANCELED")
        self.assertGreaterEqual(detail.index, 3)

    def test_fill_race_is_not_reported_as_canceled(self):
        sync = AsyncOrderSynchronizer(poll_interval_ms=25)
        result = sync.run_order(
            SequenceDetail(["PENDING_CANCEL", "FILLED"]),
            desired_statuses={"CANCELED"},
            failure_statuses={"FILLED"},
            timeout_s=0.3,
        )
        self.assertFalse(result.confirmed)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.status, "FILLED")

    def test_unknown_status_times_out_without_false_confirmation(self):
        sync = AsyncOrderSynchronizer(poll_interval_ms=25)
        result = sync.run_order(
            SequenceDetail(["DESCONOCIDO"]),
            desired_statuses={"CANCELED"},
            failure_statuses={"FILLED"},
            timeout_s=0.06,
        )
        self.assertFalse(result.confirmed)
        self.assertTrue(result.timed_out)


class ExecutorLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.executor = EjecutorHibridoDeOrdenes(
            paper_trading=False,
            access_token="test-token",
            config={
                "order_sync": {
                    "poll_interval_ms": 25,
                    "ack_timeout_ms": 200,
                    "cancel_timeout_ms": 300,
                    "fill_timeout_ms": 300,
                    "flat_timeout_ms": 300,
                    "request_timeout_sec": 1,
                }
            },
        )
        self.executor._session = FakeSession()

    def test_cancel_waits_for_broker_terminal_state(self):
        detail = SequenceDetail(["WORKING", "PENDING_CANCEL", "CANCELED"])
        self.executor.detalle_orden = lambda account, oid: detail()
        self.executor._solicitar_cancelacion = lambda account, oid: True
        result = self.executor.cancelar_orden_confirmada("hash", "42")
        self.assertTrue(result["confirmado"])
        self.assertEqual(result["broker_status"], "CANCELED")

    def test_breakeven_stop_is_not_placed_when_cancel_is_unconfirmed(self):
        calls = []
        self.executor.cancelar_orden_confirmada = lambda account, oid: {
            "confirmado": False,
            "broker_status": "PENDING_CANCEL",
        }
        self.executor.colocar_salida = lambda *args, **kwargs: calls.append(args)
        result = self.executor.mover_stop_breakeven(
            "AAPL", "BUY", 3, 200.0, "old-stop", "hash"
        )
        self.assertEqual(result["status"], "CANCEL_FALLO")
        self.assertEqual(calls, [])

    def test_initial_protection_places_stop_before_take_profit(self):
        calls = []

        def fake_exit(ticker, side, quantity, price, order_type, account):
            calls.append(order_type)
            return {"status": "OK", "order_id": f"{order_type}-1"}

        self.executor.colocar_salida = fake_exit
        result = self.executor.montar_proteccion_parcial(make_order(), "hash")
        self.assertEqual(calls, ["STOP", "LIMIT"])
        self.assertEqual(result["sl"]["status"], "OK")

    def test_all_intraday_payloads_force_day_duration(self):
        self.executor.esperar_estado_orden = lambda *args, **kwargs: {
            "confirmado": True,
            "broker_status": "WORKING",
            "filled": 0,
            "avg_price": 0,
            "latency_ms": 10,
            "timeout": False,
            "error": "",
        }
        self.executor.esperar_posicion_plana = lambda *args, **kwargs: {
            "confirmado": True,
            "cantidad": 0,
            "latency_ms": 10,
            "timeout": False,
            "error": "",
        }

        order = make_order()
        self.executor._enviar_orden_real(order, "LIMIT", "hash", "test")
        self.executor.colocar_salida("AAPL", "BUY", 3, 200.0, "STOP", "hash")
        self.executor.esperar_estado_orden = lambda *args, **kwargs: {
            "confirmado": True,
            "broker_status": "FILLED",
            "filled": 3,
            "avg_price": 200,
            "latency_ms": 10,
            "timeout": False,
            "error": "",
        }
        self.executor.liquidar_posicion("AAPL", "BUY", 3, "hash", "test")

        self.assertEqual(len(self.executor._session.posts), 3)
        for sent in self.executor._session.posts:
            self.assertEqual(sent["json"]["duration"], "DAY")

    def test_fixed_sizing_is_not_multiplied_by_ai_probability(self):
        executor = EjecutorHibridoDeOrdenes(
            paper_trading=True,
            config={"risk": {"sizing_mode": "fixed"}},
        )
        order = make_order()
        result = executor.enviar_entrada(
            order,
            ContextoMercado(
                atr_relativo=0.2,
                es_rango_lateral=False,
                prob_ia=0.99,
                es_senal_maestra=True,
            ),
            "paper",
        )
        self.assertEqual(result["orden_ajustada"].cantidad, order.cantidad)

    def test_revoked_token_is_removed_from_injected_session(self):
        self.executor._http_session()
        self.assertIn("Authorization", self.executor._session.headers)
        self.executor.set_token(None)
        self.assertNotIn("Authorization", self.executor._session.headers)

    def test_token_provider_refreshes_executor_session(self):
        self.executor.set_token_provider(lambda: "fresh-token")
        session = self.executor._http_session()
        self.assertEqual(
            session.headers.get("Authorization"),
            "Bearer fresh-token",
        )


if __name__ == "__main__":
    unittest.main()
