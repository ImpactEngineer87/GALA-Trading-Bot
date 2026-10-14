"""Offline regression: do not hammer Schwab OAuth after HTTP 403."""
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import requests

from api.schwab_oauth import (
    SchwabOAuthCooldown,
    SchwabOAuthFailure,
    exchange_authorization_code,
    refresh_oauth_token,
    reset_oauth_refresh_cooldown,
)


def response(code, body):
    r = requests.Response()
    r.status_code = code
    r.encoding = "utf-8"
    r.headers["Content-Type"] = "application/json"
    r._content = json.dumps(body).encode("utf-8")
    return r


class OAuthCooldownTests(unittest.TestCase):
    def setUp(self):
        reset_oauth_refresh_cooldown()

    def tearDown(self):
        reset_oauth_refresh_cooldown()

    def test_refresh_403_blocks_followup_network_requests(self):
        forbidden = response(403, {"message": "Forbidden"})
        with patch("api.schwab_oauth.requests.post", return_value=forbidden) as post:
            with self.assertRaises(SchwabOAuthFailure):
                refresh_oauth_token("client", "secret", "refresh")
            with self.assertRaises(SchwabOAuthCooldown) as raised:
                refresh_oauth_token("client", "secret", "refresh")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(raised.exception.last_status, 403)
            self.assertGreater(raised.exception.remaining_seconds, 800)

    def test_refresh_429_also_defers_followup(self):
        with patch("api.schwab_oauth.requests.post", return_value=response(429, {})) as post:
            with self.assertRaises(SchwabOAuthFailure):
                refresh_oauth_token("client", "secret", "refresh")
            with self.assertRaises(SchwabOAuthCooldown):
                refresh_oauth_token("client", "secret", "refresh")
            self.assertEqual(post.call_count, 1)

    def test_authorization_code_reconnect_can_clear_cooldown(self):
        with patch("api.schwab_oauth.requests.post", return_value=response(403, {})):
            with self.assertRaises(SchwabOAuthFailure):
                refresh_oauth_token("client", "secret", "refresh")
        with patch("api.schwab_oauth.requests.post", return_value=response(200, {
            "access_token": "new-access", "refresh_token": "new-refresh",
            "expires_in": 1800,
        })):
            exchange_authorization_code("client", "secret", "new-code")
        with patch("api.schwab_oauth.requests.post", return_value=response(200, {
            "access_token": "later-access", "refresh_token": "later-refresh",
            "expires_in": 1800,
        })) as post:
            refresh_oauth_token("client", "secret", "new-refresh")
            self.assertEqual(post.call_count, 1)

    def test_server_does_not_retry_rejected_refresh_or_reuse_expired_token(self):
        import server

        keys = (
            "schwab_token", "schwab_refresh_token", "schwab_token_expiry",
            "schwab_reauthorization_required", "schwab_auth_state",
            "schwab_auth_message", "schwab_auth_next_check_monotonic",
            "schwab_oauth_diagnostic", "schwab_auth_verified",
            "schwab_auth_verified_at",
        )
        saved = {key: server.BOT_STATE.get(key) for key in keys}
        try:
            server.BOT_STATE.update({
                "schwab_token": "expired-access",
                "schwab_refresh_token": "saved-refresh",
                "schwab_token_expiry": datetime.now() - timedelta(seconds=30),
                "schwab_reauthorization_required": False,
                "schwab_auth_next_check_monotonic": 0,
            })
            settings = {
                "SCHWAB_CLIENT_ID": "client",
                "SCHWAB_CLIENT_SECRET": "secret",
                "SCHWAB_REFRESH_TOKEN": "saved-refresh",
            }
            with patch.object(server, "get_env", side_effect=lambda k, default="": settings.get(k, default)), \\
                 patch.object(server.requests, "post", return_value=response(403, {})) as post:
                self.assertFalse(server.schwab_ensure_token())
                self.assertFalse(server.schwab_ensure_token())
                self.assertEqual(post.call_count, 1)
                self.assertEqual(server.BOT_STATE["schwab_auth_state"], "unavailable")
                self.assertEqual(server.schwab_headers(), {})
                self.assertEqual(post.call_count, 1)
        finally:
            server.BOT_STATE.update(saved)


if __name__ == "__main__":
    unittest.main()
