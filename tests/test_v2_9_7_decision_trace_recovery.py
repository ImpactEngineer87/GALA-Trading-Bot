from pathlib import Path
from types import SimpleNamespace

from engine.bot_engine import ChocoletraBot


def _bot(tmp_path):
    bot = ChocoletraBot.__new__(ChocoletraBot)
    bot.config = {
        "live_trading": {
            "reconciliation_retry_attempts": 3,
            "reconciliation_retry_delay_sec": 0,
        }
    }
    bot.tickers = ["META", "NVDA"]
    bot.account_number = "HASH"
    bot._broker_reconciliation_state_path = str(tmp_path / "broker_state.json")
    bot._broker_reconciliation_ok = False
    bot._broker_reconciliation = {"message": "old transient failure"}
    bot._last_order_failures = {}
    bot._real_tickers = lambda tickers: ["META", "NVDA"]
    bot._emit_log = lambda *a, **k: None
    return bot


def test_broker_read_retry_recovers_after_transient_none(tmp_path):
    bot = _bot(tmp_path)
    calls = {"n": 0}

    def read():
        calls["n"] += 1
        return None if calls["n"] < 3 else []

    ok, value, error = bot._broker_read_with_retry("positions", read)
    assert ok is True
    assert value == []
    assert error == ""
    assert calls["n"] == 3


def test_pre_entry_stale_lock_self_heals_when_schwab_is_now_flat(tmp_path):
    bot = _bot(tmp_path)
    bot.schwab = SimpleNamespace(
        obtener_posiciones=lambda account: [],
        obtener_ordenes_abiertas=lambda account: [],
    )
    assert bot._confirm_real_ticker_flat_before_entry("META") is True
    assert bot._broker_reconciliation_ok is True


def test_pre_entry_remains_fail_closed_after_persistent_broker_failure(tmp_path):
    bot = _bot(tmp_path)
    bot.schwab = SimpleNamespace(
        obtener_posiciones=lambda account: None,
        obtener_ordenes_abiertas=lambda account: None,
    )
    assert bot._confirm_real_ticker_flat_before_entry("META") is False
    assert bot._broker_reconciliation_ok is False


def test_dashboard_explains_threshold_ba_and_manual_pending_retrain():
    html = (Path(__file__).resolve().parents[1] / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "Reentrenar pendientes" in html
    assert "Candidate BA (same test)" in html
    assert "Incumbent BA (same test)" in html
    assert "100% aquí no es una precisión permanente" in html
    assert "production_threshold" in html
    assert "ai_decision" in html


def test_server_manual_retraining_is_eligible_only_and_signal_context_is_persisted():
    source = (Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")
    assert "eligible = trainer.retraining.eligible_tickers(tickers)" in source
    assert "solo_elegibles=True" in source
    assert 'sig["ai_threshold"]' in source
    assert 'sig["ai_decision"]' in source
    assert 'sig["execution_mode"]' in source
