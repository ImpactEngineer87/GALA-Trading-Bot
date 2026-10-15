"""Offline regressions for dashboard reconnect and persisted OAuth lifetimes."""
import copy
import os
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from flask import session

import server
from api.schwab_oauth import SchwabOAuthFailure


class DashboardAuthTests(unittest.TestCase):
    def setUp(self):
        previous = copy.deepcopy(server.BOT_STATE)
        self.addCleanup(lambda: server.BOT_STATE.update(previous))
        self.env = patch.dict(os.environ, {
            "SCHWAB_CLIENT_ID": "test-client", "SCHWAB_CLIENT_SECRET": "test-secret",
            "SCHWAB_ACCESS_TOKEN": "", "SCHWAB_REFRESH_TOKEN": "",
            "SCHWAB_TOKEN_EXPIRES_AT": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        server.BOT_STATE.update(
            schwab_token=None, schwab_refresh_token=None, schwab_token_expiry=None,
            schwab_reauthorization_required=False, schwab_oauth_diagnostic=None,
            schwab_api_diagnostic=None, schwab_oauth_last_code_fingerprint="",
            schwab_auth_state="unknown", schwab_auth_next_check_monotonic=0.0,
        )
        self.log = patch.object(server, "_log").start()
        self.addCleanup(patch.stopall)
        self.exchange = patch.object(server, "exchange_authorization_code").start()
        self.refresh = patch.object(server, "refresh_oauth_token").start()
        self.persist_impl = server._persist_schwab_credentials
        self.persist = patch.object(server, "_persist_schwab_credentials").start()
        self.sync = patch.object(server, "_sync_schwab_token_to_engine").start()
        self.http_get = patch.object(server.requests, "get").start()
        self.market_get = patch.object(server, "guarded_market_get").start()

    def connect(self, state="new-session", saved_session=None, bare_code=False):
        callback = "fresh-code" if bare_code else server.SCHWAB_REDIRECT_URI + "?" + urlencode({
            "code": "fresh-code", "state": state,
        })
        with server.app.test_request_context(
            "/api/schwab/connect", method="POST", json={"auth_code": callback}
        ):
            if saved_session:
                session.update(saved_session)
            response = server.app.make_response(server.schwab_connect())
            return response.status_code, response.get_json()

    @staticmethod
    def auth_session(age=0, state="new-session"):
        return {
            "schwab_oauth_state": state,
            "schwab_oauth_state_created_at": int(time.time()) - age,
        }

    def test_missing_browser_session_is_not_reported_as_old_authorization(self):
        status, body = self.connect()
        self.assertEqual(status, 400)
        self.assertEqual(body["diagnostic"]["oauth_error"], "oauth_session_missing")
        self.exchange.assert_not_called()

    def test_expired_session_is_rejected_before_contacting_schwab(self):
        status, body = self.connect(saved_session=self.auth_session(age=901))
        self.assertEqual(status, 400)
        self.assertEqual(body["diagnostic"]["oauth_error"], "state_expired")
        self.exchange.assert_not_called()

    def test_pasting_bare_code_does_not_bypass_expired_session(self):
        status, body = self.connect(saved_session=self.auth_session(age=901), bare_code=True)
        self.assertEqual(status, 400)
        self.assertEqual(body["diagnostic"]["oauth_error"], "state_expired")
        self.exchange.assert_not_called()

    def test_previous_login_url_is_rejected_after_opening_new_authorization(self):
        status, body = self.connect(state="previous-session", saved_session=self.auth_session())
        self.assertEqual(status, 400)
        self.assertEqual(body["diagnostic"]["oauth_error"], "state_mismatch")
        self.exchange.assert_not_called()

    def test_opening_new_authorization_recovers_expired_session(self):
        with server.app.test_request_context("/api/schwab/auth-url"):
            session.update(self.auth_session(age=3600, state="expired-session"))
            response = server.schwab_auth_url_route()
            new_session = dict(session)
            self.assertIn("state=", response.get_json()["auth_url"])
        self.exchange.return_value = {
            "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 1800,
        }
        self.http_get.return_value = Mock(ok=True)
        self.http_get.return_value.json.return_value = []
        status, body = self.connect(
            state=new_session["schwab_oauth_state"], saved_session=new_session
        )
        self.assertEqual((status, body["status"]), (200, "ok"))
        self.exchange.assert_called_once()
        self.persist.assert_called_once()

    def test_old_background_poll_cannot_overwrite_new_oauth_session_cookie(self):
        client = server.app.test_client()
        with client.session_transaction() as saved:
            saved["logged_in"] = True
            saved.permanent = True
        old_cookie = client.get_cookie("session").value
        auth_response = client.get("/api/schwab/auth-url")
        self.assertIn("Set-Cookie", auth_response.headers)
        with client.session_transaction() as saved:
            self.assertTrue(saved.get("schwab_oauth_state"))

        # Simulate an old in-flight poll completing after the auth-url request.
        old_poll = server.app.test_client()
        old_poll.set_cookie("session", old_cookie)
        with patch.object(server, "_verify_schwab_connection", return_value={"status": "ok"}):
            poll_response = old_poll.get("/api/schwab/status")
        self.assertEqual(poll_response.status_code, 200)
        self.assertNotIn("Set-Cookie", poll_response.headers)

    def test_repeated_successful_callback_does_not_invalidate_connected_session(self):
        self.exchange.return_value = {"access_token": "new-access", "refresh_token": "new-refresh"}
        self.http_get.return_value = Mock(ok=True)
        self.http_get.return_value.json.return_value = []
        saved = self.auth_session()
        self.assertEqual(self.connect(saved_session=saved)[0], 200)
        status, body = self.connect(saved_session=saved)
        self.assertEqual(status, 400)
        self.assertEqual(body["diagnostic"]["oauth_error"], "authorization_code_reused")
        self.exchange.assert_called_once()
        self.assertFalse(server.BOT_STATE["schwab_reauthorization_required"])

    def test_reconnect_publishes_new_token_even_if_env_file_is_not_writable(self):
        self.exchange.return_value = {"access_token": "new-access", "refresh_token": "new-refresh"}
        self.http_get.return_value = Mock(ok=True)
        self.http_get.return_value.json.return_value = []
        self.persist.side_effect = self.persist_impl
        with patch.object(server, "read_env_dict", return_value={}), patch.object(
            server, "write_env_dict", side_effect=PermissionError("read only")
        ):
            status, body = self.connect(saved_session=self.auth_session())
        self.assertEqual(status, 200)
        self.assertTrue(body["warning"])
        self.assertEqual(os.environ["SCHWAB_ACCESS_TOKEN"], "new-access")
        self.assertEqual(os.environ["SCHWAB_REFRESH_TOKEN"], "new-refresh")
        self.sync.assert_called_once()

    def test_saved_expiry_survives_loading_and_expired_bearer_is_not_sent(self):
        old_expiry = datetime.now() - timedelta(hours=1)
        os.environ.update({
            "SCHWAB_ACCESS_TOKEN": "expired-access",
            "SCHWAB_TOKEN_EXPIRES_AT": old_expiry.isoformat(),
        })
        server._load_schwab_auth_from_env()
        self.assertEqual(server.BOT_STATE["schwab_token_expiry"], old_expiry)
        self.assertFalse(server.schwab_headers())
        self.market_get.assert_not_called()
        self.assertTrue(server.BOT_STATE["schwab_reauthorization_required"])

    def test_401_refresh_does_not_adopt_rejected_process_bearer(self):
        expiry = datetime.now() + timedelta(minutes=20)
        os.environ.update({
            "SCHWAB_ACCESS_TOKEN": "rejected-access", "SCHWAB_REFRESH_TOKEN": "old-refresh",
            "SCHWAB_TOKEN_EXPIRES_AT": expiry.isoformat(),
        })
        self.refresh.return_value = {"access_token": "new-access", "refresh_token": "new-refresh"}
        self.assertTrue(server.schwab_refresh("old-refresh", rejected_access_token="rejected-access"))
        self.refresh.assert_called_once()
        self.assertEqual(server.BOT_STATE["schwab_token"], "new-access")

    def test_engine_refresh_can_be_adopted_without_second_token_exchange(self):
        expiry = datetime.now() + timedelta(minutes=20)
        os.environ.update({
            "SCHWAB_ACCESS_TOKEN": "engine-access", "SCHWAB_REFRESH_TOKEN": "engine-refresh",
            "SCHWAB_TOKEN_EXPIRES_AT": expiry.isoformat(),
        })
        self.assertTrue(server.schwab_refresh("old-refresh", rejected_access_token="old-access"))
        self.refresh.assert_not_called()
        self.assertEqual(server.BOT_STATE["schwab_token_expiry"], expiry)

    def test_rotation_is_published_before_another_worker_can_exchange(self):
        self.refresh.return_value = {"access_token": "new-access", "refresh_token": "new-refresh"}
        observed = []
        finished = threading.Event()
        workers = []

        def observer():
            with server.OAUTH_ROTATION_LOCK:
                observed.append(os.environ.get("SCHWAB_REFRESH_TOKEN"))
            finished.set()

        def publish(access, refresh, expiry):
            worker = threading.Thread(target=observer, daemon=True)
            workers.append(worker)
            worker.start()
            self.assertFalse(finished.wait(0.05), "OAuth lock released before token publication")
            os.environ.update({
                "SCHWAB_ACCESS_TOKEN": access, "SCHWAB_REFRESH_TOKEN": refresh,
                "SCHWAB_TOKEN_EXPIRES_AT": expiry.isoformat(),
            })

        self.persist.side_effect = publish
        self.assertTrue(server.schwab_refresh("old-refresh"))
        self.assertTrue(finished.wait(2))
        for worker in workers:
            worker.join(2)
        self.assertEqual(observed, ["new-refresh"])

    def test_true_oauth_403_remains_distinct_from_local_session_failure(self):
        diagnostic = {
            "phase": "authorization_code_exchange", "http_status": 403,
            "content_type": "text/html", "response_kind": "html",
            "recommended_action": "CHECK_VPS_CONNECTIVITY_AND_APP_STATUS",
            "request_id": "safe-request-id",
        }
        self.exchange.side_effect = SchwabOAuthFailure("HTTP 403", diagnostic)
        status, body = self.connect(saved_session=self.auth_session())
        self.assertEqual(status, 400)
        self.assertEqual(body["diagnostic"], diagnostic)
        log_message = self.log.call_args.args[1]
        self.assertIn("safe-request-id", log_message)
        self.assertNotIn("fresh-code", log_message)
        self.assertNotIn("test-secret", log_message)

    def test_market_403_does_not_rotate_oauth(self):
        server.BOT_STATE.update(
            schwab_token="valid-access", schwab_refresh_token="valid-refresh",
            schwab_token_expiry=datetime.now() + timedelta(minutes=20),
        )
        self.market_get.return_value = Mock(
            ok=False, status_code=403, headers={"Content-Type": "application/json"}, text=""
        )
        self.market_get.return_value.json.return_value = {"message": "Access denied"}
        result = server._verify_schwab_connection(force=True)
        self.assertEqual(result["auth_state"], "unavailable")
        self.assertFalse(result["auth_expired"])
        self.assertEqual(result["api_diagnostic"]["http_status"], 403)
        self.refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
