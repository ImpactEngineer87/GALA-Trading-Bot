#!/usr/bin/env python3
"""Issue an explicit NY-session grant for the exact archived seven-record hold.

Requires root, inactive gala.service, fresh broker-flat evidence and operator
acknowledgment. Never stops/starts the engine, edits accounting/config/history,
places orders, recognizes old P/L or changes AI eligibility.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.archived_accounting_resume import (
    ACK, APPROVAL_KIND, NY, approval_path, fingerprint, guard_path,
    inspect_original_archive, read_json, strict_archived_guard,
    _trusted_approval_file,
)
from core.project_paths import resolve_runtime_path


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def service_inactive():
    response = subprocess.run(
        ["systemctl", "show", "gala", "-p", "ActiveState", "-p", "MainPID", "--no-pager"],
        capture_output=True, text=True, check=True, timeout=10)
    fields = dict(line.split("=", 1) for line in response.stdout.splitlines() if "=" in line)
    require(fields.get("ActiveState") == "inactive" and fields.get("MainPID") == "0",
            "GALA_MUST_ALREADY_BE_STOPPED")


def verify_broker_flat(client, account, *, now=None):
    """GET-only strict raw-account and unfiltered pending-order verification."""
    require(bool(str(account or "").strip()) and client._asegurar_token(), "BROKER_AUTH_REQUIRED")
    account_hash = client.obtener_account_hash(account)
    require(bool(account_hash), "EXACT_ACCOUNT_HASH_REQUIRED")
    base = client.TRADER_URL + "/accounts/" + quote(str(account_hash), safe="")
    response = client._get_autorizado(base, params={"fields": "positions"}, timeout=20)
    require(response is not None and response.status_code == 200, "BROKER_POSITIONS_GET_FAILED")
    data = response.json()
    security = data.get("securitiesAccount") if isinstance(data, dict) else None
    require(isinstance(security, dict) and security.get("accountNumber") is not None
            and str(security["accountNumber"]) == str(account) and bool(security.get("type")),
            "BROKER_ACCOUNT_IDENTITY_INVALID")
    require(security.get("positions", []) == [], "BROKER_POSITIONS_NOT_EMPTY")
    now = now or datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    response = client._get_autorizado(base + "/orders", params={
        "fromEnteredTime": (now - timedelta(days=59)).strftime(fmt),
        "toEnteredTime": (now + timedelta(days=1)).strftime(fmt), "maxResults": 500,
    }, timeout=20)
    require(response is not None and response.status_code == 200, "BROKER_ORDERS_GET_FAILED")
    orders = response.json()
    require(isinstance(orders, list) and len(orders) < 500,
            "BROKER_ORDERS_INVALID_OR_TRUNCATED")
    terminal = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED"}
    pending = list(orders)
    while pending:
        order = pending.pop()
        require(isinstance(order, dict) and order.get("status") in terminal,
                "BROKER_PENDING_ORDER_EXISTS")
        children = order.get("childOrderStrategies", [])
        require(isinstance(children, list), "BROKER_ORDER_CHILDREN_INVALID")
        pending.extend(children)
    return {"positions": 0, "pending_orders": 0,
            "checked_utc": now.astimezone(timezone.utc).isoformat()}


def _read_bytes(path):
    require(path.is_file() and not path.is_symlink(), "LOCAL_EVIDENCE_UNAVAILABLE_OR_SYMLINK")
    return path.read_bytes()


def safe_inputs(config, root, today):
    from core.dashboard_evidence import annotate_history, mode_of, session_date
    require((config.get("scheduler") or {}).get("modo") == "manual", "MANUAL_SCHEDULER_REQUIRED")
    ks = config.get("kill_switch") or {}
    require((ks.get("daily_loss") or {}).get("enabled", True) is True, "DAILY_LOSS_PROTECTION_REQUIRED")
    require(ks.get("liquidar_al_apagar", True) is True, "SHUTDOWN_PROTECTION_REQUIRED")
    auto = ((config.get("agents") or {}).get("entrenamiento") or {}).get("automatic_retraining") or {}
    require(auto.get("auto_activate_production", False) is False, "AUTOMATIC_PROMOTION_FORBIDDEN")
    logging = config.get("logging") or {}
    history = Path(resolve_runtime_path(logging.get(
        "historial_operaciones", "data/historial_operaciones.csv"), project_root=root))
    audit = Path(resolve_runtime_path(logging.get(
        "auditoria_ordenes", "data/auditoria_ordenes.csv"), project_root=root))
    snapshots = {history: _read_bytes(history), audit: _read_bytes(audit)}
    reader = csv.DictReader(io.StringIO(snapshots[history].decode("utf-8-sig")))
    fields = set(reader.fieldnames or [])
    require({"modo", "ticker", "pnl_usd"} <= fields and bool(fields & {"fecha", "timestamp_utc"}),
            "HISTORY_SCHEMA_UNKNOWN")
    rows = [row for row in reader if mode_of(row) == "REAL"
            and session_date(row) == today
            and (row.get("evento") or "POSITION_CLOSE") == "POSITION_CLOSE"]
    checked = annotate_history(rows, csv.DictReader(io.StringIO(snapshots[audit].decode("utf-8-sig"))))
    require(all(row.get("performance_verified") is True for row in checked),
            "CURRENT_SESSION_REAL_ACCOUNTING_UNVERIFIED")
    return snapshots


def _write_private(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def authorize(root, config, account, reviewer, *, ack, renew_expired=False,
              backup_root=Path("/opt/gala/backups"), now=None,
              check_service=service_inactive, client=None):
    require(hasattr(os, "geteuid") and os.geteuid() == 0, "ROOT_REQUIRED")
    require(ack == ACK and bool(str(reviewer or "").strip()), "EXPLICIT_REVIEW_REQUIRED")
    now = now or datetime.now(NY)
    require(now.tzinfo is not None, "TIMEZONE_REQUIRED")
    today = now.astimezone(NY).date().isoformat()
    require(today > "2026-10-29", "PAST_ACCOUNTING_ONLY")
    check_service()
    root = Path(root)
    config_path = root / "config.yaml"
    config_before = _read_bytes(config_path)
    import yaml
    require((yaml.safe_load(config_before) or {}) == config, "CONFIG_CHANGED_BEFORE_PREFLIGHT")
    path = guard_path(config, root)
    before = _read_bytes(path)
    digest, keys, hold = strict_archived_guard(path)
    original = Path(hold["archive_path"])
    archive_digest = inspect_original_archive(original)
    archive_before = _read_bytes(original)
    require(fingerprint(archive_before) == archive_digest, "ARCHIVE_CHANGED_BEFORE_PREFLIGHT")
    snapshots = safe_inputs(config, root, today)
    target = approval_path(config, root)
    require(target.parent.is_dir() and not target.parent.is_symlink()
            and not target.is_symlink(), "APPROVAL_PATH_INVALID")
    prior = None
    if target.exists():
        require(renew_expired and _trusted_approval_file(target), "PRIOR_GRANT_REVIEW_REQUIRED")
        prior, old = read_json(target)
        require(old.get("kind") == APPROVAL_KIND
                and isinstance(old.get("session_ny"), str) and old["session_ny"] < today,
                "ONLY_EXPIRED_GRANT_MAY_BE_RENEWED")
    if client is None:
        from api.schwab_client import SchwabClient, TRADER_URL
        client = SchwabClient(config)
        client.TRADER_URL = TRADER_URL
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        broker = verify_broker_flat(client, account, now=now)
    check_service()
    require(_read_bytes(path) == before and strict_archived_guard(path)[0] == digest
            and _read_bytes(original) == archive_before
            and _read_bytes(config_path) == config_before
            and all(_read_bytes(item) == raw for item, raw in snapshots.items()),
            "EVIDENCE_CHANGED_DURING_PREFLIGHT")
    # Root-private review evidence is separate from the bot-readable grant.
    backup_root = Path(backup_root)
    require(backup_root.is_dir() and not backup_root.is_symlink(), "BACKUP_ROOT_INVALID")
    folder = Path(tempfile.mkdtemp(prefix="archived-accounting-session-", dir=backup_root))
    os.chmod(folder, 0o700)
    grant = {"kind": APPROVAL_KIND, "session_ny": today,
             "guard_sha256": digest, "keys": keys,
             "archive_path": hold["archive_path"], "original_archive_sha256": archive_digest,
             "archived_record_count": 7, "account_fingerprint": fingerprint(account),
             "broker_flat_verified": True, "broker_preflight": broker,
             "operator_ack": ACK, "reviewer": str(reviewer).strip(),
             "approved_at_utc": now.astimezone(timezone.utc).isoformat(),
             "historical_pnl_verified": False, "accounting_records_modified": False,
             "review_backup": str(folder)}
    raw = (json.dumps(grant, indent=2) + "\n").encode()
    _write_private(folder / "guard_before.json", before)
    _write_private(folder / "original_accounting.json", archive_before)
    _write_private(folder / "authorization.json", raw)
    _write_private(folder / "preserved_input_hashes.json", (json.dumps({
        str(item): fingerprint(value) for item, value in
        {config_path: config_before, **snapshots}.items()}, indent=2) + "\n").encode())
    if prior is not None:
        _write_private(folder / "prior_authorization.json", prior)
    directory_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    import grp
    fd, temporary = tempfile.mkstemp(prefix=".archived-session-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchown(handle.fileno(), 0, grp.getgrnam("gala").gr_gid)
            os.fchmod(handle.fileno(), 0o640)
        check_service()
        require(_read_bytes(path) == before and _read_bytes(original) == archive_before
                and _read_bytes(config_path) == config_before
                and all(_read_bytes(item) == value for item, value in snapshots.items()),
                "EVIDENCE_CHANGED_BEFORE_AUTHORIZATION")
        if prior is None:
            # Atomic no-clobber publication; never replace an unexpected grant.
            os.link(temporary, target)
        else:
            require(_trusted_approval_file(target) and _read_bytes(target) == prior,
                    "PRIOR_GRANT_CHANGED")
            os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"status": "ARCHIVED_HOLD_SESSION_AUTHORIZED", "session_ny": today,
            "authorization_path": str(target), "review_backup": str(folder),
            "archived_records_retained": 7, "historical_pnl_verified": False,
            "accounting_modified": False, "engine_started": False,
            "config_opt_in_active": (config.get("live_trading") or {}).get(
                "archived_accounting_resume") is True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ack", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--renew-expired", action="store_true")
    args = parser.parse_args()
    import yaml
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    account = os.getenv("SCHWAB_ACCOUNT_NUMBER", "").strip()
    require(bool(account), "SCHWAB_ACCOUNT_REQUIRED")
    print(json.dumps(authorize(ROOT, config, account, args.reviewer,
                               ack=args.ack, renew_expired=args.renew_expired), indent=2))


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        # Only fixed refusal codes may be displayed; arbitrary broker/config
        # exception messages can contain private account or credential details.
        import re
        code = str(exc)
        print("REFUSED: " + (code if re.fullmatch(r"[A-Z_]+", code)
                              else "PRIVATE_EVIDENCE_CHECK_FAILED"), file=sys.stderr)
        raise SystemExit(2)
    except Exception:
        print("REFUSED: archived scope, authorization, stopped-service or broker preflight failed. "
              "No accounting changes or engine start.", file=sys.stderr)
        raise SystemExit(2)
