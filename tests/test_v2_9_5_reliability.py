from __future__ import annotations

import threading
from types import SimpleNamespace

from api.hybrid_order_executor import EjecutorHibridoDeOrdenes
from engine.bot_engine import ChocoletraBot


class _Resp:
    def __init__(self, payload=None, status=200, location=""):
        self._payload = payload or {}
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self.text = ""
    def json(self):
        return self._payload
    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            e = requests.HTTPError(f"HTTP {self.status_code}")
            e.response = self
            raise e


class _Session:
    def __init__(self, position_payload):
        self.position_payload = position_payload
        self.posts = []
    def get(self, *args, **kwargs):
        return _Resp(self.position_payload)
    def post(self, url, json=None, **kwargs):
        self.posts.append(json)
        return _Resp(status=201, location="https://x/orders/123")


def _executor(position_payload):
    ex = EjecutorHibridoDeOrdenes.__new__(EjecutorHibridoDeOrdenes)
    ex.paper_trading = False
    ex.request_timeout_s = 1.0
    ex.fill_timeout_s = 1.0
    ex.flat_timeout_s = 1.0
    ex._audit = lambda *a, **k: None
    sess = _Session(position_payload)
    ex._http_session = lambda: sess
    ex.esperar_estado_orden = lambda *a, **k: {
        "confirmado": True, "broker_status": "FILLED", "latency_ms": 1,
        "error": "",
    }
    ex.esperar_posicion_plana = lambda *a, **k: {
        "confirmado": True, "cantidad": 0, "latency_ms": 1,
        "timeout": False, "error": "",
    }
    ex.detalle_orden = lambda *a, **k: {"avg_price": 100.0, "status": "FILLED"}
    return ex, sess


def test_real_liquidation_uses_broker_short_side_even_if_local_says_long():
    payload = {"securitiesAccount": {"positions": [{
        "instrument": {"symbol": "META"},
        "longQuantity": 0,
        "shortQuantity": 1,
        "averagePrice": 550,
    }]}}
    ex, sess = _executor(payload)
    result = ex.liquidar_posicion("META", "BUY", 99, "acct", "reversal")
    assert result["status"] == "OK"
    assert result["broker_side"] == "SELL"
    assert sess.posts[0]["orderLegCollection"][0]["instruction"] == "BUY_TO_COVER"
    assert sess.posts[0]["orderLegCollection"][0]["quantity"] == 1


def test_real_liquidation_uses_broker_long_side_even_if_local_says_short():
    payload = {"securitiesAccount": {"positions": [{
        "instrument": {"symbol": "NVDA"},
        "longQuantity": 1,
        "shortQuantity": 0,
        "averagePrice": 220,
    }]}}
    ex, sess = _executor(payload)
    result = ex.liquidar_posicion("NVDA", "SELL", 8, "acct", "kill switch")
    assert result["status"] == "OK"
    assert result["broker_side"] == "BUY"
    assert sess.posts[0]["orderLegCollection"][0]["instruction"] == "SELL"
    assert sess.posts[0]["orderLegCollection"][0]["quantity"] == 1


def test_real_liquidation_does_not_submit_when_broker_is_flat():
    payload = {"securitiesAccount": {"positions": []}}
    ex, sess = _executor(payload)
    result = ex.liquidar_posicion("META", "SELL", 1, "acct", "kill switch")
    assert result["status"] == "OK"
    assert result["posicion_plana"] is True
    assert sess.posts == []


def test_post_session_training_can_run_with_unresolved_real_exposure_when_no_auto_promotion():
    bot = ChocoletraBot.__new__(ChocoletraBot)
    bot._activo = False
    bot._posiciones = {}
    bot._fin_sesion_alcanzado = False
    bot._post_session_retrain_scheduled = False
    bot.ks_liquidar_apagar = False
    bot.config = {
        "agents": {"entrenamiento": {
            "reentrenar_fin_sesion": True,
            "automatic_retraining": {"auto_activate_production": False},
        }}
    }
    bot.tickers = ["META", "NCLH"]
    bot._detener_monitor_ordenes = lambda: None
    bot._post_session_training_safe = lambda: False
    called = []
    bot.orquestador = SimpleNamespace(al_fin_sesion=lambda tickers: called.append(list(tickers)) or True)
    bot.tos_reader = SimpleNamespace(detener=lambda: None)
    bot.telegram = SimpleNamespace(enviar_fin_sesion=lambda *_: None)
    bot._resumen = {}
    bot._emit_log = lambda *a, **k: None
    bot.detener(fin_sesion=True)
    assert called == [["META", "NCLH"]]
    assert bot._post_session_retrain_scheduled is True


def _slot_bot():
    bot = ChocoletraBot.__new__(ChocoletraBot)
    bot._lock = threading.RLock()
    bot._activo = True
    bot._posiciones = {}
    bot.max_paper_positions = 30
    bot.max_real_positions = 30
    bot.max_total_positions = 60
    bot._daily_blocked = lambda: False
    bot._emit_log = lambda *a, **k: None
    bot.execution_mode_for = lambda ticker: "REAL" if str(ticker).startswith("R") else "PAPER"
    bot._is_paper_ticker = lambda ticker, pos=None: str((pos or {}).get("execution_mode", "PAPER")).upper() == "PAPER"
    return bot


def test_thirty_paper_plus_thirty_real_capacity_is_independent():
    bot = _slot_bot()
    for i in range(30):
        assert bot._reservar_slot(f"P{i}", "BUY")
    for i in range(30):
        assert bot._reservar_slot(f"R{i}", "BUY")
    assert len(bot._posiciones) == 60
    assert bot._reservar_slot("P30", "BUY") is False
    assert bot._reservar_slot("R30", "BUY") is False
