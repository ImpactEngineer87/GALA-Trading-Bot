"""Regression: read-only Schwab reconciliation must not claim success on broker failure."""
import unittest
from unittest.mock import patch

import server


class ReadOnlyBrokerReconciliationStatusTests(unittest.TestCase):
    def test_empty_broker_response_is_not_success(self):
        state = {
            "ok": False,
            "flat": False,
            "message": "No se pudo confirmar Schwab posiciones: respuesta vacía",
            "positions": [],
            "open_order_symbols": [],
        }
        with server.app.app_context():
            with patch.object(
                server, "_refresh_broker_reconciliation_read_only", return_value=state
            ), patch.object(server, "_log") as log:
                response, code = server.reconcile_broker_read_only()
        self.assertEqual(code, 409)
        payload = response.get_json()
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["broker_reconciliation"]["ok"], False)
        self.assertIn("respuesta vacía", payload["message"])
        self.assertNotIn("completada", str(log.call_args_list))

    def test_only_confirmed_broker_state_reports_success(self):
        state = {
            "ok": True,
            "flat": True,
            "message": "Broker confirmado plano",
            "positions": [],
            "open_order_symbols": [],
        }
        with server.app.app_context():
            with patch.object(
                server, "_refresh_broker_reconciliation_read_only", return_value=state
            ), patch.object(server, "_log"):
                response = server.reconcile_broker_read_only()
        self.assertEqual(response.get_json()["status"], "ok")
        self.assertTrue(response.get_json()["broker_reconciliation"]["ok"])


if __name__ == "__main__":
    unittest.main()
