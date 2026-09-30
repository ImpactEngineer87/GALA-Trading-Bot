from __future__ import annotations

import threading
from types import SimpleNamespace

from engine.bot_engine import ChocoletraBot


def test_recover_tracked_stop_fill_returns_real_order_id_and_price():
    bot = ChocoletraBot.__new__(ChocoletraBot)
    bot.account_number = "acct"
    events = []
    bot._audit_event = lambda event, **fields: events.append((event, fields)) or fields
    bot.ejecutor_hibrido = SimpleNamespace(
        detalle_orden=lambda account, oid: {
            "status": "FILLED",
            "filled": 1,
            "avg_price": 99.25,
        }
    )

    result = bot._recuperar_cierre_stop_llenado(
        "META",
        {"sl_order_id": "STOP-123"},
        1,
        {"broker_status": "FILLED", "filled": 1},
    )

    assert result["order_id"] == "STOP-123"
    assert result["avg_price"] == 99.25
    assert events and events[-1][0] == "EXIT_ORDER_ID_RECOVERED"


def test_broker_already_flat_uses_filled_stop_for_learning_and_pnl():
    bot = ChocoletraBot.__new__(ChocoletraBot)
    bot._lock = threading.RLock()
    bot.account_number = "acct"
    bot._cerrando = set()
    bot._posiciones = {
        "META": {
            "tipo": "BUY",
            "entrada": 100.0,
            "orden": SimpleNamespace(cantidad=1),
            "cantidad_resto": 1,
            "tp1_hit": False,
            "tp1_order_id": "",
            "sl_order_id": "STOP-REAL-1",
            "position_id": "p1",
            "execution_id": "e1",
            "correlation_id": "c1",
            "execution_mode": "REAL",
            "pnl_realizado": 0.0,
        }
    }
    bot._is_paper_ticker = lambda ticker, pos=None: False
    bot._position_lifecycle_context = lambda ticker, pos: {
        "execution_id": "e1",
        "position_id": "p1",
        "correlation_id": "c1",
        "signal_id": "s1",
        "reversal_id": "",
        "signal_side": "BUY",
        "state_before": "LONG",
        "state_after": "OUT",
        "position_side": "LONG",
        "order_action": "SELL",
    }
    audits = []
    bot._audit_event = lambda event, **fields: audits.append((event, fields)) or {"event": event}
    bot._emit_log = lambda *a, **k: None
    bot._asegurar_token_trading = lambda: True
    bot._asegurar_ticker_limpio = lambda ticker: True
    bot._cantidad_real_broker = lambda ticker, side: 0
    bot._marcar_liquidado = lambda ticker: None
    bot.ejecutor_hibrido = SimpleNamespace(
        cancelar_orden_confirmada=lambda account, oid: {
            "confirmado": False,
            "broker_status": "FILLED",
            "filled": 1,
            "latency_ms": 1,
        },
        detalle_orden=lambda account, oid: {
            "status": "FILLED",
            "filled": 1,
            "avg_price": 99.0,
        },
    )
    bot._state_machine = lambda: SimpleNamespace(
        close=lambda *a, **k: SimpleNamespace(
            state_before=SimpleNamespace(value="LONG"),
            state_after=SimpleNamespace(value="OUT"),
            position_id="p1",
        )
    )
    pnl = []
    bot._registrar_pnl_realizado_diario = lambda value, **kw: pnl.append(value)
    bot.motor_señales = SimpleNamespace(registrar_salida=lambda ticker: None)
    bot._history_event = lambda *a, **k: {"history": True}
    captured = {}
    bot.orquestador = SimpleNamespace(
        al_cerrar=lambda ticker, price, lifecycle=None: captured.update(
            {"ticker": ticker, "price": price, "lifecycle": dict(lifecycle or {})}
        )
    )
    bot._emit_trade = lambda *a, **k: None
    bot._evaluar_limite_perdida_diaria = lambda: False

    ok = bot._cerrar_posicion_impl("META", 100.0, motivo="señal opuesta SELL")

    assert ok is True
    assert "META" not in bot._posiciones
    assert captured["lifecycle"]["exit_order_id"] == "STOP-REAL-1"
    assert captured["lifecycle"]["exit_confirmed"] is True
    assert captured["price"] == 99.0
    assert pnl == [-1.0], "the stopped share must not be zeroed merely because broker is already flat"
    assert any(event == "EXIT_ORDER_ID_RECOVERED" for event, _ in audits)


def test_unresolved_flat_close_is_still_quarantined_not_fabricated():
    bot = ChocoletraBot.__new__(ChocoletraBot)
    bot.account_number = "acct"
    bot._audit_event = lambda *a, **k: {}
    bot.ejecutor_hibrido = SimpleNamespace(
        detalle_orden=lambda account, oid: {
            "status": "CANCELED",
            "filled": 0,
            "avg_price": 0,
        }
    )

    result = bot._recuperar_cierre_stop_llenado(
        "NVDA",
        {"sl_order_id": "STOP-NOT-FILLED"},
        1,
        {"broker_status": "CANCELED", "filled": 0},
    )
    assert result == {}
