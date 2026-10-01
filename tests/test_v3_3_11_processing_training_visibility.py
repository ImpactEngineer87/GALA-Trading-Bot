import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import requests

from api.schwab_oauth import SchwabOAuthFailure, build_safe_diagnostic
from api.schwab_client import SchwabClient

ROOT = Path(__file__).parents[1]


def _html_response(status: int, text: str) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.encoding = "utf-8"
    response._content = text.encode("utf-8")
    return response


def test_html_access_denied_is_classified_as_transient_edge_failure():
    response = _html_response(
        403,
        "<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD>"
        "<BODY>You don't have permission to access this resource. "
        "Reference #18.abc.errors.edgesuite.net</BODY></HTML>",
    )
    diag = build_safe_diagnostic(
        response,
        phase="refresh_token_exchange",
        client_id="client-ABCD",
        redirect_uri="https://127.0.0.1",
    )
    assert diag["http_status"] == 403
    assert diag["oauth_error"] is None
    assert diag["edge_access_denied"] is True
    assert diag["transient"] is True
    assert diag["recommended_action"] == "RETRY_EDGE_ACCESS"


def test_client_transient_refresh_failure_does_not_force_reauthorization():
    env = {
        "SCHWAB_CLIENT_ID": "client-id",
        "SCHWAB_CLIENT_SECRET": "client-secret",
        "SCHWAB_ACCESS_TOKEN": "",
        "SCHWAB_REFRESH_TOKEN": "",
        "SCHWAB_TOKEN_EXPIRES_AT": "",
    }
    with patch.dict(os.environ, env, clear=False):
        client = SchwabClient({})
        client.client_id = "client-id"
        client.client_secret = "client-secret"
        client.access_token = "old-access"
        client.refresh_token = "refresh"
        client.token_expiry = datetime.now() - timedelta(seconds=1)
        client.token_es_usuario = True
        failure = SchwabOAuthFailure(
            "edge",
            {
                "http_status": 403,
                "oauth_error": None,
                "transient": True,
                "edge_access_denied": True,
            },
        )
        with patch("api.schwab_client.refresh_oauth_token", side_effect=failure):
            assert client._refrescar_token() is False
        state = client.estado_autenticacion()
        assert state["reauthorization_required"] is False
        assert state["degraded"] is True
        assert state["retry_in_seconds"] > 0


def test_processing_and_retraining_visibility_sources_exist():
    engine = (ROOT / "engine" / "bot_engine.py").read_text(encoding="utf-8")
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    dashboard = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    lifecycle = (ROOT / "agents" / "retraining_lifecycle.py").read_text(encoding="utf-8")

    assert "def _set_ticker_processing(" in engine
    assert "def ticker_processing_snapshot(" in engine
    assert "@app.route('/api/ticker-processing')" in server
    assert '"version": "3.3.12"' in server
    assert "Estado de procesamiento por ticker" in dashboard
    assert "fetchTickerProcessing" in dashboard
    assert "last_supplemental_fit_samples" in lifecycle
    assert "Suplementarias usadas último reentreno" in dashboard


def test_current_version_is_3311():
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "3.3.12"
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "Trading Algorítmico v3.3.12" in html
