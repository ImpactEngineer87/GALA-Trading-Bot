from __future__ import annotations

import csv
import threading
from pathlib import Path

from agents.feedback_store import FeedbackStore
from agents.veto_observation_store import VetoObservationStore
from engine.bot_engine import ChocoletraBot


def _slot_bot() -> ChocoletraBot:
    bot = ChocoletraBot.__new__(ChocoletraBot)
    bot._lock = threading.RLock()
    bot._activo = True
    bot._posiciones = {}
    bot.max_paper_positions = 15
    bot.max_real_positions = 2
    bot.max_total_positions = 17
    bot._daily_blocked = lambda: False
    bot._emit_log = lambda *args, **kwargs: None
    bot.execution_mode_for = lambda ticker: (
        "REAL" if str(ticker).startswith("R") else "PAPER"
    )
    bot._is_paper_ticker = lambda ticker, pos=None: (
        str((pos or {}).get("execution_mode", "PAPER")).upper() == "PAPER"
    )
    return bot


def test_fifteen_paper_slots_plus_two_real_slots_are_independent():
    bot = _slot_bot()

    for i in range(15):
        assert bot._reservar_slot(f"P{i}", "BUY") is True

    # A full PAPER book does not consume either REAL slot.
    assert bot._reservar_slot("R0", "BUY") is True
    assert bot._reservar_slot("R1", "SELL") is True
    assert len(bot._posiciones) == 17

    assert bot._reservar_slot("P15", "BUY") is False
    assert bot._reservar_slot("R2", "BUY") is False


def test_two_real_slots_do_not_reduce_paper_capacity():
    bot = _slot_bot()
    assert bot._reservar_slot("R0", "BUY") is True
    assert bot._reservar_slot("R1", "SELL") is True
    for i in range(15):
        assert bot._reservar_slot(f"P{i}", "BUY") is True
    assert len(bot._posiciones) == 17


def test_ai_veto_audit_is_separate_from_x50_feedback(tmp_path: Path):
    feedback = FeedbackStore(str(tmp_path / "agentes_feedback.csv"))
    veto = VetoObservationStore(
        str(tmp_path / "ai_veto_observations.csv"), max_bars=10
    )

    row = veto.record_veto(
        ticker="META",
        tipo="BUY",
        probability=0.45,
        threshold=0.56,
        entry_price=100.0,
        stop_loss=99.0,
        take_profit=102.0,
        execution_mode="REAL",
        veto_reason="AI_THRESHOLD",
        signal_id="sig-veto-1",
        timestamp="2026-08-18 10:00:00",
    )
    assert row is not None
    assert veto.active_count("META") == 1
    # Merely observing/replaying a veto must never create a verified trade.
    assert feedback.total_cerradas_ticker("META") == 0

    assert veto.update_bar(
        "META", high=100.8, low=99.4, close=100.4,
        bar_timestamp="2026-08-18 10:01:00",
    ) == []
    resolved = veto.update_bar(
        "META", high=102.1, low=100.0, close=101.9,
        bar_timestamp="2026-08-18 10:02:00",
    )
    assert len(resolved) == 1
    assert resolved[0]["resolution"] == "TP"
    assert resolved[0]["ganadora"] == 1
    assert veto.active_count("META") == 0
    assert feedback.total_cerradas_ticker("META") == 0

    with (tmp_path / "ai_veto_observations.csv").open(
        "r", encoding="utf-8", newline=""
    ) as fh:
        rows = list(csv.DictReader(fh))
    assert [r["event"] for r in rows] == ["VETO", "RESOLVED"]
    assert all(r["notes"].startswith("audit_only") for r in rows)


def test_ai_veto_open_audit_survives_restart(tmp_path: Path):
    path = tmp_path / "ai_veto_observations.csv"
    first = VetoObservationStore(str(path), max_bars=10)
    first.record_veto(
        ticker="NVDA",
        tipo="SELL",
        probability=0.49,
        threshold=0.59,
        entry_price=180.0,
        stop_loss=181.0,
        take_profit=178.0,
        execution_mode="REAL",
        veto_reason="AI_THRESHOLD",
        signal_id="sig-veto-2",
        timestamp="2026-08-18 11:00:00",
    )
    assert first.active_count("NVDA") == 1

    second = VetoObservationStore(str(path), max_bars=10)
    assert second.active_count("NVDA") == 1
    resolved = second.update_bar(
        "NVDA", high=180.2, low=177.8, close=178.1,
        bar_timestamp="2026-08-18 11:01:00",
    )
    assert len(resolved) == 1
    assert resolved[0]["resolution"] == "TP"
    assert second.active_count("NVDA") == 0


def test_engine_records_ai_veto_without_opening_feedback():
    source = Path(__file__).parents[1] / "engine" / "bot_engine.py"
    text = source.read_text(encoding="utf-8")
    assert "def _registrar_veto_ia" in text
    assert text.count("self._registrar_veto_ia(") >= 2
    assert "audit-only, no suma x/50" in text
