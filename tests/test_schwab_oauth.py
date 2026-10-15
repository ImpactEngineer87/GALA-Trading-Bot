"""OAuth diagnostics and retry safety, without live Schwab requests."""

import json
import unittest
from unittest.mock import Mock, patch

from api import schwab_oauth


def response(status, payload=None, *, text="", headers=None):
    result = Mock()
    result.status_code = status
    result.ok = status < 400
    result.text = text
    result.headers = headers or {}
    if payload is None:
        result.json.side_effect = ValueError("Response is not JSON")
    else:
        result.json.return_value = payload
    return result


class SafeDiagnosticTests(unittest.TestCase):
    def diagnostic(self, result, **kwargs):
        return schwab_oauth.build_safe_diagnostic(
            result,
            phase="refresh_token_exchange",
            client_id="client-1234",
            redirect_uri="https://127.0.0.1",
            **kwargs,
        )

    def test_json_error_uses_only_safe_metadata(self):
        result = response(
            403,
            {
                "error": "unauthorized_client",
                "error_description": "Application access has not been approved.",
                "access_token": "payload-access-secret",
                "refresh_token": "payload-refresh-secret",
            },
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Request-ID": "request-123",
                "Authorization": "Basic private-header-value",
                "Set-Cookie": "session=private-cookie",
                "X-Private": "private-extra-header",
            },
        )

        diagnostic = self.diagnostic(result)

        self.assertEqual(diagnostic["recommended_action"], "CHECK_APP_AUTHORIZATION")
        self.assertEqual(diagnostic["response_kind"], "json")
        self.assertEqual(diagnostic["content_type"], "application/json")
        self.assertEqual(diagnostic["request_id"], "request-123")
        self.assertEqual(diagnostic["client_id_tail"], "1234")
        serialized = json.dumps(diagnostic)
        for secret in (
            "payload-access-secret", "payload-refresh-secret", "private-header-value",
            "private-cookie", "private-extra-header",
        ):
            self.assertNotIn(secret, serialized)

    def test_html_403_classifies_possible_intermediary_rejection_without_dumping_html(self):
        result = response(
            403,
            text="<!DOCTYPE html><html><script>private error page</script>Access denied</html>",
            headers={"Content-Type": "text/html; charset=UTF-8", "X-Correlation-ID": "req-456"},
        )

        diagnostic = self.diagnostic(result)

        self.assertEqual(diagnostic["content_type"], "text/html")
        self.assertEqual(diagnostic["response_kind"], "html")
        self.assertIsNone(diagnostic["oauth_error"])
        self.assertEqual(diagnostic["recommended_action"], "CHECK_VPS_CONNECTIVITY_AND_APP_STATUS")
        self.assertIn("VPS", diagnostic["description"])
        self.assertIn("Schwab app", diagnostic["description"])
        self.assertEqual(diagnostic["request_id"], "req-456")
        self.assertNotIn("<", diagnostic["description"])
        self.assertNotIn("private error page", json.dumps(diagnostic))

    def test_html_is_detected_even_without_a_matching_content_type(self):
        for headers in ({}, {"Content-Type": "application/json"}):
            with self.subTest(headers=headers):
                diagnostic = self.diagnostic(response(403, text="<html>private-body</html>", headers=headers))
                self.assertEqual(diagnostic["response_kind"], "html")
                self.assertNotIn("private-body", json.dumps(diagnostic))

    def test_quoted_token_fields_and_basic_bearer_values_are_redacted(self):
        description = (
            'Request {"code": "unlisted-code", "access_token": "unlisted-access", '
            '"refresh_token": "unlisted-refresh", "client_secret": "unlisted-secret", '
            '"id_token": "unlisted-id", "authorization": "private-authorization", '
            '"token": "generic-token", "code_verifier": "private-verifier"}; '
            "Basic c2hvcnQ6c2VjcmV0 Bearer short-bearer-token "
            "code=query-code&next=ok known-secret known-refresh"
        )
        diagnostic = self.diagnostic(
            response(
                400,
                {"error": "invalid_grant", "error_description": description},
                headers={"X-Request-ID": 'Bearer header-token code="header-code"'},
            ),
            secrets_to_redact=("known-secret", "known-refresh"),
        )

        serialized = json.dumps(diagnostic)
        for secret in (
            "unlisted-code", "unlisted-access", "unlisted-refresh", "unlisted-secret",
            "unlisted-id", "private-authorization", "generic-token", "private-verifier",
            "c2hvcnQ6c2VjcmV0", "short-bearer-token",
            "query-code", "known-secret", "known-refresh", "header-token", "header-code",
        ):
            self.assertNotIn(secret, serialized)
        self.assertIn("REDACTED", diagnostic["description"])
        self.assertEqual(diagnostic["recommended_action"], "NEW_AUTHORIZATION_CODE")

    def test_html_in_json_description_and_request_id_is_not_forwarded(self):
        diagnostic = self.diagnostic(response(
            400,
            {"error": "invalid_client", "message": "<script>private-json-body</script>"},
            headers={"request-id": "<html>private-header-body</html>"},
        ))

        serialized = json.dumps(diagnostic)
        self.assertNotIn("<", serialized)
        self.assertNotIn("private-json-body", serialized)
        self.assertNotIn("private-header-body", serialized)
        self.assertEqual(diagnostic["recommended_action"], "CHECK_CLIENT_CREDENTIALS")

    def test_plain_text_403_is_short_and_sanitized(self):
        diagnostic = self.diagnostic(response(403, text="Access denied code=secret-query " + "x " * 600))

        self.assertEqual(diagnostic["response_kind"], "text")
        self.assertEqual(diagnostic["recommended_action"], "CHECK_VPS_CONNECTIVITY_AND_APP_STATUS")
        self.assertNotIn("secret-query", diagnostic["description"])
        self.assertLessEqual(len(diagnostic["description"]), 500)

    def test_content_type_is_limited_to_a_mime_type(self):
        diagnostic = self.diagnostic(response(403, text="Denied", headers={"Content-Type": "text/html Bearer secret-header"}))

        self.assertIsNone(diagnostic["content_type"])
        self.assertNotIn("secret-header", json.dumps(diagnostic))

    def test_known_secret_is_redacted_even_from_content_type(self):
        diagnostic = self.diagnostic(
            response(403, text="Denied", headers={"Content-Type": "private-secret/json"}),
            secrets_to_redact=("private-secret",),
        )

        self.assertIsNone(diagnostic["content_type"])
        self.assertNotIn("private-secret", json.dumps(diagnostic))


class OAuthExchangeTests(unittest.TestCase):
    def setUp(self):
        schwab_oauth.reset_oauth_refresh_cooldown()

    def tearDown(self):
        schwab_oauth.reset_oauth_refresh_cooldown()

    def refresh(self, post):
        return schwab_oauth.refresh_oauth_token(
            "client-id", "client-secret", "refresh-secret", http_post=post,
        )

    def authorize(self, post):
        return schwab_oauth.exchange_authorization_code(
            "client-id", "client-secret", "authorization-code", http_post=post,
        )

    def test_authorization_exchange_uses_basic_auth_and_form_fields(self):
        post = Mock(return_value=response(200, {"access_token": "new-access", "refresh_token": "new-refresh"}))

        payload = self.authorize(post)

        self.assertEqual(payload["access_token"], "new-access")
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args, (schwab_oauth.TOKEN_URL,))
        self.assertEqual(kwargs["auth"].username, "client-id")
        self.assertEqual(kwargs["auth"].password, "client-secret")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(kwargs["data"], {
            "grant_type": "authorization_code",
            "code": "authorization-code",
            "redirect_uri": schwab_oauth.DEFAULT_REDIRECT_URI,
        })
        self.assertEqual(kwargs["timeout"], 15)

    def test_rejected_refresh_does_not_repeat_http_requests_during_cooldown(self):
        for status, delay in ((403, 900), (429, 120)):
            with self.subTest(status=status):
                schwab_oauth.reset_oauth_refresh_cooldown()
                post = Mock(return_value=response(status, {"message": "Denied"}))
                with patch.object(schwab_oauth.time, "monotonic", return_value=1000):
                    with self.assertRaises(schwab_oauth.SchwabOAuthFailure):
                        self.refresh(post)
                    with self.assertRaises(schwab_oauth.SchwabOAuthCooldown) as caught:
                        self.refresh(post)

                self.assertEqual(caught.exception.last_status, status)
                self.assertEqual(caught.exception.remaining_seconds, delay)
                post.assert_called_once()

    def test_manual_authorization_bypasses_and_clears_refresh_cooldown(self):
        post = Mock(side_effect=[
            response(403, text="<html>Denied</html>"),
            response(200, {"access_token": "manual-access", "refresh_token": "manual-refresh"}),
            response(200, {"access_token": "refreshed-access"}),
        ])
        with patch.object(schwab_oauth.time, "monotonic", return_value=1000):
            with self.assertRaises(schwab_oauth.SchwabOAuthFailure):
                self.refresh(post)
            self.assertEqual(self.authorize(post)["access_token"], "manual-access")
            self.assertEqual(self.refresh(post)["access_token"], "refreshed-access")

        self.assertEqual(post.call_count, 3)
        self.assertEqual(
            [call.kwargs["data"]["grant_type"] for call in post.call_args_list],
            ["refresh_token", "authorization_code", "refresh_token"],
        )

    def test_failed_manual_authorization_does_not_clear_refresh_cooldown(self):
        post = Mock(side_effect=[
            response(403, {"message": "Denied"}),
            response(400, {"error": "invalid_grant"}),
        ])
        with patch.object(schwab_oauth.time, "monotonic", return_value=1000):
            with self.assertRaises(schwab_oauth.SchwabOAuthFailure):
                self.refresh(post)
            with self.assertRaises(schwab_oauth.SchwabOAuthFailure):
                self.authorize(post)
            with self.assertRaises(schwab_oauth.SchwabOAuthCooldown):
                self.refresh(post)

        self.assertEqual(post.call_count, 2)

    def test_refresh_can_resume_when_cooldown_expires(self):
        post = Mock(side_effect=[
            response(403, {"message": "Denied"}),
            response(200, {"access_token": "refreshed-access"}),
        ])
        with patch.object(schwab_oauth.time, "monotonic", return_value=1000):
            with self.assertRaises(schwab_oauth.SchwabOAuthFailure):
                self.refresh(post)
        with patch.object(schwab_oauth.time, "monotonic", return_value=1901):
            self.assertEqual(self.refresh(post)["access_token"], "refreshed-access")

        self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
