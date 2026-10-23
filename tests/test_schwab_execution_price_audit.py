"""Fresh broker-price audit never invents fills or repairs recorded evidence."""
import copy
import csv
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.audit_schwab_execution_prices import compare_row, fill_evidence, main, order_index


def order(oid="entry", instruction="BUY", prices=((2, 100), (3, 102))):
    return {"orderId": oid, "status": "FILLED", "quantity": 5, "filledQuantity": 5,
        "price": 999,  # A LIMIT instruction, deliberately different from executions.
        "orderLegCollection": [{"legId": 1, "instruction": instruction,
            "instrument": {"symbol": "META", "assetType": "EQUITY"}}],
        "orderActivityCollection": [{"activityType": "EXECUTION", "executionType": "FILL",
            "executionId": 11, "executionLegs": [
                {"legId": 1, "quantity": qty, "price": price} for qty, price in prices]}]}


def row(**changes):
    return {**dict(fecha="2026-10-22", hora_ny="10:19:09", modo="REAL", ticker="META",
        evento="POSITION_CLOSE", position_side="LONG", lado="BUY", cantidad="5",
        entrada="100", salida="105", pnl_usd="25", parent_order_id="entry", order_id="exit"), **changes}


class SchwabExecutionPriceAuditTests(unittest.TestCase):
    def test_weighted_execution_prices_not_limit_and_input_unchanged(self):
        raw = order()
        original = copy.deepcopy(raw)
        evidence = fill_evidence(raw, "entry", "META", "BUY")
        self.assertTrue(evidence["verified"])
        self.assertAlmostEqual(evidence["average_price"], 101.2)
        self.assertEqual(raw, original)

    def test_filled_status_and_limit_without_execution_legs_are_not_a_price(self):
        raw = order()
        raw.pop("orderActivityCollection")
        evidence = fill_evidence(raw, "entry", "META", "BUY")
        self.assertFalse(evidence["verified"])
        self.assertIsNone(evidence["average_price"])

    def test_actual_broker_order_type_and_closing_instruction_are_reported(self):
        for broker_type in ("MARKET", "LIMIT", "STOP", "STOP_LIMIT"):
            with self.subTest(order_type=broker_type):
                orders = {"entry": {**order("entry", "SELL_SHORT"), "orderType": "LIMIT"},
                          "exit": {**order("exit", "BUY_TO_COVER"), "orderType": broker_type}}
                result = compare_row(row(position_side="SHORT", lado="SELL", orderType="LIMIT"),
                                     lambda oid: (orders[oid], ""))
                self.assertTrue(result["full_quantity_pair_verified"])
                self.assertEqual(result["broker_final_close"]["order_type"], broker_type)
                self.assertEqual(result["broker_final_close"]["instruction"], "BUY_TO_COVER")

    def test_missing_or_invalid_type_is_not_inferred_from_limit_price(self):
        for value in (None, "", "UNKNOWN", "PRIVATE-UNTRUSTED", {"bad": "value"}, []):
            with self.subTest(order_type=value):
                evidence = fill_evidence({**order(), "orderType": value}, "entry", "META", "BUY")
                self.assertTrue(evidence["verified"])
                self.assertIsNone(evidence["order_type"])

    def test_wrong_order_identity_cannot_supply_type_or_instruction(self):
        evidence = fill_evidence({**order(), "orderType": "MARKET"}, "other-id", "META", "BUY")
        self.assertFalse(evidence["verified"])
        self.assertIsNone(evidence["order_type"])
        self.assertIsNone(evidence["instruction"])

    def test_partial_price_coverage_cannot_price_the_whole_filled_quantity(self):
        evidence = fill_evidence(order(prices=((2, 100),)), "entry", "META", "BUY")
        self.assertFalse(evidence["verified"])
        self.assertIn("coverage", evidence["reason"])

    def test_invalid_execution_prices_are_unresolved(self):
        for bad in (None, "", 0, -10, "NaN", "Infinity", True):
            with self.subTest(price=bad):
                self.assertFalse(fill_evidence(order(prices=((5, bad),)), "entry", "META", "BUY")["verified"])

    def test_exact_order_symbol_side_and_single_equity_leg_required(self):
        for changed in ("id", "symbol", "side", "asset", "multi", "leg"):
            with self.subTest(changed=changed):
                raw = order()
                leg = raw["orderLegCollection"][0]
                if changed == "id": raw["orderId"] = "another"
                if changed == "symbol": leg["instrument"]["symbol"] = "TSLA"
                if changed == "side": leg["instruction"] = "SELL"
                if changed == "asset": leg["instrument"]["assetType"] = "OPTION"
                if changed == "multi": raw["orderLegCollection"].append(copy.deepcopy(leg))
                if changed == "leg": leg["legId"] = 2
                self.assertFalse(fill_evidence(raw, "entry", "META", "BUY")["verified"])

    def test_working_nonfill_and_duplicate_activities_are_unresolved(self):
        for changed in ("working", "type", "duplicate"):
            raw = order()
            if changed == "working": raw["status"] = "WORKING"
            if changed == "type": raw["orderActivityCollection"][0]["activityType"] = "CANCEL"
            if changed == "duplicate":
                raw["orderActivityCollection"].append(copy.deepcopy(raw["orderActivityCollection"][0]))
            self.assertFalse(fill_evidence(raw, "entry", "META", "BUY")["verified"])

    def test_canceled_partial_has_a_price_but_not_whole_trade_pnl(self):
        orders = {"entry": order(prices=((5, 100),)), "exit": order("exit", "SELL", ((2, 110),))}
        orders["exit"].update(status="CANCELED", filledQuantity=2)
        result = compare_row(row(), lambda oid: (orders[oid], ""))
        self.assertTrue(result["broker_final_close"]["verified"])
        self.assertFalse(result["full_quantity_pair_verified"])
        self.assertIsNone(result["local_gross_calculated_from_broker_fills"])

    def test_missing_exit_id_never_looks_up_or_matches_a_ticker_candidate(self):
        lookup = Mock(return_value=(order(), ""))
        result = compare_row(row(order_id=""), lookup)
        lookup.assert_called_once_with("entry")
        self.assertFalse(result["broker_final_close"]["verified"])
        self.assertIsNone(result["difference_from_recorded_gross"])

    def test_local_filled_and_flat_claims_do_not_override_failed_broker_get(self):
        result = compare_row(row(estado="FILLED", broker_confirmed=True), lambda oid: (None, "Broker GET HTTP 503"))
        self.assertFalse(result["broker_entry"]["verified"])
        self.assertFalse(result["broker_final_close"]["verified"])
        self.assertIn("503", result["broker_entry"]["reason"])

    def test_long_and_short_calculations_keep_recorded_pnl_separate(self):
        for side, legacy, actions, expected in (
            ("LONG", "BUY", ("BUY", "SELL"), 50),
            ("SHORT", "SELL", ("SELL_SHORT", "BUY_TO_COVER"), -50),
        ):
            raw = row(position_side=side, lado=legacy)
            original = dict(raw)
            orders = {"entry": order("entry", actions[0], ((5, 100),)),
                      "exit": order("exit", actions[1], ((5, 110),))}
            result = compare_row(raw, lambda oid: (orders[oid], ""))
            self.assertTrue(result["full_quantity_pair_verified"])
            self.assertEqual(result["local_gross_calculated_from_broker_fills"], expected)
            self.assertEqual(result["local_record"]["pnl_usd"], 25)
            self.assertEqual(result["difference_from_recorded_gross"], expected - 25)
            self.assertEqual(raw, original)

    def test_contradictory_direction_does_not_qualify(self):
        result = compare_row(row(lado="SELL"), lambda oid: (order(oid), ""))
        self.assertFalse(result["full_quantity_pair_verified"])

    def test_nested_exact_order_and_conflicting_duplicate_ids(self):
        child = order()
        self.assertEqual(order_index([{"childOrderStrategies": [child]}])["entry"], child)
        self.assertIsNone(order_index([child, {**child, "quantity": 6}])["entry"])

    def test_command_gets_only_and_leaves_files_and_private_ids_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history.csv"
            with history.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row()))
                writer.writeheader()
                writer.writerow(row())
            (root / "config.yaml").write_text(json.dumps({"logging": {"historial_operaciones": str(history)},
                "execution_engine": {"enabled": True, "market_fallback": False, "limit_wait_sec": 3}}))
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            client = Mock(spec=["obtener_account_hash", "_get_autorizado"])
            client.obtener_account_hash.return_value = "PRIVATE-HASH"
            response = Mock(status_code=200)
            response.json.return_value = [{**order(), "orderType": "LIMIT"},
                                         {**order("exit", "SELL", ((5, 110),)), "orderType": "MARKET"}]
            client._get_autorizado.return_value = response
            output = io.StringIO()
            with patch("scripts.audit_schwab_execution_prices.ROOT", root), \
                 patch.dict(os.environ, {"SCHWAB_ACCOUNT_NUMBER": "PRIVATE-ACCOUNT"}), \
                 patch("api.schwab_client.SchwabClient", return_value=client), redirect_stdout(output):
                self.assertEqual(main(["--date", "2026-10-22"]), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["counts"]["full_quantity_pairs_verified"], 1)
            self.assertEqual(report["trades"][0]["broker_final_close"]["order_type"], "MARKET")
            self.assertTrue(report["current_exit_settings"]["v2_enabled"])
            self.assertFalse(report["current_exit_settings"]["market_fallback"])
            self.assertEqual(report["current_exit_settings"]["limit_wait_sec"], 3)
            self.assertFalse(report["history_models_training_modified"])
            self.assertEqual(client._get_autorizado.call_count, 1)
            client.obtener_account_hash.assert_called_once_with("PRIVATE-ACCOUNT")
            self.assertNotIn("PRIVATE", output.getvalue())
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})


if __name__ == "__main__":
    unittest.main()
