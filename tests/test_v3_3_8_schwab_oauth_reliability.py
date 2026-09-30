import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from api.schwab_oauth import (
    SchwabOAuthFailure,
    build_safe_diagnostic,
    exchange_authorization_code,
    parse_authorization_input,
)
from api.schwab_client import SchwabClient

ROOT = Path(__file__).parents[1]


def response(status_code, payload=None, text=None):
    r = requests.Response()
    r.status_code = status_code
    r.encoding = "utf-8"
    if payload is not None:
        r._content = json.dumps(payload).encode("utf-8")
        r.headers["Content-Type"] = "application/json"
    else:
        r._content = (text or "").encode("utf-8")
    return r


def test_callback_parser_accepts_exact_registered_uri_and_rejects_other_port():
    parsed = parse_authorization_input(
        "https://127.0.0.1/?code=fresh-code&state=abc",
        expected_redirect_uri="https://127.0.0.1",
    )
    assert parsed == {"code": "fresh-code", "state": "abc"}
    assert parse_authorization_input("code=bare-code")["code"] == "bare-code"

    try:
        parse_authorization_input(
            "https://127.0.0.1:5000/?code=fresh-code",
            expected_redirect_uri="https://127.0.0.1",
        )
    except ValueError as exc:
        assert "Callback URL" in str(exc)
    else:
        raise AssertionError("mismatched callback port must be rejected")


def test_failed_exchange_exposes_safe_reason_but_never_secret_or_code():
    secret = "SUPER-SECRET-CLIENT-VALUE"
    code = "VERY-SENSITIVE-AUTH-CODE"
    rejected = response(
        403,
        {
            "error": "invalid_client",
            "error_description": f"bad secret={secret} code={code}",
        },
    )
    post = Mock(return_value=rejected)

    try:
        exchange_authorization_code(
            "client-ABCD",
            secret,
            code,
            http_post=post,
        )
    except SchwabOAuthFailure as exc:
        diag = exc.diagnostic
    else:
        raise AssertionError("403 must raise SchwabOAuthFailure")

    rendered = json.dumps(diag)
    assert diag["http_status"] == 403
    assert diag["oauth_error"] == "invalid_client"
    assert diag["recommended_action"] == "CHECK_CLIENT_CREDENTIALS"
    assert diag["client_id_tail"] == "ABCD"
    assert secret not in rendered
    assert code not in rendered
    assert "[REDACTED]" in rendered


def test_exchange_uses_http_basic_auth_and_exact_callback():
    accepted = response(
        200,
        {"access_token": "access", "refresh_token": "refresh", "expires_in": 1800},
    )
    post = Mock(return_value=accepted)
    result = exchange_authorization_code(
        "client-id",
        "client-secret",
        "one-time-code",
        redirect_uri="https://127.0.0.1",
        http_post=post,
    )
    assert result["access_token"] == "access"
    kwargs = post.call_args.kwargs
    assert kwargs["data"]["redirect_uri"] == "https://127.0.0.1"
    assert kwargs["data"]["grant_type"] == "authorization_code"
    assert kwargs["auth"].username == "client-id"
    assert kwargs["auth"].password == "client-secret"


def test_schwab_client_adopts_newest_process_token_before_refreshing():
    future = (datetime.now() + timedelta(minutes=20)).isoformat()
    env = {
        "SCHWAB_CLIENT_ID": "new-client",
        "SCHWAB_CLIENT_SECRET": "new-secret",
        "SCHWAB_ACCESS_TOKEN": "new-access",
        "SCHWAB_REFRESH_TOKEN": "new-refresh",
        "SCHWAB_TOKEN_EXPIRES_AT": future,
    }
    with patch.dict(os.environ, env, clear=False):
        client = SchwabClient({})
        client.access_token = "stale-access"
        client.refresh_token = "stale-refresh"
        client.token_expiry = datetime.now() - timedelta(minutes=1)
        with patch("api.schwab_client.refresh_oauth_token") as refresh:
            assert client._refrescar_token() is True
            refresh.assert_not_called()

        assert client.client_id == "new-client"
        assert client.client_secret == "new-secret"
        assert client.access_token == "new-access"
        assert client.refresh_token == "new-refresh"


def test_server_source_invalidates_old_tokens_when_app_credentials_change():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    assert 'schwab_credentials_changed = any(' in source
    assert 'env_d["SCHWAB_ACCESS_TOKEN"] = ""' in source
    assert 'env_d["SCHWAB_REFRESH_TOKEN"] = ""' in source
    assert '_clear_schwab_oauth_tokens("Client ID/Client Secret de Schwab modificados")' in source
    assert 'BOT_STATE["schwab_oauth_diagnostic"] = diag' in source


def test_dashboard_renders_safe_oauth_diagnostic_and_blocks_double_submit():
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert 'id="schwabOAuthDiagnostic"' in html
    assert 'id="schwabConnectBtn"' in html
    assert 'function renderSchwabOAuthDiagnostic(diag)' in html
    assert "btn.disabled=true" in html
    assert "diag.oauth_error" in html
    assert "diag.client_id_tail" in html
