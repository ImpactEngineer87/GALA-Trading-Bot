import copy
import csv
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import reconcile_verified_history as repair

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


class BrokerReadOnly:
    TRADER_URL = "https://broker.invalid/trader/v1"

    def __init__(self):
        self.positions = []
        self.working = []
        self.calls = []
        self.status = 200
        self.orders = {}
        for oid, instruction, price, stamp in (
            ("entry-private", "SELL_SHORT", 37.73, "2026-10-23T13:48:19Z"),
            ("exit-private", "BUY_TO_COVER", 37.375, "2026-10-23T14:22:15Z"),
        ):
            self.orders[oid] = {
                "orderId": oid, "accountNumber": "123456789", "status": "FILLED", "orderType": "LIMIT" if instruction == "SELL_SHORT" else "MARKET",
                "quantity": 10, "filledQuantity": 10,
                "orderLegCollection": [{"legId": 1, "instruction": instruction, "instrument": {"symbol": "NKE", "assetType": "EQUITY"}}],
                "orderActivityCollection": [{"activityType": "EXECUTION", "executionType": "FILL", "executionId": oid + "-fill",
                    "executionLegs": [{"legId": 1, "quantity": 10, "price": price, "time": stamp}]}],
            }

    def obtener_account_hash(self, account):
        assert account == "123456789"
        return "account-private"

    def obtener_posiciones(self, account):
        assert account == "account-private", "Position read must use the Schwab account hash"
        return self.positions

    def obtener_ordenes_abiertas(self, account):
        assert account == "account-private", "Working-order read must use the Schwab account hash"
        return self.working

    def _get_autorizado(self, url, **kwargs):
        assert "/accounts/account-private/orders/" in url
        self.calls.append(("GET", url))
        oid = url.rsplit("/", 1)[-1]
        return SimpleNamespace(status_code=self.status, json=lambda: copy.deepcopy(self.orders[oid]))


class VerifiedHistoricalReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "runtime"
        (self.root / "data").mkdir(parents=True)
        (self.root / "models").mkdir()
        self.backups = Path(self.temp.name) / "backups"
        (self.root / "config.yaml").write_text("{}")
        self.guard = self.root / "data/real_accounting_reconciliation.json"
        self.state = {"unresolved": {
            "position-private": {"ticker": "NKE", "entry_order_id": "entry-private", "exit_order_id": "exit-private", "reason": "P/L unknown"},
            "history:2026-10-23:position-private": {"ticker": "NKE", "reason": "P/L absent"},
            "ba-private": {"ticker": "BA", "reason": "Missing exit"},
            "history:2026-10-23:ba-private": {"ticker": "BA", "reason": "P/L absent"},
            "meta-private": {"ticker": "META", "reason": "Missing IDs"},
        }, "another_existing_key": {"keep": True}}
        self.write_state()
        self.guard.chmod(0o600)
        self.history = [{"fecha": "2026-10-23", "modo": "REAL", "ticker": "NKE", "position_id": "position-private",
                         "execution_id": "execution-private", "position_side": "SHORT", "lado": "SELL", "cantidad": "10", "entrada": "37.73", "salida": "", "pnl_usd": "",
                         "evento": "POSITION_CLOSE", "parent_order_id": "entry-private", "order_id": "exit-private"}]
        base = {"fecha": "2026-10-23", "modo": "REAL", "ticker": "NKE", "position_id": "position-private", "execution_id": "execution-private",
                "position_side": "SHORT", "cantidad": "10", "order_id": "", "parent_order_id": "", "evento": "", "detalle": "{}"}
        self.audit = [
            {**base, "evento": "ENTRY_FILLED", "order_id": "entry-private", "detalle": '{"broker_price_verified":false}'},
            {**base, "evento": "EXIT_SUBMITTED", "order_id": "exit-private"},
            {**base, "evento": "POSITION_CLOSED", "order_id": "exit-private", "parent_order_id": "entry-private", "detalle": '{"tp1_ejecutado":false,"fill_price_verified":false}'},
        ]
        self.write_ledgers()
        for name, value in ((".env", "PRIVATE-CREDENTIAL"), ("models/NKE.pkl", "MODEL"),
                            ("data/retraining_state.json", '{"new_observations":9}'),
                            ("data/retraining_state.history.jsonl", '{"event":"KEEP"}\n'),
                            ("data/daily_risk.json", '{"session":"2026-10-26","loss":12}')):
            (self.root / name).write_text(value)
        self.client = BrokerReadOnly()
        self.original = self.guard.read_bytes()

    def write_state(self):
        self.guard.write_text(json.dumps(self.state))

    def write_ledgers(self):
        for name, rows in (("historial_operaciones.csv", self.history), ("auditoria_ordenes.csv", self.audit)):
            fields = list(dict.fromkeys(key for row in rows for key in row))
            with (self.root / "data" / name).open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)

    def scope(self):
        return repair.scope_for(self.root, repair.reference("position-private"), "2026-10-23", NOW)

    def proof(self, scope):
        return repair.proof_for(scope, self.client, "123456789", "account-private")

    def plan(self):
        scope = self.scope()
        return repair.plan_for(scope, self.proof(scope))

    def apply(self, plan, checker=lambda: None):
        return repair.apply_plan(self.root, plan, self.client, "123456789", "account-private", self.backups, check_service=checker, now=NOW)

    def test_prepare_does_not_modify_runtime_or_expose_private_ids(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        out = io.StringIO()
        scope = self.scope()
        with patch.object(repair, "scope_for", return_value=scope), patch("api.schwab_client.SchwabClient", return_value=self.client), patch.dict(os.environ, {"SCHWAB_ACCOUNT_NUMBER": "123456789"}), redirect_stdout(out):
            self.assertEqual(repair.main(["--prepare", "--runtime-root", str(self.root), "--date", "2026-10-23", "--position-ref", scope["position_ref"], "--backup-root", str(self.backups)]), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "PREPARED_ONLY")
        self.assertFalse(result["accounting_modified"])
        self.assertEqual(result["historical_gross_pnl_before_fees"], 3.55)
        self.assertNotIn("private\"", out.getvalue())
        self.assertNotIn("123456789", out.getvalue())
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertEqual(stat.S_IMODE(Path(result["private_plan_file"]).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(result["private_plan_file"]).parent.stat().st_mode), 0o700)

    def test_apply_resolves_only_two_proven_records_and_archives_originals(self):
        result = self.apply(self.plan())
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(result["unresolved"], 3)
        self.assertTrue(result["blocked"])
        state = json.loads(self.guard.read_text())
        self.assertEqual(state["another_existing_key"], self.state["another_existing_key"])
        expected = {k: v for k, v in self.state["unresolved"].items() if v["ticker"] != "NKE"}
        self.assertEqual(state["unresolved"], expected)
        from core.real_accounting import RealAccountingGuard
        live_guard = RealAccountingGuard(self.guard)
        live_guard.inspect_session(self.root / "data/historial_operaciones.csv", self.root / "data/auditoria_ordenes.csv", "2026-10-26")
        self.assertEqual(live_guard.snapshot()["tickers"], ["BA", "META"])
        self.assertEqual(live_guard.snapshot()["unresolved"], 3)
        self.assertTrue(live_guard.snapshot()["blocked"])
        archive = state[repair.ARCHIVE_KEY]["position-private"]
        self.assertEqual(len(archive["original_unresolved_records"]), 2)
        self.assertEqual(archive["proof"]["verified_historical_gross_pnl"], 3.55)
        self.assertNotIn("123456789", self.guard.read_text())
        self.assertNotIn("raw_broker_orders", archive["proof"])
        self.assertEqual((Path(result["private_backup_directory"]) / "original_guard_private.json").read_bytes(), self.original)

    def test_apply_preserves_histories_models_counts_risk_and_guard_metadata(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file() and p != self.guard}
        old = self.guard.stat()
        self.apply(self.plan())
        after = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file() and p != self.guard and not p.name.endswith(".reconcile.lock")}
        self.assertEqual(before, after)
        current = self.guard.stat()
        self.assertEqual((current.st_uid, current.st_gid, stat.S_IMODE(current.st_mode)), (old.st_uid, old.st_gid, stat.S_IMODE(old.st_mode)))

    def test_service_must_be_stopped_before_apply_reads_broker(self):
        plan = self.plan()
        self.client.calls.clear()
        def active():
            raise repair.Refused("SERVICE_MUST_BE_STOPPED")
        with self.assertRaisesRegex(repair.Refused, "SERVICE_MUST_BE_STOPPED"):
            self.apply(plan, active)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.guard.read_bytes(), self.original)

    def test_apply_requires_fresh_zero_positions_and_zero_orders(self):
        plan = self.plan()
        for positions, working in ((None, []), ([{"symbol": "BA"}], []), ([], None), ([], [{"id": "working"}])):
            with self.subTest(positions=positions, working=working):
                self.client.positions, self.client.working = positions, working
                with self.assertRaisesRegex(repair.Refused, "BROKER_MUST_CONFIRM_ZERO"):
                    self.apply(plan)
                self.assertEqual(self.guard.read_bytes(), self.original)

    def test_missing_runtime_or_history_ids_never_uses_a_matching_price(self):
        self.state["unresolved"]["position-private"]["exit_order_id"] = ""
        self.write_state()
        with self.assertRaisesRegex(repair.Refused, "RUNTIME_HISTORY_ORDER_IDS_DIFFER"):
            self.scope()
        self.history[0]["order_id"] = ""
        self.write_ledgers()
        with self.assertRaisesRegex(repair.Refused, "BOTH_EXACT_RECORDED_ORDER_IDS_REQUIRED"):
            self.scope()

    def test_duplicate_history_is_ambiguous(self):
        self.history.append(dict(self.history[0]))
        self.write_ledgers()
        with self.assertRaisesRegex(repair.Refused, "ONE_EXACT_HISTORY_CLOSE_REQUIRED"):
            self.scope()

    def test_incomplete_or_wrong_audit_identity_is_rejected(self):
        self.audit[0]["execution_id"] = "other-execution"
        self.write_ledgers()
        with self.assertRaisesRegex(repair.Refused, "EXACT_ENTRY_AND_CLOSE_AUDIT_REQUIRED"):
            self.scope()

    def test_missing_exit_submission_cannot_be_reconstructed_from_flatness(self):
        self.audit = [r for r in self.audit if r["evento"] != "EXIT_SUBMITTED"]
        self.write_ledgers()
        with self.assertRaisesRegex(repair.Refused, "EXACT_EXIT_SUBMISSION_REQUIRED"):
            self.scope()

    def test_partial_trade_history_or_audit_requires_separate_review(self):
        self.history.append({**self.history[0], "evento": "POSITION_PARTIAL"})
        self.write_ledgers()
        with self.assertRaisesRegex(repair.Refused, "PARTIAL_HISTORY_REQUIRES_SEPARATE_REVIEW"):
            self.scope()
        self.history.pop()
        self.audit[-1]["detalle"] = '{"tp1_ejecutado":true}'
        self.write_ledgers()
        with self.assertRaisesRegex(repair.Refused, "PARTIAL_CLOSE_REQUIRES_SEPARATE_REVIEW"):
            self.scope()

    def test_current_day_and_future_dates_cannot_change_risk_baselines(self):
        for day in ("2026-10-26", "2026-10-27"):
            with self.subTest(day=day), self.assertRaisesRegex(repair.Refused, "PAST_SESSIONS_ONLY"):
                repair.scope_for(self.root, repair.reference("position-private"), day, NOW)

    def test_failed_broker_get_cannot_be_overridden_by_local_filled_flags(self):
        self.client.status = 403
        with self.assertRaisesRegex(repair.Refused, "BROKER_EXACT_ORDER_GET_FAILED"):
            self.proof(self.scope())
        self.assertEqual(self.guard.read_bytes(), self.original)

    def test_wrong_account_symbol_and_instruction_are_rejected(self):
        for field, value in (("account", "999999999"), ("symbol", "BA"), ("instruction", "BUY")):
            with self.subTest(field=field):
                original = copy.deepcopy(self.client.orders)
                if field == "account":
                    self.client.orders["exit-private"]["accountNumber"] = value
                elif field == "symbol":
                    self.client.orders["exit-private"]["orderLegCollection"][0]["instrument"]["symbol"] = value
                else:
                    self.client.orders["exit-private"]["orderLegCollection"][0]["instruction"] = value
                with self.assertRaises(repair.Refused):
                    self.proof(self.scope())
                self.client.orders = original

    def test_limit_price_without_execution_prices_is_not_a_fill(self):
        self.client.orders["exit-private"]["price"] = "37.375"
        self.client.orders["exit-private"]["orderActivityCollection"] = []
        with self.assertRaisesRegex(repair.Refused, "COMPLETE_TERMINAL_FILLS_REQUIRED"):
            self.proof(self.scope())

    def test_canceled_partial_cannot_resolve_a_full_quantity_position(self):
        order = self.client.orders["exit-private"]
        order["status"], order["filledQuantity"] = "CANCELED", 4
        order["orderActivityCollection"][0]["executionLegs"][0]["quantity"] = 4
        with self.assertRaisesRegex(repair.Refused, "COMPLETE_TERMINAL_FILLS_REQUIRED"):
            self.proof(self.scope())

    def test_fill_day_and_causal_order_must_match(self):
        for stamp in ("2026-10-24T14:22:15Z", "2026-10-23T12:00:00Z", "2026-10-23T14:22:15"):
            with self.subTest(stamp=stamp):
                self.client.orders["exit-private"]["orderActivityCollection"][0]["executionLegs"][0]["time"] = stamp
                with self.assertRaises(repair.Refused):
                    self.proof(self.scope())

    def test_changed_broker_price_or_guard_rejects_a_prepared_plan(self):
        plan = self.plan()
        self.client.orders["exit-private"]["orderActivityCollection"][0]["executionLegs"][0]["price"] = 37.3
        with self.assertRaisesRegex(repair.Refused, "PLAN_STALE_OR_BROKER_EVIDENCE_CHANGED"):
            self.apply(plan)
        self.assertEqual(self.guard.read_bytes(), self.original)
        self.client = BrokerReadOnly()
        self.state["unresolved"]["new-block"] = {"ticker": "TSLA"}
        self.write_state()
        changed = self.guard.read_bytes()
        with self.assertRaisesRegex(repair.Refused, "PLAN_STALE_OR_BROKER_EVIDENCE_CHANGED"):
            self.apply(plan)
        self.assertEqual(self.guard.read_bytes(), changed)

    def test_resealed_plan_cannot_add_an_unrelated_guard_record(self):
        plan = self.plan()
        plan["keys"].append("ba-private")
        plan["fingerprint"] = repair.digest({k: v for k, v in plan.items() if k != "fingerprint"})
        with self.assertRaisesRegex(repair.Refused, "PLAN_STALE_OR_BROKER_EVIDENCE_CHANGED"):
            self.apply(plan)
        self.assertEqual(self.guard.read_bytes(), self.original)

    def test_failure_before_atomic_commit_leaves_original_guard_intact(self):
        with patch.object(repair.os, "replace", side_effect=OSError("offline disk failure")):
            with self.assertRaises(OSError):
                self.apply(self.plan())
        self.assertEqual(self.guard.read_bytes(), self.original)
        self.assertEqual(list(self.guard.parent.glob(self.guard.name + ".reconcile-*")), [])

    def test_replay_is_idempotent_and_does_not_reverify_or_clear_other_blocks(self):
        plan = self.plan()
        self.apply(plan)
        after = self.guard.read_bytes()
        self.client.calls.clear()
        result = self.apply(plan)
        self.assertEqual(result["status"], "ALREADY_APPLIED")
        self.assertTrue(result["blocked"])
        self.assertEqual(result["unresolved"], 3)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.guard.read_bytes(), after)

    def test_existing_archived_proof_cannot_be_overwritten_by_a_new_plan(self):
        self.state[repair.ARCHIVE_KEY] = {"position-private": {"plan_fingerprint": "older-proof"}}
        self.write_state()
        before = self.guard.read_bytes()
        with self.assertRaisesRegex(repair.Refused, "POSITION_ALREADY_RECONCILED_REVIEW_REQUIRED"):
            self.scope()
        self.assertEqual(self.guard.read_bytes(), before)

    def test_old_plan_replay_preserves_a_newly_raised_block_for_same_position(self):
        plan = self.plan()
        self.apply(plan)
        updated = json.loads(self.guard.read_text())
        updated["unresolved"]["position-private"] = {"ticker": "NKE", "reason": "New review required"}
        self.guard.write_text(json.dumps(updated))
        before = self.guard.read_bytes()
        result = self.apply(plan)
        self.assertEqual(result["status"], "ALREADY_APPLIED")
        self.assertEqual(result["unresolved"], 4)
        self.assertTrue(result["blocked"])
        self.assertEqual(self.guard.read_bytes(), before)

    def test_guard_change_during_broker_verification_is_not_overwritten(self):
        plan = self.plan()
        get_order = self.client._get_autorizado
        changed = []
        def concurrent_change(url, **kwargs):
            response = get_order(url, **kwargs)
            if url.endswith('/exit-private'):
                external = json.loads(self.guard.read_text())
                external['unresolved']['new-concurrent-block'] = {'ticker': 'TSLA', 'reason': 'Keep blocked'}
                self.guard.write_text(json.dumps(external))
                changed.append(self.guard.read_bytes())
            return response
        self.client._get_autorizado = concurrent_change
        with self.assertRaisesRegex(repair.Refused, 'ACCOUNTING_STATE_CHANGED'):
            self.apply(plan)
        self.assertEqual(self.guard.read_bytes(), changed[0])
        self.assertEqual(len(json.loads(self.guard.read_text())['unresolved']), 6)

    def test_post_rename_sync_failure_is_recoverable_by_idempotent_replay(self):
        plan = self.plan()
        fsync = os.fsync
        def fail_directory_sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('offline directory sync failure')
            fsync(fd)
        with patch.object(repair.os, 'fsync', side_effect=fail_directory_sync):
            with self.assertRaises(OSError):
                self.apply(plan)
        committed = self.guard.read_bytes()
        state = json.loads(committed)
        self.assertEqual(len(state['unresolved']), 3)
        self.assertIn('position-private', state[repair.ARCHIVE_KEY])
        self.assertEqual(self.apply(plan)['status'], 'ALREADY_APPLIED')
        self.assertEqual(self.guard.read_bytes(), committed)


if __name__ == "__main__":
    unittest.main()
