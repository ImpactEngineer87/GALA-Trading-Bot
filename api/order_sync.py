"""
Asynchronous polling primitives for the Schwab order lifecycle.

The trading engine itself is synchronous and runs one worker per ticker.  These
helpers keep broker polling non-blocking internally (``asyncio.to_thread``) and
give the synchronous engine one explicit, testable result for every transition.
An HTTP 2xx response is only an acknowledgement; it is never treated as proof
that an order has reached its requested broker state.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Collection, Dict, Optional


UNKNOWN_STATUS = "DESCONOCIDO"


def normalize_status(value: Any) -> str:
    text = str(value or UNKNOWN_STATUS).strip().upper()
    return text or UNKNOWN_STATUS


def valid_order_snapshot(value: Any) -> bool:
    """Require complete broker order records before trusting list membership.

    An empty list is a valid snapshot. A malformed response is unknown, so it
    must never become evidence that no order is reserving or closing shares.
    Nested order strategies need the same identity and status checks as roots.
    """
    if not isinstance(value, list):
        return False
    for order in value:
        if not isinstance(order, dict):
            return False
        order_id = order.get("orderId")
        if isinstance(order_id, str):
            if not order_id.strip():
                return False
        elif type(order_id) is not int or order_id <= 0:
            return False
        status = order.get("status")
        if not isinstance(status, str) or not status.strip():
            return False
        children = order.get("childOrderStrategies")
        if children is not None and not valid_order_snapshot(children):
            return False
    return True


@dataclass(frozen=True)
class OrderWaitResult:
    """Final observation produced by a broker polling operation."""

    status: str
    confirmed: bool
    timed_out: bool
    elapsed_ms: int
    filled: float = 0.0
    avg_price: float = 0.0
    detail: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class ValueWaitResult:
    """Result of polling an arbitrary broker value (for example position size)."""

    confirmed: bool
    timed_out: bool
    elapsed_ms: int
    value: Any = None
    error: str = ""


class AsyncOrderSynchronizer:
    """
    Polls blocking REST callbacks without blocking an asyncio event loop.

    Public ``run_*`` methods are synchronous adapters for the current threaded
    engine.  The async methods are intentionally public as well so a future
    native-async engine can await the exact same state machine.
    """

    def __init__(self, poll_interval_ms: int = 100):
        self.poll_interval = max(0.025, float(poll_interval_ms) / 1000.0)

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int(round((time.monotonic() - started) * 1000.0))

    async def wait_for_order(
        self,
        detail_getter: Callable[[], Dict[str, Any]],
        desired_statuses: Collection[str],
        failure_statuses: Collection[str] = (),
        timeout_s: float = 2.5,
    ) -> OrderWaitResult:
        desired = {normalize_status(s) for s in desired_statuses}
        failures = {normalize_status(s) for s in failure_statuses}
        timeout_s = max(0.0, float(timeout_s))
        started = time.monotonic()
        deadline = started + timeout_s
        last: Dict[str, Any] = {}
        last_error = ""

        while True:
            try:
                raw = await asyncio.to_thread(detail_getter)
                last = raw if isinstance(raw, dict) else {}
                last_error = ""
            except Exception as exc:  # broker/network error: retry until deadline
                last = {}
                last_error = str(exc)

            status = normalize_status(last.get("status"))
            filled = float(last.get("filled", 0.0) or 0.0)
            avg_price = float(last.get("avg_price", 0.0) or 0.0)
            elapsed = self._elapsed_ms(started)

            if status in desired:
                return OrderWaitResult(
                    status=status,
                    confirmed=True,
                    timed_out=False,
                    elapsed_ms=elapsed,
                    filled=filled,
                    avg_price=avg_price,
                    detail=last,
                )
            if status in failures:
                return OrderWaitResult(
                    status=status,
                    confirmed=False,
                    timed_out=False,
                    elapsed_ms=elapsed,
                    filled=filled,
                    avg_price=avg_price,
                    detail=last,
                    error=f"broker terminal status: {status}",
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return OrderWaitResult(
                    status=status,
                    confirmed=False,
                    timed_out=True,
                    elapsed_ms=elapsed,
                    filled=filled,
                    avg_price=avg_price,
                    detail=last,
                    error=last_error or f"timeout waiting for {sorted(desired)}",
                )
            await asyncio.sleep(min(self.poll_interval, remaining))

    async def wait_for_value(
        self,
        value_getter: Callable[[], Any],
        predicate: Callable[[Any], bool],
        timeout_s: float = 2.5,
    ) -> ValueWaitResult:
        timeout_s = max(0.0, float(timeout_s))
        started = time.monotonic()
        deadline = started + timeout_s
        last_value: Any = None
        last_error = ""

        while True:
            try:
                last_value = await asyncio.to_thread(value_getter)
                last_error = ""
                if predicate(last_value):
                    return ValueWaitResult(
                        confirmed=True,
                        timed_out=False,
                        elapsed_ms=self._elapsed_ms(started),
                        value=last_value,
                    )
            except Exception as exc:
                last_error = str(exc)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ValueWaitResult(
                    confirmed=False,
                    timed_out=True,
                    elapsed_ms=self._elapsed_ms(started),
                    value=last_value,
                    error=last_error or "timeout waiting for broker value",
                )
            await asyncio.sleep(min(self.poll_interval, remaining))

    @staticmethod
    def _run(coroutine):
        """
        Run from the current synchronous engine.

        Engine workers do not own asyncio loops.  Raising explicitly if that
        assumption changes prevents a hidden nested-loop failure.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        raise RuntimeError(
            "Synchronous order adapter called from an active asyncio loop; "
            "await wait_for_order/wait_for_value directly."
        )

    def run_order(self, *args, **kwargs) -> OrderWaitResult:
        return self._run(self.wait_for_order(*args, **kwargs))

    def run_value(self, *args, **kwargs) -> ValueWaitResult:
        return self._run(self.wait_for_value(*args, **kwargs))
