"""The reviewed manual TSLA exception changes only the new REAL entry gate."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from core import accounting_entry_policy, reviewed_manual_tsla_resume as resume
from core.archived_accounting_resume import HOLD_KEY, HOLD_REASON, HOLD_STAMP, fingerprint
from core.real_accounting import RealAccountingGuard

NOW = datetime(2026, 11, 3, 14, 30, tzinfo=timezone.utc)


class ReviewedManualTslaResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.guard = self.root / "guard.json"
        self.grant_path = self.root / "grant.json"
        self.archive = self.root / "original_accounting.json"
        self.config = {
            "live_trading": {"reviewed_manual_tsla_resume": True,
                             "archived_accounting_resume": True},
            "execution_engine": {
                "reviewed_manual_tsla_authorization_path": str(self.grant_path),
                "accounting_state_path": str(self.guard)},
        }
        self.state = {
            "unresolved": {
                HOLD_KEY: {
                    "kind": "OPERATOR_HOLD", "recorded_utc": HOLD_STAMP,
                    "reason": HOLD_REASON, "archive_path": str(self.archive),
                    "archived_record_count": 7,
                },
                "tsla-short": {
                    "recorded_utc": "2026-11-02T16:42:12+00:00",
                    "ticker": "TSLA", "reason": resume.RUNTIME_REASON,
                    "entry_order_id": "gala-entry", "exit_order_id": None,
                    "requested_exit_price": 410.6, "broker_exit_price": None,
                    "broker_flat": True,
                },
                "tsla-long": {
                    "recorded_utc": "2026-11-02T17:37:36+00:00",
                    "ticker": "TSLA", "reason": resume.RUNTIME_REASON,
                    "entry_order_id": None, "exit_order_id": None,
                    "requested_exit_price": 409.0, "broker_exit_price": None,
                    "broker_flat": True,
                },
                "history:2026-11-02:tsla-short": {
                    "recorded_utc": "2026-11-03T00:00:00+00:00",
                    "ticker": "TSLA", "session_ny": "2026-11-02",
                    "reason": resume.HISTORY_REASON,
                },
                "history:2026-11-02:tsla-long": {
                    "recorded_utc": "2026-11-03T00:00:00+00:00",
                    "ticker": "TSLA", "session_ny": "2026-11-02",
                    "reason": resume.HISTORY_REASON,
                },
            },
            "verified_historical_reconciliations": {"a": {}, "b": {}, "c": {}},
        }
        self.write_guard()
        digest, keys, _ = resume.strict_manual_guard(self.guard)
        self.grant = {
            "kind": resume.APPROVAL_KIND,
            "source_session_ny": resume.SOURCE_DAY,
            "session_ny": "2026-11-03",
            "guard_sha256": digest, "keys": keys,
            "archive_path": str(self.archive),
            "original_archive_sha256": "a" * 64,
            "broker_proof_sha256": "b" * 64,
            "account_fingerprint": fingerprint("offline-account"),
            "broker_flat_verified": True, "manual_order_count": 7,
            "manual_activity_training_eligible": False,
            "historical_pnl_verified": False,
            "accounting_records_modified": False,
            "operator_ack": resume.ACK,
        }
        self.write_grant()

    def write_guard(self):
        self.guard.write_text(json.dumps(self.state), encoding="utf-8")

    def write_grant(self):
        self.grant_path.write_text(json.dumps(self.grant), encoding="utf-8")

    def status(self, *, account="offline-account", now=NOW):
        with patch.object(resume, "_trusted_approval_file", return_value=True):
            return accounting_entry_policy.exception_status(
                self.config, self.guard, account, now=now, root=self.root)

    def test_exact_grant_allows_new_entries_without_clearing_accounting(self):
        before = {path: path.read_bytes() for path in (self.guard, self.grant_path)}
        state = self.status()
        self.assertFalse(state["entry_blocked"])
        self.assertTrue(state["manual_tsla_resume_active"])
        self.assertTrue(RealAccountingGuard(self.guard).snapshot()["blocked"])
        self.assertEqual(RealAccountingGuard(self.guard).snapshot()["unresolved"], 5)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_new_discrepancy_or_changed_existing_record_blocks_again(self):
        self.state["unresolved"]["new-real-trade"] = {"ticker": "BA"}
        self.write_guard()
        self.assertTrue(self.status()["entry_blocked"])
        del self.state["unresolved"]["new-real-trade"]
        self.state["unresolved"]["tsla-short"]["broker_flat"] = False
        self.write_guard()
        self.assertTrue(self.status()["entry_blocked"])

    def test_other_account_expired_session_and_invalid_day_block(self):
        self.assertTrue(self.status(account="other-account")["entry_blocked"])
        self.assertTrue(self.status(now=datetime(2026, 11, 4, 14, tzinfo=timezone.utc))["entry_blocked"])
        for day in ("2026-11-4", "2026-11-32", "2026-11-05"):
            with self.subTest(day=day):
                self.grant["session_ny"] = day
                self.write_grant()
                self.assertTrue(self.status()["entry_blocked"])

    def test_explicit_future_session_opt_in_keeps_exact_scope_only(self):
        self.config["live_trading"]["reviewed_manual_tsla_future_sessions"] = True
        tomorrow = datetime(2026, 11, 4, 14, tzinfo=timezone.utc)
        status = self.status(now=tomorrow)
        self.assertFalse(status["entry_blocked"])
        self.assertEqual(status["historical_resume_reason"],
                         "REVIEWED_MANUAL_TSLA_PRIOR_SESSION_APPROVED")
        self.state["unresolved"]["new-manual-trade"] = {"ticker": "NKE"}
        self.write_guard()
        self.assertTrue(self.status(now=tomorrow)["entry_blocked"])

    def test_missing_trust_and_forged_pnl_claim_block(self):
        with patch.object(resume, "_trusted_approval_file", return_value=False):
            self.assertTrue(accounting_entry_policy.exception_status(
                self.config, self.guard, "offline-account", now=NOW,
                root=self.root)["entry_blocked"])
        self.grant["historical_pnl_verified"] = True
        self.write_grant()
        self.assertTrue(self.status()["entry_blocked"])

    def test_invalid_manual_grant_cannot_fall_back_to_old_archive_permission(self):
        self.grant_path.unlink()
        self.assertTrue(self.status()["entry_blocked"])

    def test_engine_failure_overrides_stored_grant(self):
        from engine.bot_engine import ChocoletraBot
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.config = self.config
        bot.real_accounting_guard = RealAccountingGuard(self.guard)
        bot._account_plain = "offline-account"
        bot.account_number = "broker-hash"
        bot._ahora_et = lambda: NOW
        with patch.object(resume, "_trusted_approval_file", return_value=True):
            self.assertFalse(bot._real_accounting_status()["entry_blocked"])
            bot.real_accounting_guard._failure = "persistence unavailable"
            self.assertTrue(bot._real_accounting_status()["entry_blocked"])


if __name__ == "__main__":
    unittest.main()
