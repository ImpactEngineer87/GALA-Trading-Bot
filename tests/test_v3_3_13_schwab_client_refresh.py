"""Exercise token renewal, rotated generations, and rejected-token recovery."""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import requests

from api.schwab_client import SchwabClient
from api.schwab_oauth import SchwabOAuthFailure


@pytest.fixture
def client(monkeypatch, tmp_path):
    values = {
        "SCHWAB_CLIENT_ID": "test-client",
        "SCHWAB_CLIENT_SECRET": "test-secret",
        "SCHWAB_ACCESS_TOKEN": "old-access",
        "SCHWAB_REFRESH_TOKEN": "old-refresh",
        "SCHWAB_TOKEN_EXPIRES_AT": (datetime.now() + timedelta(minutes=20)).isoformat(),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    result = SchwabClient({})
    save = result._save_env_token
    monkeypatch.setattr(result, "_save_env_token", lambda _: save(str(tmp_path / ".env")))
    return result


def _fresh_payload():
    return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 1800}


def _response(status):
    result = requests.Response()
    result.status_code = status
    result._content = b"{}"
    return result


def _edge_failure():
    return SchwabOAuthFailure("edge denial", {
        "http_status": 403, "edge_access_denied": True, "transient": True,
    })


def test_proactive_refresh_does_not_adopt_its_own_environment_token(client, monkeypatch):
    expiry = datetime.now() + timedelta(minutes=2)
    client.token_expiry = expiry
    monkeypatch.setenv("SCHWAB_TOKEN_EXPIRES_AT", expiry.isoformat())
    refresh = Mock(return_value=_fresh_payload())
    monkeypatch.setattr("api.schwab_client.refresh_oauth_token", refresh)

    assert client._asegurar_token() is True
    refresh.assert_called_once()
    assert client.access_token == "new-access"
    assert os.environ["SCHWAB_REFRESH_TOKEN"] == "new-refresh"
    assert client.token_expiry > datetime.now() + timedelta(minutes=25)


def test_forced_refresh_renews_even_when_own_token_has_future_expiry(client, monkeypatch):
    refresh = Mock(return_value=_fresh_payload())
    monkeypatch.setattr("api.schwab_client.refresh_oauth_token", refresh)

    assert client._asegurar_token(forzar_refresh=True) is True
    refresh.assert_called_once()
    assert client.access_token == "new-access"


def test_a_new_process_token_recovers_during_old_cooldown(client, monkeypatch):
    client._marcar_degradado("previous edge denial", cooldown_seconds=900)
    client.token_expiry = datetime.now() - timedelta(seconds=1)
    monkeypatch.setenv("SCHWAB_ACCESS_TOKEN", "dashboard-access")
    monkeypatch.setenv("SCHWAB_REFRESH_TOKEN", "dashboard-refresh")
    monkeypatch.setenv("SCHWAB_TOKEN_EXPIRES_AT", (datetime.now() + timedelta(minutes=25)).isoformat())
    refresh = Mock()
    monkeypatch.setattr("api.schwab_client.refresh_oauth_token", refresh)

    assert client._asegurar_token() is True
    refresh.assert_not_called()
    assert client.access_token == "dashboard-access"
    assert client.refresh_token == "dashboard-refresh"
    assert client.estado_autenticacion()["degraded"] is False


def test_a_401_refreshes_instead_of_readopting_rejected_environment_token(client, monkeypatch):
    refresh = Mock(return_value=_fresh_payload())
    monkeypatch.setattr("api.schwab_client.refresh_oauth_token", refresh)
    client._session.get = Mock(side_effect=[_response(401), _response(200)])

    result = client._get_autorizado("https://api.schwabapi.com/marketdata/v1/quotes")

    assert result.status_code == 200
    refresh.assert_called_once()
    assert [call.kwargs["headers"]["Authorization"] for call in client._session.get.call_args_list] == [
        "Bearer old-access", "Bearer new-access",
    ]


def test_a_late_401_for_old_request_keeps_concurrently_synced_token(client, monkeypatch):
    client.autenticar_con_token("dashboard-access", "dashboard-refresh")
    refresh = Mock()
    monkeypatch.setattr("api.schwab_client.refresh_oauth_token", refresh)

    assert client._recuperar_401("old-access") is True
    refresh.assert_not_called()
    assert client.access_token == "dashboard-access"
    assert client._session.headers["Authorization"] == "Bearer dashboard-access"


def test_a_second_401_stops_after_one_refresh_and_observes_cooldown(client, monkeypatch):
    refresh = Mock(return_value=_fresh_payload())
    monkeypatch.setattr("api.schwab_client.refresh_oauth_token", refresh)
    client._session.get = Mock(side_effect=[_response(401), _response(401)])

    assert client._get_autorizado("https://api.schwabapi.com/marketdata/v1/quotes").status_code == 401
    assert client.access_token is None
    assert client.estado_autenticacion()["degraded"] is True
    assert client._get_autorizado("https://api.schwabapi.com/marketdata/v1/quotes") is None
    refresh.assert_called_once()
    assert client._session.get.call_count == 2


def test_edge_denial_preserves_still_valid_token_and_backs_off(client, monkeypatch):
    expiry = datetime.now() + timedelta(minutes=2)
    client.token_expiry = expiry
    monkeypatch.setenv("SCHWAB_TOKEN_EXPIRES_AT", expiry.isoformat())
    refresh = Mock(side_effect=_edge_failure())
    monkeypatch.setattr("api.schwab_client.refresh_oauth_token", refresh)

    assert client._asegurar_token() is True
    assert client._asegurar_token() is True
    refresh.assert_called_once()
    assert client.access_token == "old-access"
    assert client.reauthorization_required is False
    assert client.estado_autenticacion()["degraded"] is True


def test_401_during_edge_cooldown_does_not_send_another_refresh(client, monkeypatch):
    refresh = Mock(side_effect=_edge_failure())
    monkeypatch.setattr("api.schwab_client.refresh_oauth_token", refresh)
    assert client._asegurar_token(forzar_refresh=True) is True

    assert client._recuperar_401("old-access") is False
    assert client._asegurar_token() is False
    refresh.assert_called_once()
    assert client.access_token is None


def test_invalid_grant_cannot_restore_the_old_environment_access_token(client, monkeypatch):
    refresh = Mock(side_effect=SchwabOAuthFailure("invalid grant", {
        "http_status": 400, "oauth_error": "invalid_grant", "transient": False,
    }))
    monkeypatch.setattr("api.schwab_client.refresh_oauth_token", refresh)

    assert client._asegurar_token(forzar_refresh=True) is False
    client._auth_retry_after = datetime.now() - timedelta(seconds=1)
    assert client._asegurar_token() is False
    refresh.assert_called_once()
    assert client.access_token is None
    assert client.reauthorization_required is True


def test_repeated_edge_denials_open_circuit_for_expired_token(client, monkeypatch):
    expiry = datetime.now() - timedelta(seconds=1)
    client.token_expiry = expiry
    monkeypatch.setenv("SCHWAB_TOKEN_EXPIRES_AT", expiry.isoformat())
    refresh = Mock(side_effect=_edge_failure())
    monkeypatch.setattr("api.schwab_client.refresh_oauth_token", refresh)
    for _ in range(3):
        client._auth_retry_after = datetime.now() - timedelta(seconds=1)
        assert client._asegurar_token() is False

    assert client.estado_autenticacion()["edge_circuit_open"] is True
    client._auth_retry_after = datetime.now() - timedelta(seconds=1)
    assert client._refrescar_token() is False
    assert refresh.call_count == 3
    assert client.reauthorization_required is False


def test_rotated_process_tokens_remain_published_when_disk_write_fails(client, monkeypatch, tmp_path):
    original = tmp_path / ".env"
    original.write_text("SCHWAB_ACCESS_TOKEN=old-access\nUNRELATED_CONFIG=keep\n", encoding="utf-8")
    monkeypatch.setattr("api.schwab_client.os.replace", Mock(side_effect=PermissionError("read-only")))
    refresh = Mock(return_value=_fresh_payload())
    monkeypatch.setattr("api.schwab_client.refresh_oauth_token", refresh)

    assert client._asegurar_token(forzar_refresh=True) is True
    assert os.environ["SCHWAB_ACCESS_TOKEN"] == "new-access"
    assert os.environ["SCHWAB_REFRESH_TOKEN"] == "new-refresh"
    assert original.read_text(encoding="utf-8") == "SCHWAB_ACCESS_TOKEN=old-access\nUNRELATED_CONFIG=keep\n"
    assert list(tmp_path.glob(".schwab-env-*")) == []


@pytest.mark.parametrize("suffix", ["Z", "+00:00"])
def test_constructor_normalizes_persisted_utc_expiry(client, monkeypatch, suffix):
    future = datetime.now(timezone.utc) + timedelta(minutes=20)
    monkeypatch.setenv("SCHWAB_TOKEN_EXPIRES_AT", future.replace(tzinfo=None).isoformat() + suffix)

    loaded = SchwabClient({})

    assert loaded.token_expiry.tzinfo is None
    assert loaded.estado_autenticacion()["connected"] is True
    assert loaded.estado_autenticacion()["expires_in_seconds"] > 1100


def test_import_does_not_override_service_credentials_with_stale_env(client, tmp_path):
    # Execute the actual module with a temporary installation path so its .env
    # contains a stale generation, while the service environment has the new one.
    from pathlib import Path

    module_path = tmp_path / "api" / "schwab_client.py"
    module_path.parent.mkdir()
    (tmp_path / ".env").write_text("SCHWAB_ACCESS_TOKEN=stale-file-token\nSCHWAB_CLIENT_SECRET=stale-file-secret\n", encoding="utf-8")
    source_path = Path(__file__).parents[1] / "api" / "schwab_client.py"
    namespace = {"__file__": str(module_path), "__name__": "client_import_test"}
    exec(compile(source_path.read_text(encoding="utf-8"), str(module_path), "exec"), namespace)

    assert os.environ["SCHWAB_ACCESS_TOKEN"] == "old-access"
    assert os.environ["SCHWAB_CLIENT_SECRET"] == "test-secret"
