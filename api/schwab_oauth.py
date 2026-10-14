# -*- coding: utf-8 -*-
"""Shared Schwab OAuth helpers.

v3.3.8 goals:
- one process-wide lock for authorization-code / refresh token exchanges
- safe diagnostics that never expose client secrets, auth codes, access tokens,
  or refresh tokens
- consistent HTTP Basic authentication and callback handling
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

import requests
from requests.auth import HTTPBasicAuth

TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
DEFAULT_REDIRECT_URI = "https://127.0.0.1"

# Shared by dashboard + engine/client instances in the same Python process.
OAUTH_ROTATION_LOCK = threading.RLock()
# This process-wide gate applies only to token refreshes, never to the operator's
# authorization-code reconnect. No credentials or tokens are stored in it.
_OAUTH_RETRY_NOT_BEFORE = 0.0
_OAUTH_RETRY_STATUS = None


class SchwabOAuthCooldown(RuntimeError):
    """Previous refresh was rejected; wait without retrying the token endpoint."""

    def __init__(self, remaining_seconds: float, last_status=None):
        self.remaining_seconds = max(0.0, float(remaining_seconds))
        self.last_status = last_status
        super().__init__("Schwab OAuth refresh paused after HTTP rejection")


def reset_oauth_refresh_cooldown():
    """A successful authorization-code exchange clears rejected refresh state."""
    global _OAUTH_RETRY_NOT_BEFORE, _OAUTH_RETRY_STATUS
    with OAUTH_ROTATION_LOCK:
        _OAUTH_RETRY_NOT_BEFORE = 0.0
        _OAUTH_RETRY_STATUS = None


def _check_oauth_refresh_cooldown():
    remaining = _OAUTH_RETRY_NOT_BEFORE - time.monotonic()
    if remaining > 0:
        raise SchwabOAuthCooldown(remaining, _OAUTH_RETRY_STATUS)


def _record_oauth_refresh_rejection(status):
    global _OAUTH_RETRY_NOT_BEFORE, _OAUTH_RETRY_STATUS
    # Unspecified 403 is not automatically a rate limit; a long cooldown
    # prevents credential-retry storms while the operator reviews app access.
    delay = 900.0 if status == 403 else (120.0 if status == 429 else 0.0)
    if delay:
        _OAUTH_RETRY_NOT_BEFORE = max(
            _OAUTH_RETRY_NOT_BEFORE, time.monotonic() + delay
        )
        _OAUTH_RETRY_STATUS = status


@dataclass
class SchwabOAuthFailure(RuntimeError):
    message: str
    diagnostic: Dict[str, Any]

    def __str__(self) -> str:
        return self.message


def client_id_tail(client_id: str) -> str:
    value = str(client_id or "").strip()
    if not value:
        return ""
    return value[-4:]


def secret_fingerprint(secret: str) -> str:
    """Non-reversible short fingerprint for change detection/debugging only."""
    value = str(secret or "")
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def code_fingerprint(code: str) -> str:
    value = str(code or "")
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def parse_authorization_input(raw_value: str, *, expected_redirect_uri: str = DEFAULT_REDIRECT_URI) -> Dict[str, str]:
    """Parse a pasted Schwab callback URL or a bare authorization code.

    Sensitive values are returned to the caller but this helper never logs them.
    For a full URL we verify scheme/host/path against the configured callback so
    a copied redirect from a different app/port is rejected locally.
    """
    raw = str(raw_value or "").strip()
    if not raw:
        raise ValueError("Código OAuth vacío")

    state = ""
    if raw.startswith("code=") or raw.startswith("?code="):
        query = parse_qs(raw.lstrip("?"))
        code = (query.get("code") or [""])[0].strip()
        state = (query.get("state") or [""])[0].strip()
    elif "://" in raw:
        parsed = urlparse(raw)
        expected = urlparse(expected_redirect_uri)
        actual_origin = (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port, parsed.path.rstrip("/"))
        expected_origin = (expected.scheme.lower(), (expected.hostname or "").lower(), expected.port, expected.path.rstrip("/"))
        if actual_origin != expected_origin:
            raise ValueError(
                "La URL de retorno no coincide exactamente con la Callback URL configurada "
                f"({expected_redirect_uri})."
            )
        query = parse_qs(parsed.query)
        code = (query.get("code") or [""])[0].strip()
        state = (query.get("state") or [""])[0].strip()
    else:
        code = raw

    if not code:
        raise ValueError("La respuesta de Schwab no contiene un código OAuth válido")
    return {"code": code, "state": state}


def _response_payload(response) -> Dict[str, Any]:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    text = str(getattr(response, "text", "") or "").strip()
    if text:
        return {"raw_message": text[:800]}
    return {}


def _clean_text(value: Any, secrets_to_redact) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for secret in secrets_to_redact:
        secret = str(secret or "")
        if secret:
            text = text.replace(secret, "[REDACTED]")
    # OAuth/JWT-looking blobs and query-code fragments should never reach logs/UI.
    text = re.sub(r"(?i)(code|access_token|refresh_token|client_secret)\s*[=:]\s*[^\s,&]+", r"\1=[REDACTED]", text)
    text = re.sub(r"\b[A-Za-z0-9_-]{80,}\b", "[REDACTED_LONG_VALUE]", text)
    return text[:500]


def build_safe_diagnostic(
    response,
    *,
    phase: str,
    client_id: str,
    redirect_uri: str,
    secrets_to_redact=(),
) -> Dict[str, Any]:
    payload = _response_payload(response)
    oauth_error = _clean_text(payload.get("error", ""), secrets_to_redact).lower()
    description = _clean_text(
        payload.get("error_description") or payload.get("message") or payload.get("raw_message") or "",
        secrets_to_redact,
    )
    status = int(getattr(response, "status_code", 0) or 0)

    if oauth_error == "invalid_client":
        action = "CHECK_CLIENT_CREDENTIALS"
    elif oauth_error == "invalid_grant":
        action = "NEW_AUTHORIZATION_CODE"
    elif oauth_error in {"access_denied", "unauthorized_client"}:
        action = "CHECK_APP_AUTHORIZATION"
    elif status == 429:
        action = "RETRY_LATER"
    elif status >= 500:
        action = "RETRY_LATER"
    elif status in {401, 403}:
        action = "VERIFY_APP_CREDENTIALS_AND_STATUS"
    else:
        action = "REVIEW_OAUTH_RESPONSE"

    request_id = ""
    headers = getattr(response, "headers", {}) or {}
    for key in ("x-request-id", "request-id", "x-correlation-id", "trace-id"):
        if headers.get(key):
            request_id = _clean_text(headers.get(key), secrets_to_redact)
            break

    return {
        "phase": phase,
        "http_status": status,
        "oauth_error": oauth_error or None,
        "description": description or None,
        "recommended_action": action,
        "client_id_tail": client_id_tail(client_id),
        "redirect_uri": redirect_uri,
        "request_id": request_id or None,
    }


def _raise_for_oauth_failure(response, *, phase: str, client_id: str, redirect_uri: str, secrets_to_redact=()):
    if getattr(response, "ok", False):
        return
    diag = build_safe_diagnostic(
        response,
        phase=phase,
        client_id=client_id,
        redirect_uri=redirect_uri,
        secrets_to_redact=secrets_to_redact,
    )
    error = diag.get("oauth_error")
    status = diag.get("http_status")
    if error:
        message = f"Schwab rechazó OAuth ({error}, HTTP {status})."
    else:
        message = f"Schwab rechazó OAuth (HTTP {status})."
    raise SchwabOAuthFailure(message, diag)


def exchange_authorization_code(
    client_id: str,
    client_secret: str,
    code: str,
    *,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    timeout: int = 15,
    http_post: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    cid = str(client_id or "").strip()
    secret = str(client_secret or "").strip()
    auth_code = str(code or "").strip()
    if not cid or not secret:
        raise ValueError("Faltan Client ID o Client Secret de Schwab")
    if not auth_code:
        raise ValueError("Código OAuth vacío")

    post = http_post or requests.post
    with OAUTH_ROTATION_LOCK:
        response = post(
            TOKEN_URL,
            auth=HTTPBasicAuth(cid, secret),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": redirect_uri,
            },
            timeout=timeout,
        )
        _raise_for_oauth_failure(
            response,
            phase="authorization_code_exchange",
            client_id=cid,
            redirect_uri=redirect_uri,
            secrets_to_redact=(secret, auth_code),
        )
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise SchwabOAuthFailure(
                "Schwab respondió sin access_token.",
                {
                    "phase": "authorization_code_exchange",
                    "http_status": int(getattr(response, "status_code", 0) or 0),
                    "oauth_error": "missing_access_token",
                    "description": None,
                    "recommended_action": "REVIEW_OAUTH_RESPONSE",
                    "client_id_tail": client_id_tail(cid),
                    "redirect_uri": redirect_uri,
                    "request_id": None,
                },
            )
        reset_oauth_refresh_cooldown()
        return payload


def refresh_oauth_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    *,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    timeout: int = 15,
    http_post: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    cid = str(client_id or "").strip()
    secret = str(client_secret or "").strip()
    refresh = str(refresh_token or "").strip()
    if not cid or not secret or not refresh:
        raise ValueError("Faltan credenciales o refresh_token para renovar Schwab")

    post = http_post or requests.post
    with OAUTH_ROTATION_LOCK:
        _check_oauth_refresh_cooldown()
        response = post(
            TOKEN_URL,
            auth=HTTPBasicAuth(cid, secret),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={"grant_type": "refresh_token", "refresh_token": refresh},
            timeout=timeout,
        )
        try:
            _raise_for_oauth_failure(
                response,
                phase="refresh_token_exchange",
                client_id=cid,
                redirect_uri=redirect_uri,
                secrets_to_redact=(secret, refresh),
            )
        except SchwabOAuthFailure as exc:
            _record_oauth_refresh_rejection(
                int((exc.diagnostic or {}).get("http_status") or 0)
            )
            raise
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise SchwabOAuthFailure(
                "Schwab respondió al refresh sin access_token.",
                {
                    "phase": "refresh_token_exchange",
                    "http_status": int(getattr(response, "status_code", 0) or 0),
                    "oauth_error": "missing_access_token",
                    "description": None,
                    "recommended_action": "REVIEW_OAUTH_RESPONSE",
                    "client_id_tail": client_id_tail(cid),
                    "redirect_uri": redirect_uri,
                    "request_id": None,
                },
            )
        reset_oauth_refresh_cooldown()
        return payload
