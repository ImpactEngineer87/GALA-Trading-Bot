from __future__ import annotations

import csv
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from agents.veto_observation_store import VetoObservationStore
from engine.bot_engine import ChocoletraBot


def test_veto_summary_is_separate_and_deduplicates_veto_plus_resolution(tmp_path):
    path = tmp_path / "veto.csv"
    store = VetoObservationStore(str(path), enabled=True, max_bars=2)
    row = store.record_veto(
        ticker="BA", tipo="SELL", probability=0.30, threshold=0.56,
        entry_price=200.0, stop_loss=202.0, take_profit=196.0,
        execution_mode="REAL", veto_reason="AI_THRESHOLD",
        signal_id="sig-1", timestamp="2026-08-18T10:00:00-04:00",
    )
    assert row is not None
    # SELL hits TP => hypothetical winner => missed/bad veto.
    resolved = store.update_bar(
        "BA", high=200.5, low=195.5, close=196.0,
        bar_timestamp="2026-08-18T10:01:00-04:00",
    )
    assert len(resolved) == 1
    summary = store.summary_by_ticker(["BA"])[0]
    assert summary["veto_total"] == 1
    assert summary["veto_resolved"] == 1
    assert summary["veto_bad"] == 1
    assert summary["veto_good"] == 0
    assert summary["veto_open"] == 0


def _bot_for_reconciliation(tmp_path, positions, orders):
    bot = ChocoletraBot.__new__(ChocoletraBot)
    bot.tickers = ["META", "NVDA", "BA"]
    bot.account_number = "HASH"
    bot._broker_reconciliation_state_path = str(tmp_path / "broker_state.json")
    bot._broker_reconciliation_ok = True
    bot._broker_reconciliation = {}
    bot._last_order_failures = {}
    bot._real_tickers = lambda tickers: ["META", "NVDA"]
    bot._emit_log = lambda *a, **k: None
    bot.schwab = SimpleNamespace(
        obtener_posiciones=lambda account: positions,
        obtener_ordenes_abiertas=lambda account: orders,
    )
    return bot


def test_refresh_reconciliation_requires_true_broker_flat_for_kill_switch(tmp_path):
    bot = _bot_for_reconciliation(
        tmp_path,
        positions=[{"symbol": "META", "tipo": "SELL", "cantidad": 1, "precio": 500}],
        orders=[],
    )
    state = bot._refresh_broker_reconciliation(context="KILL_SWITCH", require_flat=True)
    assert state["ok"] is False
    assert state["flat"] is False
    assert state["positions"] == [{"ticker": "META", "side": "SELL", "quantity": 1}]
    persisted = json.loads(Path(bot._broker_reconciliation_state_path).read_text(encoding="utf-8"))
    assert persisted["ok"] is False


def test_refresh_reconciliation_passes_when_schwab_is_flat(tmp_path):
    bot = _bot_for_reconciliation(tmp_path, positions=[], orders=[])
    state = bot._refresh_broker_reconciliation(context="KILL_SWITCH", require_flat=True)
    assert state["ok"] is True
    assert state["flat"] is True


def test_pre_entry_recovers_when_startup_reconciliation_was_transient(tmp_path):
    bot = _bot_for_reconciliation(tmp_path, positions=[], orders=[])
    bot.config = {"live_trading": {"reconciliation_retry_attempts": 3, "reconciliation_retry_delay_sec": 0}}
    bot._broker_reconciliation_ok = False
    bot._broker_reconciliation = {"message": "startup failed"}
    assert bot._confirm_real_ticker_flat_before_entry("META") is True
    assert bot._broker_reconciliation_ok is True


def test_pre_entry_detects_hidden_broker_position_and_persists_block(tmp_path):
    bot = _bot_for_reconciliation(
        tmp_path,
        positions=[{"symbol": "NVDA", "tipo": "BUY", "cantidad": 1, "precio": 180}],
        orders=[],
    )
    assert bot._confirm_real_ticker_flat_before_entry("NVDA") is False
    assert bot._broker_reconciliation_ok is False
    assert "NVDA no está plano" in bot._broker_reconciliation["message"]


def test_dashboard_contains_veto_counter_and_broker_sync_panel():
    html = (Path(__file__).resolve().parents[1] / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "Vetadas IA" in html
    assert "brokerSyncCard" in html
    assert "Últimos rechazos" in html
    assert "no incrementan x/50" in html or "no incrementa x/50" in html
