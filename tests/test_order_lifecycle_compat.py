"""Offline regression coverage for safe cancellation compatibility."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from api.order_lifecycle_compat import cancelar_orden_compatible


class OrderLifecycleCompatTests(unittest.TestCase):
    def cancel(self, executor):
        return cancelar_orden_compatible(executor, "test-account", "test-order", ticker="AAPL")

    def test_modern_confirmed_method_preserves_all_evidence(self):
        evidence = {
            "confirmado": True,
            "broker_status": "CANCELED",
            "latency_ms": 27,
            "timeout": False,
            "filled": 3.0,
            "avg_price": 123.45,
            "error": "",
            "solicitud_aceptada": True,
            "cancel_http_status": 200,
            "terminal_by_absence": False,
            "terminal_by_broker_error": True,
            "extra_evidence": {"source": "broker"},
        }
        confirmed = Mock(return_value=evidence)
        legacy = Mock()
        result = self.cancel(SimpleNamespace(
            cancelar_orden_confirmada=confirmed, cancelar_orden=legacy,
        ))
        self.assertEqual(result, evidence)
        self.assertIsNot(result, evidence)
        confirmed.assert_called_once_with("test-account", "test-order", ticker="AAPL")
        legacy.assert_not_called()

    def test_older_two_argument_confirmed_method_is_called_once(self):
        calls = []

        def confirmed(account, order):
            calls.append((account, order))
            return {"confirmado": True, "broker_status": "CANCELED"}

        result = self.cancel(SimpleNamespace(cancelar_orden_confirmada=confirmed))
        self.assertTrue(result["confirmado"])
        self.assertEqual(calls, [("test-account", "test-order")])

    def test_internal_type_error_after_side_effect_never_retries(self):
        calls = []

        def confirmed(account, order, ticker=""):
            calls.append((account, order, ticker))
            raise TypeError("failure after cancellation submission")

        legacy = Mock(return_value=True)
        result = self.cancel(SimpleNamespace(
            cancelar_orden_confirmada=confirmed, cancelar_orden=legacy,
        ))
        self.assertFalse(result["confirmado"])
        self.assertEqual(result["broker_status"], "DESCONOCIDO")
        self.assertIn("failure after cancellation submission", result["error"])
        self.assertEqual(calls, [("test-account", "test-order", "AAPL")])
        legacy.assert_not_called()

    def test_legacy_boolean_cancel_is_not_submitted(self):
        legacy = Mock(return_value=True)
        result = self.cancel(SimpleNamespace(cancelar_orden=legacy))
        self.assertFalse(result["confirmado"])
        self.assertIn("not submitted", result["error"])
        legacy.assert_not_called()

    def test_missing_method_is_structured_failure(self):
        result = self.cancel(SimpleNamespace())
        self.assertFalse(result["confirmado"])
        self.assertEqual(result["broker_status"], "DESCONOCIDO")
        self.assertIn("broker-confirmed", result["error"])

    def test_fill_race_retains_fill_evidence_and_is_never_confirmed(self):
        for claimed_confirmation in (True, False):
            with self.subTest(claimed_confirmation=claimed_confirmation):
                result = self.cancel(SimpleNamespace(cancelar_orden_confirmada=Mock(return_value={
                    "confirmado": claimed_confirmation,
                    "broker_status": "FILLED",
                    "filled": 12.0,
                    "avg_price": 87.65,
                    "terminal_by_broker_error": True,
                })))
                self.assertFalse(result["confirmado"])
                self.assertEqual(result["broker_status"], "FILLED")
                self.assertEqual(result["filled"], 12.0)
                self.assertEqual(result["avg_price"], 87.65)
                self.assertTrue(result["terminal_by_broker_error"])

    def test_not_working_requires_explicit_confirmed_absence(self):
        for absent, claimed, expected in (
            (True, True, True), (False, True, False),
            ("true", True, False), (True, False, False),
        ):
            with self.subTest(absent=absent, claimed=claimed):
                result = self.cancel(SimpleNamespace(cancelar_orden_confirmada=Mock(return_value={
                    "confirmado": claimed,
                    "broker_status": "NOT_WORKING",
                    "terminal_by_absence": absent,
                    "filled": 2.0,
                })))
                self.assertIs(result["confirmado"], expected)
                self.assertEqual(result["terminal_by_absence"], absent)
                self.assertEqual(result["filled"], 2.0)

    def test_terminal_states_are_recognized_without_overriding_false(self):
        for status in ("CANCELED", "CANCELLED", "EXPIRED", "REJECTED", " canceled "):
            for claimed in (True, False):
                with self.subTest(status=status, claimed=claimed):
                    result = self.cancel(SimpleNamespace(cancelar_orden_confirmada=Mock(return_value={
                        "confirmado": claimed, "broker_status": status,
                    })))
                    self.assertIs(result["confirmado"], claimed)
                    self.assertEqual(result["broker_status"], status.strip().upper())

    def test_live_unknown_and_replaced_states_cannot_confirm_cancellation(self):
        for status in ("WORKING", "PENDING_CANCEL", "DESCONOCIDO", "REPLACED"):
            with self.subTest(status=status):
                result = self.cancel(SimpleNamespace(cancelar_orden_confirmada=Mock(return_value={
                    "confirmado": True, "broker_status": status,
                })))
                self.assertFalse(result["confirmado"])
                self.assertEqual(result["broker_status"], status)
                self.assertTrue(result["error"])

    def test_malformed_results_fail_closed(self):
        for evidence in (
            None, True, False, [], "CANCELED", {},
            {"confirmado": True},
            {"broker_status": "CANCELED"},
            {"confirmado": "true", "broker_status": "CANCELED"},
            {"confirmado": 1, "broker_status": "CANCELED"},
            {"confirmado": True, "broker_status": None},
        ):
            with self.subTest(evidence=evidence):
                result = self.cancel(SimpleNamespace(cancelar_orden_confirmada=Mock(return_value=evidence)))
                self.assertFalse(result["confirmado"])
                self.assertTrue(result["error"])

    def test_unsupported_signature_is_not_invoked(self):
        calls = []

        def confirmed(account):
            calls.append(account)

        result = self.cancel(SimpleNamespace(cancelar_orden_confirmada=confirmed))
        self.assertFalse(result["confirmado"])
        self.assertIn("signature", result["error"])
        self.assertEqual(calls, [])

    def test_uninspectable_method_is_not_invoked(self):
        confirmed = Mock()
        with patch("api.order_lifecycle_compat.inspect.signature", side_effect=ValueError("no signature")):
            result = self.cancel(SimpleNamespace(cancelar_orden_confirmada=confirmed))
        self.assertFalse(result["confirmado"])
        confirmed.assert_not_called()

    def test_broker_exception_is_structured_failure_without_legacy_fallback(self):
        confirmed = Mock(side_effect=RuntimeError("broker unavailable"))
        legacy = Mock()
        result = self.cancel(SimpleNamespace(
            cancelar_orden_confirmada=confirmed, cancelar_orden=legacy,
        ))
        self.assertFalse(result["confirmado"])
        self.assertIn("broker unavailable", result["error"])
        confirmed.assert_called_once()
        legacy.assert_not_called()

    def test_real_hybrid_paper_executor_uses_confirmed_method_without_network(self):
        from api.hybrid_order_executor import EjecutorHibridoDeOrdenes

        with patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")) as request:
            executor = EjecutorHibridoDeOrdenes(paper_trading=True)
            self.addCleanup(executor._session.close)
            with patch.object(executor, "cancelar_orden_confirmada", wraps=executor.cancelar_orden_confirmada) as confirmed:
                result = self.cancel(executor)
            confirmed.assert_called_once_with("test-account", "test-order", ticker="AAPL")
        self.assertTrue(result["confirmado"])
        self.assertEqual(result["broker_status"], "CANCELED")
        self.assertFalse(result["terminal_by_absence"])
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
