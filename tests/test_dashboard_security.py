import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["DASHBOARD_USERNAME"] = "secure-user"
os.environ["DASHBOARD_PASSWORD"] = "secure-password-123"
os.environ["DASHBOARD_PASSWORD_HASH"] = ""
os.environ["FLASK_SECRET_KEY"] = "test-secret-key-for-dashboard-tests"

try:
    import flask  # noqa: F401
except ImportError:
    flask = None

if flask is not None:
    import server  # noqa: E402
    from werkzeug.security import generate_password_hash
else:
    server = None


@unittest.skipIf(server is None, "Flask no está instalado en este entorno de pruebas")
class DashboardSecurityTests(unittest.TestCase):
    def setUp(self):
        server.app.config.update(TESTING=True)
        server.BOT_STATE["running"] = False
        server._LOGIN_ATTEMPTS.clear()
        self.client = server.app.test_client()

    def _login(self):
        response = self.client.post("/api/login", json={
            "username": "secure-user",
            "password": "secure-password-123",
        })
        self.assertEqual(response.status_code, 200)
        token = response.get_json().get("csrf_token")
        self.assertTrue(token)
        return token

    def test_private_api_requires_login(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 401)

    def test_auth_status_does_not_auto_login(self):
        response = self.client.get("/api/auth/status")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["logged_in"])

    def test_valid_login_unlocks_private_api(self):
        self._login()
        private = self.client.get("/api/status")
        self.assertEqual(private.status_code, 200)

    def test_bot_start_is_not_available_by_get(self):
        self._login()
        response = self.client.get("/api/bot/start")
        # With static_url_path="" Flask may resolve this GET through the
        # dashboard's catch-all static route and return 404 instead of 405.
        # Either response is safe as long as GET cannot start the engine.
        self.assertIn(response.status_code, (404, 405))
        self.assertFalse(server.BOT_STATE["running"])

    def test_private_post_requires_csrf_token(self):
        self._login()
        response = self.client.post("/api/logout")
        self.assertEqual(response.status_code, 403)

    def test_private_post_accepts_valid_csrf_token(self):
        token = self._login()
        response = self.client.post(
            "/api/logout",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 200)

    def test_login_is_rate_limited_after_repeated_failures(self):
        for _ in range(server._LOGIN_MAX_FAILURES):
            response = self.client.post("/api/login", json={
                "username": "secure-user",
                "password": "incorrect-password",
            })
            self.assertEqual(response.status_code, 401)
        blocked = self.client.post("/api/login", json={
            "username": "secure-user",
            "password": "secure-password-123",
        })
        self.assertEqual(blocked.status_code, 429)

    def test_security_headers_are_present(self):
        response = self.client.get("/api/auth/status")
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_hashed_password_is_preferred_over_legacy_plaintext(self):
        with patch.dict(os.environ, {
            "DASHBOARD_USERNAME": "secure-user",
            "DASHBOARD_PASSWORD": "wrong-legacy-password",
            "DASHBOARD_PASSWORD_HASH": generate_password_hash(
                "secure-password-123"
            ),
        }):
            response = self.client.post("/api/login", json={
                "username": "secure-user",
                "password": "secure-password-123",
            })
        self.assertEqual(response.status_code, 200)

    def test_dashboard_cannot_auto_start_real_money(self):
        token = self._login()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yaml"
            cfg.write_text(
                "risk:\n  paper_trading: false\n"
                "scheduler:\n  modo: manual\n"
                "  hora_inicio: '09:15'\n"
                "  hora_fin: '15:45'\n",
                encoding="utf-8",
            )
            with patch.object(server, "CONFIG_PATH", str(cfg)):
                response = self.client.post(
                    "/api/config",
                    json={
                        "scheduler": {
                            "modo": "automatico",
                            "hora_inicio": "09:15",
                            "hora_fin": "15:45",
                        }
                    },
                    headers={"X-CSRF-Token": token},
                )
        self.assertEqual(response.status_code, 400)
        self.assertIn("modo REAL", response.get_json()["message"])

    def test_manual_start_is_blocked_while_scheduler_controls_engine(self):
        token = self._login()
        with patch.object(server, "_scheduler_mode", return_value="automatico"), \
             patch.object(server, "_start_engine_controlled") as start:
            response = self.client.post(
                "/api/bot/start",
                json={"mode": "paper", "capital": 10000},
                headers={"X-CSRF-Token": token},
            )
        self.assertEqual(response.status_code, 409)
        start.assert_not_called()

    def test_scheduler_callback_cannot_start_real_configuration(self):
        server.BOT_STATE["running"] = False
        with patch.object(
            server,
            "_read_config_runtime",
            return_value={"risk": {"paper_trading": False}},
        ), patch.object(server, "_start_engine") as start:
            ok, message = server._start_engine_controlled("scheduler")

        self.assertFalse(ok)
        self.assertIn("modo REAL", message)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
