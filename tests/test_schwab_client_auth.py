"""Offline regressions for persisted Schwab credentials and 401 recovery."""

import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from dotenv import load_dotenv

from api import schwab_client
from api.schwab_oauth import SchwabOAuthFailure


class SchwabClientAuthTests(unittest.TestCase):
    def setUp(self):
        self.expiry = datetime.now() + timedelta(minutes=10)
        env = {
            "SCHWAB_CLIENT_ID": "test-client",
            "SCHWAB_CLIENT_SECRET": "test-secret",
            "SCHWAB_ACCESS_TOKEN": "saved-access",
            "SCHWAB_REFRESH_TOKEN": "saved-refresh",
            "SCHWAB_TOKEN_EXPIRES_AT": self.expiry.isoformat(),
        }
        self.env_patch = patch.dict(os.environ, env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.session = Mock()
        self.session.headers = {}
        session_patch = patch.object(schwab_client.requests, "Session", return_value=self.session)
        session_patch.start()
        self.addCleanup(session_patch.stop)
        log_patch = patch.object(schwab_client, "_log")
        log_patch.start()
        self.addCleanup(log_patch.stop)
        # Successful refreshes must not write this workspace's real .env.
        save_patch = patch.object(schwab_client.SchwabClient, "_save_env_token")
        self.save_tokens = save_patch.start()
        self.addCleanup(save_patch.stop)
        refresh_patch = patch.object(schwab_client, "refresh_oauth_token")
        self.refresh = refresh_patch.start()
        self.addCleanup(refresh_patch.stop)
        self.refresh.return_value = {
            "access_token": "renewed-access",
            "refresh_token": "renewed-refresh",
            "expires_in": 1800,
        }
        self.url = schwab_client.TRADER_URL + "/accounts/accountNumbers"

    def response(self, status):
        return Mock(status_code=status, ok=200 <= status < 400)

    def sent_bearers(self):
        return [call.kwargs["headers"]["Authorization"] for call in self.session.get.call_args_list]

    def test_timezone_aware_saved_expiry_is_normalized(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        for raw in (
            future.isoformat(),
            future.isoformat().replace("+00:00", "Z"),
            future.astimezone(timezone(timedelta(hours=4))).isoformat(),
        ):
            with self.subTest(expiry_format=raw[-6:]):
                os.environ["SCHWAB_TOKEN_EXPIRES_AT"] = raw
                client = schwab_client.SchwabClient()
                self.assertIsNone(client.token_expiry.tzinfo)
                self.assertTrue(client._asegurar_token())
                self.assertTrue(client.estado_autenticacion()["connected"])
        self.refresh.assert_not_called()

    def test_missing_or_invalid_expiry_requires_refresh_before_request(self):
        for raw in ("", "invalid-expiry"):
            with self.subTest(expiry=raw):
                os.environ["SCHWAB_TOKEN_EXPIRES_AT"] = raw
                client = schwab_client.SchwabClient()
                self.assertIsNone(client.token_expiry)
                self.assertFalse(client.estado_autenticacion()["connected"])
                self.session.get.return_value = self.response(200)
                self.assertEqual(client._get_autorizado(self.url).status_code, 200)
                self.assertEqual(self.sent_bearers()[-1], "Bearer renewed-access")
        self.assertEqual(self.refresh.call_count, 2)

    def test_saved_access_without_expiry_or_refresh_fails_before_request(self):
        os.environ.pop("SCHWAB_TOKEN_EXPIRES_AT")
        os.environ.pop("SCHWAB_REFRESH_TOKEN")
        client = schwab_client.SchwabClient()
        self.assertIsNone(client._get_autorizado(self.url))
        self.assertTrue(client.reauthorization_required)
        self.session.get.assert_not_called()
        self.refresh.assert_not_called()

    def test_refresh_only_credentials_recover_without_reauthorization(self):
        os.environ.pop("SCHWAB_ACCESS_TOKEN")
        os.environ.pop("SCHWAB_TOKEN_EXPIRES_AT")
        client = schwab_client.SchwabClient()
        self.assertEqual(client.refresh_token, "saved-refresh")
        self.assertFalse(client.reauthorization_required)
        self.assertTrue(client._asegurar_token())
        self.refresh.assert_called_once()
        self.assertEqual(client.access_token, "renewed-access")
        self.assertFalse(client._paper)
        self.assertTrue(client.estado_autenticacion()["connected"])

    def test_explicit_token_expiry_preserves_remaining_lifetime(self):
        client = schwab_client.SchwabClient()
        exact_expiry = datetime.now(timezone.utc) + timedelta(seconds=85)
        self.assertTrue(client.autenticar_con_token(
            "other-access", "other-refresh", expires_at=exact_expiry
        ))
        self.assertEqual(
            client.token_expiry,
            exact_expiry.astimezone().replace(tzinfo=None),
        )
        self.assertTrue(client._asegurar_token())
        self.refresh.assert_not_called()

    def test_explicit_expired_token_does_not_gain_a_new_lifetime(self):
        client = schwab_client.SchwabClient()
        exact_expiry = datetime.now() - timedelta(seconds=1)
        self.assertTrue(client.autenticar_con_token(
            "expired-access", expires_at=exact_expiry
        ))
        self.assertEqual(client.token_expiry, exact_expiry)
        self.assertFalse(client._asegurar_token())
        self.assertFalse(client.estado_autenticacion()["connected"])
        self.refresh.assert_not_called()

    def test_401_refreshes_instead_of_readopting_rejected_saved_token(self):
        client = schwab_client.SchwabClient()
        self.session.get.side_effect = [self.response(401), self.response(200)]
        self.assertEqual(client._get_autorizado(self.url).status_code, 200)
        self.refresh.assert_called_once()
        self.assertEqual(self.sent_bearers(), ["Bearer saved-access", "Bearer renewed-access"])
        self.save_tokens.assert_called_once_with(schwab_client.ENV_PATH)

    def test_401_adopts_newer_token_rotated_by_another_client(self):
        client = schwab_client.SchwabClient()

        def request(*args, **kwargs):
            if self.session.get.call_count == 1:
                os.environ.update({
                    "SCHWAB_ACCESS_TOKEN": "other-access",
                    "SCHWAB_REFRESH_TOKEN": "other-refresh",
                })
                return self.response(401)
            return self.response(200)

        self.session.get.side_effect = request
        self.assertEqual(client._get_autorizado(self.url).status_code, 200)
        self.refresh.assert_not_called()
        self.assertEqual(self.sent_bearers(), ["Bearer saved-access", "Bearer other-access"])
        self.assertEqual(client.refresh_token, "other-refresh")

    def test_delayed_401_does_not_invalidate_newer_local_token(self):
        client = schwab_client.SchwabClient()

        def request(*args, **kwargs):
            if self.session.get.call_count == 1:
                client.autenticar_con_token("other-access", "other-refresh")
                return self.response(401)
            return self.response(200)

        self.session.get.side_effect = request
        self.assertEqual(client._get_autorizado(self.url).status_code, 200)
        self.refresh.assert_not_called()
        self.assertEqual(self.sent_bearers(), ["Bearer saved-access", "Bearer other-access"])

    def test_401_does_not_retry_rejected_token_after_refresh_failure(self):
        client = schwab_client.SchwabClient()
        self.session.get.return_value = self.response(401)
        self.refresh.side_effect = [
            SchwabOAuthFailure("OAuth rejected", {"http_status": 403}),
            self.refresh.return_value,
        ]
        self.assertIsNone(client._get_autorizado(self.url))
        self.assertEqual(self.session.get.call_count, 1)
        client._auth_retry_after = datetime.min
        self.assertTrue(client._asegurar_token())
        self.assertEqual(self.refresh.call_count, 2)
        self.assertEqual(client.access_token, "renewed-access")

    def test_401_retry_is_bounded_when_refreshed_token_is_also_rejected(self):
        client = schwab_client.SchwabClient()
        self.session.get.side_effect = [self.response(401), self.response(401)]
        self.assertEqual(client._get_autorizado(self.url).status_code, 401)
        self.assertEqual(self.session.get.call_count, 2)
        self.refresh.assert_called_once()

    def test_403_does_not_rotate_or_retry_oauth(self):
        client = schwab_client.SchwabClient()
        self.session.get.return_value = self.response(403)
        self.assertEqual(client._get_autorizado(self.url).status_code, 403)
        self.assertEqual(self.session.get.call_count, 1)
        self.refresh.assert_not_called()
        self.assertFalse(client.reauthorization_required)

    def test_import_uses_repo_env_without_overwriting_host_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / ".env"
            fixture.write_text("SCHWAB_CLIENT_ID=stale-file-client\n", encoding="utf-8")
            spec = importlib.util.spec_from_file_location("schwab_client_env_test", schwab_client.__file__)
            module = importlib.util.module_from_spec(spec)

            def load_fixture(dotenv_path, *, override):
                return load_dotenv(fixture, override=override)

            with patch("dotenv.load_dotenv", side_effect=load_fixture) as loader:
                spec.loader.exec_module(module)
            loader.assert_called_once_with(schwab_client.ENV_PATH, override=False)
            self.assertEqual(os.environ["SCHWAB_CLIENT_ID"], "test-client")


if __name__ == "__main__":
    unittest.main()
