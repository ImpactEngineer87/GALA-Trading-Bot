"""Exercise dashboard recovery through Flask without a broker or real tokens."""

import copy
import json
import socket
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

import server


ACCESS_TOKEN = "regression-access-token"
REFRESH_TOKEN = "regression-refresh-token"
EDGE_HTML = (
    "<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY>"
    "You don't have permission to access this server. "
    "Reference #18.regression.123 "
    "https://errors.edgesuite.net/18.regression.123</BODY></HTML>"
)


def _response(status, payload=None, *, text=None, content_type=None):
    result = requests.Response()
    result.status_code = status
    result.encoding = "utf-8"
    if payload is not None:
        result._content = json.dumps(payload).encode("utf-8")
        result.headers["Content-Type"] = content_type or "application/json"
    else:
        result._content = (text or "").encode("utf-8")
        result.headers["Content-Type"] = content_type or "text/html"
    return result


@pytest.fixture
def dashboard(monkeypatch, tmp_path):
    """Isolate process state, files, credentials, and every possible HTTP call."""
    isolated_state = copy.deepcopy(server.BOT_STATE)
    for key in list(isolated_state):
        if key.startswith("schwab_"):
            isolated_state.pop(key)
    isolated_state.update({
        "running": False,
        "schwab_token": ACCESS_TOKEN,
        "schwab_refresh_token": REFRESH_TOKEN,
        "schwab_token_expiry": datetime.now() + timedelta(minutes=25),
        "schwab_auth_state": "unknown",
        "schwab_auth_verified": False,
        "schwab_auth_next_check_monotonic": 0.0,
        "schwab_reauthorization_required": False,
        "schwab_oauth_diagnostic": None,
        "schwab_oauth_last_code_fingerprint": "",
        "schwab_refresh_retry_after_monotonic": 0.0,
        "schwab_refresh_backoff_seconds": 30,
        "schwab_edge_denial_count": 0,
        "schwab_edge_circuit_until_monotonic": 0.0,
        "schwab_edge_last_reference": "",
        "schwab_network_diagnostic": None,
        "account_number": "",
    })
    monkeypatch.setattr(server, "BOT_STATE", isolated_state)
    monkeypatch.setattr(server, "_ENGINE", None)
    monkeypatch.setattr(server, "_log", Mock())
    monkeypatch.setitem(server.app.config, "TESTING", True)
    monkeypatch.setitem(server.app.config, "SECRET_KEY", "regression-session-key")
    monkeypatch.setitem(server.app.config, "SESSION_COOKIE_SECURE", False)

    env_path = tmp_path / "regression.env"
    env_path.write_text("# isolated regression environment\n", encoding="utf-8")
    monkeypatch.setattr(server, "ENV_PATH", str(env_path))
    values = {
        "SCHWAB_CLIENT_ID": "regression-client-Q2dK",
        "SCHWAB_CLIENT_SECRET": "regression-client-secret",
        "SCHWAB_ACCESS_TOKEN": ACCESS_TOKEN,
        "SCHWAB_REFRESH_TOKEN": REFRESH_TOKEN,
        "SCHWAB_TOKEN_EXPIRES_AT": isolated_state["schwab_token_expiry"].isoformat(),
        "SCHWAB_ACCOUNT_NUMBER": "",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    blocked_get = Mock(side_effect=AssertionError("unexpected HTTP GET"))
    blocked_post = Mock(side_effect=AssertionError("unexpected HTTP POST"))
    blocked_request = Mock(side_effect=AssertionError("unexpected HTTP request"))
    monkeypatch.setattr(server.requests, "get", blocked_get)
    monkeypatch.setattr(server.requests, "post", blocked_post)
    monkeypatch.setattr(server.requests, "request", blocked_request)
    monkeypatch.setattr(server.socket, "gethostname", lambda: "regression-vps-host")
    monkeypatch.setattr(server.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", 443))
    ])

    client = server.app.test_client()
    csrf = "regression-csrf-token"
    with client.session_transaction() as session:
        session.update({"logged_in": True, "username": "regression-user", "csrf_token": csrf})

    return SimpleNamespace(
        client=client,
        csrf=csrf,
        env_path=env_path,
        get=blocked_get,
        post=blocked_post,
        request=blocked_request,
    )


@pytest.mark.parametrize("edge_denial", [True, False], ids=["akamai-html", "api-json"])
def test_marketdata_403_preserves_tokens_without_refresh_or_expiry(
    dashboard, monkeypatch, edge_denial
):
    broker_response = (
        _response(403, text=EDGE_HTML)
        if edge_denial
        else _response(403, {"message": "Insufficient market-data permissions"})
    )
    dashboard.get.side_effect = None
    dashboard.get.return_value = broker_response
    refresh = Mock(side_effect=AssertionError("403 must not rotate OAuth tokens"))
    persist = Mock(side_effect=AssertionError("403 must not persist token changes"))
    monkeypatch.setattr(server, "schwab_refresh", refresh)
    monkeypatch.setattr(server, "_persist_schwab_credentials", persist)
    before = tuple(server.BOT_STATE[key] for key in (
        "schwab_token", "schwab_refresh_token", "schwab_token_expiry"
    ))

    response = dashboard.client.get("/api/schwab/status")

    assert response.status_code == 200
    status = response.get_json()
    assert status["auth_state"] == "unavailable"
    assert status["auth_expired"] is False
    assert status["verified"] is False
    assert status["oauth_diagnostic"]["request_origin"] == "bot_server"
    assert status["oauth_diagnostic"]["execution_host"] == "regression-vps-host"
    assert server.BOT_STATE["schwab_reauthorization_required"] is False
    assert tuple(server.BOT_STATE[key] for key in (
        "schwab_token", "schwab_refresh_token", "schwab_token_expiry"
    )) == before
    refresh.assert_not_called()
    persist.assert_not_called()
    dashboard.post.assert_not_called()
    dashboard.get.assert_called_once()
    assert dashboard.env_path.read_text(encoding="utf-8") == "# isolated regression environment\n"
    assert ACCESS_TOKEN not in response.get_data(as_text=True)
    assert REFRESH_TOKEN not in response.get_data(as_text=True)


def test_authorization_exchange_edge_denial_preserves_existing_authorization(dashboard):
    dashboard.post.side_effect = None
    dashboard.post.return_value = _response(403, text=EDGE_HTML)
    before = tuple(server.BOT_STATE[key] for key in (
        "schwab_token", "schwab_refresh_token", "schwab_token_expiry"
    ))

    response = dashboard.client.post(
        "/api/schwab/connect", json={"auth_code": "regression-new-code"},
        headers={"X-CSRF-Token": dashboard.csrf},
    )

    assert response.status_code == 503
    result = response.get_json()
    assert result["diagnostic"]["edge_access_denied"] is True
    assert result["diagnostic"]["edge_reference"] == "18.regression.123"
    assert result["diagnostic"]["request_origin"] == "bot_server"
    assert result["diagnostic"]["execution_host"] == "regression-vps-host"
    assert result["same_network_retry_recommended"] is False
    assert server.BOT_STATE["schwab_auth_state"] == "unavailable"
    assert server.BOT_STATE["schwab_reauthorization_required"] is False
    assert tuple(server.BOT_STATE[key] for key in (
        "schwab_token", "schwab_refresh_token", "schwab_token_expiry"
    )) == before
    dashboard.post.assert_called_once()
    dashboard.get.assert_not_called()
    assert dashboard.env_path.read_text(encoding="utf-8") == "# isolated regression environment\n"
    assert ACCESS_TOKEN not in response.get_data(as_text=True)
    assert REFRESH_TOKEN not in response.get_data(as_text=True)


@pytest.mark.parametrize("pause_key", [
    "schwab_refresh_retry_after_monotonic",
    "schwab_edge_circuit_until_monotonic",
], ids=["refresh-backoff", "edge-circuit"])
def test_direct_refresh_respects_existing_pause(dashboard, monkeypatch, pause_key):
    server.BOT_STATE[pause_key] = server.time.monotonic() + 300
    exchange = Mock(side_effect=AssertionError("paused refresh reached OAuth"))
    monkeypatch.setattr(server, "refresh_oauth_token", exchange)

    assert server.schwab_refresh(REFRESH_TOKEN) is False

    exchange.assert_not_called()
    dashboard.post.assert_not_called()
    assert server.BOT_STATE["schwab_token"] == ACCESS_TOKEN
    assert server.BOT_STATE["schwab_refresh_token"] == REFRESH_TOKEN


def test_expired_token_is_not_sent_during_refresh_backoff(dashboard, monkeypatch):
    server.BOT_STATE["schwab_token_expiry"] = datetime.now() - timedelta(seconds=1)
    server.BOT_STATE["schwab_refresh_retry_after_monotonic"] = server.time.monotonic() + 300
    refresh = Mock(side_effect=AssertionError("backoff must suppress refresh"))
    monkeypatch.setattr(server, "schwab_refresh", refresh)

    assert server.schwab_headers() == {}

    refresh.assert_not_called()
    assert server.BOT_STATE["schwab_token"] == ACCESS_TOKEN
    assert server.BOT_STATE["schwab_reauthorization_required"] is False


def test_still_valid_token_remains_usable_during_refresh_backoff(dashboard, monkeypatch):
    server.BOT_STATE["schwab_token_expiry"] = datetime.now() + timedelta(minutes=2)
    server.BOT_STATE["schwab_refresh_retry_after_monotonic"] = server.time.monotonic() + 300
    refresh = Mock(side_effect=AssertionError("backoff must suppress early refresh"))
    monkeypatch.setattr(server, "schwab_refresh", refresh)

    assert server.schwab_headers()["Authorization"] == f"Bearer {ACCESS_TOKEN}"

    refresh.assert_not_called()


@pytest.mark.parametrize("oauth_error", ["invalid_grant", "invalid_client"])
def test_proactive_refresh_rejection_cannot_readopt_same_process_token(
    dashboard, monkeypatch, oauth_error
):
    expiry = datetime.now() + timedelta(minutes=2)
    server.BOT_STATE["schwab_token_expiry"] = expiry
    monkeypatch.setenv("SCHWAB_TOKEN_EXPIRES_AT", expiry.isoformat())
    dashboard.post.side_effect = None
    dashboard.post.return_value = _response(400, {"error": oauth_error})

    assert server.schwab_ensure_token() is False

    assert server.BOT_STATE["schwab_reauthorization_required"] is True
    assert server.BOT_STATE["schwab_rejected_access_token"] == ACCESS_TOKEN
    assert server.BOT_STATE["schwab_token"] is None
    assert server.get_env("SCHWAB_ACCESS_TOKEN") == ACCESS_TOKEN
    dashboard.post.assert_called_once()
    # The old process token is still within its timestamp, but OAuth rejected
    # the authorization. Later requests must retain that verified rejection.
    dashboard.post.reset_mock()
    assert server.schwab_ensure_token() is False
    forced = server._verify_schwab_connection(force=True)
    assert forced["auth_state"] == "expired"
    assert forced["auth_expired"] is True
    assert forced["connected"] is False
    assert server.BOT_STATE["schwab_reauthorization_required"] is True
    assert server.BOT_STATE["schwab_token"] is None
    assert server.schwab_headers() == {}
    dashboard.get.assert_not_called()
    dashboard.post.assert_not_called()
    dashboard.request.assert_not_called()


def test_candle_401_during_refresh_pause_does_not_resend_rejected_token(
    dashboard, monkeypatch
):
    server.BOT_STATE["schwab_refresh_retry_after_monotonic"] = server.time.monotonic() + 300
    dashboard.get.side_effect = None
    dashboard.get.return_value = _response(401, {"message": "Unauthorized"})
    fallback_frame = object()
    fallback = Mock(return_value=fallback_frame)
    monkeypatch.setattr(server, "_synthetic_candles", fallback)

    assert server.schwab_get_candles("BA", 5) is fallback_frame

    dashboard.get.assert_called_once()
    actual_header = dashboard.get.call_args.kwargs["headers"]["Authorization"]
    assert actual_header == f"Bearer {ACCESS_TOKEN}"
    assert server.BOT_STATE["schwab_rejected_access_token"] == actual_header.removeprefix("Bearer ")
    fallback.assert_called_once_with("BA", 5)
    assert server.schwab_headers() == {}
    dashboard.post.assert_not_called()
    dashboard.request.assert_not_called()


def _network_diagnostic(dashboard):
    response = dashboard.client.post(
        "/api/schwab/network-diagnostics",
        json={},
        headers={"X-CSRF-Token": dashboard.csrf},
    )
    assert response.status_code == 200
    result = response.get_json()
    assert result["request_origin"] == "bot_server"
    assert result["execution_host"] == "regression-vps-host"
    assert result["oauth_credentials_verified"] is False
    assert result["dns_addresses"] == ["203.0.113.10"]
    assert server.BOT_STATE["schwab_network_diagnostic"] == result
    dashboard.get.assert_not_called()
    dashboard.post.assert_not_called()
    assert dashboard.env_path.read_text(encoding="utf-8") == "# isolated regression environment\n"
    return result


@pytest.mark.parametrize("failure_type", [requests.Timeout, requests.exceptions.SSLError])
def test_all_failed_network_probes_are_not_reported_reachable(dashboard, failure_type):
    dashboard.request.side_effect = failure_type("regression transport failure")

    result = _network_diagnostic(dashboard)

    assert result["conclusion"] == "NETWORK_PROBE_FAILED"
    assert result["same_network_retry_recommended"] is False
    assert dashboard.request.call_count == 3
    for probe in result["probes"].values():
        assert probe["ok"] is False
        assert probe["http_status"] == 0
        assert probe["network_error"] == failure_type.__name__
        assert probe["edge_access_denied"] is False


@pytest.mark.parametrize("token_response", [
    _response(503, {"error": "temporarily_unavailable"}),
    _response(200, text="<HTML>Temporary gateway page</HTML>"),
    _response(200, {"message": "unexpected response"}),
    _response(403, {"message": "Forbidden"}),
], ids=["oauth-server-failure", "html-without-edge-denial", "unstructured-json", "generic-forbidden"])
def test_unconfirmed_oauth_probe_is_inconclusive(dashboard, token_response):
    dashboard.request.side_effect = [
        _response(302, text=""), token_response, _response(401, {"message": "Unauthorized"})
    ]

    result = _network_diagnostic(dashboard)

    assert result["conclusion"] == "ROUTE_INCONCLUSIVE"
    assert result["same_network_retry_recommended"] is False
    assert result["probes"]["token_endpoint"]["oauth_response"] is False
    if token_response.status_code >= 500:
        assert result["probes"]["token_endpoint"]["ok"] is False


@pytest.mark.parametrize("status,error", [
    (400, "invalid_grant"), (401, "invalid_client")
])
def test_expected_oauth_error_proves_endpoint_reachability_only(dashboard, status, error):
    dashboard.request.side_effect = [
        _response(302, text=""),
        _response(status, {"error": error}),
        _response(401, {"message": "Unauthorized"}),
    ]

    result = _network_diagnostic(dashboard)

    assert result["conclusion"] == "OAUTH_ENDPOINT_REACHABLE"
    assert result["same_network_retry_recommended"] is True
    assert result["probes"]["token_endpoint"]["oauth_response"] is True
    for call in dashboard.request.call_args_list:
        args, kwargs = call
        assert "auth" not in kwargs
        assert "Authorization" not in kwargs["headers"]
        assert ACCESS_TOKEN not in repr(call)
        assert REFRESH_TOKEN not in repr(call)
        assert "regression-client-secret" not in repr(call)
        if args[1] == server.SCHWAB_TOKEN_URL:
            assert args[0] == "POST"
            assert kwargs["data"] == {
                "grant_type": "refresh_token", "refresh_token": "network-diagnostic-invalid"
            }


def test_network_diagnostic_requires_session_and_csrf(dashboard):
    response = dashboard.client.post("/api/schwab/network-diagnostics", json={})
    assert response.status_code == 403
    anonymous = server.app.test_client().post(
        "/api/schwab/network-diagnostics", json={},
        headers={"X-CSRF-Token": dashboard.csrf},
    )
    assert anonymous.status_code == 401
    dashboard.request.assert_not_called()


def test_edge_denial_is_reported_from_server_route_even_if_other_probes_answer(dashboard):
    dashboard.request.side_effect = [
        _response(302, text=""), _response(403, text=EDGE_HTML),
        _response(401, {"message": "Unauthorized"}),
    ]

    result = _network_diagnostic(dashboard)

    assert result["conclusion"] == "EDGE_ROUTE_BLOCKED"
    assert result["same_network_retry_recommended"] is False
    assert result["references"] == ["18.regression.123"]
    assert result["probes"]["token_endpoint"]["ok"] is False
    assert result["probes"]["token_endpoint"]["edge_access_denied"] is True
    assert server.BOT_STATE["schwab_token"] == ACCESS_TOKEN
    assert server.BOT_STATE["schwab_refresh_token"] == REFRESH_TOKEN


@pytest.mark.parametrize("grant", ["refresh", "authorization_code"])
def test_rotation_lock_covers_token_publication(dashboard, monkeypatch, grant):
    """Another client cannot consume an old refresh token during persistence."""
    rotated = {
        "access_token": "regression-rotated-access",
        "refresh_token": "regression-rotated-refresh",
        "expires_in": 1800,
    }
    helper = "refresh_oauth_token" if grant == "refresh" else "exchange_authorization_code"
    monkeypatch.setattr(server, helper, Mock(return_value=rotated))
    publication_observations = []

    def observe_publication(access_token, refresh_token, expiry):
        competing_lock = []

        def contender():
            acquired = server.OAUTH_ROTATION_LOCK.acquire(blocking=False)
            competing_lock.append(acquired)
            if acquired:
                server.OAUTH_ROTATION_LOCK.release()

        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        publication_observations.append(competing_lock)
        assert competing_lock == [False]
        assert access_token == rotated["access_token"]
        assert refresh_token == rotated["refresh_token"]
        assert expiry > datetime.now()
        # Account discovery uses schwab_headers(), which re-reads the published
        # process tokens. Mimic the real publisher without writing any secrets.
        monkeypatch.setenv("SCHWAB_ACCESS_TOKEN", access_token)
        monkeypatch.setenv("SCHWAB_REFRESH_TOKEN", refresh_token)
        monkeypatch.setenv("SCHWAB_TOKEN_EXPIRES_AT", expiry.isoformat(timespec="seconds"))

    persist = Mock(side_effect=observe_publication)
    monkeypatch.setattr(server, "_persist_schwab_credentials", persist)
    if grant == "refresh":
        assert server.schwab_refresh(REFRESH_TOKEN) is True
    else:
        dashboard.get.side_effect = None
        dashboard.get.return_value = _response(200, [])
        response = dashboard.client.post(
            "/api/schwab/connect", json={"auth_code": "regression-fresh-auth-code"},
            headers={"X-CSRF-Token": dashboard.csrf},
        )
        assert response.status_code == 200
        assert response.get_json()["status"] == "ok"
    persist.assert_called_once()
    assert publication_observations == [[False]]
    assert server.BOT_STATE["schwab_token"] == rotated["access_token"]
    assert server.BOT_STATE["schwab_refresh_token"] == rotated["refresh_token"]
    assert dashboard.env_path.read_text(encoding="utf-8") == "# isolated regression environment\n"


def test_refresh_disk_failure_keeps_rotated_tokens_published(dashboard, monkeypatch):
    rotated = {
        "access_token": "regression-rotated-access",
        "refresh_token": "regression-rotated-refresh",
        "expires_in": 1800,
    }
    monkeypatch.setattr(server, "refresh_oauth_token", Mock(return_value=rotated))
    write = Mock(side_effect=OSError("regression read-only environment file"))
    monkeypatch.setattr(server, "write_env_dict", write)

    assert server.schwab_refresh(REFRESH_TOKEN) is True

    write.assert_called_once()
    assert server.get_env("SCHWAB_ACCESS_TOKEN") == rotated["access_token"]
    assert server.get_env("SCHWAB_REFRESH_TOKEN") == rotated["refresh_token"]
    assert server.schwab_headers()["Authorization"] == f"Bearer {rotated['access_token']}"
    assert server.BOT_STATE["schwab_token"] == rotated["access_token"]
    assert server.BOT_STATE["schwab_refresh_token"] == rotated["refresh_token"]
    assert dashboard.env_path.read_text(encoding="utf-8") == "# isolated regression environment\n"
    dashboard.post.assert_not_called()
