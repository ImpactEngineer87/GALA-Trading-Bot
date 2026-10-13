"""Shared, bounded pacing for Schwab MARKET-DATA GET requests.

Only market-data GETs go through this guard. Trading orders, cancels and
broker-position reconciliation must remain independent and fail closed.
A 403 is *not* assumed to mean rate limiting (permissions/app status
may be involved); it is never treated as automatic OAuth expiry.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import requests


class SchwabMarketBackoff(requests.RequestException):
    """Market-data calls are temporarily deferred, without making a request."""


def _setting(name: str, default: float, low: float, high: float) -> float:
    try:
        return min(high, max(low, float(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


class MarketDataGuard:
    def __init__(self, *, min_interval: Optional[float] = None):
        self.min_interval = (
            _setting("SCHWAB_MARKET_MIN_INTERVAL_SEC", 0.20, 0.0, 2.0)
            if min_interval is None else max(0.0, float(min_interval))
        )
        self._lock = threading.Lock()
        self._next_call = 0.0
        self._cooldown_until = 0.0
        self._last_rejection: dict = {}

    def acquire(self) -> None:
        """Space requests across all ticker/dashboard threads in this process."""
        with self._lock:
            now = time.monotonic()
            if now < self._cooldown_until:
                raise SchwabMarketBackoff(
                    "Schwab market-data cooldown %.1fs remaining"
                    % (self._cooldown_until - now)
                )
            scheduled = max(now, self._next_call)
            self._next_call = scheduled + self.min_interval
        wait = scheduled - now
        if wait > 0:
            time.sleep(wait)
        # A 403/429 may have arrived while this request was waiting its turn.
        with self._lock:
            remaining = self._cooldown_until - time.monotonic()
            if remaining > 0:
                raise SchwabMarketBackoff(
                    "Schwab market-data cooldown %.1fs remaining" % remaining
                )

    def observe(self, response, url: str) -> None:
        """Record safe diagnostics; never retry 403, and never rotate OAuth."""
        status = int(getattr(response, "status_code", 0) or 0)
        if status not in (403, 429):
            return
        headers = getattr(response, "headers", {}) or {}
        delay = (
            _setting("SCHWAB_MARKET_403_COOLDOWN_SEC", 20.0, 1.0, 120.0)
            if status == 403 else
            _setting("SCHWAB_MARKET_429_COOLDOWN_SEC", 30.0, 1.0, 120.0)
        )
        if status == 429:
            try:
                delay = max(delay, min(120.0, float(headers.get("Retry-After", 0))))
            except (TypeError, ValueError):
                pass
        request_id = next(
            (str(headers.get(key))[:100]
             for key in ("x-request-id", "request-id", "x-correlation-id")
             if headers.get(key)), ""
        )
        endpoint = urlparse(url).path  # do not log query/tokens/account data
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + delay)
            self._last_rejection = {
                "status": status,
                "endpoint": endpoint,
                "request_id": request_id,
                "cooldown_sec": delay,
            }
        print(
            "[Schwab market-data] HTTP %s %s; cooldown %.0fs; request_id=%s; "
            "check app permissions/OAuth and Schwab response headers."
            % (status, endpoint, delay, request_id or "N/A")
        )

    def snapshot(self) -> dict:
        with self._lock:
            return {
                **self._last_rejection,
                "cooldown_remaining_sec": round(
                    max(0.0, self._cooldown_until - time.monotonic()), 2
                ),
            }


MARKET_DATA_GUARD = MarketDataGuard()


def guarded_market_get(url: str, **kwargs):
    """Shared pacing for direct dashboard market-data requests."""
    MARKET_DATA_GUARD.acquire()
    response = requests.get(url, **kwargs)
    MARKET_DATA_GUARD.observe(response, url)
    return response
