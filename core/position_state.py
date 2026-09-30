"""Explicit three-state lifecycle for every managed ticker.

The broker position is either OUT, LONG, or SHORT.  Pending orders are not a
fourth position state: the ticker remains OUT until an entry fill is confirmed.
This keeps the state model identical to the client-approved Excel logic while
allowing the order lifecycle to retain its richer pending/cancel states.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional


class PositionState(str, Enum):
    OUT = "OUT"
    LONG = "LONG"
    SHORT = "SHORT"


SIGNAL_TO_STATE = {
    "BUY": PositionState.LONG,
    "SELL": PositionState.SHORT,
}


def state_for_signal(signal_side: str) -> PositionState:
    try:
        return SIGNAL_TO_STATE[str(signal_side or "").strip().upper()]
    except KeyError as exc:
        raise ValueError(f"Dirección de señal inválida: {signal_side}") from exc


def position_side_for_signal(signal_side: str) -> str:
    return state_for_signal(signal_side).value


def entry_action_for_state(state: PositionState | str) -> str:
    normalized = PositionState(str(getattr(state, "value", state)).upper())
    if normalized == PositionState.LONG:
        return "BUY"
    if normalized == PositionState.SHORT:
        return "SELL_SHORT"
    return ""


def exit_action_for_state(state: PositionState | str) -> str:
    normalized = PositionState(str(getattr(state, "value", state)).upper())
    if normalized == PositionState.LONG:
        return "SELL"
    if normalized == PositionState.SHORT:
        return "BUY_TO_COVER"
    return ""


@dataclass(frozen=True)
class PositionStateSnapshot:
    ticker: str
    state: PositionState
    position_id: str = ""


@dataclass(frozen=True)
class PositionTransition:
    ticker: str
    state_before: PositionState
    state_after: PositionState
    position_id: str


class InvalidPositionTransition(RuntimeError):
    pass


class PositionStateMachine:
    """Thread-safe OUT/LONG/SHORT registry with guarded transitions."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: Dict[str, PositionStateSnapshot] = {}

    @staticmethod
    def _ticker(ticker: str) -> str:
        value = str(ticker or "").strip().upper()
        if not value:
            raise ValueError("ticker vacío")
        return value

    def snapshot(self, ticker: str) -> PositionStateSnapshot:
        symbol = self._ticker(ticker)
        with self._lock:
            return self._states.get(
                symbol,
                PositionStateSnapshot(symbol, PositionState.OUT, ""),
            )

    def open(
        self,
        ticker: str,
        signal_side: str,
        position_id: Optional[str] = None,
    ) -> PositionTransition:
        symbol = self._ticker(ticker)
        target = state_for_signal(signal_side)
        pid = str(position_id or uuid.uuid4().hex)
        with self._lock:
            current = self.snapshot(symbol)
            if current.state != PositionState.OUT:
                if current.state == target and current.position_id == pid:
                    return PositionTransition(
                        symbol, current.state, current.state, current.position_id
                    )
                raise InvalidPositionTransition(
                    f"{symbol}: no se puede abrir {target.value} desde "
                    f"{current.state.value}"
                )
            self._states[symbol] = PositionStateSnapshot(symbol, target, pid)
            return PositionTransition(symbol, PositionState.OUT, target, pid)

    def close(
        self,
        ticker: str,
        *,
        expected_state: Optional[PositionState | str] = None,
        position_id: str = "",
    ) -> PositionTransition:
        symbol = self._ticker(ticker)
        expected = None
        if expected_state is not None:
            expected = PositionState(
                str(getattr(expected_state, "value", expected_state)).upper()
            )
        with self._lock:
            current = self.snapshot(symbol)
            if current.state == PositionState.OUT:
                raise InvalidPositionTransition(
                    f"{symbol}: no se puede cerrar una posición OUT"
                )
            if expected is not None and current.state != expected:
                raise InvalidPositionTransition(
                    f"{symbol}: estado {current.state.value}; se esperaba "
                    f"{expected.value}"
                )
            if position_id and current.position_id and position_id != current.position_id:
                raise InvalidPositionTransition(
                    f"{symbol}: position_id no coincide con la posición activa"
                )
            self._states.pop(symbol, None)
            return PositionTransition(
                symbol, current.state, PositionState.OUT, current.position_id
            )

    def adopt(
        self,
        ticker: str,
        signal_side: str,
        position_id: Optional[str] = None,
    ) -> PositionTransition:
        """Reconcile a broker-confirmed position after restart.

        Adoption is deliberately explicit.  It never flips a non-OUT state to
        the opposite direction; that mismatch must be investigated instead.
        """

        symbol = self._ticker(ticker)
        target = state_for_signal(signal_side)
        pid = str(position_id or uuid.uuid4().hex)
        with self._lock:
            current = self.snapshot(symbol)
            if current.state == PositionState.OUT:
                self._states[symbol] = PositionStateSnapshot(symbol, target, pid)
                return PositionTransition(symbol, PositionState.OUT, target, pid)
            if current.state == target:
                return PositionTransition(
                    symbol, current.state, current.state,
                    current.position_id or pid,
                )
            raise InvalidPositionTransition(
                f"{symbol}: broker {target.value} contradice estado local "
                f"{current.state.value}"
            )

