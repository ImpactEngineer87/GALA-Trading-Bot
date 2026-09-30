from __future__ import annotations

import threading
from types import SimpleNamespace
from pathlib import Path

from core.ticker_performance import build_ticker_performance
from engine.bot_engine import ChocoletraBot


def test_clean_symbol_flattens_nested_orders_and_no_nameerror():
    bot = ChocoletraBot.__new__(ChocoletraBot)
    bot.paper_trading = False
    bot.per_ticker_execution = False
    bot.account_number = "acct"
    bot._emit_log = lambda *a, **k: None

    calls = {"reads": 0, "cancelled": []}

    nested = [{
        "orderId": "PARENT",
        "orderLegCollection": [],
        "childOrderStrategies": [{
            "orderId": "CHILD",
            "orderLegCollection": [{
                "instrument": {"symbol": "META"}
            }],
        }],
    }]

    def open_orders(_account):
        calls["reads"] += 1
        return nested if calls["reads"] == 1 else []

    bot.schwab = SimpleNamespace(obtener_ordenes_abiertas=open_orders)
    bot.ejecutor_hibrido = SimpleNamespace(
        cancelar_orden_confirmada=lambda account, oid: (
            calls["cancelled"].append(oid)
            or {"confirmado": True, "broker_status": "CANCELED"}
        )
    )

    assert bot._asegurar_ticker_limpio("META") is True
    assert "CHILD" in calls["cancelled"]


def test_ai_veto_opposite_real_close_reaches_broker_and_keeps_local_on_failure():
    bot = ChocoletraBot.__new__(ChocoletraBot)
    bot._lock = threading.RLock()
    bot.account_number = "acct"
    order = SimpleNamespace(cantidad=1)
    pos = {
        "tipo": "BUY",
        "entrada": 100.0,
        "orden": order,
        "cantidad_resto": 1,
        "tp1_hit": False,
        "tp1_order_id": "",
        "sl_order_id": "",
        "position_id": "p1",
        "execution_mode": "REAL",
        "pnl_realizado": 0.0,
    }
    bot._posiciones = {"META": pos}
    bot._is_paper_ticker = lambda ticker, pos=None: False
    bot._position_lifecycle_context = lambda ticker, pos: {}
    bot._audit_event = lambda *a, **k: {}
    bot._emit_log = lambda *a, **k: None
    bot._asegurar_token_trading = lambda: True
    bot._asegurar_ticker_limpio = lambda ticker: True
    bot._cantidad_real_broker = lambda ticker, side: 1

    calls = []
    bot.ejecutor_hibrido = SimpleNamespace(
        liquidar_posicion=lambda *args: (
            calls.append(args)
            or {"status": "ERROR", "mensaje": "broker rejected"}
        ),
    )

    ok = bot._cerrar_posicion_impl(
        "META",
        99.0,
        motivo="señal opuesta SELL (entrada IA vetada)",
    )

    assert ok is False
    assert calls, "AI-veto close-only must still send a REAL broker close"
    assert "META" in bot._posiciones, "unconfirmed REAL close must stay tracked"


def test_per_ticker_performance_counts_only_closes_for_pnl_and_combines_floating():
    rows = build_ticker_performance(
        ["META", "NVDA"],
        session_date="2026-08-21",
        signal_rows=[
            {"date": "2026-08-21", "ticker": "META", "tipo": "BUY", "ai_decision": "APPROVED"},
            {"date": "2026-08-21", "ticker": "META", "tipo": "SELL", "ai_decision": "VETO"},
            {"date": "2026-08-21", "ticker": "NVDA", "tipo": "SELL", "ai_decision": "APPROVED"},
            {"date": "2026-08-20", "ticker": "META", "tipo": "BUY", "ai_decision": "APPROVED"},
        ],
        history_rows=[
            {"fecha": "2026-08-21", "ticker": "META", "evento": "POSITION_OPEN", "pnl_usd": 999},
            {"fecha": "2026-08-21", "ticker": "META", "evento": "POSITION_CLOSE", "pnl_usd": 12.5},
            {"fecha": "2026-08-21", "ticker": "NVDA", "evento": "POSITION_CLOSE", "pnl_usd": -4},
            {"fecha": "2026-08-20", "ticker": "META", "evento": "POSITION_CLOSE", "pnl_usd": 100},
        ],
        positions=[
            {"ticker": "META", "status": "OPEN", "floating_pnl": -2.0},
        ],
        execution_rows=[
            {"ticker": "META", "effective_mode": "REAL", "shares": 1},
            {"ticker": "NVDA", "effective_mode": "REAL", "shares": 1},
        ],
    )
    by_ticker = {row["ticker"]: row for row in rows}
    assert by_ticker["META"]["signals"] == 2
    assert by_ticker["META"]["vetoed_signals"] == 1
    assert by_ticker["META"]["closed_trades"] == 1
    assert by_ticker["META"]["realized_pnl"] == 12.5
    assert by_ticker["META"]["unrealized_pnl"] == -2.0
    assert by_ticker["META"]["total_pnl"] == 10.5
    assert by_ticker["NVDA"]["realized_pnl"] == -4.0


def test_dashboard_contains_mobile_chart_and_per_ticker_pnl_features():
    root = Path(__file__).resolve().parents[1]
    html = (root / "dashboard" / "index.html").read_text(encoding="utf-8")
    server = (root / "server.py").read_text(encoding="utf-8")
    engine = (root / "engine" / "bot_engine.py").read_text(encoding="utf-8")

    assert "Rendimiento por ticker · hoy" in html
    assert "fetchTickerPerformance" in html
    assert "renderedWidth" in html
    assert "orientationchange" in html
    assert "position-level-label" in html
    assert "/api/ticker-performance" in server
    assert "flat_ordenes = list(self._iter_broker_orders(ordenes))" in engine
    assert "Cierre REAL NO confirmado en Schwab" in engine
