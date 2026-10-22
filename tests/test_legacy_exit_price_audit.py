"""Offline regression: legacy Schwab close must not falsely audit a zero-price fill."""
import unittest
from unittest.mock import Mock

from api.hybrid_order_executor import EjecutorHibridoDeOrdenes


class LegacyExitPriceAuditTests(unittest.TestCase):
    def make_executor(self, *, price=0.0, status="FILLED"):
        executor = EjecutorHibridoDeOrdenes.__new__(EjecutorHibridoDeOrdenes)
        executor.paper_trading = False
        executor.request_timeout_s = 1
        executor.fill_timeout_s = 1
        executor.flat_timeout_s = 1
        executor._position_snapshot = Mock(return_value={
            "long": 5, "short": 0, "average_price": 101.0,
        })
        accepted = Mock()
        accepted.headers = {"Location": "https://api.schwabapi.com/orders/exit-id"}
        accepted.raise_for_status = Mock(return_value=None)
        executor._submit_order_request = Mock(return_value=accepted)
        executor.esperar_estado_orden = Mock(return_value={
            "broker_status": status, "latency_ms": 10,
        })
        executor.esperar_posicion_plana = Mock(return_value={
            "confirmado": True, "latency_ms": 11,
        })
        executor.detalle_orden = Mock(return_value={
            "status": status, "avg_price": price, "filled": 5,
        })
        executor._audit = Mock()
        return executor

    def test_missing_broker_fill_price_is_quarantined_and_audit_not_marked_filled(self):
        executor = self.make_executor(price=0)
        result = executor._liquidar_market_legacy(
            "META", "BUY", 5, "fake-account", "test-only"
        )
        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["posicion_plana"])
        self.assertFalse(result["fill_price_verified"])
        self.assertEqual(result["avg_price"], 0)
        events = [call.args[0] for call in executor._audit.call_args_list]
        self.assertIn("EXIT_FLAT_PRICE_UNVERIFIED", events)
        self.assertNotIn("EXIT_FILLED", events)

    def test_positive_confirmed_price_retains_valid_fill_audit(self):
        executor = self.make_executor(price=101.37)
        result = executor._liquidar_market_legacy(
            "META", "BUY", 5, "fake-account", "test-only"
        )
        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["fill_price_verified"])
        self.assertAlmostEqual(result["avg_price"], 101.37)
        events = [call.args[0] for call in executor._audit.call_args_list]
        self.assertIn("EXIT_FILLED", events)

    def test_positive_price_without_filled_order_status_is_not_verified(self):
        executor = self.make_executor(price=101.37, status="DESCONOCIDO")
        result = executor._liquidar_market_legacy(
            "META", "BUY", 5, "fake-account", "test-only"
        )
        self.assertFalse(result["fill_price_verified"])
        events = [call.args[0] for call in executor._audit.call_args_list]
        self.assertIn("EXIT_FLAT_PRICE_UNVERIFIED", events)
        self.assertNotIn("EXIT_FILLED", events)


if __name__ == "__main__":
    unittest.main()
