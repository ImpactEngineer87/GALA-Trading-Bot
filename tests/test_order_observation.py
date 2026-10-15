"""Offline regressions for scoped order observations and fresh broker evidence."""

import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from api.hybrid_order_executor import EjecutorHibridoDeOrdenes
from core.execution_v2 import SmartExit
from core.risk_manager import OrdenTradingCalculada
from core.runtime_store import JsonState


class OrderObservationTests(unittest.TestCase):
    def setUp(self):
        request_patch = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Unexpected broker/HTTP request"),
        )
        socket_patch = patch(
            "socket.socket.connect",
            side_effect=AssertionError("Unexpected network connection"),
        )
        self.request = request_patch.start()
        self.socket_connect = socket_patch.start()
        self.addCleanup(request_patch.stop)
        self.addCleanup(socket_patch.stop)
        self.executor = EjecutorHibridoDeOrdenes(paper_trading=False)
        self.addCleanup(self.executor._session.close)

    def tearDown(self):
        self.request.assert_not_called()
        self.socket_connect.assert_not_called()

    @staticmethod
    def response(data=None, status=200):
        response = Mock()
        response.status_code = status
        response.json.return_value = data
        response.headers = {"Location": "/accounts/account-a/orders/new-order"}
        if status >= 400:
            response.text = "order lookup failed"
            response.raise_for_status.side_effect = requests.HTTPError(
                "order lookup failed", response=response,
            )
        return response

    def test_working_order_in_scoped_account_skips_individual_lookup(self):
        working = [{"orderId": 123, "status": "WORKING"}]
        with patch.object(self.executor, "_buscar_orden_en_listado") as lookup:
            with self.executor.order_observation_scope(
                working, account_number="account-a",
            ):
                self.assertEqual(self.executor._estado_orden("account-a", "123"), "WORKING")
                self.assertEqual(self.executor._estado_orden("account-a", 123), "WORKING")
            lookup.assert_not_called()

    def test_missing_terminal_and_pending_statuses_use_fresh_lookup(self):
        snapshot = [
            {"orderId": "filled", "status": "FILLED"},
            {"orderId": "pending", "status": "QUEUED"},
            {"orderId": "unknown"},
        ]
        with patch.object(
            self.executor, "_buscar_orden_en_listado", return_value={"status": "CANCELED"},
        ) as lookup:
            with self.executor.order_observation_scope(snapshot, account_number="account-a"):
                for order_id in ("filled", "pending", "unknown", "absent"):
                    self.assertEqual(self.executor._estado_orden("account-a", order_id), "CANCELED")
            self.assertEqual(lookup.call_count, 4)

    def test_same_order_id_for_different_account_never_uses_observation(self):
        snapshot = [{"orderId": "123", "status": "WORKING"}]
        with patch.object(
            self.executor, "_buscar_orden_en_listado", return_value={"status": "FILLED"},
        ) as lookup:
            with self.executor.order_observation_scope(snapshot, account_number="account-a"):
                self.assertEqual(self.executor._estado_orden("account-b", "123"), "FILLED")
            lookup.assert_called_once_with("account-b", "123")

    def test_scope_without_account_does_not_reuse_cross_account_evidence(self):
        with patch.object(
            self.executor, "_buscar_orden_en_listado", return_value={"status": "FILLED"},
        ) as lookup:
            with self.executor.order_observation_scope([{"orderId": "123", "status": "WORKING"}]):
                self.assertEqual(self.executor._estado_orden("account-a", "123"), "FILLED")
            lookup.assert_called_once_with("account-a", "123")

    def test_nested_working_child_is_available_without_lookup(self):
        snapshot = [{"orderId": "parent", "status": "FILLED", "childOrderStrategies": [
            {"orderId": "oco", "childOrderStrategies": [
                {"orderId": "stop", "status": "WORKING"},
            ]},
        ]}]
        with patch.object(self.executor, "_buscar_orden_en_listado") as lookup:
            with self.executor.order_observation_scope(snapshot, account_number="account-a"):
                self.assertEqual(self.executor._estado_orden("account-a", "stop"), "WORKING")
            lookup.assert_not_called()

    def test_scope_is_a_fixed_observation_and_does_not_follow_source_mutation(self):
        snapshot = [{"orderId": "123", "status": "WORKING"}]
        with patch.object(self.executor, "_buscar_orden_en_listado") as lookup:
            with self.executor.order_observation_scope(snapshot, account_number="account-a"):
                snapshot[0]["status"] = "FILLED"
                snapshot.clear()
                self.assertEqual(self.executor._estado_orden("account-a", "123"), "WORKING")
            lookup.assert_not_called()

    def test_nested_scope_restores_parent_after_exception_and_expires_on_exit(self):
        with patch.object(
            self.executor, "_buscar_orden_en_listado", return_value={"status": "FILLED"},
        ) as lookup:
            with self.executor.order_observation_scope(
                [{"orderId": "outer", "status": "WORKING"}], account_number="account-a",
            ):
                try:
                    with self.executor.order_observation_scope(
                        [{"orderId": "inner", "status": "WORKING"}], account_number="account-b",
                    ):
                        self.assertEqual(self.executor._estado_orden("account-b", "inner"), "WORKING")
                        self.assertEqual(self.executor._estado_orden("account-a", "outer"), "FILLED")
                        raise RuntimeError("scope interrupted")
                except RuntimeError:
                    pass
                self.assertEqual(self.executor._estado_orden("account-a", "outer"), "WORKING")
            self.assertEqual(self.executor._estado_orden("account-a", "outer"), "FILLED")
            self.assertEqual(lookup.call_count, 2)

    def test_scoped_observations_are_thread_local(self):
        with patch.object(
            self.executor, "_buscar_orden_en_listado", return_value={"status": "FILLED"},
        ) as lookup:
            with self.executor.order_observation_scope(
                [{"orderId": "123", "status": "WORKING"}], account_number="account-a",
            ):
                with ThreadPoolExecutor(max_workers=1) as workers:
                    result = workers.submit(self.executor._estado_orden, "account-a", "123").result(timeout=2)
                self.assertEqual(result, "FILLED")
                self.assertEqual(self.executor._estado_orden("account-a", "123"), "WORKING")
            lookup.assert_called_once_with("account-a", "123")

    def test_invalidation_in_another_thread_expires_existing_scope(self):
        with patch.object(
            self.executor, "_buscar_orden_en_listado", return_value={"status": "FILLED"},
        ) as lookup:
            with self.executor.order_observation_scope(
                [{"orderId": "123", "status": "WORKING"}], account_number="account-a",
            ):
                with ThreadPoolExecutor(max_workers=1) as workers:
                    workers.submit(self.executor._invalidate_order_observations).result(timeout=2)
                self.assertEqual(self.executor._estado_orden("account-a", "123"), "FILLED")
            lookup.assert_called_once_with("account-a", "123")

    def test_invalidated_parent_is_not_revived_after_nested_scope_exit(self):
        snapshot = [{"orderId": "123", "status": "WORKING"}]
        with patch.object(
            self.executor, "_buscar_orden_en_listado", return_value={"status": "FILLED"},
        ) as lookup:
            with self.executor.order_observation_scope(snapshot, account_number="account-a"):
                with self.executor.order_observation_scope(snapshot, account_number="account-a"):
                    self.executor._invalidate_order_observations()
                self.assertEqual(self.executor._estado_orden("account-a", "123"), "FILLED")
            lookup.assert_called_once_with("account-a", "123")

    def test_invalidation_during_scope_construction_does_not_revalidate_snapshot(self):
        original_iterator = self.executor._iter_order_nodes

        def concurrently_invalidated(orders):
            self.executor._invalidate_order_observations()
            yield from original_iterator(orders)

        with patch.object(self.executor, "_iter_order_nodes", side_effect=concurrently_invalidated), \
             patch.object(self.executor, "_buscar_orden_en_listado", return_value={"status": "FILLED"}) as lookup:
            with self.executor.order_observation_scope(
                [{"orderId": "123", "status": "WORKING"}], account_number="account-a",
            ):
                self.assertEqual(self.executor._estado_orden("account-a", "123"), "FILLED")
            lookup.assert_called_once_with("account-a", "123")

    def test_order_list_fallback_finds_nested_child(self):
        child = {"orderId": "123", "status": "CANCELED"}
        transport = Mock()
        transport.get.side_effect = [
            self.response(status=400),
            self.response([{"orderId": "parent", "status": "FILLED", "childOrderStrategies": [
                {"orderId": "oco", "status": "AWAITING_CONDITION", "childOrderStrategies": [child]},
            ]}]),
        ]
        with patch.object(self.executor, "_http_session", return_value=transport):
            self.assertEqual(self.executor._buscar_orden_en_listado("account-a", "123"), child)
        self.assertEqual(transport.get.call_count, 2)

    def test_direct_lookup_rejects_different_order_identity(self):
        expected = {"orderId": "123", "status": "WORKING"}
        transport = Mock()
        transport.get.side_effect = [
            self.response({"orderId": "different-order", "status": "FILLED"}),
            self.response([expected]),
        ]
        with patch.object(self.executor, "_http_session", return_value=transport):
            self.assertEqual(self.executor._buscar_orden_en_listado("account-a", "123"), expected)
        self.assertEqual(transport.get.call_count, 2)

    def test_valid_direct_lookup_recovers_once_without_listing(self):
        direct_error = self.response(status=400).raise_for_status.side_effect
        list_error = self.response(status=500).raise_for_status.side_effect
        expected = {"orderId": 123, "status": "FILLED", "filledQuantity": 2}
        transport = Mock()
        transport.get.return_value = self.response(expected)
        with patch("api.hybrid_order_executor._ps") as printer, \
             patch("api.hybrid_order_executor.time.monotonic", return_value=100), \
             patch.object(self.executor, "_http_session", return_value=transport):
            self.executor._report_order_read_failure("account-a", direct_error, list_error)
            self.executor._report_order_read_failure("account-a", direct_error, list_error)
            self.assertEqual(self.executor._buscar_orden_en_listado("account-a", "123"), expected)
            self.assertEqual(self.executor._buscar_orden_en_listado("account-a", "123"), expected)
        self.assertEqual(printer.call_count, 2)
        self.assertIn("[WARN]", printer.call_args_list[0].args[0])
        self.assertIn("[OK]", printer.call_args_list[1].args[0])
        self.assertIn("repeticiones agrupadas=1", printer.call_args_list[1].args[0])
        self.assertEqual(transport.get.call_count, 2)
        for call in transport.get.call_args_list:
            self.assertTrue(call.args[0].endswith("/accounts/account-a/orders/123"))

    def test_order_read_diagnostics_throttle_repeats_for_thirty_seconds(self):
        direct_error = self.response(status=400).raise_for_status.side_effect
        list_error = self.response(status=500).raise_for_status.side_effect
        with patch("api.hybrid_order_executor._ps") as printer, \
             patch("api.hybrid_order_executor.time.monotonic", side_effect=[100, 101, 129.9, 130]):
            for _ in range(4):
                self.executor._report_order_read_failure("account-a", direct_error, list_error)
        self.assertEqual(printer.call_count, 2)
        self.assertIn("GET por id HTTP=400", printer.call_args_list[0].args[0])
        self.assertIn("HTTP 500", printer.call_args_list[0].args[0])
        self.assertIn("repeticiones=2", printer.call_args_list[1].args[0])

    def test_new_error_signature_and_other_accounts_report_immediately(self):
        direct_error = self.response(status=400).raise_for_status.side_effect
        list_error = self.response(status=500).raise_for_status.side_effect
        changed_error = self.response(status=503).raise_for_status.side_effect
        with patch("api.hybrid_order_executor._ps") as printer, \
             patch("api.hybrid_order_executor.time.monotonic", return_value=100):
            self.executor._report_order_read_failure("account-a", direct_error, list_error)
            self.executor._report_order_read_failure("account-a", direct_error, changed_error)
            self.executor._report_order_read_failure("account-b", direct_error, list_error)
        self.assertEqual(printer.call_count, 3)

    def test_detail_ignores_scope_and_observes_new_fill(self):
        live = {"orderId": "123", "status": "FILLED", "filledQuantity": 2,
                "orderActivityCollection": [{"executionLegs": [{"price": 101.5, "quantity": 2}]}]}
        with patch.object(self.executor, "_buscar_orden_en_listado", return_value=live) as lookup:
            with self.executor.order_observation_scope(
                [{"orderId": "123", "status": "WORKING"}], account_number="account-a",
            ):
                detail = self.executor.detalle_orden("account-a", "123")
        self.assertEqual(detail["status"], "FILLED")
        self.assertEqual(detail["filled"], 2.0)
        self.assertEqual(detail["avg_price"], 101.5)
        lookup.assert_called_once_with("account-a", "123")

    def test_working_check_reads_fresh_snapshot_even_inside_scope(self):
        provider = Mock(side_effect=[[], [{"orderId": "123", "status": "WORKING"}]])
        self.executor.set_working_orders_provider(provider)
        with self.executor.order_observation_scope(
            [{"orderId": "123", "status": "WORKING"}], account_number="account-a",
        ):
            self.assertFalse(self.executor.orden_trabajando("account-a", "123"))
            self.assertTrue(self.executor.orden_trabajando("account-a", "123"))
        self.assertEqual(provider.call_count, 2)

    def test_detail_fallback_retains_nested_partial_fill_and_weighted_execution_price(self):
        child = {"orderId": "123", "status": "WORKING", "filledQuantity": 1,
                 "orderActivityCollection": [{"executionLegs": [
                     {"price": 100, "quantity": 1}, {"price": 103, "quantity": 2},
                 ]}]}
        provider = Mock(return_value=[{
            "orderId": "parent", "status": "FILLED", "childOrderStrategies": [child],
        }])
        self.executor.set_working_orders_provider(provider)
        with patch.object(self.executor, "_buscar_orden_en_listado", return_value=None):
            detail = self.executor.detalle_orden("account-a", "123")
        self.assertEqual(detail["status"], "WORKING")
        self.assertEqual(detail["filled"], 3.0)
        self.assertAlmostEqual(detail["avg_price"], 102.0)
        self.assertEqual(detail.get("status_source"), "WORKING_SNAPSHOT")
        provider.assert_called_once_with("account-a")

    def test_detail_fallback_does_not_invent_average_price_from_limit(self):
        provider = Mock(return_value=[{
            "orderId": "123", "status": "WORKING", "filledQuantity": 2, "price": 105,
        }])
        self.executor.set_working_orders_provider(provider)
        with patch.object(self.executor, "_buscar_orden_en_listado", return_value=None):
            detail = self.executor.detalle_orden("account-a", "123")
        self.assertEqual(detail["filled"], 2.0)
        self.assertEqual(detail["avg_price"], 0.0)

    def test_missing_order_after_fallback_remains_unknown(self):
        self.executor.set_working_orders_provider(Mock(return_value=[]))
        with patch.object(self.executor, "_buscar_orden_en_listado", return_value=None):
            self.assertEqual(self.executor.detalle_orden("account-a", "123"), {
                "status": "DESCONOCIDO", "filled": 0.0, "avg_price": 0.0,
            })

    def test_malformed_provider_snapshot_fails_closed(self):
        for data in (None, {}, "bad response", [None], ["bad order"],
                     [{"childOrderStrategies": {"orderId": "123"}}]):
            with self.subTest(data=data):
                self.executor.set_working_orders_provider(Mock(return_value=data))
                self.assertIsNone(self.executor._working_orders_snapshot("account-a"))
                self.assertIsNone(self.executor.orden_trabajando("account-a", "123"))
                self.assertIsNone(self.executor.ordenes_trabajando_ticker("account-a", "AAPL"))

    def test_malformed_raw_snapshot_fails_closed(self):
        for data in ({}, "bad response", [None],
                     [{"childOrderStrategies": "bad children"}]):
            with self.subTest(data=data):
                transport = Mock()
                transport.get.return_value = self.response(data)
                with patch.object(self.executor, "_http_session", return_value=transport):
                    self.assertIsNone(self.executor._working_orders_snapshot("account-a"))

    def test_live_cancel_uses_fresh_filled_state_instead_of_stale_working_scope(self):
        with patch.object(self.executor, "_buscar_orden_en_listado", return_value={
            "orderId": "123", "status": "FILLED", "filledQuantity": 2,
        }), patch.object(self.executor, "_solicitar_cancelacion") as cancel:
            with self.executor.order_observation_scope(
                [{"orderId": "123", "status": "WORKING"}], account_number="account-a",
            ):
                result = self.executor.cancelar_orden_confirmada("account-a", "123", ticker="AAPL")
        self.assertFalse(result["confirmado"])
        self.assertEqual(result["broker_status"], "FILLED")
        self.assertEqual(result["filled"], 2.0)
        cancel.assert_not_called()

    def test_snapshot_observed_during_http_mutation_expires_after_completion(self):
        for reject in (False, True):
            with self.subTest(reject=reject):
                observed_generations = []
                transport = Mock()

                def broker_submission(*args, **kwargs):
                    observed_generations.append(self.executor._order_observation_generation)
                    if reject:
                        raise requests.HTTPError("offline rejected submission")
                    return self.response(status=201)

                transport.post.side_effect = broker_submission
                with patch.object(self.executor, "_http_session", return_value=transport), \
                     patch.object(self.executor, "_buscar_orden_en_listado", return_value={"status": "FILLED"}) as lookup:
                    try:
                        self.executor._submit_order_request("post", "https://offline.invalid/orders")
                    except requests.HTTPError:
                        self.assertTrue(reject)
                    self.assertEqual(len(observed_generations), 1)
                    with self.executor.order_observation_scope(
                        [{"orderId": "123", "status": "WORKING"}], account_number="account-a",
                        generation=observed_generations[0],
                    ):
                        self.assertEqual(self.executor._estado_orden("account-a", "123"), "FILLED")
                    lookup.assert_called_once_with("account-a", "123")

    def test_smart_exit_rejection_invalidates_observation_and_retains_durable_intent(self):
        account, ticker = "account-a", "AAPL"
        key = hashlib.sha256(account.encode()).hexdigest()[:16] + ":" + ticker
        transport = Mock()
        with tempfile.TemporaryDirectory(prefix="gala-order-observation-") as folder:
            pending_path = Path(folder) / "pending.json"
            smart_exit = SmartExit.__new__(SmartExit)
            smart_exit.executor = self.executor
            smart_exit.config = {}
            smart_exit.pending = JsonState(pending_path)
            smart_exit.history = Mock()

            def reject_submission(*args, **kwargs):
                self.assertEqual(self.executor._estado_orden(account, "123"), "FILLED")
                persisted = JsonState(pending_path).get(key)
                self.assertEqual(persisted["quantity"], 2)
                self.assertEqual(persisted["side"], "BUY")
                self.assertEqual(len(persisted["legs"]), 1)
                self.assertEqual(persisted["legs"][0]["status"], "SUBMITTING")
                self.assertEqual(persisted["legs"][0]["order_id"], "")
                raise requests.HTTPError("offline rejected smart exit")

            transport.post.side_effect = reject_submission
            with patch.object(self.executor, "_http_session", return_value=transport), \
                 patch.object(self.executor, "_buscar_orden_en_listado", return_value={"status": "FILLED"}) as lookup, \
                 patch.object(self.executor, "_position_snapshot", return_value={"long": 2, "short": 0}), \
                 patch.object(self.executor, "esperar_estado_orden") as wait:
                with self.executor.order_observation_scope(
                    [{"orderId": "123", "status": "WORKING"}], account_number=account,
                ):
                    self.assertEqual(self.executor._estado_orden(account, "123"), "WORKING")
                    result = smart_exit.close(ticker, "BUY", 2, account, "stop loss")
                    self.assertEqual(self.executor._estado_orden(account, "123"), "FILLED")
                self.assertEqual(lookup.call_count, 2)
                wait.assert_not_called()
            self.assertEqual(result["status"], "RECONCILIAR")
            self.assertFalse(result["posicion_plana"])
            self.assertEqual(result["cantidad"], 2)
            self.assertIn("HTTPError", result["mensaje"])
            persisted = JsonState(pending_path).get(key)
            self.assertEqual(persisted["legs"][0]["status"], "SUBMITTING")
            self.assertEqual(persisted["legs"][0]["filled"], 0)
            smart_exit.history.append.assert_not_called()
        transport.post.assert_called_once()
        submitted = transport.post.call_args.kwargs["json"]
        self.assertEqual(submitted["orderType"], "MARKET")
        leg = submitted["orderLegCollection"][0]
        self.assertEqual(leg["instruction"], "SELL")
        self.assertEqual(leg["positionEffect"], "CLOSING")
        self.assertEqual(leg["quantity"], 2)

    def test_every_real_mutation_invalidates_observations_before_http_submission(self):
        order = OrdenTradingCalculada(
            ticker="AAPL", tipo="BUY", precio_limite=100.0,
            stop_loss=98.0, trailing_stop_pct=1.0, cantidad=2,
            riesgo_usd=4.0, atr_actual=1.0,
        )
        mutations = {
            "entry": lambda: self.executor._enviar_orden_real(order, "LIMIT", "account-a", "offline test"),
            "exit": lambda: self.executor.colocar_salida("AAPL", "BUY", 2, 98.0, "STOP", "account-a"),
            "liquidate": lambda: self.executor._liquidar_market_legacy("AAPL", "BUY", 2, "account-a"),
            "cancel": lambda: self.executor._solicitar_cancelacion("account-a", "123"),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                transport = Mock()
                http_calls = []

                def reject_submission(*args, **kwargs):
                    http_calls.append((args, kwargs))
                    self.assertEqual(self.executor._estado_orden("account-a", "123"), "FILLED")
                    raise requests.HTTPError("offline rejected submission")

                transport.post.side_effect = reject_submission
                transport.delete.side_effect = reject_submission
                with patch.object(self.executor, "_http_session", return_value=transport), \
                     patch.object(self.executor, "_buscar_orden_en_listado", return_value={"status": "FILLED"}) as lookup, \
                     patch.object(self.executor, "_position_snapshot", return_value={"long": 2, "short": 0, "average_price": 100}):
                    with self.executor.order_observation_scope(
                        [{"orderId": "123", "status": "WORKING"}], account_number="account-a",
                    ):
                        self.assertEqual(self.executor._estado_orden("account-a", "123"), "WORKING")
                        mutate()
                        self.assertEqual(self.executor._estado_orden("account-a", "123"), "FILLED")
                    self.assertEqual(len(http_calls), 1)
                    self.assertEqual(lookup.call_count, 2)


if __name__ == "__main__":
    unittest.main()
