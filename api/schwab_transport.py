"""Shared Schwab request pause, following Trader API support's 403 guidance.

One bot process owns the session. Its HTTP callers share this gate; a small
credential-free runtime file preserves the pause when that process restarts.
"""

from __future__ import annotations

import json
import html
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import re
import tempfile
import threading
import time
from collections import Counter, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests


MIN_FORBIDDEN_PAUSE_SECONDS = 10 * 60
MAX_FORBIDDEN_PAUSE_SECONDS = 60 * 60
DEFAULT_STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "schwab_request_cooldown.json"
_LOG = logging.getLogger(__name__)
_TRAFFIC_CONTEXT = threading.local()
_DIAGNOSTIC_LOCK = threading.Lock()
_DIAGNOSTIC_HANDLER = None


class SchwabRequestDeferred(requests.RequestException):
    """Local admission/backoff refusal; the request was never submitted."""

    def __init__(self, seconds, reason):
        self.request_sent = False
        self.retry_after_seconds = max(1, math.ceil(seconds))
        self.reason = reason
        super().__init__(f"SchwabRequestDeferred: {reason}; espere {self.retry_after_seconds}s")


@contextmanager
def schwab_control_requests():
    """Reserve capacity for bounded order confirmation and reconciliation reads.

    Apply in the thread making the HTTP call, including synchronizer workers.
    The reserve does not bypass the global budget, cooldown or read backoff.
    """
    previous = getattr(_TRAFFIC_CONTEXT, "control", False)
    _TRAFFIC_CONTEXT.control = True
    try:
        yield
    finally:
        _TRAFFIC_CONTEXT.control = previous


@contextmanager
def schwab_warmup_requests(headroom, allowed=None):
    """Mark background history downloads in this thread.

    They may not use the last ``headroom`` background slots of the rolling
    minute, which stay free for per-cycle reads, and are refused while the
    ``allowed`` event is clear. OAuth and other control traffic is unaffected.
    """
    previous = getattr(_TRAFFIC_CONTEXT, "warmup", None)
    _TRAFFIC_CONTEXT.warmup = (max(0, int(headroom)), allowed)
    try:
        yield
    finally:
        _TRAFFIC_CONTEXT.warmup = previous


def _safe_endpoint(path):
    parts = path.strip("/").split("/")
    if len(parts) >= 4 and parts[:3] == ["trader", "v1", "accounts"]:
        if parts[3] == "accountNumbers":
            return "/trader/v1/accounts/accountNumbers"
        if len(parts) >= 5 and parts[4] == "orders":
            return "/trader/v1/accounts/{account}/orders" + ("/{order}" if len(parts) > 5 else "")
        return "/trader/v1/accounts/{account}"
    if path in ("/v1/oauth/token", "/trader/v1/accounts", "/trader/v1/orders",
                "/marketdata/v1/quotes", "/marketdata/v1/pricehistory"):
        return path
    return "/other"  # No arbitrary paths, query strings or identifiers in logs.


def _write_diagnostic(event):
    global _DIAGNOSTIC_HANDLER
    with _DIAGNOSTIC_LOCK:
        try:
            if _DIAGNOSTIC_HANDLER is None:
                path = DEFAULT_STATE_PATH.parents[1] / "logs" / "schwab_http_errors.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                _DIAGNOSTIC_HANDLER = RotatingFileHandler(
                    path, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")
                _DIAGNOSTIC_HANDLER.setFormatter(logging.Formatter("%(message)s"))
            record = logging.LogRecord(__name__, logging.WARNING, "", 0,
                                       json.dumps(event, ensure_ascii=True), (), None)
            _DIAGNOSTIC_HANDLER.emit(record)
        except OSError:
            _LOG.warning("No se pudo guardar el diagnóstico HTTP Schwab redactado.")


class SchwabRequestPaused(requests.RequestException):
    """A request was stopped locally; no HTTP request/order was submitted."""

    def __init__(self, seconds: int, *, recovery_check: bool = False, reason="http_403"):
        self.retry_after_seconds = max(1, int(seconds))
        self.request_sent = False
        self.recovery_check = recovery_check
        message = (
            "Schwab en pausa: espere una verificación de conexión antes de enviar órdenes."
            if recovery_check else
            f"Schwab en pausa ({reason}); próximo intento permitido en {self.retry_after_seconds}s."
        )
        super().__init__(message)


class SchwabRequestGate:
    def __init__(self, state_path=DEFAULT_STATE_PATH, *, clock=None, monotonic=None,
                 requests_per_minute=90, control_reserve=30, burst_per_second=6,
                 diagnostic_sink=None, wait=None):
        self.state_path = Path(state_path)
        self._clock = clock or time.time
        self._monotonic = monotonic or time.monotonic
        self._wait = wait or time.sleep
        self._lock = threading.RLock()
        self._until = 0.0
        self._deadline = 0.0
        self._denials = 0
        self._recovery_pending = False
        self._probe_inflight = False
        self._persistence_error = ""
        # Application defaults, NOT an assertion of Schwab's contractual quota.
        self._budget = max(1, int(requests_per_minute))
        self._reserve = max(0, min(self._budget - 1, int(control_reserve)))
        self._burst = max(1, int(burst_per_second))
        self._sent = deque()
        self._responses = deque()
        self._sent_total = 0
        self._deferred = Counter()
        self._read_failures = {}
        self._pause_reason = "http_403"
        self._diagnostic_sink = diagnostic_sink or _write_diagnostic
        self._load()

    def _load(self):
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            until = float(state.get("retry_at_epoch", 0))
            if not math.isfinite(until):
                raise ValueError("invalid pause deadline")
            self._denials = max(0, min(8, int(state.get("denial_count", 0))))
            self._recovery_pending = bool(state.get("recovery_pending"))
            self._pause_reason = "http_429" if state.get("pause_reason") == "http_429" else "http_403"
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
                "pause_reason": self._pause_reason,
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
            self._prune()
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
                "pause_reason": self._pause_reason if remaining else None,
                "request_metrics": {
                    "scope": "this_process_since_start",
                    "sent_total": self._sent_total,
                    "sent_last_60_seconds": len(self._sent),
                    "by_endpoint_last_60_seconds": dict(Counter(item[2] for item in self._sent)),
                    "responses_last_60_seconds": dict(Counter(str(item[1]) for item in self._responses)),
                    "locally_deferred_total": dict(self._deferred),
                    "budget_per_60_seconds": self._budget,
                    "control_reserve": self._reserve,
                    "burst_per_second": self._burst,
                },
            }

    def _prune(self):
        cutoff = self._monotonic() - 60
        while self._sent and self._sent[0][0] <= cutoff:
            self._sent.popleft()
        while self._responses and self._responses[0][0] <= cutoff:
            self._responses.popleft()

    def _retry_after(self, value):
        try:
            delay = float(value)
            return max(0, delay) if math.isfinite(delay) else 0
        except (ValueError, TypeError):
            try:
                return max(0, parsedate_to_datetime(value).timestamp() - self._clock())
            except (ValueError, TypeError, OverflowError):
                return 0

    def _defer(self, seconds, reason):
        self._deferred[reason] += 1
        raise SchwabRequestDeferred(seconds, reason)

    def _admit(self, path, control, read_key, warmup=None):
        self._prune()
        now = self._monotonic()
        failure = self._read_failures.get(read_key)
        if failure and failure[0] > now:
            self._defer(failure[0] - now, "order_read_backoff")
        if len(self._sent) >= self._budget:
            self._defer(self._sent[0][0] + 60 - now, "request_budget")
        background = [entry for entry in self._sent if not entry[1]]
        if not control and len(background) >= self._budget - self._reserve:
            self._defer(background[0][0] + 60 - now, "control_capacity_reserved")
        if warmup is not None and not control:
            headroom, allowed = warmup
            if allowed is not None and not allowed.is_set():
                self._defer(1, "warmup_paused")
            if len(background) >= max(1, self._budget - self._reserve - headroom):
                self._defer(background[0][0] + 60 - now, "warmup_headroom_reserved")
        recent = [entry for entry in self._sent if entry[0] > now - 1]
        background_recent = [entry for entry in recent if not entry[1]]
        if len(recent) >= self._burst:
            self._defer(recent[0][0] + 1 - now, "request_burst")
        if not control and len(background_recent) >= max(1, self._burst - 2):
            self._defer(background_recent[0][0] + 1 - now, "control_burst_reserved")
        self._sent.append((now, control, _safe_endpoint(path)))
        self._sent_total += 1

    def _read_backoff(self, key):
        if key is None:
            return
        failures = min(5, self._read_failures.get(key, (0, 0))[1] + 1)
        self._read_failures[key] = (self._monotonic() + min(60, 5 * 2 ** (failures - 1)), failures)

    def _diagnose(self, path, method, response=None, exception=None, orders_query=""):
        event = {"time_utc": datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(),
                 "endpoint": _safe_endpoint(path), "method": method,
                 "http_status": int(getattr(response, "status_code", 0) or 0)}
        if orders_query.startswith("status="):
            event["orders_query"] = orders_query    # WORKING or ALL; no identifiers
        if exception is not None:
            event["exception_type"] = type(exception).__name__
        if response is not None:
            try:
                body = response.json()
                errors = body.get("errors", []) if isinstance(body, dict) else []
                if isinstance(errors, list):
                    event["broker_statuses"] = [int(e["status"]) for e in errors[:5]
                        if isinstance(e, dict) and str(e.get("status", "")).isdigit()][:5]
                    event["correlation_ids"] = [e["id"] for e in errors[:5]
                        if isinstance(e, dict) and re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", str(e.get("id", "")))]
            except Exception:
                pass
            text = html.unescape(str(getattr(response, "text", ""))[:4096])
            ref = re.search(r"Reference\s*#\s*([0-9]+\.[0-9a-fA-F]+\.[0-9]+\.[0-9a-fA-F]+)", text)
            if ref:
                event["akamai_reference"] = ref.group(1)
        try:
            self._diagnostic_sink(event)
        except Exception:
            _LOG.warning("Falló el registro HTTP Schwab redactado.")

    def _pause(self, *, new_denial: bool = False, retry_after=0, reason=None):
        if new_denial:
            self._denials = min(8, self._denials + 1)
        reason = reason or self._pause_reason
        delay = (60 if reason == "http_429" else min(MAX_FORBIDDEN_PAUSE_SECONDS,
                    MIN_FORBIDDEN_PAUSE_SECONDS * 2 ** max(0, self._denials - 1)))
        delay = max(delay, self._retry_after(retry_after))
        if self._monotonic() + delay >= self._deadline:
            self._pause_reason = reason
        self._until = max(self._until, self._clock() + delay)
        self._deadline = max(self._deadline, self._monotonic() + delay)
        self._recovery_pending = True
        self._save()

    def request(self, send, url: str, *, recovery_safe: bool = True,
                admission_wait_seconds=0, **kwargs):
        parsed = urlsplit(str(url))
        if (parsed.hostname or "").lower() != "api.schwabapi.com":
            return send(url, **kwargs)
        method = str(getattr(send, "__name__", "REQUEST")).upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
            method = "REQUEST"
        control = (not recovery_safe or parsed.path == "/v1/oauth/token"
                   or getattr(_TRAFFIC_CONTEXT, "control", False))
        warmup = getattr(_TRAFFIC_CONTEXT, "warmup", None)
        parts = parsed.path.strip("/").split("/")
        # Order reads back off per endpoint query (single order, WORKING list,
        # full list) and per traffic class. Schwab's single-order and full-list
        # reads can fail persistently for an account while the WORKING list
        # works; a failing background poll must never pause the reads that
        # closes, the kill switch and entry checks depend on (v3.3.19).
        read_key = None
        orders_query = ""
        if (method == "GET" and len(parts) >= 5
                and parts[:3] == ["trader", "v1", "accounts"] and parts[4] == "orders"):
            if len(parts) > 5:
                orders_query = "{order}"
            else:
                params = kwargs.get("params") or {}
                orders_query = "status=" + str(params.get("status") or "ALL").upper()
            read_key = ("/".join(parts[:5]), orders_query, control)
        # Only history GETs may wait briefly for local admission. Mutations are
        # never queued/replayed; 403/429 pauses are never waited out here.
        can_wait = recovery_safe and method == "GET" and parsed.path == "/marketdata/v1/pricehistory"
        wait_seconds = max(0, min(5, float(admission_wait_seconds or 0))) if can_wait else 0
        wait_deadline = self._monotonic() + wait_seconds
        while True:
            try:
                with self._lock:
                    remaining = self.remaining()
                    if remaining or self._probe_inflight:
                        self._deferred["http_pause"] += 1
                        raise SchwabRequestPaused(remaining or 1, reason=self._pause_reason)
                    probe = self._recovery_pending
                    if probe and not recovery_safe:
                        self._deferred["recovery_required"] += 1
                        raise SchwabRequestPaused(1, recovery_check=True)
                    self._admit(parsed.path, control, read_key, warmup)
                    if probe:
                        self._probe_inflight = True
                break
            except SchwabRequestDeferred as exc:
                available = wait_deadline - self._monotonic()
                if available <= 0 or exc.reason not in {
                        "request_budget", "control_capacity_reserved", "warmup_headroom_reserved",
                        "request_burst", "control_burst_reserved"}:
                    raise
                self._wait(min(available, exc.retry_after_seconds))
        try:
            response = send(url, **kwargs)
            status = int(getattr(response, "status_code", 0) or 0)
            with self._lock:
                self._responses.append((self._monotonic(), status))
                if read_key and (status == 400 or status >= 500):
                    self._read_backoff(read_key)
                elif read_key and 200 <= status < 300:
                    self._read_failures.pop(read_key, None)
                if status == 403:
                    self._pause(new_denial=True, retry_after=response.headers.get("Retry-After"), reason="http_403")
                elif status == 429:
                    self._pause(retry_after=response.headers.get("Retry-After"),
                                reason=self._pause_reason if probe else "http_429")
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
            if status >= 400:
                self._diagnose(parsed.path, method, response=response, orders_query=orders_query)
            return response
        except requests.RequestException as exc:
            with self._lock:
                self._responses.append((self._monotonic(), "transport_error"))
                self._read_backoff(read_key)
                if probe:
                    self._pause()
            self._diagnose(parsed.path, method, exception=exc, orders_query=orders_query)
            raise
        finally:
            if probe:
                with self._lock:
                    self._probe_inflight = False


_GATE = SchwabRequestGate()


def schwab_request(send, url: str, *, recovery_safe: bool = True, **kwargs):
    # Every redirect hop would be another unbudgeted HTTP request. API callers
    # must review a redirect response instead of following it automatically.
    kwargs.setdefault("allow_redirects", False)
    return _GATE.request(send, url, recovery_safe=recovery_safe, **kwargs)


def schwab_request_pause() -> dict:
    return _GATE.snapshot()
