"""Prove the engine, dashboard and executors share one quiet period."""

from unittest.mock import Mock

import pytest

import server
from api import schwab_transport
from api.hybrid_order_executor import EjecutorHibridoDeOrdenes
from api.order_executor import EjecutorDeOrdenes
from api.schwab_client import SchwabClient
from api.schwab_transport import SchwabRequestPaused
from core.risk_manager import OrdenTradingCalculada
from test_v3_3_13_schwab_server_recovery import (
    ACCESS_TOKEN, EDGE_HTML, REFRESH_TOKEN, _response, dashboard,
)


def deny(dashboard):
    dashboard.get.side_effect = None
    dashboard.get.return_value = _response(403, text=EDGE_HTML)
    return server._verify_schwab_connection(force=True)


def test_api_denial_blocks_manual_and_background_calls_without_losing_tokens(dashboard):
    snapshot = deny(dashboard)
    assert snapshot["retry_after_seconds"] >= 599
    for _ in range(3):
        assert server._verify_schwab_connection(force=True)["auth_state"] == "unavailable"
        assert server.schwab_refresh(REFRESH_TOKEN) is False
        assert server.schwab_headers() == {}
        assert dashboard.client.get("/api/schwab/status").status_code == 200
        for path in ["/api/schwab/auth-url", "/api/schwab/test", "/api/market/quote"]:
            result = dashboard.client.get(path)
            assert result.status_code == 503
            assert int(result.headers["Retry-After"]) >= 599
            assert result.json["request_sent"] is False
        connect = dashboard.client.post("/api/schwab/connect", json={"auth_code":"never-sent"},
                                        headers={"X-CSRF-Token":dashboard.csrf})
        assert connect.status_code == 503
        result = dashboard.client.post("/api/schwab/network-diagnostics", json={},
                                       headers={"X-CSRF-Token":dashboard.csrf})
        assert result.json["conclusion"] == "COOLDOWN_ACTIVE"
        assert all(row["request_sent"] is False for row in result.json["probes"].values())
    assert dashboard.get.call_count == 1
    dashboard.post.assert_not_called()
    dashboard.request.assert_not_called()
    assert server.BOT_STATE["schwab_token"] == ACCESS_TOKEN
    assert server.BOT_STATE["schwab_refresh_token"] == REFRESH_TOKEN
    assert server.BOT_STATE["schwab_reauthorization_required"] is False


def test_network_diagnostic_stops_at_the_first_denied_probe(dashboard):
    dashboard.request.side_effect = None
    dashboard.request.return_value = _response(403, text=EDGE_HTML)
    result = dashboard.client.post("/api/schwab/network-diagnostics", json={},
                                   headers={"X-CSRF-Token":dashboard.csrf})
    assert result.status_code == 200
    assert result.json["conclusion"] == "EDGE_ROUTE_BLOCKED"
    assert result.json["probes"]["token_endpoint"]["request_sent"] is False
    assert result.json["probes"]["trader"]["request_sent"] is False
    assert dashboard.request.call_count == 1


def test_token_denial_pauses_market_data_and_preserves_saved_generation(dashboard):
    dashboard.post.side_effect = None
    dashboard.post.return_value = _response(403, text=EDGE_HTML)
    assert server.schwab_refresh(REFRESH_TOKEN) is False
    assert server._verify_schwab_connection(force=True)["auth_state"] == "unavailable"
    assert dashboard.post.call_count == 1
    dashboard.get.assert_not_called()
    assert server.BOT_STATE["schwab_token"] == ACCESS_TOKEN
    assert server.BOT_STATE["schwab_refresh_token"] == REFRESH_TOKEN


def test_new_client_and_token_sync_cannot_bypass_the_shared_pause(dashboard, monkeypatch):
    deny(dashboard)
    client = SchwabClient({})
    client.autenticar_con_token("new-synced-access", "new-synced-refresh")
    # Production publishes the new generation before synchronizing a client.
    monkeypatch.setenv("SCHWAB_ACCESS_TOKEN", client.access_token)
    monkeypatch.setenv("SCHWAB_REFRESH_TOKEN", client.refresh_token)
    monkeypatch.setenv("SCHWAB_TOKEN_EXPIRES_AT", client.token_expiry.isoformat())
    client._session.get = Mock(side_effect=AssertionError("no engine HTTP while paused"))
    with pytest.raises(SchwabRequestPaused):
        client._get_autorizado(server.SCHWAB_MKT_URL + "/quotes")
    assert client.obtener_posiciones("fake-account") is None
    assert client.estado_autenticacion()["connected"] is False
    assert client.estado_autenticacion()["reauthorization_required"] is False
    assert client.access_token == "new-synced-access"
    client._session.get.assert_not_called()


def test_executor_does_not_claim_a_locally_paused_delete_succeeded(dashboard):
    deny(dashboard)
    executor = EjecutorHibridoDeOrdenes(paper_trading=False, access_token=ACCESS_TOKEN)
    executor._session = Mock()
    result = executor._solicitar_cancelacion("fake-account", "fake-order")
    assert result["aceptada"] is False
    assert result["http_status"] is None
    assert "pausa" in result["error"]
    executor._session.delete.assert_not_called()
    assert executor.paper_trading is False


@pytest.mark.parametrize("hybrid", [False, True])
def test_paused_entry_reports_error_without_submitting_or_simulating(dashboard, hybrid):
    deny(dashboard)
    cls = EjecutorHibridoDeOrdenes if hybrid else EjecutorDeOrdenes
    executor = cls(paper_trading=False, access_token=ACCESS_TOKEN)
    executor._session = Mock()
    executor._audit = Mock()
    order = OrdenTradingCalculada("META", "BUY", 100, 99, 1, 1, 1, 1)
    if hybrid:
        result = executor._enviar_orden_real(order, "LIMIT", "fake-account", "test")
    else:
        result = executor.ejecutar(order, "fake-account")
    assert result["status"] == "ERROR"
    assert "pausa" in result["mensaje"]
    assert not result.get("order_id")
    executor._session.post.assert_not_called()
    assert executor.paper_trading is False


def test_legacy_cancel_does_not_claim_success_during_pause(dashboard):
    deny(dashboard)
    executor = EjecutorDeOrdenes(paper_trading=False, access_token=ACCESS_TOKEN)
    executor._session = Mock()
    assert executor.cancelar_orden("fake-account", "fake-order") is False
    executor._session.delete.assert_not_called()


def test_reinitializing_server_auth_fields_does_not_reset_the_network_pause(dashboard):
    deny(dashboard)
    server._reset_schwab_edge_state()
    assert server._schwab_retry_remaining() >= 599
    assert server._verify_schwab_connection(force=True)["auth_state"] == "unavailable"
    assert dashboard.get.call_count == 1


def test_cooldown_restart_and_status_do_not_dispatch_a_request(dashboard, monkeypatch):
    deny(dashboard)
    restarted = schwab_transport.SchwabRequestGate(schwab_transport._GATE.state_path)
    monkeypatch.setattr(schwab_transport, "_GATE", restarted)
    server._reset_schwab_edge_state()
    result = dashboard.client.get("/api/schwab/status")
    assert result.json["retry_after_seconds"] >= 599
    assert result.json["auth_expired"] is False
    assert dashboard.get.call_count == 1


def test_connect_race_stopped_locally_does_not_consume_the_code(dashboard, monkeypatch):
    server.BOT_STATE["schwab_oauth_last_code_fingerprint"] = "previous-attempt"
    monkeypatch.setattr(server, "exchange_authorization_code", Mock(side_effect=SchwabRequestPaused(600)))
    result = dashboard.client.post("/api/schwab/connect", json={"auth_code":"never-sent"},
                                   headers={"X-CSRF-Token":dashboard.csrf})
    assert result.status_code == 503
    assert result.json["request_sent"] is False
    assert server.BOT_STATE["schwab_oauth_last_code_fingerprint"] == "previous-attempt"
    assert server.BOT_STATE["schwab_token"] == ACCESS_TOKEN
    dashboard.post.assert_not_called()
