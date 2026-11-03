"""Exact-scope entry exception for the reviewed 2026-11-02 TSLA activity.

The five accounting records stay unresolved. This module never attributes a
manual order to GALA, supplies missing P/L, or makes a trade training eligible.
Any new accounting record or changed grant immediately blocks REAL entries.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path

from core.archived_accounting_resume import (
    HOLD_KEY, HOLD_REASON, HOLD_STAMP, NY, _trusted_approval_file,
    fingerprint, read_json,
)
from core.project_paths import resolve_runtime_path

SOURCE_DAY = "2026-11-02"
APPROVAL_KIND = "REVIEWED_MANUAL_TSLA_2026_11_02_SESSION_V1"
ACK = "I CONFIRM ALL SEVEN NOV2 TSLA MARKET ORDERS WERE MANUAL"
RUNTIME_REASON = "Precio/identidad/cantidad de ejecución incompletos; P/L desconocido"
HISTORY_REASON = "P/L ausente o inválido"


def _positive(value):
    try:
        number = float(value)
        return not isinstance(value, bool) and math.isfinite(number) and number > 0
    except (TypeError, ValueError, OverflowError):
        return False


def strict_manual_guard(path):
    """Bind exactly the original hold and four Nov 2 TSLA discrepancies."""
    raw, state = read_json(path)
    rows = state.get("unresolved")
    if not isinstance(rows, dict) or len(rows) != 5 or HOLD_KEY not in rows:
        raise ValueError("EXACT_MANUAL_TSLA_SCOPE_REQUIRED")
    hold = rows[HOLD_KEY]
    if (not isinstance(hold, dict)
            or set(hold) != {"kind", "recorded_utc", "reason", "archive_path", "archived_record_count"}
            or hold.get("kind") != "OPERATOR_HOLD"
            or hold.get("recorded_utc") != HOLD_STAMP
            or hold.get("reason") != HOLD_REASON
            or type(hold.get("archived_record_count")) is not int
            or hold["archived_record_count"] != 7):
        raise ValueError("ORIGINAL_ARCHIVED_HOLD_CHANGED")
    archive = Path(str(hold.get("archive_path") or ""))
    if not archive.is_absolute() or archive.name != "original_accounting.json":
        raise ValueError("ORIGINAL_ARCHIVE_PATH_INVALID")
    verified = state.get("verified_historical_reconciliations")
    if (not isinstance(verified, dict) or len(verified) != 3
            or any(not isinstance(value, dict) for value in verified.values())):
        raise ValueError("VERIFIED_ARCHIVES_CHANGED")

    runtime, history = [], []
    for key, row in rows.items():
        if key == HOLD_KEY:
            continue
        if not isinstance(row, dict) or row.get("ticker") != "TSLA":
            raise ValueError("MANUAL_TSLA_RECORD_INVALID")
        if str(key).startswith("history:"):
            if (not str(key).startswith("history:" + SOURCE_DAY + ":")
                    or set(row) != {"recorded_utc", "ticker", "session_ny", "reason"}
                    or row.get("session_ny") != SOURCE_DAY
                    or row.get("reason") != HISTORY_REASON):
                raise ValueError("MANUAL_TSLA_HISTORY_SCOPE_INVALID")
            history.append(key)
            continue
        if (set(row) != {"recorded_utc", "ticker", "reason", "entry_order_id",
                         "exit_order_id", "requested_exit_price", "broker_exit_price", "broker_flat"}
                or row.get("reason") != RUNTIME_REASON
                or row.get("broker_flat") is not True
                or row.get("exit_order_id") not in (None, "")
                or row.get("broker_exit_price") is not None
                or not _positive(row.get("requested_exit_price"))):
            raise ValueError("MANUAL_TSLA_RUNTIME_SCOPE_INVALID")
        try:
            stamp = datetime.fromisoformat(str(row.get("recorded_utc") or "").replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.astimezone(NY).date().isoformat() != SOURCE_DAY:
                raise ValueError("MANUAL_TSLA_RUNTIME_DAY_INVALID")
        except (TypeError, ValueError, OverflowError):
            raise ValueError("MANUAL_TSLA_RUNTIME_DAY_INVALID")
        runtime.append((key, row))
    if (len(runtime) != 2 or len(history) != 2
            or sum(bool(row.get("entry_order_id")) for _, row in runtime) != 1):
        raise ValueError("MANUAL_TSLA_RECORD_COUNT_INVALID")
    return fingerprint(raw), sorted(rows), hold


def approval_path(config, root):
    value = (config.get("execution_engine") or {}).get(
        "reviewed_manual_tsla_authorization_path", "data/reviewed_manual_tsla_resume.json")
    return Path(resolve_runtime_path(value, project_root=Path(root)))


def exception_status(config, guard_path, account, *, now=None, root=None):
    reply = {"entry_blocked": True, "historical_resume_active": False,
             "manual_tsla_resume_active": False,
             "historical_resume_reason": "MANUAL_TSLA_NOT_AUTHORIZED"}
    if (config.get("live_trading") or {}).get("reviewed_manual_tsla_resume") is not True:
        return reply
    try:
        now = now or datetime.now(NY)
        if now.tzinfo is None or not str(account or "").strip():
            raise ValueError("SESSION_OR_ACCOUNT_INVALID")
        day = now.astimezone(NY).date().isoformat()
        if day <= SOURCE_DAY:
            raise ValueError("PAST_SESSION_REQUIRED")
        digest, keys, hold = strict_manual_guard(guard_path)
        root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        grant_path = approval_path(config, root)
        if not _trusted_approval_file(grant_path):
            raise ValueError("APPROVAL_PERMISSION_INVALID")
        _, grant = read_json(grant_path)
        archive_digest = grant.get("original_archive_sha256")
        broker_digest = grant.get("broker_proof_sha256")
        if (grant.get("kind") != APPROVAL_KIND
                or grant.get("source_session_ny") != SOURCE_DAY
                or grant.get("guard_sha256") != digest
                or grant.get("keys") != keys
                or grant.get("archive_path") != hold["archive_path"]
                or grant.get("account_fingerprint") != fingerprint(account)
                or grant.get("broker_flat_verified") is not True
                or grant.get("manual_order_count") != 7
                or grant.get("manual_activity_training_eligible") is not False
                or grant.get("historical_pnl_verified") is not False
                or grant.get("accounting_records_modified") is not False
                or grant.get("operator_ack") != ACK
                or not isinstance(archive_digest, str)
                or len(archive_digest) != 64
                or any(char not in "0123456789abcdef" for char in archive_digest)
                or not isinstance(broker_digest, str)
                or len(broker_digest) != 64
                or any(char not in "0123456789abcdef" for char in broker_digest)):
            raise ValueError("APPROVAL_MISMATCH")
        approved_day = grant.get("session_ny")
        if (not isinstance(approved_day, str)
                or date.fromisoformat(approved_day).isoformat() != approved_day
                or approved_day > day or approved_day <= SOURCE_DAY):
            raise ValueError("APPROVAL_DAY_INVALID")
        reply.update(authorization_session_ny=approved_day, current_session_ny=day)
        future_sessions = (config.get("live_trading") or {}).get(
            "reviewed_manual_tsla_future_sessions") is True
        if approved_day != day and not future_sessions:
            reply["historical_resume_reason"] = "MANUAL_TSLA_APPROVAL_EXPIRED"
            return reply
        reply.update(entry_blocked=False, historical_resume_active=True,
                     manual_tsla_resume_active=True,
                     reviewed_future_sessions_enabled=future_sessions,
                     historical_resume_reason=(
                         "REVIEWED_MANUAL_TSLA_PRIOR_SESSION_APPROVED"
                         if approved_day != day else
                         "REVIEWED_MANUAL_TSLA_SESSION_APPROVED"))
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        reply["historical_resume_reason"] = "MANUAL_TSLA_APPROVAL_INVALID_OR_STALE"
    return reply
