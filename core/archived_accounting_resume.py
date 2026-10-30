"""One-session operator exception for the exact archived October accounting hold.

This authorizes only the new-entry gate. Historical prices, P/L and training
eligibility remain unverified; neither the guard nor its archive is rewritten.
"""
from __future__ import annotations

import hashlib
import json
import stat
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.project_paths import resolve_runtime_path

NY = ZoneInfo("America/New_York")
HOLD_KEY = "operator_hold:unverified_real_accounting"
HOLD_STAMP = "2026-10-29T17:34:19.427138+00:00"
HOLD_REASON = ("Seven original REAL accounting discrepancies were archived by operator request. "
               "Execution prices and realized P/L remain unverified. REAL entries must stay blocked.")
APPROVAL_KIND = "ARCHIVED_SEVEN_ACCOUNTING_HOLD_SESSION_V1"
ACK = "I AUTHORIZE ARCHIVED ACCOUNTING FOR THIS NY SESSION"
EXPECTED_ARCHIVE = Counter({
    ("BA", "2026-10-23", "runtime"): 1,
    ("BA", "2026-10-23", "history"): 1,
    ("META", "2026-10-23", "runtime"): 1,
    ("NKE", "2026-10-29", "runtime"): 1,
    ("NKE", "2026-10-29", "history"): 1,
    ("TSLA", "2026-10-29", "runtime"): 1,
    ("TSLA", "2026-10-29", "history"): 1,
})


def fingerprint(value):
    return hashlib.sha256(value if isinstance(value, bytes) else str(value).encode()).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def read_json(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError("EVIDENCE_FILE_UNAVAILABLE_OR_SYMLINK")
    raw = path.read_bytes()
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("EVIDENCE_OBJECT_REQUIRED")
    return raw, value


def guard_path(config, root):
    value = (config.get("execution_engine") or {}).get(
        "accounting_state_path", "data/real_accounting_reconciliation.json")
    return Path(resolve_runtime_path(value, project_root=root))


def approval_path(config, root):
    value = (config.get("execution_engine") or {}).get(
        "archived_resume_authorization_path", "data/archived_accounting_resume.json")
    return Path(resolve_runtime_path(value, project_root=root))


def strict_archived_guard(path):
    """Validate the actual existing hold; any other unresolved issue is refused."""
    raw, state = read_json(path)
    rows = state.get("unresolved")
    if not isinstance(rows, dict) or set(rows) != {HOLD_KEY}:
        raise ValueError("EXACT_ARCHIVED_HOLD_REQUIRED")
    row = rows[HOLD_KEY]
    fields = {"kind", "recorded_utc", "reason", "archive_path", "archived_record_count"}
    if (not isinstance(row, dict) or set(row) != fields
            or row.get("kind") != "OPERATOR_HOLD"
            or row.get("recorded_utc") != HOLD_STAMP
            or row.get("reason") != HOLD_REASON
            or type(row.get("archived_record_count")) is not int
            or row["archived_record_count"] != 7):
        raise ValueError("ARCHIVED_HOLD_SCOPE_CHANGED")
    archive = Path(str(row.get("archive_path") or ""))
    if not archive.is_absolute() or archive.name != "original_accounting.json":
        raise ValueError("ARCHIVED_HOLD_PATH_INVALID")
    verified = state.get("verified_historical_reconciliations")
    if (not isinstance(verified, dict) or len(verified) != 3
            or any(not isinstance(value, dict) for value in verified.values())):
        raise ValueError("VERIFIED_ARCHIVES_CHANGED")
    return fingerprint(raw), [HOLD_KEY], row


def inspect_original_archive(path):
    """Issuer-only check of the seven preserved records, never a P/L proof."""
    raw, state = read_json(path)
    rows = state.get("unresolved")
    if not isinstance(rows, dict) or len(rows) != 7:
        raise ValueError("SEVEN_ORIGINAL_RECORDS_REQUIRED")
    observed = Counter()
    for key, row in rows.items():
        if not isinstance(row, dict) or not row.get("reason"):
            raise ValueError("ARCHIVE_RECORD_INVALID")
        stamp = datetime.fromisoformat(str(row.get("recorded_utc", "")).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("ARCHIVE_TIMESTAMP_INVALID")
        if str(key).startswith("history:"):
            day = row.get("session_ny")
            if not isinstance(day, str) or not str(key).startswith("history:" + day + ":"):
                raise ValueError("ARCHIVE_HISTORY_IDENTITY_INVALID")
            kind = "history"
        else:
            day = stamp.astimezone(NY).date().isoformat()
            kind = "runtime"
            if not {"entry_order_id", "exit_order_id", "requested_exit_price",
                    "broker_exit_price", "broker_flat"} <= set(row):
                raise ValueError("ARCHIVE_RUNTIME_SCHEMA_INVALID")
        observed[(row.get("ticker"), day, kind)] += 1
    if observed != EXPECTED_ARCHIVE:
        raise ValueError("ORIGINAL_ARCHIVED_SCOPE_CHANGED")
    return fingerprint(raw)


def _trusted_approval_file(path):
    if not path.is_file() or path.is_symlink():
        return False
    info = path.stat(follow_symlinks=False)
    return info.st_uid == 0 and not (stat.S_IMODE(info.st_mode) & 0o022)


def exception_status(config, path, account, *, now=None, root=None):
    reply = {"entry_blocked": True, "historical_resume_active": False,
             "archived_resume_active": False,
             "historical_resume_reason": "ARCHIVED_HOLD_NOT_AUTHORIZED"}
    if (config.get("live_trading") or {}).get("archived_accounting_resume") is not True:
        return reply
    try:
        now = now or datetime.now(NY)
        if now.tzinfo is None or not str(account or "").strip():
            raise ValueError("SESSION_OR_ACCOUNT_INVALID")
        day = now.astimezone(NY).date().isoformat()
        if day <= "2026-10-29":
            raise ValueError("PAST_ARCHIVE_REQUIRED")
        digest, keys, hold = strict_archived_guard(path)
        root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        grant_path = approval_path(config, root)
        if not _trusted_approval_file(grant_path):
            raise ValueError("APPROVAL_PERMISSION_INVALID")
        _, grant = read_json(grant_path)
        archive_digest = grant.get("original_archive_sha256")
        if (grant.get("kind") != APPROVAL_KIND or grant.get("session_ny") != day
                or grant.get("guard_sha256") != digest or grant.get("keys") != keys
                or grant.get("account_fingerprint") != fingerprint(account)
                or grant.get("archive_path") != hold["archive_path"]
                or not isinstance(archive_digest, str) or len(archive_digest) != 64
                or any(c not in "0123456789abcdef" for c in archive_digest)
                or grant.get("archived_record_count") != 7
                or grant.get("broker_flat_verified") is not True
                or grant.get("operator_ack") != ACK
                or grant.get("historical_pnl_verified") is not False
                or grant.get("accounting_records_modified") is not False):
            raise ValueError("APPROVAL_MISMATCH")
        reply.update(entry_blocked=False, historical_resume_active=True,
                     archived_resume_active=True,
                     historical_resume_reason="EXACT_ARCHIVED_HOLD_SESSION_APPROVED")
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        reply["historical_resume_reason"] = "ARCHIVED_APPROVAL_INVALID_OR_STALE"
    return reply
