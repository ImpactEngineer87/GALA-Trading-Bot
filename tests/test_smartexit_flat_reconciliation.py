"""SmartExit: a truly FLAT broker position must not remain stuck in RECONCILIAR.

No test performs a live HTTP call or places a broker order.
"""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from core.execution_v2 import SmartExit


class FlatPositionReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(self.temp)
        self.account = "mock-account"
        self.ticker = "NVDA"
        self.key = hashlib.sha256(self.account.encode()).hexdigest()[:16] + ":NVDA"
        self.executor = Mock()
        self.executor._position_snapshot.return_value = {
            "long": 0, "short": 0
        }
        self.executor.ordenes_trabajando_ticker.return_value = []
        self.executor.detalle_orden.return_value = {
            "status": "FILLED", "filled": 3, "avg_price": 0
        }
        self.bot = SmartExit(self.executor, {
            "pending_path": str(root / "pending.json"),
            "history_path": str(root / "history.jsonl"),
        })
        self.state = {
            "execution_id": "exit-identifier",
            "position_id": "position-NVDA",
            "ticker": "NVDA", "side": "BUY", "quantity": 3,
            "reason": "señal opuesta SELL",
            "quote": {}, "limit": None,
            "reference_price": None,
            "legs": [{
                "order_id": "broker-order-id", "status": "FILLED",
                "filled": 3, "avg_price": 0, "type": "MARKET",
            }],
        }
        self.bot.pending.put(self.key, self.state)

    def close(self):
        return self.bot.close(
            "NVDA", "BUY", 3, self.account, "señal opuesta SELL",
            position_id="position-NVDA",
        )

    def test_flat_terminal_order_missing_price_is_released_but_never_trained(self):
        result = self.close()
        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["posicion_plana"])
        self.assertFalse(result["fill_price_verified"])
        self.assertEqual(result["avg_price"], 0)
        self.assertIsNone(self.bot.pending.get(self.key))
        self.assertEqual(
            self.bot.history.recent()[-1]["event"], "EXIT_RECOVERED_FLAT"
        )
        self.executor._submit_order_request.assert_not_called()

    def test_flat_fill_quantity_disagreement_is_released_quarantined(self):
        self.executor.detalle_orden.return_value = {
            "status": "CANCELED", "filled": 1, "avg_price": 100
        }
        result = self.close()
        self.assertEqual(result["status"], "OK")
        self.assertFalse(result["fill_price_verified"])
        self.assertIsNone(self.bot.pending.get(self.key))
        self.executor._submit_order_request.assert_not_called()

    def test_flat_position_with_working_orders_must_stay_pending(self):
        self.executor.ordenes_trabajando_ticker.return_value = ["working-stop"]
        result = self.close()
        self.assertEqual(result["status"], "RECONCILIAR")
        self.assertFalse(result["posicion_plana"])
        self.assertIsNotNone(self.bot.pending.get(self.key))
        self.executor._submit_order_request.assert_not_called()

    def test_flat_position_without_working_snapshot_must_stay_pending(self):
        self.executor.ordenes_trabajando_ticker.return_value = None
        result = self.close()
        self.assertEqual(result["status"], "RECONCILIAR")
        self.assertIn("snapshot unavailable", result["mensaje"])
        self.assertIsNotNone(self.bot.pending.get(self.key))
        self.executor._submit_order_request.assert_not_called()

    def test_unverified_old_intent_for_another_position_never_trains(self):
        self.state["position_id"] = "other-NVDA-position"
        self.bot.pending.put(self.key, self.state)
        result = self.close()
        self.assertEqual(result["status"], "OK")
        self.assertFalse(result["fill_price_verified"])
        self.executor._submit_order_request.assert_not_called()

    def test_broker_still_exposed_cannot_release_pending_as_flat(self):
        self.executor._position_snapshot.return_value = {
            "long": 3, "short": 0
        }
        result = self.close()
        self.assertEqual(result["status"], "RECONCILIAR")
        self.assertFalse(result["posicion_plana"])
        self.assertIsNotNone(self.bot.pending.get(self.key))
        self.executor._submit_order_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
