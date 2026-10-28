"""Strict offline regression coverage for the historical/manual-close exception.

No account connections, live order sends, engine launches, config writes or
unverified P/L reconstruction.
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.historical_manual_resume import (
    APPROVAL_KIND, _fingerprint, exception_status, strict_historical_guard,
)
from core.real_accounting import RealAccountingGuard
from engine.bot_engine import ChocoletraBot


class ManualCloseHistoricalResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "data").mkdir()
        self.state_path = self.root / "data" / "real_accounting_reconciliation.json"
        self.grant_path = self.root / "data" / "historical_manual_close_resume.json"
        self.now = datetime(2026, 10, 28, 14, 30, tzinfo=timezone.utc)
        self.account = "offline-sample-account"
        self.state = {
            "unresolved": {
                "ba-local": {
                    "ticker": "BA", "recorded_utc": "2026-10-23T13:51:19+00:00",
                    "reason": "manual close / no order identity",
                },
                "history:2026-10-23:ba-local": {
                    "ticker": "BA", "session_ny": "2026-10-23",
                    "reason": "unknown exit P/L",
                },
                "meta-local": {
                    "ticker": "META", "recorded_utc": "2026-10-23T18:45:16+00:00",
                    "reason": "unknown broker fill",
                },
            },
            "verified_historical_reconciliations": {
                "nke-position": {"ticker": "NKE", "source": "FRESH_SCHWAB_GET_EXACT_EXISTING_ORDER_IDS"}
            },
        }
        self.config = {"live_trading": {"historical_manual_close_resume": True},
                       "execution_engine": {
                           "historical_resume_authorization_path": str(self.grant_path)
                       }}
        self.write_state()
        self.issue_approval()

    def write_state(self):
        self.state_path.write_text(json.dumps(self.state))

    def issue_approval(self):
        sha, keys = strict_historical_guard(self.state_path)
        self.grant = {
            "kind": APPROVAL_KIND, "session_ny": "2026-10-28",
            "source_session_ny": "2026-10-23", "guard_sha256": sha,
            "keys": keys, "account_fingerprint": _fingerprint(self.account),
            "broker_flat_verified": True, "operator_ack": "MANUAL_INTERVENTION_REVIEWED",
        }
        self.grant_path.write_text(json.dumps(self.grant))

    def status(self, account=None, now=None, config=None):
        with patch("core.historical_manual_resume._trusted_approval_file", return_value=True):
            return exception_status(config or self.config, self.state_path,
                                    account or self.account, now=now or self.now,
                                    root=self.root)

    def test_default_off_does_not_approve_old_guards(self):
        self.config["live_trading"]["historical_manual_close_resume"] = False
        self.assertTrue(self.status()["entry_blocked"])
        self.assertFalse(self.status()["historical_resume_active"])

    def test_exact_reviewed_old_rows_can_be_approved_for_one_session_only(self):
        permission = self.status()
        self.assertFalse(permission["entry_blocked"])
        self.assertTrue(permission["historical_resume_active"])
        self.assertTrue(RealAccountingGuard(self.state_path).snapshot()["blocked"])
        self.assertEqual(RealAccountingGuard(self.state_path).snapshot()["unresolved"], 3)

    def test_new_accounting_issue_immediately_blocks_further_entries(self):
        self.assertFalse(self.status()["entry_blocked"])
        self.state["unresolved"]["new-live-issue"] = {
            "ticker": "AAPL", "recorded_utc": "2026-10-28T14:31:00+00:00"}
        self.write_state()
        self.assertTrue(self.status()["entry_blocked"])
        self.assertTrue(RealAccountingGuard(self.state_path).snapshot()["blocked"])

    def test_modified_historical_evidence_revokes_grant(self):
        self.state["unresolved"]["ba-local"]["reason"] = "changed local evidence"
        self.write_state()
        self.assertTrue(self.status()["entry_blocked"])

    def test_wrong_account_cannot_reuse_operator_approval(self):
        self.assertTrue(self.status(account="different-account")["entry_blocked"])

    def test_next_new_york_session_requires_separate_authorization(self):
        tomorrow = datetime(2026, 10, 29, 14, 30, tzinfo=timezone.utc)
        self.assertTrue(self.status(now=tomorrow)["entry_blocked"])

    def test_same_day_new_risk_guard_not_suppressed(self):
        self.state["unresolved"]["meta-local"]["recorded_utc"] = "2026-10-28T14:25:00Z"
        self.write_state()
        self.assertTrue(self.status()["entry_blocked"])
        with self.assertRaisesRegex(ValueError, "HISTORICAL_DAY_UNPROVEN"):
            strict_historical_guard(self.state_path)

    def test_missing_nke_verified_archive_refuses(self):
        del self.state["verified_historical_reconciliations"]
        self.write_state()
        self.assertTrue(self.status()["entry_blocked"])

    def test_broker_flat_flag_and_ack_cannot_be_omitted(self):
        self.grant["broker_flat_verified"] = False
        self.grant_path.write_text(json.dumps(self.grant))
        self.assertTrue(self.status()["entry_blocked"])
        self.grant["broker_flat_verified"] = True
        self.grant["operator_ack"] = ""
        self.grant_path.write_text(json.dumps(self.grant))
        self.assertTrue(self.status()["entry_blocked"])

    def test_operator_grant_must_be_root_owned_and_not_writable_by_bot(self):
        with patch("core.historical_manual_resume._trusted_approval_file", return_value=False):
            self.assertTrue(exception_status(
                self.config, self.state_path, self.account, now=self.now,
                root=self.root)["entry_blocked"])

    def test_corrupt_guard_fails_closed(self):
        self.state_path.write_text("{not JSON")
        self.assertTrue(self.status()["entry_blocked"])

    def test_engine_reports_unresolved_truth_with_narrow_entry_exemption(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.config = self.config
        bot.account_number = self.account
        bot.real_accounting_guard = RealAccountingGuard(self.state_path)
        bot._ahora_et = lambda: self.now
        with patch("core.historical_manual_resume._trusted_approval_file", return_value=True):
            state = bot._real_accounting_status()
        self.assertFalse(state["entry_blocked"])
        self.assertTrue(state["blocked"])
        self.assertEqual(state["unresolved"], 3)
        self.assertTrue(state["historical_resume_active"])

    def test_accounting_runtime_failure_must_override_historical_grant(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.config = self.config
        bot.account_number = self.account
        bot.real_accounting_guard = RealAccountingGuard(self.state_path)
        bot.real_accounting_guard._failure = "Current-session history could not be audited"
        bot._ahora_et = lambda: self.now
        with patch("core.historical_manual_resume._trusted_approval_file", return_value=True):
            state = bot._real_accounting_status()
        self.assertTrue(state["blocked"])
        self.assertTrue(state["entry_blocked"])
        self.assertFalse(state["historical_resume_active"])
        self.assertEqual(state["unresolved"], 3)

    def test_any_other_symbol_requires_new_independent_review(self):
        self.state["unresolved"]["meta-local"]["ticker"] = "MSFT"
        self.write_state()
        self.assertTrue(self.status()["entry_blocked"])


if __name__ == "__main__":
    unittest.main()
