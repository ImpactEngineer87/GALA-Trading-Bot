"""Offline coverage: narrow entry permission never clears historical accounting."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import archived_accounting_resume as resume
from core.real_accounting import RealAccountingGuard
from scripts.authorize_archived_accounting_resume import verify_broker_flat
from scripts import authorize_archived_accounting_resume as issuer

NOW = datetime(2026, 10, 30, 14, 30, tzinfo=timezone.utc)


class ArchivedAccountingResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.guard = self.root / "guard.json"
        self.archive = self.root / "original_accounting.json"
        self.approval = self.root / "grant.json"
        self.config = {"live_trading": {"archived_accounting_resume": True},
                       "execution_engine": {"archived_resume_authorization_path": str(self.approval)}}
        self.state = {"unresolved": {resume.HOLD_KEY: {
            "kind": "OPERATOR_HOLD", "recorded_utc": resume.HOLD_STAMP,
            "reason": resume.HOLD_REASON, "archive_path": str(self.archive),
            "archived_record_count": 7}},
            "verified_historical_reconciliations": {"a": {}, "b": {}, "c": {}}}
        self.original = {"unresolved": {}, "verified_historical_reconciliations": {}}
        for ticker, day, kind in resume.EXPECTED_ARCHIVE:
            key = "history:" + day + ":" + ticker if kind == "history" else ticker + "-position"
            row = {"ticker": ticker, "recorded_utc": day + "T18:00:00+00:00",
                   "reason": "Missing exact historical execution evidence"}
            if kind == "history":
                row["session_ny"] = day
            else:
                row.update(entry_order_id="", exit_order_id="", requested_exit_price=1,
                           broker_exit_price=None, broker_flat=True)
            self.original["unresolved"][key] = row
        self.archive.write_text(json.dumps(self.original))
        self.guard.write_text(json.dumps(self.state))
        digest, keys, _ = resume.strict_archived_guard(self.guard)
        self.grant = {"kind": resume.APPROVAL_KIND, "session_ny": "2026-10-30",
                      "guard_sha256": digest, "keys": keys,
                      "archive_path": str(self.archive),
                      "original_archive_sha256": resume.inspect_original_archive(self.archive),
                      "account_fingerprint": resume.fingerprint("offline-account"),
                      "archived_record_count": 7, "broker_flat_verified": True,
                      "operator_ack": resume.ACK, "historical_pnl_verified": False,
                      "accounting_records_modified": False}
        self.write_grant()

    def write_grant(self):
        self.approval.write_text(json.dumps(self.grant))

    def status(self, *, account="offline-account", now=NOW):
        with patch.object(resume, "_trusted_approval_file", return_value=True):
            return resume.exception_status(self.config, self.guard, account, now=now, root=self.root)

    def test_exact_session_grant_only_changes_entry_permission_preserving_all_evidence(self):
        original = {path: path.read_bytes() for path in (self.archive, self.guard, self.approval)}
        self.assertFalse(self.status()["entry_blocked"])
        self.assertTrue(self.status()["archived_resume_active"])
        self.assertTrue(RealAccountingGuard(self.guard).snapshot()["blocked"])
        self.assertEqual(RealAccountingGuard(self.guard).snapshot()["unresolved"], 1)
        self.assertEqual(original, {path: path.read_bytes() for path in original})

    def test_default_off_and_missing_grant_block(self):
        self.config["live_trading"].clear()
        self.assertTrue(self.status()["entry_blocked"])
        self.config["live_trading"]["archived_accounting_resume"] = True
        self.approval.unlink()
        self.assertTrue(self.status()["entry_blocked"])

    def test_new_discrepancy_immediately_revokes_old_grant(self):
        self.state["unresolved"]["new-position"] = {"ticker": "BA", "reason": "New missing fill"}
        self.guard.write_text(json.dumps(self.state))
        self.assertTrue(self.status()["entry_blocked"])

    def test_changed_guard_even_outside_unresolved_revokes(self):
        self.state["verified_historical_reconciliations"]["a"]["source"] = "changed"
        self.guard.write_text(json.dumps(self.state))
        self.assertTrue(self.status()["entry_blocked"])

    def test_other_account_or_next_ny_session_requires_new_grant(self):
        self.assertTrue(self.status(account="different-account")["entry_blocked"])
        self.assertTrue(self.status(now=datetime(2026, 10, 31, 5, tzinfo=timezone.utc))["entry_blocked"])
        # UTC midnight is still the same New York session.
        self.assertFalse(self.status(now=datetime(2026, 10, 31, 1, tzinfo=timezone.utc))["entry_blocked"])

    def test_missing_root_trust_and_pnl_claims_cannot_authorize(self):
        with patch.object(resume, "_trusted_approval_file", return_value=False):
            self.assertTrue(resume.exception_status(
                self.config, self.guard, "offline-account", now=NOW, root=self.root)["entry_blocked"])
        for field, value in (("historical_pnl_verified", True), ("accounting_records_modified", True),
                             ("broker_flat_verified", False), ("operator_ack", ""),
                             ("original_archive_sha256", "not-a-digest")):
            with self.subTest(field=field):
                before = copy.deepcopy(self.grant)
                self.grant[field] = value
                self.write_grant()
                self.assertTrue(self.status()["entry_blocked"])
                self.grant = before

    def test_corrupt_or_duplicate_guard_keys_fail_closed(self):
        self.guard.write_text("not json")
        self.assertTrue(self.status()["entry_blocked"])
        self.guard.write_text('{"unresolved": {}, "unresolved": {}}')
        self.assertTrue(self.status()["entry_blocked"])

    def test_relabeling_arbitrary_hold_is_refused_even_with_new_digest(self):
        self.state["unresolved"][resume.HOLD_KEY]["recorded_utc"] = "2026-10-30T12:00:00+00:00"
        self.guard.write_text(json.dumps(self.state))
        self.grant["guard_sha256"] = resume.fingerprint(self.guard.read_bytes())
        self.write_grant()
        self.assertTrue(self.status()["entry_blocked"])

    def test_original_archive_must_match_exact_seven_ticker_day_types(self):
        self.original["unresolved"]["NKE-position"]["ticker"] = "BA"
        self.archive.write_text(json.dumps(self.original))
        with self.assertRaisesRegex(ValueError, "ORIGINAL_ARCHIVED_SCOPE_CHANGED"):
            resume.inspect_original_archive(self.archive)

    def test_original_archive_requires_all_seven_and_exact_history_identity(self):
        key = "history:2026-10-29:NKE"
        self.original["unresolved"][key]["session_ny"] = "2026-10-28"
        self.archive.write_text(json.dumps(self.original))
        with self.assertRaisesRegex(ValueError, "ARCHIVE_HISTORY_IDENTITY_INVALID"):
            resume.inspect_original_archive(self.archive)
        del self.original["unresolved"][key]
        self.archive.write_text(json.dumps(self.original))
        with self.assertRaisesRegex(ValueError, "SEVEN_ORIGINAL_RECORDS_REQUIRED"):
            resume.inspect_original_archive(self.archive)

    def test_runtime_needs_no_access_to_root_private_archive(self):
        # Only the root issuer reads the private archive. The trusted grant
        # preserves its SHA, while the immutable guard binds its path and count.
        self.archive.unlink()
        self.assertFalse(self.status()["entry_blocked"])


class ArchivedAccountingIntegrationTests(unittest.TestCase):
    setUp = ArchivedAccountingResumeTests.setUp
    write_grant = ArchivedAccountingResumeTests.write_grant

    def test_engine_and_mixed_preflight_share_exact_archived_permission(self):
        from engine.bot_engine import ChocoletraBot
        from core.mixed_accounting_start import unresolved_real_accounting
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.config = self.config
        bot.real_accounting_guard = RealAccountingGuard(self.guard)
        bot._account_plain = "offline-account"
        bot.account_number = "resolved-schwab-hash"
        bot._ahora_et = lambda: NOW
        self.config["execution_engine"]["accounting_state_path"] = str(self.guard)
        with patch.object(resume, "_trusted_approval_file", return_value=True), \
                patch.object(resume, "datetime") as clock:
            clock.now.return_value = NOW
            state = bot._real_accounting_status()
            self.assertTrue(state["blocked"])
            self.assertTrue(state["archived_resume_active"])
            self.assertFalse(state["entry_blocked"])
            self.assertFalse(unresolved_real_accounting(self.config, self.root, "offline-account")[0])
            bot.real_accounting_guard._failure = "Persistence unavailable"
            self.assertTrue(bot._real_accounting_status()["entry_blocked"])
            bot.real_accounting_guard._failure = ""
            bot.real_accounting_guard.require_reconciliation("new-current-issue", ticker="BA", reason="Missing fill")
            self.assertTrue(bot._real_accounting_status()["entry_blocked"])
            self.assertTrue(unresolved_real_accounting(self.config, self.root, "offline-account")[0])


class BrokerFlatAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.positions = {"securitiesAccount": {"accountNumber": "offline-account", "type": "MARGIN"}}
        self.orders = []
        self.calls = []
        self.status = 200
        self.client = SimpleNamespace(
            TRADER_URL="https://broker.invalid", _asegurar_token=lambda: True,
            obtener_account_hash=lambda account: "offline-hash", _get_autorizado=self.get)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        data = self.orders if url.endswith("/orders") else self.positions
        return SimpleNamespace(status_code=self.status, json=lambda: copy.deepcopy(data))

    def test_exact_account_empty_positions_and_only_terminal_order_tree_pass(self):
        self.orders = [{"status": "FILLED", "childOrderStrategies": [{"status": "CANCELED"}]}]
        self.assertEqual(verify_broker_flat(self.client, "offline-account", now=NOW)["pending_orders"], 0)
        self.assertTrue(all("offline-hash" in url for url, _ in self.calls))
        self.assertNotIn("status", self.calls[-1][1]["params"])

    def test_malformed_account_or_other_account_is_not_flat(self):
        for data in ({}, {"securitiesAccount": {"accountNumber": "other", "type": "MARGIN"}}):
            self.positions = data
            with self.assertRaisesRegex(ValueError, "BROKER_ACCOUNT_IDENTITY_INVALID"):
                verify_broker_flat(self.client, "offline-account", now=NOW)

    def test_nonempty_or_unknown_positions_are_not_flat(self):
        for positions in (None, {}, [{"longQuantity": 1}]):
            self.positions["securitiesAccount"]["positions"] = positions
            with self.assertRaisesRegex(ValueError, "BROKER_POSITIONS_NOT_EMPTY"):
                verify_broker_flat(self.client, "offline-account", now=NOW)

    def test_pending_nested_order_and_truncated_snapshot_block(self):
        for orders in ([{"status": "PENDING_ACTIVATION"}],
                       [{"status": "FILLED", "childOrderStrategies": [{"status": "WORKING"}]}],
                       [{"status": "FILLED"}] * 500, {}):
            with self.subTest(orders_type=type(orders).__name__):
                self.orders = orders
                with self.assertRaises(ValueError):
                    verify_broker_flat(self.client, "offline-account", now=NOW)

    def test_api_error_cannot_authorize(self):
        self.status = 503
        with self.assertRaisesRegex(ValueError, "BROKER_POSITIONS_GET_FAILED"):
            verify_broker_flat(self.client, "offline-account", now=NOW)


class AuthorizationPublicationTests(unittest.TestCase):
    """Exercise actual issuance and preservation with broker/systemd mocked."""
    write_grant = ArchivedAccountingResumeTests.write_grant
    status = ArchivedAccountingResumeTests.status

    def setUp(self):
        ArchivedAccountingResumeTests.setUp(self)
        self.approval.unlink()
        self.config["execution_engine"]["accounting_state_path"] = str(self.guard)
        self.config["scheduler"] = {"modo": "manual"}
        (self.root / "data").mkdir()
        self.history = self.root / "data" / "historial_operaciones.csv"
        self.audit = self.root / "data" / "auditoria_ordenes.csv"
        self.history.write_text("fecha,modo,ticker,pnl_usd\n")
        self.audit.write_text("timestamp_utc,modo,ticker,evento\n")
        self.config_file = self.root / "config.yaml"
        import yaml
        self.config_file.write_text(yaml.safe_dump(self.config))
        self.backups = self.root / "backups"
        self.backups.mkdir()

    def issue(self, *, check_service=lambda: None, broker=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(issuer.os, "geteuid", return_value=0, create=True))
            stack.enter_context(patch.object(issuer.os, "fchown", create=True))
            stack.enter_context(patch.object(issuer.os, "fchmod", create=True))
            stack.enter_context(patch.object(issuer.os, "fsync"))
            stack.enter_context(patch.object(issuer.os, "O_DIRECTORY", 0, create=True))
            stack.enter_context(patch.dict("sys.modules", {
                "grp": SimpleNamespace(getgrnam=lambda name: SimpleNamespace(gr_gid=1))}))
            # Windows cannot open directories with os.open; avoid only those
            # platform durability handles while exercising real file creation.
            native_open = issuer.os.open
            def portable_open(path, flags, *args, **kwargs):
                if Path(path).is_dir():
                    return native_open(self.config_file, issuer.os.O_RDONLY)
                return native_open(path, flags, *args, **kwargs)
            stack.enter_context(patch.object(issuer.os, "open", side_effect=portable_open))
            stack.enter_context(patch.object(issuer, "verify_broker_flat", side_effect=broker or (
                lambda *args, **kwargs: {"positions": 0, "pending_orders": 0,
                                        "checked_utc": NOW.isoformat()})))
            return issuer.authorize(self.root, self.config, "offline-account", "offline-reviewer",
                                    ack=resume.ACK, backup_root=self.backups, now=NOW,
                                    check_service=check_service, client=object())

    def test_publishes_only_grant_and_private_backups_leaving_runtime_evidence_unchanged(self):
        evidence = {path: path.read_bytes() for path in (
            self.archive, self.guard, self.history, self.audit, self.config_file)}
        result = self.issue()
        self.assertEqual(result["status"], "ARCHIVED_HOLD_SESSION_AUTHORIZED")
        self.assertEqual(evidence, {path: path.read_bytes() for path in evidence})
        self.assertFalse(self.status()["entry_blocked"])
        self.assertTrue(RealAccountingGuard(self.guard).snapshot()["blocked"])
        self.assertEqual(len(list(self.backups.iterdir())), 1)

    def test_active_service_refuses_before_broker_or_grant(self):
        def running():
            raise ValueError("GALA_MUST_ALREADY_BE_STOPPED")
        with self.assertRaisesRegex(ValueError, "GALA_MUST_ALREADY_BE_STOPPED"):
            self.issue(check_service=running)
        self.assertFalse(self.approval.exists())
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_new_discrepancy_during_broker_check_prevents_grant(self):
        def mutate(*args, **kwargs):
            self.state["unresolved"]["new-position"] = {"ticker": "BA"}
            self.guard.write_text(json.dumps(self.state))
            return {"positions": 0, "pending_orders": 0}
        with self.assertRaisesRegex(ValueError, "EVIDENCE_CHANGED_DURING_PREFLIGHT"):
            self.issue(broker=mutate)
        self.assertFalse(self.approval.exists())

    def test_existing_grant_is_not_silently_replaced(self):
        self.approval.write_text("existing grant")
        with self.assertRaisesRegex(ValueError, "PRIOR_GRANT_REVIEW_REQUIRED"):
            self.issue()
        self.assertEqual(self.approval.read_text(), "existing grant")


if __name__ == "__main__":
    unittest.main()
