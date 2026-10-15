"""Offline regressions for trustworthy Schwab WORKING-order snapshots."""

import unittest
from unittest.mock import Mock, patch

import requests

from api.order_sync import valid_order_snapshot
from api import schwab_client


class OrderSnapshotValidationTests(unittest.TestCase):
    def test_empty_and_complete_snapshots_are_valid(self):
        for orders in (
            [],
            [{"orderId": 123, "status": "WORKING"}],
            [{"orderId": "123", "status": "NEW_BROKER_STATE"}],
            [{"orderId": 123, "status": "WORKING", "childOrderStrategies": None}],
            [{"orderId": 123, "status": "WORKING", "childOrderStrategies": []}],
            [{
                "orderId": 123, "status": "FILLED", "childOrderStrategies": [{
                    "orderId": 456, "status": "WORKING", "childOrderStrategies": [{
                        "orderId": 789, "status": "AWAITING_PARENT_ORDER",
                    }],
                }],
            }],
        ):
            with self.subTest(orders=orders):
                self.assertTrue(valid_order_snapshot(orders))

    def test_malformed_roots_are_unknown(self):
        for orders in (None, {}, {"errors": []}, "[]", (), [None], ["order"], [123]):
            with self.subTest(orders=orders):
                self.assertFalse(valid_order_snapshot(orders))

    def test_missing_or_invalid_identity_or_status_is_unknown(self):
        for order in (
            {}, {"orderId": 123}, {"status": "WORKING"},
            {"orderId": None, "status": "WORKING"},
            {"orderId": " ", "status": "WORKING"},
            {"orderId": 0, "status": "WORKING"},
            {"orderId": False, "status": "WORKING"},
            {"orderId": {}, "status": "WORKING"},
            {"orderId": 123, "status": None},
            {"orderId": 123, "status": " "},
            {"orderId": 123, "status": 500},
        ):
            with self.subTest(order=order):
                self.assertFalse(valid_order_snapshot([order]))

    def test_malformed_children_cannot_hide_under_valid_parent(self):
        for children in ({}, "[]", (), [None], [{}], [{"orderId": 456}]):
            with self.subTest(children=children):
                self.assertFalse(valid_order_snapshot([{
                    "orderId": 123, "status": "WORKING",
                    "childOrderStrategies": children,
                }]))

    def test_invalid_grandchild_makes_entire_snapshot_unknown(self):
        self.assertFalse(valid_order_snapshot([{
            "orderId": 123, "status": "FILLED", "childOrderStrategies": [{
                "orderId": 456, "status": "WORKING", "childOrderStrategies": [{}],
            }],
        }]))


class SchwabWorkingOrdersTests(unittest.TestCase):
    def setUp(self):
        # Bypass initialization so no operator credentials or runtime files are
        # needed. Every broker read and authentication check is a local mock.
        self.client = schwab_client.SchwabClient.__new__(schwab_client.SchwabClient)
        self.client._asegurar_token = Mock(return_value=True)
        self.response = Mock()
        self.client._get_autorizado = Mock(return_value=self.response)
        self.logs = self.enterContext(patch.object(schwab_client, "_log"))
        self.network = self.enterContext(patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Unexpected broker/HTTP request"),
        ))
        self.socket_connect = self.enterContext(patch(
            "socket.socket.connect",
            side_effect=AssertionError("Unexpected network connection"),
        ))

    def tearDown(self):
        self.network.assert_not_called()
        self.socket_connect.assert_not_called()

    def test_empty_success_is_authoritative_empty(self):
        self.response.json.return_value = []
        self.assertEqual(self.client.obtener_ordenes_abiertas("offline-account"), [])
        self.logs.assert_not_called()

    def test_complete_nested_success_preserves_original_data(self):
        orders = [{
            "orderId": 123, "status": "WORKING", "filledQuantity": 2,
            "childOrderStrategies": [{"orderId": 456, "status": "FILLED"}],
        }]
        self.response.json.return_value = orders
        self.assertIs(self.client.obtener_ordenes_abiertas("offline-account"), orders)
        self.logs.assert_not_called()
        request = self.client._get_autorizado.call_args
        self.assertTrue(request.args[0].endswith("/accounts/offline-account/orders"))
        self.assertEqual(request.kwargs["params"]["status"], "WORKING")

    def test_malformed_success_is_unknown_with_diagnostic(self):
        for payload in (
            {}, {"errors": [{"status": 500}]}, None,
            [None], [{"orderId": 123}],
            [{"orderId": 123, "status": "WORKING", "childOrderStrategies": [{}]}],
        ):
            with self.subTest(payload=payload):
                self.response.json.return_value = payload
                self.logs.reset_mock()
                self.assertIsNone(self.client.obtener_ordenes_abiertas("offline-account"))
                self.logs.assert_called_once()
                self.assertIn("desconocido", self.logs.call_args.args[0])

    def test_failed_http_or_json_read_is_unknown(self):
        self.response.raise_for_status.side_effect = requests.HTTPError("broker error")
        self.assertIsNone(self.client.obtener_ordenes_abiertas("offline-account"))
        self.response.json.assert_not_called()

        self.response.raise_for_status.side_effect = None
        self.response.json.side_effect = ValueError("malformed JSON")
        self.assertIsNone(self.client.obtener_ordenes_abiertas("offline-account"))

    def test_missing_account_or_token_does_not_request_broker(self):
        self.assertIsNone(self.client.obtener_ordenes_abiertas(""))
        self.client._asegurar_token.assert_not_called()
        self.client._asegurar_token.return_value = False
        self.assertIsNone(self.client.obtener_ordenes_abiertas("offline-account"))
        self.client._get_autorizado.assert_not_called()

    def test_authorized_read_failure_is_unknown(self):
        self.client._get_autorizado.return_value = None
        self.assertIsNone(self.client.obtener_ordenes_abiertas("offline-account"))
        self.response.json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
