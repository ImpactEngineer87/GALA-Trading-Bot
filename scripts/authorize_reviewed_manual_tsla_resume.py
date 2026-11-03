#!/usr/bin/env python3
"""Review Nov 2 manual TSLA evidence and issue a one-session REAL entry grant.

Preview is offline and read-only. Authorization requires root, inactive service,
fresh Schwab flat/pending-order GETs, unchanged local evidence and explicit
confirmation that all seven chart-tagged TSLA MARKET orders were manual.
Neither mode changes accounting, history, model results, orders or risk limits.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
import re
import stat
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.archived_accounting_resume import (
    _trusted_approval_file, fingerprint, inspect_original_archive, read_json,
)
from core.dashboard_evidence import mode_of, session_date
from core.reviewed_manual_tsla_resume import (
    ACK, APPROVAL_KIND, NY, SOURCE_DAY, approval_path, strict_manual_guard,
)
from scripts.authorize_archived_accounting_resume import (
    require, safe_inputs, service_inactive, verify_broker_flat,
)
from scripts.audit_schwab_execution_prices import fill_evidence

DEFAULT_EVIDENCE = Path("/opt/gala/backups/gala-order-evidence-2026-11-02-4llfhdpm")
EXPECTED_MANUAL = (
    ("BUY_TO_COVER", "11:42:09", Decimal("410.66")),
    ("BUY", "11:54:23", Decimal("411.2")),
    ("SELL", "12:37:34", Decimal("409.06")),
    ("SELL_SHORT", "12:39:01", Decimal("408.77")),
    ("BUY_TO_COVER", "12:39:22", Decimal("408.825")),
    ("SELL_SHORT", "12:39:46", Decimal("408.57")),
    ("BUY_TO_COVER", "12:39:56", Decimal("408.6111")),
)


def reference(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:12]


def private_json(path, *, owner_only=True):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "PRIVATE_EVIDENCE_MISSING")
    if owner_only:
        info = path.stat(follow_symlinks=False)
        require(info.st_uid == 0 and not (stat.S_IMODE(info.st_mode) & 0o077),
                "PRIVATE_EVIDENCE_PERMISSIONS_INVALID")
    return read_json(path)


def iter_orders(orders):
    pending = list(orders)
    while pending:
        order = pending.pop()
        require(isinstance(order, dict), "BROKER_ORDER_SHAPE_INVALID")
        yield order
        children = order.get("childOrderStrategies") or []
        require(isinstance(children, list), "BROKER_ORDER_CHILDREN_INVALID")
        pending.extend(children)


def _stock_leg(order):
    legs = order.get("orderLegCollection") or []
    require(isinstance(legs, list) and len(legs) == 1
            and isinstance(legs[0], dict), "BROKER_ORDER_LEG_INVALID")
    instrument = legs[0].get("instrument") or {}
    require(isinstance(instrument, dict), "BROKER_INSTRUMENT_INVALID")
    return legs[0], instrument.get("symbol")


def _fill_time(order):
    fills = [leg for activity in order.get("orderActivityCollection") or []
             if activity.get("activityType") == "EXECUTION"
             and activity.get("executionType") == "FILL"
             for leg in activity.get("executionLegs") or []]
    require(len(fills) == 1, "MANUAL_ORDER_FILL_COUNT_INVALID")
    stamp = datetime.fromisoformat(str(fills[0].get("time") or "").replace("Z", "+00:00"))
    require(stamp.tzinfo is not None
            and stamp.astimezone(NY).date().isoformat() == SOURCE_DAY,
            "MANUAL_ORDER_FILL_DAY_INVALID")
    return stamp.astimezone(NY).strftime("%H:%M:%S")


def proof_from_packets(root, account, order_path, transaction_path, *, owner_only=True):
    """Verify exact incident scope without assigning any manual fill to GALA."""
    root = Path(root)
    require(bool(str(account or "").strip()), "ACCOUNT_REQUIRED")
    guard_path = root / "data/real_accounting_reconciliation.json"
    guard_digest, keys, hold = strict_manual_guard(guard_path)
    order_raw, order_packet = private_json(order_path, owner_only=owner_only)
    txn_raw, txn_packet = private_json(transaction_path, owner_only=owner_only)
    require(order_packet.get("session_ny") == SOURCE_DAY
            and order_packet.get("order_list_may_be_truncated") is False,
            "ORDER_EVIDENCE_SCOPE_INVALID")
    orders = order_packet.get("orders")
    require(isinstance(orders, list) and len(orders) < 500,
            "BROKER_ORDER_LIST_INVALID")
    all_orders = list(iter_orders(orders))
    ids = [str(order.get("orderId") or "") for order in all_orders]
    require(all(ids) and len(ids) == len(set(ids)), "BROKER_ORDER_ID_INVALID")
    require(all(str(order.get("accountNumber")) == str(account) for order in all_orders),
            "BROKER_ORDER_ACCOUNT_MISMATCH")
    order_by_id = dict(zip(ids, all_orders))

    with (root / "data/auditoria_ordenes.csv").open(encoding="utf-8-sig", newline="") as handle:
        audit = list(csv.DictReader(handle))
    audited = {str(row.get("order_id") or "") for row in audit
               if mode_of(row) == "REAL" and row.get("order_id")}
    manual = []
    for order in all_orders:
        leg, symbol = _stock_leg(order)
        if (symbol != "TSLA" or order.get("orderType") != "MARKET"
                or order.get("tag") != "API_TOS:CHART"
                or order.get("status") != "FILLED"):
            continue
        oid = str(order["orderId"])
        require(oid not in audited, "MANUAL_ORDER_HAS_GALA_AUDIT")
        evidence = fill_evidence(order, oid, symbol, leg.get("instruction"))
        require(evidence.get("verified") is True and evidence.get("status") == "FILLED"
                and evidence.get("quantity") == 3.0,
                "MANUAL_ORDER_FILL_UNVERIFIED")
        manual.append((leg.get("instruction"), _fill_time(order),
                       Decimal(str(evidence["average_price"])), oid))
    manual.sort(key=lambda item: item[1])
    require(len(manual) == 7, "SEVEN_MANUAL_TSLA_ORDERS_REQUIRED")
    for actual, expected in zip(manual, EXPECTED_MANUAL):
        require(actual[:2] == expected[:2] and abs(actual[2] - expected[2]) <= Decimal("0.0001"),
                "MANUAL_ORDER_SEQUENCE_CHANGED")

    # Every filled order in the captured broker window must be either GALA
    # audited or one of the seven confirmed manual TSLA orders.
    manual_ids = {item[3] for item in manual}
    for order in all_orders:
        if order.get("status") == "FILLED":
            require(str(order["orderId"]) in audited | manual_ids,
                    "OTHER_UNLINKED_BROKER_FILL_EXISTS")

    state = read_json(guard_path)[1]
    runtime = [(key, row) for key, row in state["unresolved"].items()
               if key != "operator_hold:unverified_real_accounting" and not key.startswith("history:")]
    require(len(runtime) == 2, "MANUAL_RUNTIME_COUNT_INVALID")
    known_entry = next(row["entry_order_id"] for _, row in runtime if row.get("entry_order_id"))
    entry = order_by_id.get(str(known_entry))
    require(entry is not None and entry.get("status") == "FILLED"
            and entry.get("orderType") == "LIMIT"
            and _stock_leg(entry)[0].get("instruction") == "SELL_SHORT"
            and any(row.get("evento") == "ENTRY_FILLED" and row.get("order_id") == str(known_entry)
                    and row.get("ticker") == "TSLA" for row in audit),
            "GALA_SHORT_ENTRY_UNVERIFIED")
    with (root / "data/historial_operaciones.csv").open(encoding="utf-8-sig", newline="") as handle:
        closes = [row for row in csv.DictReader(handle)
                  if mode_of(row) == "REAL" and session_date(row) == SOURCE_DAY
                  and row.get("ticker") == "TSLA" and row.get("evento") == "POSITION_CLOSE"
                  and row.get("position_id") in {key for key, _ in runtime}]
    require(len(closes) == 2 and {row.get("position_side") for row in closes} == {"SHORT", "LONG"}
            and all(not row.get("order_id") and not row.get("pnl_usd") for row in closes),
            "LOCAL_MANUAL_CLOSE_SCOPE_INVALID")

    transactions = txn_packet.get("transactions")
    require(isinstance(transactions, list) and len(transactions) == 18,
            "BROKER_TRANSACTION_COUNT_INVALID")
    net = defaultdict(lambda: Decimal("0"))
    shares = defaultdict(lambda: Decimal("0"))
    transaction_orders = Counter()
    for row in transactions:
        require(isinstance(row, dict) and str(row.get("accountNumber")) == str(account),
                "BROKER_TRANSACTION_ACCOUNT_MISMATCH")
        oid = str(row.get("orderId") or "")
        require(oid in order_by_id, "BROKER_TRANSACTION_ORDER_UNLINKED")
        stamp = datetime.fromisoformat(str(row.get("time") or "").replace("Z", "+00:00"))
        require(stamp.tzinfo is not None and stamp.astimezone(NY).date().isoformat() == SOURCE_DAY,
                "BROKER_TRANSACTION_DAY_INVALID")
        equity = [item for item in row.get("transferItems") or []
                  if (item.get("instrument") or {}).get("assetType") == "EQUITY"]
        symbols = {(item.get("instrument") or {}).get("symbol") for item in equity}
        require(len(symbols) == 1 and next(iter(symbols)) in {"BA", "NKE", "TSLA"},
                "BROKER_TRANSACTION_SYMBOL_INVALID")
        symbol = next(iter(symbols))
        try:
            net[symbol] += Decimal(str(row.get("netAmount")))
            shares[symbol] += sum((Decimal(str(item.get("amount"))) for item in equity), Decimal("0"))
        except (InvalidOperation, TypeError):
            raise ValueError("BROKER_TRANSACTION_AMOUNT_INVALID")
        transaction_orders[oid] += 1
    require(all(shares[symbol] == 0 for symbol in ("BA", "NKE", "TSLA"))
            and sum(transaction_orders[oid] for oid in manual_ids) == 7
            and all(transaction_orders[oid] == 1 for oid in manual_ids)
            and net["TSLA"] == Decimal("-6.55"),
            "BROKER_TRANSACTION_RECONCILIATION_INVALID")
    summary = {
        "source_session_ny": SOURCE_DAY,
        "manual_orders_confirmed_by_client": 7,
        "manual_order_refs": [reference(item[3]) for item in manual],
        "broker_transaction_count": len(transactions),
        "net_cash_flow_by_symbol": {symbol: str(net[symbol]) for symbol in ("BA", "NKE", "TSLA")},
        "all_equity_share_quantities_flat": True,
        "guard_sha256": guard_digest,
        "guard_keys": keys,
        "original_archive_path": hold["archive_path"],
        "order_packet_sha256": fingerprint(order_raw),
        "transaction_packet_sha256": fingerprint(txn_raw),
        "gala_history_rewritten": False,
        "manual_activity_training_eligible": False,
        "historical_pnl_verified": False,
    }
    return summary


def _write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def authorize(root, config, account, reviewer, ack, order_path, transaction_path,
              *, renew_expired=False, backup_root=Path("/opt/gala/backups"),
              check_service=service_inactive, client=None, now=None):
    require(hasattr(os, "geteuid") and os.geteuid() == 0, "ROOT_REQUIRED")
    require(ack == ACK and bool(str(reviewer or "").strip()), "EXPLICIT_REVIEW_REQUIRED")
    require((config.get("live_trading") or {}).get("reviewed_manual_tsla_resume") is True,
            "CONFIG_OPT_IN_REQUIRED")
    now = now or datetime.now(NY)
    require(now.tzinfo is not None and now.astimezone(NY).date().isoformat() > SOURCE_DAY,
            "PAST_SESSION_REQUIRED")
    today = now.astimezone(NY).date().isoformat()
    root = Path(root)
    check_service()
    config_raw = (root / "config.yaml").read_bytes()
    import yaml
    require((yaml.safe_load(config_raw) or {}) == config, "CONFIG_CHANGED_BEFORE_PREFLIGHT")
    guard_path = root / "data/real_accounting_reconciliation.json"
    guard_raw = guard_path.read_bytes()
    proof = proof_from_packets(root, account, order_path, transaction_path)
    archive_path = Path(proof["original_archive_path"])
    archive_digest = inspect_original_archive(archive_path)
    archived_raw = archive_path.read_bytes()
    require(fingerprint(archived_raw) == archive_digest, "ORIGINAL_ARCHIVE_CHANGED")
    snapshots = safe_inputs(config, root, today)
    target = approval_path(config, root)
    require(target.parent.is_dir() and not target.parent.is_symlink() and not target.is_symlink(),
            "APPROVAL_PATH_INVALID")
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
        account_hash = client.obtener_account_hash(account)
    require(bool(account_hash), "EXACT_ACCOUNT_HASH_REQUIRED")
    _, order_packet = private_json(order_path)
    require(order_packet.get("account_fingerprint") == fingerprint(str(account_hash)),
            "BROKER_ACCOUNT_FINGERPRINT_MISMATCH")
    check_service()
    require((root / "config.yaml").read_bytes() == config_raw
            and guard_path.read_bytes() == guard_raw
            and archive_path.read_bytes() == archived_raw
            and all(path.read_bytes() == raw for path, raw in snapshots.items())
            and proof_from_packets(root, account, order_path, transaction_path) == proof,
            "EVIDENCE_CHANGED_DURING_PREFLIGHT")
    backup_root = Path(backup_root)
    require(backup_root.is_dir() and not backup_root.is_symlink(), "BACKUP_ROOT_INVALID")
    folder = Path(tempfile.mkdtemp(prefix="reviewed-manual-tsla-session-", dir=backup_root))
    os.chmod(folder, 0o700)
    proof_raw = (json.dumps(proof, indent=2, ensure_ascii=False) + "\n").encode()
    grant = {
        "kind": APPROVAL_KIND, "session_ny": today, "source_session_ny": SOURCE_DAY,
        "guard_sha256": proof["guard_sha256"], "keys": proof["guard_keys"],
        "archive_path": proof["original_archive_path"],
        "original_archive_sha256": archive_digest,
        "account_fingerprint": fingerprint(account),
        "broker_proof_sha256": fingerprint(proof_raw),
        "broker_flat_verified": True, "broker_preflight": broker,
        "manual_order_count": 7, "operator_ack": ACK,
        "reviewer": str(reviewer).strip(),
        "approved_at_utc": now.astimezone(timezone.utc).isoformat(),
        "manual_activity_training_eligible": False,
        "historical_pnl_verified": False, "accounting_records_modified": False,
        "review_backup": str(folder),
    }
    grant_raw = (json.dumps(grant, indent=2) + "\n").encode()
    _write_private(folder / "guard_before.json", guard_raw)
    _write_private(folder / "broker_proof.json", proof_raw)
    _write_private(folder / "authorization.json", grant_raw)
    _write_private(folder / "config_before.yaml", config_raw)
    if prior is not None:
        _write_private(folder / "prior_authorization.json", prior)
    import grp
    fd, temporary = tempfile.mkstemp(prefix=".manual-tsla-session-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(grant_raw)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchown(handle.fileno(), 0, grp.getgrnam("gala").gr_gid)
            os.fchmod(handle.fileno(), 0o640)
        check_service()
        require(guard_path.read_bytes() == guard_raw
                and (root / "config.yaml").read_bytes() == config_raw
                and proof_from_packets(root, account, order_path, transaction_path) == proof,
                "EVIDENCE_CHANGED_BEFORE_AUTHORIZATION")
        if prior is None:
            os.link(temporary, target)
        else:
            require(_trusted_approval_file(target) and target.read_bytes() == prior,
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
    return {"status": "REVIEWED_MANUAL_TSLA_SESSION_AUTHORIZED",
            "session_ny": today, "manual_orders": 7,
            "authorization_path": str(target), "review_backup": str(folder),
            "accounting_modified": False, "engine_started": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--authorize", action="store_true")
    parser.add_argument("--ack")
    parser.add_argument("--reviewer")
    parser.add_argument("--renew-expired", action="store_true")
    parser.add_argument("--runtime-root", type=Path, default=ROOT)
    parser.add_argument("--order-evidence", type=Path,
                        default=DEFAULT_EVIDENCE / "broker_orders_private.json")
    parser.add_argument("--transaction-evidence", type=Path,
                        default=DEFAULT_EVIDENCE / "broker_transactions_private.json")
    parser.add_argument("--backup-root", type=Path, default=Path("/opt/gala/backups"))
    args = parser.parse_args(argv)
    root = args.runtime_root.resolve()
    from dotenv import load_dotenv
    import yaml
    load_dotenv(root / ".env", override=False)
    account = os.getenv("SCHWAB_ACCOUNT_NUMBER", "").strip()
    require(bool(account), "ACCOUNT_REQUIRED")
    if args.preview:
        proof = proof_from_packets(root, account, args.order_evidence,
                                   args.transaction_evidence)
        print(json.dumps({"status": "REVIEW_READY", "manual_orders": 7,
                          "broker_transaction_count": proof["broker_transaction_count"],
                          "net_cash_flow_by_symbol": proof["net_cash_flow_by_symbol"],
                          "accounting_modified": False, "engine_started": False}, indent=2))
    else:
        config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) or {}
        result = authorize(root, config, account, args.reviewer, args.ack,
                           args.order_evidence, args.transaction_evidence,
                           renew_expired=args.renew_expired, backup_root=args.backup_root)
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        code = str(exc)
        print("REFUSED: " + (code if re.fullmatch(r"[A-Z_]+", code)
                              else "PRIVATE_EVIDENCE_CHECK_FAILED"), file=sys.stderr)
        raise SystemExit(2)
    except Exception:
        print("REFUSED: manual scope, broker or stopped-service verification failed. No live changes.",
              file=sys.stderr)
        raise SystemExit(2)
