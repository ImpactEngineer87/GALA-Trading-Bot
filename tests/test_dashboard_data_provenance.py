"""Offline regressions: no simulated/missing data presented as broker facts."""
import copy
import csv
import json
import threading
import time
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from core.dashboard_evidence import annotate_history, session_date
from core.ticker_performance import build_ticker_performance
from core.ticker_analysis import build_ticker_accuracy_analysis


def evidence():
    common = dict(ticker="META", modo="REAL", position_id="position-1",
                  execution_id="execution-1", position_side="SHORT")
    row = dict(common, evento="POSITION_CLOSE", fecha="2026-10-22",
               timestamp_utc="2026-10-22T19:00:00Z", entrada=100, salida=98,
               cantidad=5, pnl_usd=10, parent_order_id="entry-1", order_id="exit-1")
    entry = dict(common, evento="ENTRY_FILLED", estado="FILLED", order_id="entry-1",
                 cantidad=5, precio=100, detalle={"broker_price_verified": True})
    close = dict(common, evento="POSITION_CLOSED", estado="CLOSED", order_id="exit-1",
                 cantidad=5, precio=98, pnl_usd=10, detalle={
                     "broker_confirmed": True, "fill_price_verified": True,
                     "salida_real": 98, "tp1_ejecutado": False})
    return row, [entry, close]


class DashboardEvidenceTests(unittest.TestCase):
    def test_exact_linked_fills_are_local_gross_calculation_not_broker_pnl(self):
        row, audit = evidence()
        original = copy.deepcopy((row, audit))
        result = annotate_history([row], audit)[0]
        self.assertTrue(result["performance_verified"])
        self.assertEqual(result["display_pnl_usd"], 10)
        self.assertEqual(result["pnl_source"], "LOCAL_RECORDED_FILLS_GROSS")
        self.assertEqual((row, audit), original)

    def test_order_ids_or_local_closure_alone_do_not_prove_a_real_fill(self):
        row, audit = evidence()
        for bad_audit in ([], audit[:1], audit[1:]):
            result = annotate_history([row], bad_audit)[0]
            self.assertFalse(result["performance_verified"])
            self.assertIsNone(result["display_pnl_usd"])

    def test_incomplete_prices_wrong_ids_or_calculated_pnl_are_quarantined(self):
        row, audit = evidence()
        for changed in (
            {"entrada": 99}, {"salida": 99}, {"pnl_usd": 999},
            {"cantidad": 6}, {"position_id": "other"},
            {"execution_id": "other"}, {"ticker": "TSLA"},
            {"pnl_usd": "NaN"}, {"pnl_usd": "Infinity"},
        ):
            with self.subTest(changed=changed):
                result = annotate_history([{**row, **changed}], audit)[0]
                self.assertFalse(result["performance_verified"])

    def test_broker_flat_without_a_verified_price_is_not_a_result(self):
        row, audit = evidence()
        audit[-1]["detalle"]["fill_price_verified"] = False
        result = annotate_history([row], audit)[0]
        self.assertFalse(result["performance_verified"])
        self.assertIsNone(result["display_pnl_usd"])

    def test_partial_exit_must_reconcile_all_quantities_and_prices(self):
        row, audit = evidence()
        row.update(cantidad=10, salida=97.7, pnl_usd=23)
        audit[0]["cantidad"] = 10
        audit[-1].update(cantidad=10, precio=97.7, pnl_usd=23)
        audit[-1]["detalle"].update(tp1_ejecutado=True, salida_real=97)
        partial = dict(audit[0], evento="TP1_FILLED_70_PERCENT", order_id="tp-1",
                       cantidad=7, precio=98, detalle={"fill_price_verified": True})
        self.assertFalse(annotate_history([row], audit)[0]["performance_verified"])
        self.assertTrue(annotate_history([row], audit + [partial])[0]["performance_verified"])
        partial["cantidad"] = 8
        self.assertFalse(annotate_history([row], audit + [partial])[0]["performance_verified"])

    def test_latest_quarantine_and_duplicate_history_never_revive_old_results(self):
        row, audit = evidence()
        later = {**row, "timestamp_utc": "2026-10-22T19:01:00Z", "pnl_usd": 999}
        results = annotate_history([row, later], audit)
        self.assertFalse(any(r["performance_verified"] for r in results))
        invalid = copy.deepcopy(audit[-1])
        invalid["detalle"]["fill_price_verified"] = False
        self.assertFalse(annotate_history([row], audit + [invalid])[0]["performance_verified"])

    def test_new_york_session_date_is_not_utc_date(self):
        self.assertEqual(session_date({"timestamp_utc": "2026-10-23T01:00:00Z"}), "2026-10-22")
        self.assertEqual(session_date({"timestamp_utc": "2026-10-23T01:00:00"}), "")

    def performance(self, rows, mode="REAL", positions=()):
        return build_ticker_performance(
            ["META"], history_rows=rows, positions=positions,
            execution_rows=[dict(ticker="META", mode=mode)],
            session_date="2026-10-22")[0]

    def test_current_real_badge_cannot_turn_paper_history_into_real_profit(self):
        real, audit = evidence()
        paper = {**real, "modo": "PAPER", "position_id": "paper", "pnl_usd": 500}
        rows = annotate_history([real, paper], audit)
        result = self.performance(rows)
        self.assertEqual(result["realized_pnl"], 10)
        self.assertEqual(result["closed_trades"], 1)
        self.assertEqual(result["mode_breakdown"]["PAPER"]["realized_pnl"], 500)
        self.assertFalse(result["is_broker_reported"])
        switched = self.performance(rows, "PAPER")
        self.assertEqual(switched["realized_pnl"], 500)
        self.assertEqual(switched["mode_breakdown"]["REAL"]["realized_pnl"], 10)

    def test_missing_real_data_is_unknown_not_zero_or_a_partial_total(self):
        real, audit = evidence()
        missing = {**real, "position_id": "no-proof"}
        result = self.performance(annotate_history([real, missing], audit))
        self.assertIsNone(result["realized_pnl"])
        self.assertIsNone(result["total_pnl"])
        self.assertEqual(result["known_realized_pnl"], 10)
        self.assertEqual(result["excluded_closed_trades"], 1)
        self.assertIsNone(self.performance([])["total_pnl"])

    def test_pending_order_is_not_open_profit_and_unknown_mode_is_not_paper(self):
        result = self.performance([], positions=[dict(
            ticker="META", mode="REAL", status="PENDING", floating_pnl=999)])
        self.assertEqual(result["open_positions"], 0)
        row, audit = evidence()
        result = self.performance(annotate_history([{**row, "modo": ""}], audit))
        self.assertEqual(result["mode_breakdown"]["PAPER"]["closed_trades"], 0)
        self.assertEqual(result["mode_breakdown"]["UNKNOWN"]["excluded_closed_trades"], 1)

    def test_rankings_are_mode_specific_and_incomplete_real_records_do_not_rank(self):
        real, audit = evidence()
        rows = annotate_history([real, {**real, "modo": "PAPER", "pnl_usd": -500}], audit)
        kwargs = dict(history_rows=rows, as_of_date="2026-10-22", min_closed_trades=1)
        r = build_ticker_accuracy_analysis(["META"], evidence_mode="REAL", **kwargs)[0]
        p = build_ticker_accuracy_analysis(["META"], evidence_mode="PAPER", **kwargs)[0]
        self.assertEqual((r["closed_trades"], r["net_pnl"], r["mode"]), (1, 10, "REAL"))
        self.assertEqual((p["closed_trades"], p["net_pnl"], p["mode"]), (1, -500, "PAPER"))
        rows += annotate_history([{**real, "position_id": "missing"}], [])
        r = build_ticker_accuracy_analysis(["META"], evidence_mode="REAL", **kwargs)[0]
        self.assertFalse(r["sample_eligible"])
        self.assertIsNone(r["rank"])


class MarketDataProvenanceTests(unittest.TestCase):
    def test_schwab_outage_does_not_automatically_generate_prices(self):
        from api.schwab_client import SchwabClient, SchwabAuthorizationRequired
        client = SchwabClient.__new__(SchwabClient)
        client._asegurar_token = Mock(return_value=False)
        client._velas_fallback = Mock(side_effect=AssertionError("synthetic data used"))
        client._multitemporal_fallback = Mock(side_effect=AssertionError("synthetic data used"))
        self.assertIsNone(client.obtener_velas("META"))
        with self.assertRaises(SchwabAuthorizationRequired):
            client.obtener_multitemporal("META")
        client._velas_fallback.assert_not_called()
        client._multitemporal_fallback.assert_not_called()

    def test_dashboard_missing_market_data_stays_unavailable(self):
        import server
        with patch.object(server, "schwab_ensure_token", return_value=False), patch.object(
            server, "schwab_headers", return_value=None
        ):
            self.assertIsNone(server.schwab_get_candles("META"))

    def test_snapshot_hides_unverified_or_stale_values_without_changing_positions(self):
        from engine.bot_engine import ChocoletraBot
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot._lock = threading.RLock()
        bot._is_paper_ticker = Mock(return_value=False)
        bot._last_prices = {"META": 98}
        bot._last_price_times = {"META": time.time()}
        bot._last_bar_times = {"META": datetime.now(timezone.utc).isoformat()}
        pos = dict(tipo="SELL", cantidad=5, entrada=100,
                   orden=SimpleNamespace(cantidad=5), entry_integrity={"entry_confirmed": False})
        bot._posiciones = {"META": pos}
        self.assertIsNone(bot.dashboard_snapshot()[0]["floating_pnl"])
        self.assertIsNone(bot.dashboard_snapshot()[0]["entry_price"])
        pos["entry_integrity"]["entry_confirmed"] = True
        self.assertEqual(bot.dashboard_snapshot()[0]["floating_pnl"], 10)
        bot._last_bar_times["META"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self.assertIsNone(bot.dashboard_snapshot()[0]["floating_pnl"])
        self.assertIs(bot._posiciones["META"], pos)
        bot._last_prices["META"] = float("nan")
        snapshot = bot.dashboard_snapshot()
        self.assertIsNone(snapshot[0]["last_price"])
        json.dumps(snapshot, allow_nan=False)

    def test_chart_exit_marker_uses_confirmed_fill_not_the_signal_price(self):
        import server
        _, audit = evidence()
        close = dict(audit[-1], timestamp_utc="2026-10-22T19:00:00Z", lado="SELL")
        close["detalle"] = json.dumps({**close["detalle"], "salida_solicitada": 999})
        start = datetime(2026, 10, 22, tzinfo=timezone.utc)
        with patch("core.order_audit.read_order_audit", return_value=[close]), patch.object(
            server, "_order_audit_path", return_value="unused"
        ):
            markers = server._chart_trade_events("META", start, start + timedelta(days=1))
        self.assertEqual(markers[0]["price"], 98)


class DashboardProvenanceRoutesTests(unittest.TestCase):
    def test_unavailable_history_and_valuation_do_not_reuse_fake_zero_totals(self):
        import server
        with patch.object(server, "_dashboard_history_rows", side_effect=OSError("unavailable")):
            result = server._history_summary()
        self.assertFalse(result["available"])
        self.assertIsNone(result["real_pnl"])
        self.assertIsNone(result["paper_realized_today"])
        live = server._position_pnl_summary([dict(ticker="META", mode="REAL",
            status="OPEN", floating_pnl=None, valuation_available=False)])
        self.assertIsNone(live["real_unrealized_pnl"])
        self.assertIsNone(server._pnl_sum(10, None))

    def test_display_reads_do_not_migrate_history_or_invent_missing_audit(self):
        import server
        from core.trade_history import LEGACY_HISTORY_FIELDS
        with tempfile.TemporaryDirectory() as root:
            history = Path(root) / "history.csv"
            with history.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=LEGACY_HISTORY_FIELDS)
                writer.writeheader()
                writer.writerow(dict(fecha="2026-10-22", ticker="META", modo="REAL", pnl_usd=999))
            before = history.read_bytes()
            with patch.object(server, "_history_path", return_value=str(history)), patch.object(
                server, "_order_audit_path", return_value=str(Path(root) / "missing.csv")
            ):
                rows = server._dashboard_history_rows()
                self.assertFalse(rows[0]["performance_verified"])
                self.assertIsNone(rows[0]["display_pnl_usd"])
                self.assertEqual(history.read_bytes(), before)
                self.assertEqual(len(list(Path(root).iterdir())), 1)

    def test_performance_api_has_separate_modes_and_no_mixed_total(self):
        import server
        row, audit = evidence()
        rows = annotate_history([row, {**row, "modo": "PAPER", "pnl_usd": 500}], audit)
        now = datetime(2026, 10, 22, 16, tzinfo=timezone.utc)
        with patch.object(server, "_read_config_runtime", return_value={}), patch.object(
            server, "_dashboard_history_rows", return_value=rows
        ), patch.object(server, "_ahora_ny_datetime", return_value=now), patch.object(
            server, "_engine_positions_snapshot", return_value=[]
        ), patch.object(server, "_ticker_execution_rows", return_value=[dict(ticker="META", mode="REAL")]), patch.dict(
            server.BOT_STATE, tickers=["META"], signals=[]
        ), server.app.test_request_context("/api/ticker-performance"):
            body = server.get_ticker_performance().get_json()
        self.assertEqual(body["rows"][0]["realized_pnl"], 10)
        self.assertEqual(body["summary"]["by_mode"]["PAPER"]["realized_pnl"], 500)
        self.assertNotIn("total_pnl", body["summary"])
        self.assertFalse(body["summary"]["is_broker_reported"])

    def test_unknown_close_event_removes_position_without_fabricating_zero_pnl(self):
        import server
        with patch.dict(server.BOT_STATE, trades=[], active_positions={"META": {}},
                        stats={"pnl_total": 5.0, "trades_closed": 0, "trades_open": 1}):
            server._register_trade("SELL", "META", None, 3, "CLOSED", None)
            self.assertNotIn("META", server.BOT_STATE["active_positions"])
            self.assertIsNone(server.BOT_STATE["trades"][-1]["pnl"])
            self.assertIsNone(server.BOT_STATE["trades"][-1]["precio"])
            self.assertIsNone(server.BOT_STATE["stats"]["pnl_total"])

    def test_malformed_broker_positions_do_not_become_a_zero_balance(self):
        from core.broker_pnl import open_position_pnl_snapshot
        self.assertFalse(open_position_pnl_snapshot({})["available"])
        result = open_position_pnl_snapshot([{"symbol": "META", "tipo": "BUY", "cantidad": "bad"}])
        self.assertIsNone(result["position_day_pnl"])
        self.assertEqual(result["unknown_count"], 1)

    def test_fractional_broker_quantity_is_not_truncated_into_a_flat_account(self):
        from core.broker_pnl import open_position_pnl_snapshot
        result = open_position_pnl_snapshot([dict(
            symbol="META", tipo="BUY", cantidad=0, broker_quantity=0.5,
            broker_position_day_pnl=1.23)])
        self.assertEqual(result["positions"][0]["quantity"], 0.5)
        self.assertEqual(result["position_day_pnl"], 1.23)

    def test_broker_pnl_can_be_read_while_engine_stays_uninitialized(self):
        import server
        with patch.object(server, "_ENGINE", None), patch.dict(
            server.BOT_STATE, account_number="offline-account", running=False
        ), patch.object(server, "_read_config_runtime", return_value={}), patch(
            "api.schwab_client.SchwabClient"
        ) as client_cls, server.app.test_request_context("/api/broker/open-position-pnl"):
            client_cls.return_value.obtener_account_hash.return_value = "offline-hash"
            client_cls.return_value.obtener_posiciones.return_value = [dict(
                symbol="META", tipo="SELL", cantidad=5, broker_position_day_pnl=-12.34)]
            result = server.broker_open_position_pnl().get_json()
            client_cls.return_value.obtener_posiciones.assert_called_once_with("offline-hash")
        self.assertEqual(result["position_day_pnl"], -12.34)
        self.assertEqual(result["source"], "SCHWAB_OPEN_POSITIONS")
        self.assertIsNone(server._ENGINE)


if __name__ == "__main__":
    unittest.main()
