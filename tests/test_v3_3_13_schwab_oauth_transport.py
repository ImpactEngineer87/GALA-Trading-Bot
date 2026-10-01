import json
from unittest.mock import Mock

import pytest
import requests

from api.schwab_oauth import (
    SchwabOAuthFailure,
    build_safe_diagnostic,
    exchange_authorization_code,
    refresh_oauth_token,
)


def response(status, body):
    result = requests.Response()
    result.status_code = status
    result.encoding = "utf-8"
    result._content = body.encode("utf-8")
    return result


@pytest.mark.parametrize("grant", ["code", "refresh"])
def test_token_post_does_not_follow_redirect_or_replay_sensitive_form(grant):
    post = Mock(return_value=response(307, "Temporary Redirect"))
    with pytest.raises(SchwabOAuthFailure) as rejected:
        if grant == "code":
            exchange_authorization_code("client", "secret", "code", http_post=post)
        else:
            refresh_oauth_token("client", "secret", "refresh", http_post=post)
    assert rejected.value.diagnostic["http_status"] == 307
    assert post.call_count == 1
    assert post.call_args.kwargs["allow_redirects"] is False


def test_encoded_edge_reference_survives_long_html_error_body():
    body = ("<HTML><TITLE>Access Denied</TITLE>" + "x" * 700
            + " Reference&#32;&#35;18&#46;regression&#46;123 "
            + "https&#58;&#47;&#47;errors&#46;edgesuite&#46;net&#47;18.regression.123</HTML>")
    diag = build_safe_diagnostic(response(403, body), phase="refresh_token_exchange",
                                 client_id="client-Q2dK", redirect_uri="https://127.0.0.1")
    assert diag["edge_access_denied"] is True
    assert diag["edge_reference"] == "18.regression.123"
    assert diag["edge_url"] == "https://errors.edgesuite.net/18.regression.123"
    assert "<HTML>" not in diag["description"]


def test_html_entities_are_decoded_before_redacting_echoed_secret():
    secret = "secret&value"
    body = json.dumps({"error": "invalid_client", "error_description": "secret&amp;value"})
    diag = build_safe_diagnostic(response(401, body), phase="authorization_code_exchange",
                                 client_id="client-Q2dK", redirect_uri="https://127.0.0.1",
                                 secrets_to_redact=(secret,))
    rendered = json.dumps(diag)
    assert secret not in rendered
    assert "secret&amp;value" not in rendered
    assert "[REDACTED]" in rendered


@pytest.mark.parametrize("payload", [
    {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": "bad"},
    {"access_token": {"unexpected": "object"}},
    {"access_token": "new-access", "refresh_token": []},
    {"access_token": "new-access", "expires_in": 0},
    {"access_token": "new-access", "expires_in": 10 ** 30},
])
def test_invalid_token_generation_is_rejected_before_publication(payload):
    post = Mock(return_value=response(200, json.dumps(payload)))
    with pytest.raises(SchwabOAuthFailure) as rejected:
        refresh_oauth_token("client", "secret", "old-refresh", http_post=post)
    assert rejected.value.diagnostic["oauth_error"] == "invalid_token_response"
    assert rejected.value.diagnostic["transient"] is True
    assert "new-access" not in json.dumps(rejected.value.diagnostic)
