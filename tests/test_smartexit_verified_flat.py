"""Offline SmartExit regression tests: broker-flat does not imply a valid AI label.

All broker answers are fake. The unit tests exercise only the journal recovery
helper after its caller has proved a flat broker position and no WORKING order.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from core.execution_v2 import SmartExit


class SmartExitVerifiedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.dir = self.enterContext(TemporaryDirectory())
        root = Path(self.dir)
        self.broker_details = {}
        self.executor = Mock()
        self.executor.detalle_orden.side_effect = (
            lambda account, order_id: dict(
                self.broker_details.get(order_id, {
                    "status": "DESCONOCIDO", "filled": 0, "avg_price": 0
                })
            )
        )
        self.bot = SmartExit(self.executor, {
            "pending_path": str(root / "pending.json"),
            "history_path": str(root / "history.jsonl"),
        })
        self.key = "account-hash:META"

    def state(self, *, qty=5, side="BUY", order_ids=("limit-1", "market-2")):
        state = {
            "ticker": "META", "side": side, "quantity": qty,
            "reason": "CLOSE", "quote": {}, "limit": None,
            "reference_price": None,
            "legs": [
                {"order_id": order_id, "status": "SUBMITTED",
                 "filled": 0, "avg_price": 0, "type": "MARKET"}
                for order_id in order_ids
            ],
        }
        self.bot.pending.put(self.key, state)
        return state

    def recover(self, state, *, side="BUY", qty=5):
        return self.bot._recover_flat(
            self.key, state, "Broker confirmed FLAT; old order is not WORKING",
            account="test-account", requested_side=side, requested_quantity=qty,
        )

    def test_complete_two_leg_broker_fills_recover_true_weighted_price(self):
        state = self.state()
        self.broker_details.update({
            "limit-1": {"status": "CANCELED", "filled": 2, "avg_price": 100.0},
            "market-2": {"status": "FILLED", "filled": 3, "avg_price": 102.0},
        })
        result = self.recover(state)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["order_id"], "market-2")
        self.assertAlmostEqual(result["avg_price"], 101.2)
        self.assertTrue(result["fill_price_verified"])
        self.assertTrue(result["posicion_plana"])
        self.assertIsNone(self.bot.pending.get(self.key))
        self.assertEqual(
            [r["event"] for r in self.bot.history.recent()],
            ["EXIT_COMPLETED"],
        )
        self.assertTrue(result["execution_details"]["recovered_from_broker_flat"])
        self.executor._audit.assert_called_once()

    def test_unknown_broker_order_remains_excluded(self):
        state = self.state()
        self.broker_details["limit-1"] = {
            "status": "CANCELED", "filled": 2, "avg_price": 100.0,
        }
        res = self.recover(state)
        self.assertEqual(res["status"], "OK")
        self.assertFalse(res["fill_price_verified"])
        self.assertEqual(res["avg_price"], 0)
        self.assertEqual(self.bot.history.recent()[0]["event"], "EXIT_RECOVERED_FLAT")
        self.assertIsNone(self.bot.pending.get(self.key))

    def test_incomplete_or_bad_execution_prices_must_not_train(self):
        for details in (
            {"status": "FILLED", "filled": 5, "avg_price": 0},
            {"status": "FILLED", "filled": 4, "avg_price": 100},
            {"status": "WORKING", "filled": 5, "avg_price": 100},
            {"status": "FILLED", "filled": 6, "avg_price": 100},
        ):
            with self.subTest(details=details):
                self.bot.history.path.unlink(missing_ok=True)
                state = self.state(order_ids=("one-order",))
                self.broker_details.clear()
                self.broker_details["one-order"] = details
                res = self.recover(state)
                self.assertFalse(res["fill_price_verified"])
                self.assertEqual(self.bot.history.recent()[-1]["event"], "EXIT_RECOVERED_FLAT")

    def test_stale_request_side_or_quantity_cannot_claim_old_broker_fills(self):
        self.broker_details["one-order"] = {
            "status": "FILLED", "filled": 5, "avg_price": 100,
        }
        for side, qty in (("SELL", 5), ("BUY", 6)):
            with self.subTest(side=side, qty=qty):
                state = self.state(order_ids=("one-order",))
                res = self.recover(state, side=side, qty=qty)
                self.assertFalse(res["fill_price_verified"])
        self.executor.detalle_orden.assert_not_called()

    def test_empty_duplicate_or_partial_execution_is_never_completed(self):
        cases = [
            self.state(order_ids=()),
            self.state(order_ids=("duplicate", "duplicate")),
            self.state(order_ids=("one-order",)),
        ]
        cases[1]["legs"] = [
            {"order_id": "duplicate", "status": "FILLED", "filled": 2,
             "avg_price": 100},
            {"order_id": "duplicate", "status": "FILLED", "filled": 3,
             "avg_price": 100},
        ]
        cases[2]["legs"] = [
            {"order_id": "one-order", "status": "FILLED", "filled": 4,
             "avg_price": 100},
        ]
        for state in cases:
            self.assertIsNone(self.bot._verified_fill_legs(state))
        self.assertEqual(self.bot._complete(self.key, cases[0])["status"],
                         "RECONCILIAR")
        self.assertIsNotNone(self.bot.pending.get(self.key))


if __name__ == "__main__":
    unittest.main()
