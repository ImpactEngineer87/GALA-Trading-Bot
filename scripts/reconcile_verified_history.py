#!/usr/bin/env python3
"""Prepare/apply one exact-ID historical accounting reconciliation.

Only past NY sessions and full, non-partial positions with existing entry AND
exit IDs in both runtime state and local history qualify. Fresh Schwab GETs
must verify those exact complete fills. Missing IDs are never assigned here.
Preparation writes a private plan only. Apply requires gala.service stopped,
fresh broker positions/orders both empty, unchanged scoped evidence and guard,
and another fresh fill verification. Archive proof and the original two guard
records before resolving just those records. Preserve raw trade/AI histories,
model artifacts, training counts and current-day risk state. This utility does
not start/stop services, submit/cancel orders, promote models or enable REAL.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import math
import os
import stat
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
ARCHIVE_KEY = "verified_historical_reconciliations"


class Refused(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise Refused(code)


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(value).hexdigest()


def reference(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:12]


def positive(value):
    try:
        result = float(value)
        return result if not isinstance(value, bool) and math.isfinite(result) and result > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def load_runtime(root):
    import yaml
    from core.project_paths import resolve_runtime_path
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) or {}
    def path(value):
        return Path(resolve_runtime_path(value, project_root=root))
    log = cfg.get("logging") or {}
    guard = path((cfg.get("execution_engine") or {}).get(
        "accounting_state_path", "data/real_accounting_reconciliation.json"))
    require(guard.is_file() and not guard.is_symlink(), "ACCOUNTING_FILE_MISSING_OR_SYMLINK")
    raw = guard.read_bytes()
    state = json.loads(raw)
    require(isinstance(state, dict) and isinstance(state.get("unresolved"), dict), "ACCOUNTING_STATE_INVALID")
    require(all(isinstance(row, dict) for row in state["unresolved"].values()), "ACCOUNTING_RECORD_INVALID")
    require(isinstance(state.get(ARCHIVE_KEY, {}), dict), "RECONCILIATION_ARCHIVE_INVALID")
    return cfg, guard, raw, state, path(log.get("historial_operaciones", "data/historial_operaciones.csv")), path(log.get("auditoria_ordenes", "data/auditoria_ordenes.csv"))


def scope_for(root, position_ref, day, now=None):
    from core.dashboard_evidence import mode_of, session_date
    now = now or datetime.now(timezone.utc)
    require(date.fromisoformat(day) < now.astimezone(NY).date(), "PAST_SESSIONS_ONLY")
    cfg, guard, raw, state, history_path, audit_path = load_runtime(root)
    keys = [key for key, row in state["unresolved"].items()
            if not str(key).startswith(("history:", "pending_exit:")) and reference(key) == position_ref]
    require(len(keys) == 1, "EXACT_RUNTIME_POSITION_REQUIRED")
    position_id = keys[0]
    require(position_id not in state.get(ARCHIVE_KEY, {}), "POSITION_ALREADY_RECONCILED_REVIEW_REQUIRED")
    runtime = state["unresolved"][position_id]
    require(runtime.get("kind") in (None, ""), "UNSUPPORTED_ACCOUNTING_RECORD_KIND")
    with history_path.open(encoding="utf-8-sig", newline="") as handle:
        history = [row for row in csv.DictReader(handle)
                   if mode_of(row) == "REAL" and session_date(row) == day and row.get("position_id") == position_id]
    closes = [r for r in history if r.get("evento") == "POSITION_CLOSE"]
    require(len(closes) == 1, "ONE_EXACT_HISTORY_CLOSE_REQUIRED")
    row = closes[0]
    require(row.get("ticker") == runtime.get("ticker") and bool(row.get("execution_id")), "LOCAL_IDENTITY_INCOMPLETE")
    require(row.get("position_side") in {"LONG", "SHORT"}, "LOCAL_DIRECTION_INCOMPLETE")
    require(row.get("lado") in (None, "", "BUY" if row["position_side"] == "LONG" else "SELL"), "LOCAL_DIRECTION_CONTRADICTORY")
    qty, entry = positive(row.get("cantidad")), positive(row.get("entrada"))
    require(qty is not None and entry is not None, "LOCAL_QUANTITY_OR_ENTRY_INVALID")
    entry_id, exit_id = row.get("parent_order_id"), row.get("order_id")
    require(bool(entry_id) and bool(exit_id) and entry_id != exit_id, "BOTH_EXACT_RECORDED_ORDER_IDS_REQUIRED")
    require(runtime.get("entry_order_id") == entry_id and runtime.get("exit_order_id") == exit_id, "RUNTIME_HISTORY_ORDER_IDS_DIFFER")
    history_key = "history:" + day + ":" + str(row.get("history_id") or position_id)
    require(history_key in state["unresolved"] and state["unresolved"][history_key].get("ticker") == row["ticker"], "EXACT_HISTORY_GUARD_REQUIRED")
    saved_history = state["unresolved"][history_key]
    require(saved_history.get("kind") in (None, "") and saved_history.get("session_ny") in (None, day), "HISTORY_GUARD_SCOPE_DIFFERS")
    for field, expected in (("position_id", position_id), ("entry_order_id", entry_id), ("exit_order_id", exit_id)):
        require(saved_history.get(field) in (None, "", expected), "HISTORY_GUARD_IDENTITY_DIFFERS")
    require(all(r.get("evento") in {"POSITION_OPEN", "POSITION_CLOSE"} for r in history), "PARTIAL_HISTORY_REQUIRES_SEPARATE_REVIEW")
    with audit_path.open(encoding="utf-8-sig", newline="") as handle:
        audit = [r for r in csv.DictReader(handle)
                 if mode_of(r) == "REAL" and r.get("ticker") == row["ticker"]
                 and r.get("position_id") == position_id and r.get("execution_id") == row["execution_id"]]
    entries = [r for r in audit if r.get("evento") == "ENTRY_FILLED" and r.get("order_id") == entry_id]
    exits = [r for r in audit if r.get("evento") == "POSITION_CLOSED" and r.get("order_id") == exit_id and r.get("parent_order_id") == entry_id]
    require(len(entries) == 1 and len(exits) == 1, "EXACT_ENTRY_AND_CLOSE_AUDIT_REQUIRED")
    require(all(positive(r.get("cantidad")) == qty for r in entries + exits), "AUDIT_HISTORY_QUANTITY_DIFFERS")
    require(entries[0].get("position_side") == row["position_side"], "AUDIT_DIRECTION_DIFFERS")
    require(any(r.get("evento") == "EXIT_SUBMITTED" and r.get("order_id") == exit_id for r in audit), "EXACT_EXIT_SUBMISSION_REQUIRED")
    for event in audit:
        require(event.get("evento") != "TP1_FILLED_70_PERCENT", "PARTIAL_FILLS_REQUIRE_SEPARATE_REVIEW")
        try:
            details = json.loads(event.get("detalle") or "{}")
        except (TypeError, ValueError):
            raise Refused("LOCAL_AUDIT_DETAILS_INVALID")
        require(isinstance(details, dict) and not details.get("tp1_ejecutado"), "PARTIAL_CLOSE_REQUIRES_SEPARATE_REVIEW")
    return {"root": str(root), "cfg": cfg, "guard": guard, "guard_raw": raw, "state": state,
            "position_id": position_id, "position_ref": position_ref, "day": day,
            "ticker": row["ticker"], "side": row["position_side"], "qty": qty, "entry": entry,
            "entry_id": entry_id, "exit_id": exit_id, "keys": [position_id, history_key],
            "local_fingerprint": digest({"history": history, "audit": audit})}


def proof_for(scope, client, account, account_hash):
    from scripts.audit_schwab_execution_prices import fill_evidence
    instructions = ("BUY", "SELL") if scope["side"] == "LONG" else ("SELL_SHORT", "BUY_TO_COVER")
    proof, originals = {}, {}
    for role, oid, instruction in zip(("entry", "exit"), (scope["entry_id"], scope["exit_id"]), instructions):
        response = client._get_autorizado(
            client.TRADER_URL + "/accounts/" + quote(str(account_hash), safe="") + "/orders/" + quote(oid, safe=""), timeout=20)
        require(response is not None and response.status_code == 200, "BROKER_EXACT_ORDER_GET_FAILED")
        order = response.json()
        require(isinstance(order, dict) and str(order.get("accountNumber", "")) == str(account), "BROKER_ACCOUNT_DIFFERS")
        evidence = fill_evidence(order, oid, scope["ticker"], instruction)
        require(evidence.get("verified") is True and evidence.get("status") == "FILLED", "COMPLETE_TERMINAL_FILLS_REQUIRED")
        require(abs(evidence["quantity"] - scope["qty"]) <= 1e-6, "BROKER_LOCAL_QUANTITY_DIFFERS")
        times = []
        for activity in order.get("orderActivityCollection") or []:
            for leg in activity.get("executionLegs") or []:
                try:
                    stamp = datetime.fromisoformat(str(leg.get("time")).replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    raise Refused("BROKER_FILL_TIME_REQUIRED")
                require(stamp.tzinfo is not None and stamp.astimezone(NY).date().isoformat() == scope["day"], "BROKER_FILL_SESSION_DIFFERS")
                times.append(stamp.astimezone(timezone.utc).isoformat())
        require(bool(times), "BROKER_FILL_TIME_REQUIRED")
        proof[role] = {"order_id": oid, "quantity": evidence["quantity"], "average_price": evidence["average_price"],
                       "instruction": instruction, "order_type": evidence["order_type"], "times_utc": sorted(times)}
        originals[role] = order
    require(abs(proof["entry"]["average_price"] - scope["entry"]) <= 1e-6, "BROKER_ENTRY_BASIS_DIFFERS")
    require(max(proof["entry"]["times_utc"]) <= min(proof["exit"]["times_utc"]), "BROKER_FILL_CHRONOLOGY_INVALID")
    gross = (proof["exit"]["average_price"] - proof["entry"]["average_price"]) * scope["qty"] * (1 if scope["side"] == "LONG" else -1)
    return {"account_fingerprint": digest(str(account_hash).encode()), "fills": proof,
            "verified_historical_gross_pnl": round(gross, 8), "raw_broker_orders": originals}


def private_bytes(folder, name, value):
    path = folder / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def private_json(folder, name, value):
    return private_bytes(folder, name, json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode())


def plan_for(scope, proof):
    identity = {k: proof[k] for k in ("account_fingerprint", "fills", "verified_historical_gross_pnl")}
    plan = {"format": 1, "action": "ARCHIVE_VERIFIED_PAST_ACCOUNTING", "root": scope["root"],
            "position_id": scope["position_id"], "position_ref": scope["position_ref"], "session_ny": scope["day"],
            "ticker": scope["ticker"], "guard_sha256": digest(scope["guard_raw"]), "local_fingerprint": scope["local_fingerprint"],
            "keys": scope["keys"], "verified_proof": identity}
    plan["fingerprint"] = digest(plan)
    return plan


def public_summary(scope, proof):
    return {"ticker": scope["ticker"], "session_ny": scope["day"], "position_ref": scope["position_ref"],
            "entry_order_ref": reference(scope["entry_id"]), "exit_order_ref": reference(scope["exit_id"]),
            "quantity": scope["qty"], "entry_fill": proof["fills"]["entry"]["average_price"],
            "exit_fill": proof["fills"]["exit"]["average_price"],
            "historical_gross_pnl_before_fees": proof["verified_historical_gross_pnl"],
            "guard_records_to_resolve": [reference(key) for key in scope["keys"]],
            "remaining_unresolved_after_proposal": len(scope["state"]["unresolved"]) - len(scope["keys"]),
            "raw_history_models_training_and_current_day_risk": "UNCHANGED"}


def service_stopped():
    response = subprocess.run(["systemctl", "show", "gala", "-p", "ActiveState", "-p", "MainPID", "--no-pager"],
                              check=True, capture_output=True, text=True, timeout=5)
    fields = dict(line.split("=", 1) for line in response.stdout.splitlines() if "=" in line)
    require(fields.get("ActiveState") == "inactive" and fields.get("MainPID") == "0", "SERVICE_MUST_BE_STOPPED")


def apply_plan(root, plan, client, account, account_hash, backup_root, *, check_service=service_stopped, now=None):
    import fcntl
    require(plan.get("root") == str(root) and plan.get("format") == 1, "PLAN_SCOPE_INVALID")
    require(plan.get("action") == "ARCHIVE_VERIFIED_PAST_ACCOUNTING", "PLAN_ACTION_INVALID")
    require(plan.get("fingerprint") == digest({k: v for k, v in plan.items() if k != "fingerprint"}), "PLAN_FINGERPRINT_INVALID")
    check_service()
    _, guard, raw, state, _, _ = load_runtime(root)
    prior = state.get(ARCHIVE_KEY, {}).get(plan["position_id"])
    if isinstance(prior, dict) and prior.get("plan_fingerprint") == plan["fingerprint"]:
        return {"status": "ALREADY_APPLIED", "unresolved": len(state["unresolved"]), "blocked": bool(state["unresolved"])}
    lock_fd = os.open(guard.with_suffix(guard.suffix + ".reconcile.lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        scope = scope_for(root, plan["position_ref"], plan["session_ny"], now)
        require(client.obtener_posiciones(account_hash) == [] and client.obtener_ordenes_abiertas(account_hash) == [], "BROKER_MUST_CONFIRM_ZERO_POSITIONS_AND_ORDERS")
        proof = proof_for(scope, client, account, account_hash)
        require(plan_for(scope, proof) == plan, "PLAN_STALE_OR_BROKER_EVIDENCE_CHANGED")
        check_service()
        backup_root.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="gala-accounting-reconciled-", dir=backup_root))
        private_bytes(folder, "original_guard_private.json", scope["guard_raw"])
        private_json(folder, "verified_plan_private.json", plan)
        evidence_path = private_json(folder, "broker_fill_evidence_private.json", proof)
        updated = json.loads(scope["guard_raw"])
        original = {key: updated["unresolved"][key] for key in scope["keys"]}
        updated.setdefault(ARCHIVE_KEY, {})[scope["position_id"]] = {
            "plan_fingerprint": plan["fingerprint"], "session_ny": scope["day"], "ticker": scope["ticker"],
            "verified_utc": datetime.now(timezone.utc).isoformat(), "source": "FRESH_SCHWAB_GET_EXACT_EXISTING_ORDER_IDS",
            "proof": {key: value for key, value in proof.items() if key != "raw_broker_orders"},
            "private_evidence_file": str(evidence_path),
            "original_unresolved_records": original, "local_fingerprint": scope["local_fingerprint"],
            "raw_trade_history_preserved": True, "training_and_current_day_risk_unchanged": True,
            "scope": "Recovered historical gross accounting only; not net account P/L or AI eligibility approval"}
        for key in scope["keys"]:
            del updated["unresolved"][key]
        new_bytes = (json.dumps(updated, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        private_json(folder, "commit_intent_private.json", {"before_sha256": digest(scope["guard_raw"]), "after_sha256": digest(new_bytes)})
        require(guard.read_bytes() == scope["guard_raw"], "ACCOUNTING_STATE_CHANGED")
        old_stat = guard.stat(follow_symlinks=False)
        fd, temporary = tempfile.mkstemp(prefix=guard.name + ".reconcile-", dir=guard.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(new_bytes)
                output.flush()
                new_stat = os.fstat(output.fileno())
                if (new_stat.st_uid, new_stat.st_gid) != (old_stat.st_uid, old_stat.st_gid):
                    os.fchown(output.fileno(), old_stat.st_uid, old_stat.st_gid)
                os.fchmod(output.fileno(), stat.S_IMODE(old_stat.st_mode))
                for name in os.listxattr(guard, follow_symlinks=False):
                    os.setxattr(temporary, name, os.getxattr(guard, name, follow_symlinks=False), follow_symlinks=False)
                os.fsync(output.fileno())
            check_service()
            require(guard.read_bytes() == scope["guard_raw"], "ACCOUNTING_STATE_CHANGED")
            os.replace(temporary, guard)
            directory_fd = os.open(guard.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"status": "APPLIED", **public_summary(scope, proof), "private_backup_directory": str(folder),
                "unresolved": len(updated["unresolved"]), "blocked": bool(updated["unresolved"])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--apply", type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--position-ref")
    parser.add_argument("--backup-root", type=Path, default=Path("/opt/gala/backups"))
    args = parser.parse_args(argv)
    root = args.runtime_root.resolve()
    sys.path.insert(0, str(root))
    if args.prepare:
        require(args.date is not None and bool(args.position_ref), "DATE_AND_POSITION_REFERENCE_REQUIRED")
        scope = scope_for(root, args.position_ref, args.date.isoformat())
        cfg = scope["cfg"]
    else:
        require(os.geteuid() == 0, "ROOT_MAINTENANCE_REQUIRED")
        service_stopped()
        plan = json.loads(args.apply.read_text(encoding="utf-8"))
        cfg = load_runtime(root)[0]
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from api.schwab_client import SchwabClient, TRADER_URL
        account = os.getenv("SCHWAB_ACCOUNT_NUMBER", "").strip()
        require(bool(account), "EXACT_CONFIGURED_ACCOUNT_REQUIRED")
        client = SchwabClient(cfg)
        client.TRADER_URL = TRADER_URL
        account_hash = client.obtener_account_hash(account)
        require(bool(account_hash), "EXACT_CONFIGURED_ACCOUNT_UNAVAILABLE")
        if args.prepare:
            proof = proof_for(scope, client, account, account_hash)
            plan = plan_for(scope, proof)
            args.backup_root.mkdir(parents=True, exist_ok=True)
            folder = Path(tempfile.mkdtemp(prefix="gala-accounting-plan-", dir=args.backup_root))
            private_json(folder, "broker_fill_evidence_private.json", proof)
            plan_path = private_json(folder, "plan_private.json", plan)
            result = {"status": "PREPARED_ONLY", "accounting_modified": False, "private_plan_file": str(plan_path), **public_summary(scope, proof)}
        else:
            result = apply_plan(root, plan, client, account, account_hash, args.backup_root)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Refused as exc:
        print(json.dumps({"status": "REFUSED", "code": str(exc), "automatic_reset": False}))
        raise SystemExit(2)
    except Exception:
        print(json.dumps({"status": "UNAVAILABLE", "code": "CHECK_PRIVATE_CONFIGURATION_EVIDENCE_OR_FILE_ACCESS", "commit_status": "INSPECT_GUARD_AND_BACKUP_BEFORE_RETRY", "automatic_reset": False}))
        raise SystemExit(2)
