#!/usr/bin/env python3
"""Issue a one-session, audited historical BA/META exception with Schwab flat.

Operator-only maintenance, NEVER an order sender. Run as root with gala.service
STOPPED, after the dashboard trading engine was stopped normally and the client
approved this precise scope. Does not edit or clear historical accounting, old
trade records, models, AI feedback or daily risk. The operator must separately
enable the opt-in config switch after reviewing the release.
"""
import argparse
import contextlib
import csv
import grp
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.historical_manual_resume import (APPROVAL_KIND, SOURCE_SESSION,
    approval_path, strict_historical_guard, _fingerprint)
from core.project_paths import resolve_runtime_path


def require(check, label):
    if not check:
        raise SystemExit("REFUSED: " + label)


def service_inactive():
    r = subprocess.run(["systemctl", "show", "gala", "-p", "ActiveState",
                        "-p", "MainPID", "--no-pager"], capture_output=True,
                       text=True, check=True, timeout=10)
    fields = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    require(fields.get("ActiveState") == "inactive" and fields.get("MainPID") == "0",
            "STOP_GALA_SERVICE_FIRST_AFTER_NORMAL_ENGINE_STOP")


def safe_config_and_history(config, today):
    from core.dashboard_evidence import annotate_history, mode_of, session_date
    require((config.get("scheduler") or {}).get("modo") == "manual",
            "SCHEDULER_MUST_BE_MANUAL")
    require((config.get("kill_switch") or {}).get("daily_loss", {}).get("enabled", True) is True,
            "DAILY_LOSS_GUARD_REQUIRED")
    require((config.get("kill_switch") or {}).get("liquidar_al_apagar", True) is True,
            "SHUTDOWN_PROTECTION_REQUIRED")
    require((config.get("agents") or {}).get("entrenamiento", {}).get("automatic_retraining", {}).get(
        "auto_activate_production", False) is False, "AUTO_MODEL_PROMOTION_MUST_BE_DISABLED")
    cfg_log = config.get("logging") or {}
    hist = Path(resolve_runtime_path(cfg_log.get(
        "historial_operaciones", "data/historial_operaciones.csv"), project_root=ROOT))
    audit = Path(resolve_runtime_path(cfg_log.get(
        "auditoria_ordenes", "data/auditoria_ordenes.csv"), project_root=ROOT))
    require(hist.is_file(), "HISTORY_REQUIRED")
    with hist.open(encoding="utf-8-sig", newline="") as f:
        closed = [row for row in csv.DictReader(f) if mode_of(row) == "REAL"
                  and session_date(row) == today
                  and (row.get("evento") or "POSITION_CLOSE") == "POSITION_CLOSE"]
    if closed:
        if audit.is_file():
            with audit.open(encoding="utf-8-sig", newline="") as f:
                checked = annotate_history(closed, csv.DictReader(f))
        else:
            checked = annotate_history(closed)
        require(all(row.get("performance_verified") is True for row in checked),
                "CURRENT_SESSION_UNVERIFIED_CLOSE")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ack", required=True,
                    help="exact phrase: I AUTHORIZE OCT23 HISTORICAL ONLY")
    args = ap.parse_args()
    require(os.geteuid() == 0, "ROOT_REQUIRED")
    require(args.ack == "I AUTHORIZE OCT23 HISTORICAL ONLY",
            "EXPLICIT_OPERATOR_APPROVAL_REQUIRED")
    service_inactive()
    import yaml
    from dotenv import load_dotenv
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    today = datetime.now(NY).date().isoformat()
    require(today > SOURCE_SESSION, "PAST_SESSION_ONLY")
    safe_config_and_history(config, today)
    guard = Path(resolve_runtime_path((config.get("execution_engine") or {}).get(
        "accounting_state_path", "data/real_accounting_reconciliation.json"), project_root=ROOT))
    before, keys = strict_historical_guard(guard)
    load_dotenv(ROOT / ".env", override=False)
    account = os.getenv("SCHWAB_ACCOUNT_NUMBER", "").strip()
    require(bool(account), "SCHWAB_ACCOUNT_REQUIRED")

    # Existing read-only broker preflight resolves the configured account and
    # distinguishes a verified empty list from an unavailable API response.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from api.broker_check import ejecutar_broker_check
        broker = ejecutar_broker_check(str(ROOT / "config.yaml"), account, "MSFT")
    require(broker.get("ok") is True, "BROKER_CHECK_FAILED")
    for name in ("positions", "open_orders"):
        step = (broker.get("steps") or {}).get(name) or {}
        require(step.get("ok") is True and type(step.get("count")) is int
                and step["count"] == 0, "BROKER_NOT_CONFIRMED_FLAT_" + name.upper())
    service_inactive()
    require(strict_historical_guard(guard) == (before, keys),
            "GUARD_CHANGED_DURING_PREFLIGHT")
    path = approval_path(config, ROOT)
    require(path.parent.is_dir() and not path.parent.is_symlink(),
            "APPROVAL_PARENT_UNAVAILABLE")
    require(not path.is_symlink(), "APPROVAL_SYMLINK_FORBIDDEN")
    grant = {
        "kind": APPROVAL_KIND,
        "session_ny": today,
        "source_session_ny": SOURCE_SESSION,
        "guard_sha256": before,
        "keys": keys,
        "account_fingerprint": _fingerprint(account),
        "broker_flat_verified": True,
        "operator_ack": "MANUAL_INTERVENTION_REVIEWED",
        "approved_at_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
        "note": "Only removes old Oct-23 BA/META from new-entry gate for this NY day; old accounting remains unresolved.",
    }
    temp_name = None
    fd, temp_name = tempfile.mkstemp(prefix=".historical-grant-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(grant, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
            os.fchown(out.fileno(), 0, grp.getgrnam("gala").gr_gid)
            os.fchmod(out.fileno(), 0o640)
        service_inactive()
        require(strict_historical_guard(guard) == (before, keys),
                "GUARD_CHANGED_BEFORE_WRITE")
        # Never overwrite an existing grant without separate operator review.
        require(not path.exists(), "PRIOR_GRANT_EXISTS_REVIEW_BEFORE_REISSUE")
        os.replace(temp_name, path)
        temp_name = None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)

    print(json.dumps({"status": "APPROVED_HISTORICAL_ONLY",
        "session_ny": today,
        "old_unresolved_retained": len(keys),
        "schwab_positions": 0, "schwab_open_orders": 0,
        "approval_path": str(path),
        "config_opt_in_active": (config.get("live_trading") or {}).get(
            "historical_manual_close_resume") is True,
        "new_accounting_issue_will_block": True,
        "accounting_file_modified": False,
        "models_history_training_modified": False,
        "bot_started": False}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Never print private broker credentials or account details on errors.
        print("REFUSED: PRIVATE_BROKER_OR_RUNTIME_CHECK_FAILED", file=sys.stderr)
        raise SystemExit(2)
