import json
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from core.runtime_reliability import (
    effective_ai_threshold,
    infer_closed_bar_epoch_ms,
    marker_within_1m_candle_span,
)
from engine.bot_engine import ChocoletraBot


def _ms(text):
    dt = datetime.fromisoformat(text).replace(tzinfo=ZoneInfo("America/New_York"))
    return int(dt.timestamp() * 1000)


def test_final_candle_includes_signal_seconds_inside_same_minute():
    first = _ms("2026-09-18T13:30:00")
    last = _ms("2026-09-18T13:34:00")
    assert marker_within_1m_candle_span(
        _ms("2026-09-18T13:34:04"), first, last
    )
    assert marker_within_1m_candle_span(
        _ms("2026-09-18T13:34:59.999000"), first, last
    )
    assert not marker_within_1m_candle_span(
        _ms("2026-09-18T13:35:00"), first, last
    )


def test_legacy_signal_is_aligned_to_the_closed_bar_that_generated_it():
    # Actual incident shape: decision emitted at 13:34:04 after evaluating the
    # 13:33:00 candle once it had fully closed.
    decision = _ms("2026-09-18T13:34:04")
    source_bar = infer_closed_bar_epoch_ms(decision)
    assert source_bar == _ms("2026-09-18T13:33:00")
    assert marker_within_1m_candle_span(
        source_bar, _ms("2026-09-18T13:30:00"), _ms("2026-09-18T13:33:00")
    )


def test_shadow_decision_threshold_wins_over_production_fallback():
    assert effective_ai_threshold({"threshold": 0.41}, 0.0) == 0.41
    assert effective_ai_threshold({"decision_threshold": "0.62"}, 0.0) == 0.62
    assert effective_ai_threshold({}, 0.59) == 0.59


def test_detener_is_idempotent_when_two_threads_request_stop():
    bot = object.__new__(ChocoletraBot)
    bot._stop_lock = threading.RLock()
    bot._stop_completed = False
    bot._fin_sesion_alcanzado = False
    calls = []

    def fake_impl(fin_sesion=None):
        calls.append(fin_sesion)
        time.sleep(0.03)

    bot._detener_impl = fake_impl
    threads = [
        threading.Thread(target=bot.detener, kwargs={"fin_sesion": False}),
        threading.Thread(target=bot.detener, kwargs={"fin_sesion": False}),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert calls == [False]
    assert bot._stop_completed is True


def test_broker_reconciliation_concurrent_persistence_uses_valid_atomic_json(tmp_path):
    bot = object.__new__(ChocoletraBot)
    target = tmp_path / "broker_reconciliation_state.json"
    bot._broker_reconciliation_state_path = str(target)
    bot._broker_reconciliation_write_lock = threading.RLock()
    bot._broker_reconciliation = {
        "ok": False,
        "flat": False,
        "context": "TEST",
        "message": "pending",
    }
    bot._last_order_failures = {"UAL": {"reason": "test"}}
    warnings = []
    bot._emit_log = lambda level, message: warnings.append((level, message))

    threads = [threading.Thread(target=bot._persist_broker_reconciliation) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["context"] == "TEST"
    assert payload["last_order_failures"]["UAL"]["reason"] == "test"
    assert warnings == []
    assert not list(tmp_path.glob("*.tmp"))
