"""Fail-closed, operator-authorized exception for the *known Oct-23* manual intervention.

This does NOT erase or resolve old accounting records. It merely separates the
historical exception from the entry gate on one approved NY session. Any new
unresolved record, changed guard, missing authorization or different account
immediately blocks REAL again. The broker's live checks and daily loss guard
remain independent requirements for every REAL submission.

No automatic activation: live_trading.historical_manual_close_resume must be
explicitly enabled, and an operator must create a root-owned private approval
after separately verifying the account is flat.
"""
import hashlib
import json
import os
import stat
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.project_paths import resolve_runtime_path

NY = ZoneInfo("America/New_York")
SOURCE_SESSION = "2026-10-23"
EXPECTED_TICKERS = {"BA", "META"}
EXPECTED_COUNT = 3
APPROVAL_KIND = "HISTORICAL_MANUAL_INTERVENTION_EXACT_GUARD_V1"


def _fingerprint(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _source_day(key, row):
    """The guard record must independently describe the old NY session."""
    if not isinstance(row, dict) or row.get("ticker") not in EXPECTED_TICKERS:
        return False
    if row.get("session_ny") not in (None, "", SOURCE_SESSION):
        return False
    if str(key).startswith("history:"):
        return str(key).startswith("history:" + SOURCE_SESSION + ":")
    stamp = row.get("recorded_utc")
    if not isinstance(stamp, str) or not stamp:
        return False
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return parsed.tzinfo is not None and parsed.astimezone(NY).date().isoformat() == SOURCE_SESSION
    except (ValueError, OverflowError):
        return False


def strict_historical_guard(guard_path, *, expected_count=EXPECTED_COUNT):
    """Return (sha, keys) only for precisely the three known historical rows."""
    p = Path(guard_path)
    if not p.is_file() or p.is_symlink():
        raise ValueError("ACCOUNTING_STATE_UNAVAILABLE")
    raw = p.read_bytes()
    state = json.loads(raw)
    if not isinstance(state, dict):
        raise ValueError("ACCOUNTING_STATE_INVALID")
    rows = state.get("unresolved")
    if not isinstance(rows, dict) or len(rows) != expected_count:
        raise ValueError("HISTORICAL_SCOPE_MISMATCH")
    if {row.get("ticker") for row in rows.values() if isinstance(row, dict)} != EXPECTED_TICKERS:
        raise ValueError("HISTORICAL_SYMBOLS_MISMATCH")
    if not all(_source_day(key, row) for key, row in rows.items()):
        raise ValueError("HISTORICAL_DAY_UNPROVEN")
    if not isinstance(state.get("verified_historical_reconciliations"), dict):
        raise ValueError("PRIOR_RECONCILIATION_UNAVAILABLE")
    if not any(v.get("ticker") == "NKE" for v in state["verified_historical_reconciliations"].values() if isinstance(v, dict)):
        raise ValueError("NKE_RECOVERY_NOT_VERIFIED")
    return hashlib.sha256(raw).hexdigest(), sorted(rows)


def approval_path(config, root):
    value = (config.get("execution_engine") or {}).get(
        "historical_resume_authorization_path", "data/historical_manual_close_resume.json")
    return Path(resolve_runtime_path(value, project_root=Path(root)))


def _trusted_approval_file(path):
    if not path.is_file() or path.is_symlink():
        return False
    metadata = path.stat(follow_symlinks=False)
    return metadata.st_uid == 0 and not (stat.S_IMODE(metadata.st_mode) & 0o022)


def exception_status(config, guard_path, account, *, now=None, root=None):
    """Read-only admission decision, recomputed at entry time. Never a broker order."""
    reply = {"entry_blocked": True, "historical_resume_active": False,
             "historical_resume_reason": "NOT_AUTHORIZED"}
    if (config.get("live_trading") or {}).get("historical_manual_close_resume") is not True:
        return reply
    try:
        now = now or datetime.now(NY)
        if now.tzinfo is None:
            raise ValueError("NAIVE_CLOCK")
        session = now.astimezone(NY).date().isoformat()
        if session <= SOURCE_SESSION or not str(account or "").strip():
            raise ValueError("SESSION_OR_ACCOUNT_INVALID")
        digest, keys = strict_historical_guard(guard_path)
        root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
        path = approval_path(config, root)
        # Only root can create or rewrite the operator grant; the dashboard
        # user receives read-only group access. Never trust symlinks.
        if not _trusted_approval_file(path):
            raise ValueError("APPROVAL_PERMISSION_INVALID")
        grant = json.loads(path.read_text(encoding="utf-8"))
        if (grant.get("kind") != APPROVAL_KIND or grant.get("session_ny") != session
                or grant.get("source_session_ny") != SOURCE_SESSION
                or grant.get("guard_sha256") != digest
                or grant.get("keys") != keys
                or grant.get("account_fingerprint") != _fingerprint(account)
                or grant.get("broker_flat_verified") is not True
                or grant.get("operator_ack") != "MANUAL_INTERVENTION_REVIEWED"):
            raise ValueError("APPROVAL_MISMATCH")
        reply.update(entry_blocked=False, historical_resume_active=True,
                     historical_resume_reason="EXACT_HISTORICAL_SCOPE_APPROVED")
        return reply
    except (OSError, ValueError, TypeError, KeyError):
        reply["historical_resume_reason"] = "APPROVAL_INVALID_OR_STALE"
        return reply
