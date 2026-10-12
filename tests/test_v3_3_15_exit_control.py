"""Isolated regression checks of the production methods, with no HTTP or orders.

Run with ``python -m unittest discover -s tests -p test_v3_3_15_exit_control.py``.
AST extraction loads the actual method bodies without importing the Flask/ML
application, allowing these safety checks in a dependency-limited workspace.
These checks do not substitute for integration testing of the installed bot.
"""
import ast
import threading
import time
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import Mock
from core.position_state import PositionState

ROOT = Path(__file__).resolve().parents[1]


class LocalRequestPause(Exception):
    pass


def load_methods(path, class_name, names, namespace=None):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    cls.bases = []
    cls.decorator_list = []
    scope = {"Optional": Optional, "threading": threading, "time": time,
             "datetime": datetime, "timedelta": timedelta, "PositionState": PositionState,
             "schwab_control_requests": nullcontext}
    scope.update(namespace or {})
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 str(ROOT / path), "exec"), scope)
    return scope[class_name]


Bot = load_methods("engine/bot_engine.py", "ChocoletraBot", {
    "_procesar_ticker", "_close_opposite_without_entry", "_broker_read_with_retry",
    "_asegurar_ticker_limpio", "_cerrar_posicion_impl", "_detener_impl",
    "_refresh_broker_reconciliation", "_resolver_hash_cuenta",
})


class Frame:
    iloc = None
    index = [datetime(2026, 10, 5, 14, 0)] * 40

    def __init__(self):
        self.iloc = self

    def __len__(self):
        return 40

    def __getitem__(self, index):
        if isinstance(index, str):
            return SimpleNamespace(iloc=[100.0] * 40)
        return {"high": 101.0, "low": 99.0, "close": 100.0}


def ticker_bot(side="BUY", signal="SELL", close_ok=True):
    bot = Bot()
    bot._activo = True
    bot._lock = threading.RLock()
    bot._posiciones = {"META": {"tipo": side, "execution_mode": "REAL"}}
    bot._last_prices = {}
    bot._last_price_times = {}
    bot._gala10_contexto = {}
    bot._is_paper_ticker = lambda ticker, pos=None: False
    bot.ejecutor_hibrido = SimpleNamespace(secuencia_simbolo=lambda ticker: nullcontext())
    frame = Frame()
    data = SimpleNamespace(sintetico=False, tf=lambda name: frame)
    bot._obtener_mtf = lambda ticker: data
    bot._datos_micro_confirmados = lambda source: source
    bot._emit_candles = Mock()
    bot._set_ticker_processing = Mock()
    bot._emit_log = Mock()
    bot._emit_signal = Mock(return_value={"signal_id": "test-signal"})
    bot._audit_event = Mock()
    bot._quarantine_real_ticker = Mock()
    bot._recover_ticker_quarantine = Mock()
    bot._evaluar_limite_perdida_diaria = lambda: False
    bot._gestionar_posiciones = Mock()
    bot._senal_estrategia = lambda *args: signal
    bot.veto_observations = SimpleNamespace(update_bar=lambda *args, **kwargs: [])
    bot.signal_learning = SimpleNamespace(update_bar=lambda *args, **kwargs: [])
    bot.agentes_activos = True
    bot.live_allow_short = True
    bot.requerir_modelo_aprobado_en_real = True
    bot.orquestador = SimpleNamespace(
        entrenador=SimpleNamespace(modelo_disponible=Mock(return_value=False)),
        auditar_gala10=Mock(side_effect=RuntimeError("inference unavailable")),
    )
    bot._atr_micro = lambda frame: 1.0
    bot._gsco_features = lambda *args: {}
    bot._ahora_et = datetime.now
    bot._abrir_o_revertir = Mock(side_effect=AssertionError("new entry is forbidden"))
    bot._registrar_veto_ia = Mock()
    bot._registrar_no_op = Mock()
    bot._ai_decision_reason = lambda *args, **kwargs: "AI_THRESHOLD"
    bot._effective_ai_threshold = lambda *args: 0.6
    bot._ai_threshold_for_ticker = lambda *args: 0.6

    def close(ticker, price, motivo=""):
        if close_ok:
            bot._posiciones.pop(ticker)
        return close_ok

    bot._cerrar_posicion = Mock(side_effect=close)
    return bot


class ExitGateTests(unittest.TestCase):
    def test_missing_model_closes_existing_long_without_opening_short(self):
        bot = ticker_bot()
        bot._procesar_ticker("META")
        bot._cerrar_posicion.assert_called_once()
        self.assertNotIn("META", bot._posiciones)
        bot.orquestador.auditar_gala10.assert_not_called()
        bot._abrir_o_revertir.assert_not_called()

    def test_missing_model_closes_existing_short_without_opening_long(self):
        bot = ticker_bot(side="SELL", signal="BUY")
        bot._procesar_ticker("META")
        bot._cerrar_posicion.assert_called_once()
        bot._abrir_o_revertir.assert_not_called()

    def test_inference_exception_still_attempts_existing_position_exit(self):
        bot = ticker_bot()
        bot.orquestador.entrenador.modelo_disponible.return_value = True
        bot._procesar_ticker("META")
        bot.orquestador.auditar_gala10.assert_called_once()
        bot._cerrar_posicion.assert_called_once()
        bot._abrir_o_revertir.assert_not_called()

    def test_disabled_entry_agents_do_not_suppress_opposite_signal_exit(self):
        bot = ticker_bot()
        bot.agentes_activos = False
        bot._procesar_ticker("META")
        bot._cerrar_posicion.assert_called_once()
        bot.orquestador.auditar_gala10.assert_not_called()

    def test_failed_exit_retains_position_and_reports_unconfirmed(self):
        bot = ticker_bot(close_ok=False)
        bot._procesar_ticker("META")
        self.assertIn("META", bot._posiciones)
        call = bot._set_ticker_processing.call_args
        self.assertEqual(call.args[3], "OPPOSITE_CLOSE_NOT_CONFIRMED")
        self.assertEqual(call.kwargs["execution"], "CLOSE_NOT_CONFIRMED")
        bot._abrir_o_revertir.assert_not_called()

    def test_same_side_signal_with_missing_model_does_not_close(self):
        bot = ticker_bot(signal="BUY")
        bot._procesar_ticker("META")
        bot._cerrar_posicion.assert_not_called()

    def test_no_position_with_missing_model_cannot_open(self):
        bot = ticker_bot()
        bot._posiciones.clear()
        bot._procesar_ticker("META")
        bot._cerrar_posicion.assert_not_called()
        bot._abrir_o_revertir.assert_not_called()

    def test_synthetic_data_never_drives_an_exit(self):
        bot = ticker_bot()
        data = SimpleNamespace(sintetico=True, tf=lambda name: Frame())
        bot._obtener_mtf = lambda ticker: data
        bot._procesar_ticker("META")
        bot._cerrar_posicion.assert_not_called()

    def test_stopped_engine_cannot_close_from_late_worker(self):
        bot = ticker_bot()
        bot._activo = False
        self.assertFalse(bot._close_opposite_without_entry("META", "SELL", 100, "AI_UNAVAILABLE"))
        bot._cerrar_posicion.assert_not_called()

    def test_ai_veto_does_not_report_close_success_when_close_fails(self):
        bot = ticker_bot(close_ok=False)
        bot.orquestador.entrenador.modelo_disponible.return_value = True
        bot.orquestador.auditar_gala10.side_effect = None
        bot.orquestador.auditar_gala10.return_value = SimpleNamespace(
            operar=False, prob_meta=0.4, ai_model_snapshot={}
        )
        bot._procesar_ticker("META")
        self.assertIn("META", bot._posiciones)
        self.assertEqual(bot._set_ticker_processing.call_args.kwargs["execution"], "CLOSE_NOT_CONFIRMED")


class BrokerConfirmationTests(unittest.TestCase):
    def test_missing_account_cannot_claim_real_broker_is_flat(self):
        bot = Bot()
        bot.tickers = ["META"]
        bot._real_tickers = lambda tickers: ["META"]
        bot.account_number = ""
        bot._set_broker_reconciliation = lambda **values: values
        state = bot._refresh_broker_reconciliation(context="STOP_VERIFY", require_flat=True)
        self.assertFalse(state["ok"])
        self.assertFalse(state["flat"])

    def test_unconfirmed_protective_order_never_allows_a_liquidation(self):
        bot = ticker_bot()
        bot.account_number = "test-hash"
        bot._posiciones["META"].update(
            entrada=100, orden=SimpleNamespace(cantidad=3), cantidad_resto=3,
            tp1_hit=False, sl_order_id="protective-1",
        )
        bot._position_lifecycle_context = lambda *args: {}
        bot._asegurar_token_trading = lambda: True
        bot._broker_retry_settings = lambda: (1, 0)
        working = [{"orderId": "protective-1", "symbol": "META"}]
        bot.schwab = SimpleNamespace(obtener_ordenes_abiertas=lambda account: working)
        bot._iter_broker_orders = lambda rows: iter(rows)
        bot._symbol_from_broker_order = lambda order: order["symbol"]
        cancel = Mock(return_value={"confirmado": False, "broker_status": "WORKING"})
        liquidation = Mock(side_effect=AssertionError("must not send a close"))
        bot.ejecutor_hibrido.cancelar_orden_confirmada = cancel
        bot.ejecutor_hibrido.liquidar_posicion = liquidation
        self.assertFalse(bot._cerrar_posicion_impl("META", 100, "reversión"))
        self.assertIn("META", bot._posiciones)
        liquidation.assert_not_called()

    def test_broker_none_keeps_specific_safe_diagnostic(self):
        bot = Bot()
        bot._broker_retry_settings = lambda: (2, 0)
        bot._emit_log = Mock()
        bot.schwab = SimpleNamespace(broker_read_error=lambda op: "HTTP 401: AUTH_UNAVAILABLE")
        read = Mock(return_value=None)
        ok, value, error = bot._broker_read_with_retry("positions", read, operation="positions")
        self.assertFalse(ok)
        self.assertIsNone(value)
        self.assertIn("HTTP 401", error)
        self.assertEqual(read.call_count, 2)

    def test_empty_list_is_successful_broker_confirmation(self):
        bot = Bot()
        bot._broker_retry_settings = lambda: (1, 0)
        self.assertEqual(bot._broker_read_with_retry("positions", lambda: []), (True, [], ""))

    def test_stop_without_liquidation_does_not_claim_open_broker_is_flat(self):
        bot = ticker_bot()
        bot.ks_liquidar_apagar = False
        bot._fin_sesion_alcanzado = False
        bot._post_session_retrain_scheduled = False
        bot._detener_monitor_ordenes = Mock()
        bot._has_real_tickers = lambda: True
        bot._broker_reconciliation = {"ok": True, "flat": False}
        bot._refresh_broker_reconciliation = Mock(return_value={"ok": False, "flat": False})
        bot._liquidar_todo = Mock()
        bot.tos_reader = SimpleNamespace(detener=Mock())
        bot.telegram = SimpleNamespace(enviar_fin_sesion=Mock())
        bot._resumen = {}
        result = bot._detener_impl()
        self.assertFalse(result["broker_flat"])
        self.assertFalse(bot._activo)
        bot._liquidar_todo.assert_not_called()
        bot._refresh_broker_reconciliation.assert_called_once_with(context="STOP_VERIFY", require_flat=True)


class FakeHTTPError(Exception):
    def __init__(self, response):
        self.response = response
        super().__init__("sensitive account URL must not enter the diagnostic")


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise FakeHTTPError(self)


Client = load_methods("api/schwab_client.py", "SchwabClient", {
    "obtener_posiciones", "obtener_ordenes_abiertas", "_broker_read_failed", "broker_read_error"
}, {"TRADER_URL": "https://test.invalid", "_log": lambda msg: None,
    "SchwabRequestPaused": LocalRequestPause, "SchwabRequestDeferred": LocalRequestPause,
    "schwab_request_pause": lambda: {"paused": False}})


def client_response(payload, status=200):
    client = Client()
    client._broker_read_errors = threading.local()
    client._asegurar_token = Mock(return_value=True)
    client._get_autorizado = Mock(return_value=Response(payload, status))
    return client


class BrokerReadTests(unittest.TestCase):
    def test_empty_account_position_list_remains_valid(self):
        client = client_response({"securitiesAccount": {"positions": []}})
        self.assertEqual(client.obtener_posiciones("test"), [])
        self.assertEqual(client.broker_read_error("positions"), "")

    def test_unexpected_account_payload_is_unknown_not_flat(self):
        client = client_response({"unexpected": "response"})
        self.assertIsNone(client.obtener_posiciones("test"))
        self.assertIn("INVALID_ACCOUNT_RESPONSE", client.broker_read_error("positions"))

    def test_nonlist_positions_payload_is_unknown_not_flat(self):
        client = client_response({"securitiesAccount": {"positions": {}}})
        self.assertIsNone(client.obtener_posiciones("test"))

    def test_nonlist_order_payload_is_unknown_not_absent(self):
        client = client_response({"unexpected": "response"})
        self.assertIsNone(client.obtener_ordenes_abiertas("test"))
        self.assertIn("INVALID_ORDERS_RESPONSE", client.broker_read_error("orders"))

    def test_http_error_diagnostic_does_not_include_response_or_url(self):
        client = client_response({"secret": "not-for-display"}, 403)
        self.assertIsNone(client.obtener_posiciones("test"))
        detail = client.broker_read_error("positions")
        self.assertIn("HTTP 403", detail)
        self.assertNotIn("sensitive", detail)
        self.assertNotIn("not-for-display", detail)

    def test_missing_auth_remains_unknown_without_http(self):
        client = client_response({})
        client._asegurar_token.return_value = False
        self.assertIsNone(client.obtener_posiciones("test"))
        self.assertIn("AUTH_UNAVAILABLE", client.broker_read_error("positions"))
        client._get_autorizado.assert_not_called()

    def test_broker_errors_are_isolated_between_worker_threads(self):
        client = client_response({})
        barrier = threading.Barrier(2)
        results = []

        def worker(label):
            client._broker_read_failed("positions", label)
            barrier.wait()
            results.append(client.broker_read_error("positions"))

        threads = [threading.Thread(target=worker, args=(label,)) for label in ["first", "second"]]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(set(results), {"first", "second"})


def stop_server(engine, broker_state=None):
    tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name in {"_stop_engine_controlled", "stop_bot"}]
    for node in nodes:
        node.decorator_list = []
    state = {"running": True}
    scope = {
        "_ENGINE": engine, "BOT_STATE": state,
        "_ENGINE_CONTROL_LOCK": threading.RLock(), "_log": Mock(),
        "_read_config_runtime": lambda: {}, "_real_tickers_runtime": lambda cfg: ["META"],
        "_refresh_broker_reconciliation_read_only": Mock(return_value=broker_state),
        "_SCHEDULER": None, "_scheduler_mode": lambda: "manual",
        "jsonify": lambda value: value,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 str(ROOT / "server.py"), "exec"), scope)
    return scope


class ServerStopTests(unittest.TestCase):
    def test_unresolved_stop_keeps_engine_context_and_reports_running_false(self):
        engine = SimpleNamespace(detener=Mock(return_value={"broker_flat": False, "message": "unknown"}))
        scope = stop_server(engine)
        payload, status = scope["stop_bot"]()
        self.assertEqual(status, 500)
        self.assertFalse(payload["running"])
        self.assertFalse(payload["broker_flat"])
        self.assertFalse(scope["BOT_STATE"]["running"])
        self.assertIs(scope["_ENGINE"], engine)

    def test_no_engine_is_not_proof_of_flat_broker_after_restart(self):
        scope = stop_server(None, {"ok": True, "flat": False, "message": "META BUY 3"})
        payload, status = scope["stop_bot"]()
        self.assertEqual(status, 500)
        self.assertFalse(payload["broker_flat"])
        scope["_refresh_broker_reconciliation_read_only"].assert_called_once()

    def test_no_engine_with_failed_read_cannot_claim_flat(self):
        scope = stop_server(None)
        scope["_refresh_broker_reconciliation_read_only"].side_effect = RuntimeError("AUTH_UNAVAILABLE")
        payload, status = scope["stop_bot"]()
        self.assertEqual(status, 500)
        self.assertFalse(payload["broker_flat"])

    def test_verified_flat_stop_releases_engine_context(self):
        engine = SimpleNamespace(detener=Mock(return_value={"broker_flat": True}))
        scope = stop_server(engine)
        payload, status = scope["stop_bot"]()
        self.assertEqual(status, 200)
        self.assertTrue(payload["broker_flat"])
        self.assertIsNone(scope["_ENGINE"])


if __name__ == "__main__":
    unittest.main()
