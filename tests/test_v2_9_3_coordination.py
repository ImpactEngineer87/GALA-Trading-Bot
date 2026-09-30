from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from agents.contracts import DecisionFinal, PropuestaOperacion
from agents.feedback_store import FeedbackStore
from core.position_state import PositionState, PositionStateSnapshot
from engine.bot_engine import ChocoletraBot


def _decision(ticker: str, side: str) -> DecisionFinal:
    entry = 100.0
    sl = 99.0 if side == "BUY" else 101.0
    tp = 104.0 if side == "BUY" else 96.0
    prop = PropuestaOperacion(
        ticker=ticker,
        tipo=side,
        precio_entrada=entry,
        stop_loss=sl,
        take_profit=tp,
        ratio_rb=4.0,
        timestamp=datetime(2026, 8, 18, 10, 0),
    )
    return DecisionFinal(
        ticker=ticker,
        operar=True,
        tipo=side,
        propuesta=prop,
        timestamp=datetime(2026, 8, 18, 10, 0),
    )


def _entry_lifecycle(side: str, mode: str, suffix: str) -> dict:
    position = "LONG" if side == "BUY" else "SHORT"
    action = "BUY" if side == "BUY" else "SELL_SHORT"
    return {
        "execution_id": f"exec-{suffix}",
        "position_id": f"pos-{suffix}",
        "signal_id": f"sig-{suffix}",
        "correlation_id": f"corr-{suffix}",
        "reversal_id": "",
        "modo": mode,
        "entry_order_id": f"entry-{suffix}",
        "entry_confirmed": True,
        "state_before": "OUT",
        "state_after": position,
        "position_side": position,
        "signal_side": side,
        "entry_action": action,
        "audit_linked": True,
        "history_linked": True,
        "slippage_linked": True,
    }


def _close_lifecycle(side: str, mode: str, suffix: str) -> dict:
    position = "LONG" if side == "BUY" else "SHORT"
    action = "SELL" if side == "BUY" else "BUY_TO_COVER"
    return {
        "signal_id": f"exit-sig-{suffix}",
        "correlation_id": f"corr-{suffix}",
        "reversal_id": "",
        "modo": mode,
        "exit_order_id": f"exit-{suffix}",
        "exit_confirmed": True,
        "state_before": position,
        "state_after": "OUT",
        "position_side": position,
        "exit_action": action,
        "audit_linked": True,
        "history_linked": True,
    }


def test_paper_and_real_share_one_valid_learning_counter(tmp_path: Path):
    store = FeedbackStore(str(tmp_path / "feedback.csv"))

    d1 = _decision("META", "BUY")
    store.abrir(d1, 100.0, 99.0, lifecycle=_entry_lifecycle("BUY", "PAPER", "p"))
    r1 = store.cerrar("META", 102.0, lifecycle=_close_lifecycle("BUY", "PAPER", "p"))
    assert r1 and int(r1["execution_valid"]) == 1
    assert r1["modo"] == "PAPER"
    assert store.total_cerradas_ticker("META") == 1

    d2 = _decision("META", "BUY")
    store.abrir(d2, 100.0, 99.0, lifecycle=_entry_lifecycle("BUY", "REAL", "r"))
    r2 = store.cerrar("META", 103.0, lifecycle=_close_lifecycle("BUY", "REAL", "r"))
    assert r2 and int(r2["execution_valid"]) == 1
    assert r2["modo"] == "REAL"
    assert store.total_cerradas_ticker("META") == 2


def test_open_feedback_survives_restart(tmp_path: Path):
    path = tmp_path / "feedback.csv"
    first = FeedbackStore(str(path))
    d = _decision("NVDA", "SELL")
    first.abrir(d, 100.0, 101.0, lifecycle=_entry_lifecycle("SELL", "PAPER", "restart"))

    sidecar = Path(str(path) + ".open.json")
    assert sidecar.exists()
    assert "NVDA" in json.loads(sidecar.read_text(encoding="utf-8"))["open"]

    # Simulate server restart: a fresh store has no DecisionFinal object in RAM.
    second = FeedbackStore(str(path))
    assert second.tiene_abierta("NVDA")
    row = second.cerrar(
        "NVDA", 98.0,
        lifecycle=_close_lifecycle("SELL", "PAPER", "restart"),
    )
    assert row and int(row["execution_valid"]) == 1
    assert row["tipo"] == "SELL"
    assert second.total_cerradas_ticker("NVDA") == 1
    assert "NVDA" not in json.loads(sidecar.read_text(encoding="utf-8"))["open"]


def test_real_long_only_sell_closes_long_without_opening_short():
    bot = ChocoletraBot.__new__(ChocoletraBot)
    bot._lock = threading.RLock()
    bot._activo = True
    bot._posiciones = {
        "META": {
            "tipo": "BUY",
            "position_id": "pos-1",
            "execution_mode": "REAL",
        }
    }
    bot._reversal_contexts = {}
    bot._resumen = {"reversiones": 0}
    bot.reversion_activa = True
    bot.live_allow_short = False
    bot._daily_blocked = lambda: False
    bot._state_snapshot = lambda ticker: PositionStateSnapshot(
        ticker, PositionState.LONG, "pos-1"
    )
    bot._emit_log = lambda *args, **kwargs: None
    bot._audit_event = lambda *args, **kwargs: {}
    bot._is_paper_ticker = lambda ticker, pos=None: False

    closed = []
    def _close(ticker, price, motivo=""):
        closed.append((ticker, motivo))
        bot._posiciones.pop(ticker, None)
        return True
    bot._cerrar_posicion = _close

    # If the method ever tries to open the opposite SHORT, this test must fail.
    bot._reservar_slot = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("long-only SELL must stop at OUT")
    )

    decision = SimpleNamespace(
        ticker="META", tipo="SELL", signal_id="sig-sell",
        reversal_id="", correlation_id="",
    )
    bot._abrir_o_revertir(decision, 99.5, None)

    assert closed
    assert "META" not in bot._posiciones
    assert bot._resumen["reversiones"] == 1


def test_patch_contains_real_sell_entry_path_when_shorts_enabled():
    # Regression guard for the original v2.9.2 problem: SELL must not be
    # unconditionally blocked merely because a ticker is REAL.
    source = Path(__file__).parents[1] / "engine" / "bot_engine.py"
    text = source.read_text(encoding="utf-8")
    assert "and not self.live_allow_short" in text
    assert "and not _existing_long" in text
    assert "IA vetó la nueva entrada" in text
