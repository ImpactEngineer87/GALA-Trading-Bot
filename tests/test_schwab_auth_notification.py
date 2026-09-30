import json
import os
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import server


def response(status_code, payload=None):
    result = requests.Response()
    result.status_code = status_code
    result.encoding = "utf-8"
    result._content = json.dumps(payload or {}).encode("utf-8")
    return result


class SchwabAuthVerificationTests(unittest.TestCase):
    def setUp(self):
        self.saved = {
            key: server.BOT_STATE.get(key)
            for key in (
                "schwab_token",
                "schwab_token_expiry",
                "schwab_refresh_token",
                "schwab_auth_state",
                "schwab_auth_message",
                "schwab_auth_verified",
                "schwab_auth_verified_at",
                "schwab_auth_next_check_monotonic",
                "schwab_reauthorization_required",
            )
        }
        server.BOT_STATE.update({
            "schwab_token": "expired-access",
            "schwab_token_expiry": None,
            "schwab_refresh_token": "saved-refresh",
            "schwab_auth_state": "unknown",
            "schwab_auth_message": "",
            "schwab_auth_verified": False,
            "schwab_auth_verified_at": None,
            "schwab_auth_next_check_monotonic": 0.0,
            "schwab_reauthorization_required": False,
        })
        self.saved_env = {
            key: os.environ.get(key)
            for key in (
                "SCHWAB_ACCESS_TOKEN",
                "SCHWAB_REFRESH_TOKEN",
                "SCHWAB_TOKEN_EXPIRES_AT",
            )
        }

    def tearDown(self):
        server.BOT_STATE.update(self.saved)
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @staticmethod
    def env_value(key, default=""):
        return {
            "SCHWAB_ACCESS_TOKEN": "expired-access",
            "SCHWAB_REFRESH_TOKEN": "saved-refresh",
            "SCHWAB_CLIENT_ID": "client-id",
            "SCHWAB_CLIENT_SECRET": "client-secret",
        }.get(key, default)

    def test_rejected_access_and_invalid_grant_are_verified_expired(self):
        with patch.object(server, "get_env", side_effect=self.env_value), \
             patch.object(server.requests, "get", return_value=response(401)), \
             patch.object(
                 server.requests,
                 "post",
                 return_value=response(400, {"error": "invalid_grant"}),
             ):
            result = server._verify_schwab_connection(force=True)

        self.assertFalse(result["connected"])
        self.assertTrue(result["auth_expired"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["auth_state"], "expired")
        self.assertEqual(result["notification_interval_seconds"], 300)
        self.assertGreaterEqual(result["next_verification_in_seconds"], 298)

    def test_network_error_is_not_mislabeled_as_expired(self):
        with patch.object(server, "get_env", side_effect=self.env_value), \
             patch.object(
                 server.requests,
                 "get",
                 side_effect=requests.ConnectionError("offline"),
             ):
            result = server._verify_schwab_connection(force=True)

        self.assertFalse(result["connected"])
        self.assertFalse(result["auth_expired"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["auth_state"], "unavailable")

    def test_successful_refresh_is_retried_and_verified(self):
        with patch.object(server, "get_env", side_effect=self.env_value), \
             patch.object(
                 server.requests,
                 "get",
                 side_effect=[response(401), response(200, {"BA": {}})],
             ) as live_get, \
             patch.object(
                 server.requests,
                 "post",
                 return_value=response(200, {
                     "access_token": "fresh-access",
                     "refresh_token": "fresh-refresh",
                     "expires_in": 1800,
                 }),
             ), \
             patch.object(server, "_persist_schwab_credentials"):
            result = server._verify_schwab_connection(force=True)

        self.assertTrue(result["connected"])
        self.assertFalse(result["auth_expired"])
        self.assertTrue(result["verified"])
        self.assertEqual(live_get.call_count, 2)
        self.assertEqual(server.BOT_STATE["schwab_token"], "fresh-access")

    def test_expired_result_is_cached_to_avoid_refresh_spam(self):
        with patch.object(server, "get_env", side_effect=self.env_value), \
             patch.object(server.requests, "get", return_value=response(401)) as live_get, \
             patch.object(
                 server.requests,
                 "post",
                 return_value=response(400, {"error": "invalid_grant"}),
             ) as refresh:
            first = server._verify_schwab_connection(force=True)
            second = server._verify_schwab_connection()

        self.assertTrue(first["auth_expired"])
        self.assertTrue(second["auth_expired"])
        self.assertEqual(live_get.call_count, 1)
        self.assertEqual(refresh.call_count, 1)

    def test_refreshed_token_is_injected_into_running_engine(self):
        engine = types.SimpleNamespace(
            schwab=types.SimpleNamespace(autenticar_con_token=Mock()),
            ejecutor_hibrido=types.SimpleNamespace(set_token=Mock()),
        )
        with patch.object(server, "_ENGINE", engine):
            server._sync_schwab_token_to_engine(
                "fresh-access", "fresh-refresh", 1800
            )

        engine.schwab.autenticar_con_token.assert_called_once_with(
            "fresh-access", "fresh-refresh", expires_in=1800
        )
        engine.ejecutor_hibrido.set_token.assert_called_once_with("fresh-access")

    def test_saved_expiry_is_loaded_for_the_first_dashboard_check(self):
        future = datetime.now() + timedelta(minutes=20)

        def saved_value(key, default=""):
            return {
                "SCHWAB_ACCESS_TOKEN": "saved-access",
                "SCHWAB_REFRESH_TOKEN": "saved-refresh",
                "SCHWAB_TOKEN_EXPIRES_AT": future.isoformat(),
            }.get(key, default)

        with patch.object(server, "get_env", side_effect=saved_value):
            server._load_schwab_auth_from_env()

        self.assertEqual(server.BOT_STATE["schwab_token"], "saved-access")
        self.assertEqual(server.BOT_STATE["schwab_refresh_token"], "saved-refresh")
        self.assertEqual(server.BOT_STATE["schwab_token_expiry"], future)

    def test_rotated_expiry_is_persisted_and_available_without_restart(self):
        expiry = datetime.now() + timedelta(minutes=25)
        written = {}

        with patch.object(server, "read_env_dict", return_value={}), \
             patch.object(server, "write_env_dict", side_effect=lambda d: written.update(d)):
            server._persist_schwab_credentials("new-access", "new-refresh", expiry)

        self.assertEqual(written["SCHWAB_ACCESS_TOKEN"], "new-access")
        self.assertEqual(written["SCHWAB_REFRESH_TOKEN"], "new-refresh")
        self.assertEqual(written["SCHWAB_TOKEN_EXPIRES_AT"], expiry.isoformat(timespec="seconds"))
        self.assertEqual(os.environ["SCHWAB_ACCESS_TOKEN"], "new-access")
        self.assertEqual(os.environ["SCHWAB_REFRESH_TOKEN"], "new-refresh")

    def test_dashboard_throttles_verified_alerts_across_page_refreshes(self):
        html = (Path(__file__).parents[1] / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("SCHWAB_AUTH_ALERT_INTERVAL_MS = 5 * 60 * 1000", html)
        self.assertIn("gala10_schwab_auth_expired_alert_at", html)
        self.assertIn("status.auth_expired || !status.verified", html)
        self.assertIn("maybeNotifySchwabAuthExpired(d)", html)
        self.assertIn("localStorage.setItem(SCHWAB_AUTH_ALERT_STORAGE_KEY", html)
        self.assertIn('id="schwabAuthBanner"', html)
        self.assertIn("function updateSchwabAuthBanner", html)
        self.assertIn("updateSchwabAuthBanner(d)", html)


if __name__ == "__main__":
    unittest.main()
