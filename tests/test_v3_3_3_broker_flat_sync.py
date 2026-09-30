import hashlib
import threading
from types import SimpleNamespace

from core.execution_v2 import SmartExit
from core.position_state import PositionState, PositionStateMachine
from engine.bot_engine import ChocoletraBot


class _Capacity:
    def __init__(self):
        self.released = []

    def release(self, ticker):
        self.released.append(ticker)


class _Executor:
    def __init__(self):
        self.retired = []

    def retire_pending_exit_after_authoritative_flat(self, account, ticker, reason):
        self.retired.append((account, ticker, reason))
        return True


def _bot_with_positions(active=False):
    bot = object.__new__(ChocoletraBot)
    bot._activo = active
    bot._lock = threading.RLock()
    bot._posiciones = {
        "NKE": {
            "execution_mode": "REAL", "tipo": "SELL", "cantidad": 3,
            "cantidad_resto": 3, "position_id": "pid-nke",
        },
        "NVDA": {
            "execution_mode": "REAL", "tipo": "BUY", "cantidad": 3,
            "cantidad_resto": 3, "position_id": "pid-nvda",
        },
        "META": {
            "execution_mode": "REAL", "tipo": "BUY", "cantidad": 3,
            "cantidad_resto": 3, "position_id": "pid-meta",
        },
        "AAPL": {
            "execution_mode": "PAPER", "tipo": "BUY", "cantidad": 20,
            "cantidad_resto": 20, "position_id": "pid-paper",
        },
    }
    bot.position_states = PositionStateMachine()
    bot.position_states.adopt("NKE", "SELL", "pid-nke")
    bot.position_states.adopt("NVDA", "BUY", "pid-nvda")
    bot.position_states.adopt("META", "BUY", "pid-meta")
    bot.position_states.adopt("AAPL", "BUY", "pid-paper")
    bot.real_capacity = _Capacity()
    bot.ejecutor_hibrido = _Executor()
    bot.account_number = "acct"
    bot._last_order_failures = {
        "NKE": {"reason": "old"},
        "NVDA": {"reason": "old"},
        "META": {"reason": "old"},
    }
    bot._broker_reconciliation = {}
    bot._persist_broker_reconciliation = lambda: None
    bot._audit_event = lambda *args, **kwargs: None
    bot._emit_log = lambda *args, **kwargs: None
    return bot


def test_stopped_flat_broker_retires_only_local_real_positions():
    bot = _bot_with_positions(active=False)
    state = {
        "ok": True,
        "flat": True,
        "message": "Broker confirmado plano",
        "positions": [],
        "open_order_symbols": [],
    }

    result = bot.synchronize_stopped_real_positions_from_broker(state)

    assert result["applied"] is True
    assert set(result["retired"]) == {"NKE", "NVDA", "META"}
    assert set(bot._posiciones) == {"AAPL"}
    assert bot.position_states.snapshot("NKE").state == PositionState.OUT
    assert bot.position_states.snapshot("NVDA").state == PositionState.OUT
    assert bot.position_states.snapshot("META").state == PositionState.OUT
    assert bot.position_states.snapshot("AAPL").state == PositionState.LONG
    assert set(bot.real_capacity.released) == {"NKE", "NVDA", "META"}
    assert {row[1] for row in bot.ejecutor_hibrido.retired} == {"NKE", "NVDA", "META"}
    assert bot._last_order_failures == {}


def test_known_broker_exposure_and_working_order_are_not_retired():
    bot = _bot_with_positions(active=False)
    state = {
        "ok": True,
        "flat": False,
        "message": "Broker sincronizado; exposición REAL conocida",
        "positions": [{"ticker": "NVDA", "side": "BUY", "quantity": 3}],
        "open_order_symbols": ["NKE"],
    }

    result = bot.synchronize_stopped_real_positions_from_broker(state)

    assert set(result["retired"]) == {"META"}
    assert set(result["kept"]) == {"NKE", "NVDA"}
    assert set(bot._posiciones) == {"NKE", "NVDA", "AAPL"}
    assert "META" not in bot._last_order_failures
    assert "NKE" in bot._last_order_failures
    assert "NVDA" in bot._last_order_failures


def test_active_engine_never_mutates_local_positions_from_manual_read_only_sync():
    bot = _bot_with_positions(active=True)
    before = set(bot._posiciones)
    result = bot.synchronize_stopped_real_positions_from_broker({
        "ok": True, "flat": True, "positions": [], "open_order_symbols": []
    })
    assert result["applied"] is False
    assert result["reason"] == "ENGINE_ACTIVE"
    assert set(bot._posiciones) == before


def test_failed_broker_read_never_mutates_local_positions():
    bot = _bot_with_positions(active=False)
    before = set(bot._posiciones)
    result = bot.synchronize_stopped_real_positions_from_broker({
        "ok": False, "flat": False, "positions": [], "open_order_symbols": []
    })
    assert result["applied"] is False
    assert result["reason"] == "BROKER_NOT_CONFIRMED"
    assert set(bot._posiciones) == before


class _PendingAuditExecutor:
    def __init__(self, tmp_path):
        self.audit = []
        self.config = {
            "pending_path": str(tmp_path / "pending.json"),
            "history_path": str(tmp_path / "history.jsonl"),
        }

    def _audit(self, event, **fields):
        self.audit.append((event, fields))


def test_authoritative_flat_retires_durable_pending_exit_without_broker_mutation(tmp_path):
    executor = _PendingAuditExecutor(tmp_path)
    smart = SmartExit(executor, executor.config)
    key = hashlib.sha256(b"acct").hexdigest()[:16] + ":NKE"
    smart.pending.put(key, {
        "execution_id": "old", "ticker": "NKE", "side": "SELL",
        "quantity": 3, "reason": "old exit", "created_utc": "old",
        "legs": [{"order_id": "123", "status": "DESCONOCIDO"}],
    })

    assert smart.retire_pending_after_authoritative_flat("acct", "NKE") is True
    assert smart.pending.get(key) is None
    assert any(event == "STALE_EXIT_RETIRED_BROKER_FLAT" for event, _ in executor.audit)
    # Idempotent: already retired means no second audit/action.
    assert smart.retire_pending_after_authoritative_flat("acct", "NKE") is False
