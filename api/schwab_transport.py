"""Shared Schwab request pause, following Trader API support's 403 guidance.

One bot process owns the session. Its HTTP callers share this gate; a small
credential-free runtime file preserves the pause when that process restarts.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests


MIN_FORBIDDEN_PAUSE_SECONDS = 10 * 60
MAX_FORBIDDEN_PAUSE_SECONDS = 60 * 60
DEFAULT_STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "schwab_request_cooldown.json"
_LOG = logging.getLogger(__name__)


class SchwabRequestPaused(requests.RequestException):
    """A request was stopped locally; no HTTP request/order was submitted."""

    def __init__(self, seconds: int, *, recovery_check: bool = False):
        self.retry_after_seconds = max(1, int(seconds))
        self.request_sent = False
        self.recovery_check = recovery_check
        message = (
            "Schwab en pausa: espere una verificación de conexión antes de enviar órdenes."
            if recovery_check else
            f"Schwab en pausa tras HTTP 403; próximo intento permitido en {self.retry_after_seconds}s."
        )
        super().__init__(message)


class SchwabRequestGate:
    def __init__(self, state_path=DEFAULT_STATE_PATH, *, clock=None, monotonic=None):
        self.state_path = Path(state_path)
        self._clock = clock or time.time
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.RLock()
        self._until = 0.0
        self._deadline = 0.0
        self._denials = 0
        self._recovery_pending = False
        self._probe_inflight = False
        self._persistence_error = ""
        self._load()

    def _load(self):
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            until = float(state.get("retry_at_epoch", 0))
            if not math.isfinite(until):
                raise ValueError("invalid pause deadline")
            self._denials = max(0, min(8, int(state.get("denial_count", 0))))
            self._recovery_pending = bool(state.get("recovery_pending"))
            self._until = until
            self._deadline = self._monotonic() + max(0, until - self._clock())
        except FileNotFoundError:
            return
        except (OSError, TypeError, ValueError, AttributeError):
            # A damaged/unreadable existing record must not turn a restart into
            # an immediate retry. Keep the file and explain the storage issue.
            self._persistence_error = "No se pudo leer el estado de espera Schwab."
            self._denials = 1
            self._recovery_pending = True
            self._until = self._clock() + MIN_FORBIDDEN_PAUSE_SECONDS
            self._deadline = self._monotonic() + MIN_FORBIDDEN_PAUSE_SECONDS
            _LOG.warning(self._persistence_error)

    def _save(self):
        temporary = None
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "retry_at_epoch": self._until,
                "denial_count": self._denials,
                "recovery_pending": self._recovery_pending,
            }
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.state_path.parent,
                prefix=".schwab-cooldown-", suffix=".tmp", delete=False,
            ) as stream:
                temporary = stream.name
                json.dump(state, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            temporary = None
            self._persistence_error = ""
        except OSError:
            self._persistence_error = (
                "La espera Schwab sigue activa, pero no pudo guardarse. "
                "Revise permisos de data antes de reiniciar el bot."
            )
            _LOG.warning(self._persistence_error)
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def remaining(self) -> int:
        with self._lock:
            return max(0, math.ceil(self._deadline - self._monotonic()))

    def snapshot(self) -> dict:
        with self._lock:
            remaining = self.remaining()
            return {
                "paused": remaining > 0 or self._probe_inflight,
                "retry_after_seconds": max(remaining, int(self._probe_inflight)),
                "next_retry_at_utc": (
                    datetime.fromtimestamp(self._until, timezone.utc).isoformat(timespec="seconds")
                    if remaining else None
                ),
                "denial_count": self._denials,
                "recovery_pending": self._recovery_pending,
                "persistence_error": self._persistence_error or None,
            }

    def _pause(self, *, new_denial: bool = False, retry_after=0):
        if new_denial:
            self._denials = min(8, self._denials + 1)
        delay = min(MAX_FORBIDDEN_PAUSE_SECONDS,
                    MIN_FORBIDDEN_PAUSE_SECONDS * 2 ** max(0, self._denials - 1))
        try:
            delay = max(delay, int(retry_after or 0))
        except (TypeError, ValueError):
            pass
        self._until = max(self._until, self._clock() + delay)
        self._deadline = max(self._deadline, self._monotonic() + delay)
        self._recovery_pending = True
        self._save()

    def request(self, send, url: str, *, recovery_safe: bool = True, **kwargs):
        if (urlsplit(str(url)).hostname or "").lower() != "api.schwabapi.com":
            return send(url, **kwargs)
        with self._lock:
            remaining = self.remaining()
            if remaining or self._probe_inflight:
                raise SchwabRequestPaused(remaining or 1)
            probe = self._recovery_pending
            if probe and not recovery_safe:
                raise SchwabRequestPaused(1, recovery_check=True)
            if probe:
                self._probe_inflight = True
        try:
            response = send(url, **kwargs)
            status = int(getattr(response, "status_code", 0) or 0)
            with self._lock:
                if status == 403:
                    self._pause(new_denial=True, retry_after=response.headers.get("Retry-After"))
                elif probe and (status == 0 or status == 429 or status >= 500):
                    # A failed recovery attempt must not create a busy loop.
                    self._pause(retry_after=response.headers.get("Retry-After"))
                elif probe and 200 <= status < 500 and not self.remaining():
                    # This confirms an HTTP route only, not credentials or a
                    # broker position. Never override a newer concurrent 403.
                    self._until = 0.0
                    self._deadline = 0.0
                    self._denials = 0
                    self._recovery_pending = False
                    self._save()
            return response
        except requests.RequestException:
            if probe:
                with self._lock:
                    self._pause()
            raise
        finally:
            if probe:
                with self._lock:
                    self._probe_inflight = False


_GATE = SchwabRequestGate()


def schwab_request(send, url: str, *, recovery_safe: bool = True, **kwargs):
    return _GATE.request(send, url, recovery_safe=recovery_safe, **kwargs)


def schwab_request_pause() -> dict:
    return _GATE.snapshot()
