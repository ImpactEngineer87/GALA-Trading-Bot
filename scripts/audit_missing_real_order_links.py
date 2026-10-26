#!/usr/bin/env python3
"""Collect unlinked Schwab order evidence for BA, NKE and META without repair.

Order operations are GET-only. OAuth may refresh authentication tokens. Save
one private evidence packet; never edit accounting/history, train, promote,
stop/start the engine, submit or cancel an order. Public output uses hashed
references. An order's verified fills do not establish its local trade link.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

TICKERS = {"BA", "NKE", "META"}
NY = ZoneInfo("America/New_York")


def reference(value):
    value = str(value or "").strip()
    return hashlib.sha256(value.encode()).hexdigest()[:12] if value else None


def ny_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(NY).isoformat() if parsed.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def broker_rows(orders, day, fill_evidence, order_index):
    index = order_index(orders)
    parents = defaultdict(set)
    pending = [(order, "") for order in orders]
    while pending:
        order, parent = pending.pop()
        if not isinstance(order, dict):
            continue
        oid = str(order.get("orderId") or "")
        if oid and parent:
            parents[oid].add(reference(parent))
        for child in order.get("childOrderStrategies") or []:
            pending.append((child, oid))
    result = []
    unsupported = 0
    for oid, order in index.items():
        if not isinstance(order, dict):
            continue
        legs = order.get("orderLegCollection") or []
        target_legs = [leg for leg in legs if isinstance(leg, dict)
                       and isinstance(leg.get("instrument"), dict)
                       and leg["instrument"].get("symbol") in TICKERS]
        if not target_legs:
            continue
        if len(legs) != 1 or len(target_legs) != 1:
            unsupported += 1
            continue
        leg = target_legs[0]
        times = set()
        for activity in order.get("orderActivityCollection") or []:
            if not isinstance(activity, dict):
                continue
            for execution in activity.get("executionLegs") or []:
                if isinstance(execution, dict):
                    stamp = ny_time(execution.get("time"))
                    if stamp:
                        times.add(stamp)
        entered = ny_time(order.get("enteredTime"))
        if not any(stamp[:10] == day for stamp in times | ({entered} if entered else set())):
            continue
        ticker = leg["instrument"]["symbol"]
        instruction = leg.get("instruction")
        if instruction not in {"BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"}:
            unsupported += 1
            continue
        evidence = fill_evidence(order, oid, ticker, instruction)
        result.append({
            "broker_order_ref": reference(oid), "ticker": ticker,
            "broker_parent_refs": sorted(parents[oid]),
            "entered_ny": entered, "execution_times_ny": sorted(times),
            "instruction": instruction, "order_type": evidence.get("order_type"),
            "status": evidence.get("status"),
            "broker_fill_verified": evidence["verified"],
            "filled_quantity": evidence.get("quantity"),
            "whole_order_average_fill": evidence.get("average_price"),
            "fill_reason": evidence["reason"],
            "local_trade_attribution_verified": False,
        })
    result.sort(key=lambda row: (row["entered_ny"] or "", row["broker_order_ref"]))
    return result, sum(value is None for value in index.values()), unsupported


def local_links(state, history_path, day, mode_of, session_date):
    unresolved = state.get("unresolved")
    if not isinstance(unresolved, dict) or any(not isinstance(v, dict) for v in unresolved.values()):
        raise ValueError("Invalid accounting state")
    matched = defaultdict(list)
    if history_path.exists():
        with history_path.open(encoding="utf-8-sig", newline="") as handle:
            counter = 0
            for row in csv.DictReader(handle):
                if (mode_of(row) != "REAL" or session_date(row) != day
                        or (row.get("evento") or "POSITION_CLOSE") != "POSITION_CLOSE"):
                    continue
                key = "history:" + day + ":" + str(row.get("history_id") or row.get("position_id") or counter)
                counter += 1
                if key in unresolved:
                    matched[key].append(row)
    output = []
    for key, row in unresolved.items():
        if row.get("ticker") not in TICKERS:
            continue
        records = matched.get(key, [])
        history = records[0] if len(records) == 1 and records[0].get("ticker") == row.get("ticker") else {}
        output.append({
            "accounting_ref": reference(key), "ticker": row.get("ticker"),
            "source": "history_check" if str(key).startswith("history:") else "runtime_check",
            "recorded_ny": ny_time(row.get("recorded_utc")),
            "exact_history_key_matches": len(records),
            "history_position_ref": reference(history.get("position_id")),
            "recorded_entry_ref": reference(row.get("entry_order_id")),
            "recorded_exit_ref": reference(row.get("exit_order_id")),
            "history_entry_ref": reference(history.get("parent_order_id")),
            "history_exit_ref": reference(history.get("order_id")),
        })
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--evidence-root", type=Path, default=Path("/opt/gala/backups"))
    args = parser.parse_args(argv)
    runtime_root = args.runtime_root.resolve()
    sys.path.insert(0, str(runtime_root))
    import yaml
    from core.project_paths import resolve_runtime_path
    from scripts.audit_schwab_execution_prices import fill_evidence, order_index
    from core.dashboard_evidence import mode_of, session_date

    cfg = yaml.safe_load((runtime_root / "config.yaml").read_text(encoding="utf-8")) or {}
    def runtime_path(value):
        return Path(resolve_runtime_path(value, project_root=runtime_root))
    state_path = runtime_path((cfg.get("execution_engine") or {}).get(
        "accounting_state_path", "data/real_accounting_reconciliation.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    history_path = runtime_path((cfg.get("logging") or {}).get(
        "historial_operaciones", "data/historial_operaciones.csv"))
    links = local_links(state, history_path, args.date.isoformat(), mode_of, session_date)
    start = datetime.combine(args.date - timedelta(days=2), time.min, NY)
    end = datetime.combine(args.date + timedelta(days=1), time.min, NY)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    params = {"fromEnteredTime": start.astimezone(timezone.utc).strftime(fmt),
              "toEnteredTime": end.astimezone(timezone.utc).strftime(fmt), "maxResults": 500}
    # The normal client loads .env on import and may refresh OAuth. Suppress
    # its incidental output so private broker exception text cannot leak.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from api.schwab_client import SchwabClient, TRADER_URL
        account = os.getenv("SCHWAB_ACCOUNT_NUMBER", "").strip()
        if not account:
            raise ValueError("Exact configured Schwab account unavailable")
        client = SchwabClient(cfg)
        account_hash = client.obtener_account_hash(account)
        if not account_hash:
            raise ValueError("Exact configured Schwab account unavailable")
        response = client._get_autorizado(
            TRADER_URL + "/accounts/" + quote(str(account_hash), safe="") + "/orders",
            params=params, timeout=20)
    if response is None or response.status_code != 200:
        raise ValueError("Broker order GET unsuccessful; no fill inferred")
    orders = response.json()
    if not isinstance(orders, list) or any(not isinstance(order, dict) for order in orders):
        raise ValueError("Broker order list invalid; no fill inferred")
    rows, ambiguous, unsupported = broker_rows(orders, args.date.isoformat(), fill_evidence, order_index)
    packet = {"as_of_utc": datetime.now(timezone.utc).isoformat(),
              "session_ny": args.date.isoformat(), "order_query": params,
              "account_fingerprint": hashlib.sha256(str(account_hash).encode()).hexdigest(),
              "scope": "Broker evidence only; no trade attribution or accounting repair",
              "order_list_may_be_truncated": len(orders) >= 500,
              "accounting_state_at_read": state, "orders": orders}
    args.evidence_root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="gala-order-evidence-" + args.date.isoformat() + "-",
                                   dir=args.evidence_root))
    evidence_path = folder / "broker_orders_private.json"
    fd = os.open(evidence_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(packet, handle, ensure_ascii=False, allow_nan=False)
    print(json.dumps({
        "broker_order_operations": "GET_ONLY", "history_models_accounting_modified": False,
        "private_evidence_file": str(evidence_path), "session_ny": args.date.isoformat(),
        "order_query_from_ny": start.isoformat(), "order_query_to_ny": end.isoformat(),
        "top_level_orders_returned": len(orders), "order_list_may_be_truncated": len(orders) >= 500,
        "ambiguous_order_ids": ambiguous, "unsupported_target_orders": unsupported,
        "note": "Unlinked broker evidence. Missing IDs are not guessed. Query window may omit older-entered orders. Keep the private evidence file on the VPS.",
        "local_order_references": links, "broker_order_evidence": rows,
    }, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Broker exceptions can contain URLs, account identifiers and tokens.
        print("Evidence audit unavailable; no order/accounting repair performed. Check private configuration, file access and Schwab authorization.", file=sys.stderr)
        raise SystemExit(2)
