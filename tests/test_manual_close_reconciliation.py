"""Offline broker-fill evidence for manual REAL close recovery."""
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.manual_close_reconciliation import (
    verified_gala_orphan_entry, verified_manual_close,
)


def order(oid, instruction, price, when, *, tag="", order_type="LIMIT"):
    return {
        "orderId": oid, "status": "FILLED", "orderType": order_type,
        "tag": tag, "quantity": 3, "filledQuantity": 3,
        "orderLegCollection": [{"instruction": instruction,
                                "instrument": {"assetType": "EQUITY", "symbol": "TSLA"}}],
        "orderActivityCollection": [{"activityType": "EXECUTION",
                                     "executionType": "FILL", "executionId": oid + "-fill",
                                     "executionLegs": [{"quantity": 3, "price": price,
                                                        "time": when.isoformat()}]}],
    }


class ManualCloseProofTests(unittest.TestCase):
    def setUp(self):
        start = datetime.now(ZoneInfo("America/New_York")).replace(
            hour=9, minute=31, second=0, microsecond=0)
        self.orders = [
            order("entry", "SELL_SHORT", 410.8, start),
            order("external", "BUY_TO_COVER", 410.66, start + timedelta(minutes=1),
                  tag="API_TOS:CHART", order_type="MARKET"),
        ]
        self.args = dict(ticker="TSLA", side="SELL", quantity=3,
                         total_quantity=3, entry_order_id="entry",
                         entry_price=410.8, managed_order_ids={"entry", "stop"},
                         session_ny=start.date().isoformat())

    def test_exact_market_or_limit_manual_fill_counts_for_risk(self):
        proof = verified_manual_close(self.orders, **self.args)
        self.assertEqual(proof["order_id"], "external")
        self.assertEqual(proof["avg_price"], 410.66)
        self.assertEqual(proof["order_type"], "MARKET")
        self.orders[1]["orderType"] = "LIMIT"
        self.assertEqual(verified_manual_close(self.orders, **self.args)["order_type"], "LIMIT")

    def test_ambiguous_or_unpriced_external_fills_stay_blocked(self):
        self.orders.append(order("another", "BUY_TO_COVER", 410.5,
                                 datetime.fromisoformat(self.orders[1]["orderActivityCollection"][0]
                                                          ["executionLegs"][0]["time"]) + timedelta(seconds=1),
                                 tag="API_TOS:CHART"))
        self.assertEqual(verified_manual_close(self.orders, **self.args), {})
        self.orders.pop()
        self.orders[1]["orderActivityCollection"][0]["executionLegs"][0]["price"] = 0
        self.assertEqual(verified_manual_close(self.orders, **self.args), {})

    def test_exact_external_close_is_verified_independently_of_broker_tag(self):
        for tag in ('OTHER_API','API_TOS:TraderAPI',''):
            self.orders[1]['tag']=tag
            self.assertEqual(verified_manual_close(self.orders, **self.args)['order_id'],'external')

    def test_bad_entry_and_saturated_window_never_gain_proof(self):
        self.assertEqual(verified_manual_close(self.orders, **{**self.args, "entry_price": 411}), {})
        self.assertEqual(verified_manual_close(self.orders * 250, **self.args), {})

    def test_other_owned_fills_must_balance_the_expected_remaining_quantity(self):
        extra=order('stop','BUY_TO_COVER',410.7,datetime.fromisoformat(
            self.orders[1]['orderActivityCollection'][0]['executionLegs'][0]['time']))
        self.assertEqual(verified_manual_close(self.orders+[extra],**self.args),{})
        self.assertEqual(verified_manual_close(self.orders,**{**self.args,'total_quantity':4}),{})

    def test_orphan_adoption_requires_latest_audited_gala_entry(self):
        entry = self.orders[0]
        kwargs = dict(ticker="TSLA", side="SELL", quantity=3,
                      broker_price=410.8, submitted_entry_ids={"entry"},
                      submitted_order_ids={"entry"},
                      session_ny=self.args["session_ny"])
        self.assertEqual(verified_gala_orphan_entry([entry], **kwargs)["order_id"], "entry")
        entry["tag"] = "API_TOS:TraderAPI"
        self.assertEqual(verified_gala_orphan_entry([entry], **kwargs)["order_id"], "entry")
        self.assertEqual(verified_gala_orphan_entry(self.orders, **kwargs), {})
        self.assertEqual(verified_gala_orphan_entry([entry],
                          **{**kwargs, "submitted_entry_ids": set()}), {})
        self.assertEqual(verified_gala_orphan_entry([entry],
                          **{**kwargs, "broker_price": 409}), {})
        partial = order("partial", "BUY_TO_COVER", 409.5,
                        datetime.fromisoformat(self.orders[1]["orderActivityCollection"][0]
                                                 ["executionLegs"][0]["time"]))
        partial["quantity"] = partial["filledQuantity"] = 1
        partial["orderActivityCollection"][0]["executionLegs"][0]["quantity"] = 1
        self.assertEqual(verified_gala_orphan_entry(
            [entry, partial], **{**kwargs, "quantity": 2,
                                 "submitted_order_ids": {"entry", "partial"}})["order_id"], "entry")


if __name__ == "__main__":
    unittest.main()
