#!/usr/bin/env python3
"""Compare REAL ledger prices with fresh Schwab GETs; never repair the ledgers.

Only exact, already-recorded order IDs are queried. Missing IDs stay unresolved.
No trading executor, order submission, cancellation, model or AI-state writes.
The existing OAuth client may refresh and persist its authentication tokens.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.dashboard_evidence import mode_of, number, session_date
from core.project_paths import resolve_runtime_path


def fill_evidence(order, order_id, ticker, instruction):
    """Verify one equity order's complete execution-price coverage, not its limit."""
    result = {"verified": False, "status": None, "quantity": None,
              "average_price": None, "reason": "Order data unavailable"}

    def reject(reason):
        return {**result, "reason": reason}

    if not isinstance(order, dict):
        return result
    result["status"] = str(order.get("status") or "UNKNOWN").upper()
    if not order_id or str(order.get("orderId") or "") != str(order_id):
        return reject("Order ID mismatch")
    if not instruction:
        return reject("Local position direction missing or contradictory")
    legs = order.get("orderLegCollection")
    if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], dict):
        return reject("Expected exactly one equity order leg")
    leg = legs[0]
    instrument = leg.get("instrument") or {}
    if (not isinstance(instrument, dict)
            or instrument.get("assetType") != "EQUITY"
            or str(instrument.get("symbol") or "").upper() != ticker.upper()
            or leg.get("instruction") != instruction):
        return reject("Symbol, asset type or instruction mismatch")
    ordered = number(order.get("quantity"))
    if ordered is None or ordered <= 0:
        return reject("Ordered quantity unavailable")
    reported = number(order.get("filledQuantity"))
    if order.get("filledQuantity") not in (None, "") and (reported is None or reported < 0):
        return reject("Invalid reported filled quantity")
    activities = order.get("orderActivityCollection") or []
    if not isinstance(activities, list):
        return reject("Invalid execution activities")
    quantities, values, execution_ids = [], [], set()
    for activity in activities:
        if not isinstance(activity, dict):
            return reject("Invalid execution activity")
        fills = activity.get("executionLegs") or []
        if not isinstance(fills, list):
            return reject("Invalid execution legs")
        if not fills:
            continue
        if (activity.get("activityType") not in (None, "EXECUTION")
                or activity.get("executionType") not in (None, "FILL")):
            return reject("Non-fill execution activity")
        execution_id = activity.get("executionId")
        if execution_id is not None:
            if str(execution_id) in execution_ids:
                return reject("Duplicate execution activity")
            execution_ids.add(str(execution_id))
        for fill in fills:
            if not isinstance(fill, dict):
                return reject("Invalid execution leg")
            qty, price = number(fill.get("quantity")), number(fill.get("price"))
            if qty is None or qty <= 0 or price is None or price <= 0:
                return reject("Missing or invalid execution quantity/price")
            if (leg.get("legId") is not None and fill.get("legId") is not None
                    and str(leg["legId"]) != str(fill["legId"])):
                return reject("Execution leg identity mismatch")
            quantities.append(qty)
            values.append(qty * price)
    quantity, value = math.fsum(quantities), math.fsum(values)
    if quantity <= 0:
        return reject("No execution-leg prices; order limit is not a fill price")
    if (quantity > ordered + 1e-6
            or (reported is not None and abs(quantity - reported) > 1e-6)
            or (result["status"] == "FILLED" and abs(quantity - ordered) > 1e-6)):
        return reject("Execution-price coverage does not match broker quantity")
    if result["status"] not in {"FILLED", "CANCELED", "CANCELLED", "EXPIRED"}:
        return reject("Order is not terminal; execution set may still change")
    average = value / quantity
    if not math.isfinite(average) or average <= 0:
        return reject("Invalid weighted execution price")
    return {**result, "verified": True, "quantity": quantity,
            "average_price": average, "reason": "Complete broker execution legs"}


def order_index(orders):
    """Index exact IDs, retaining ambiguity instead of selecting a duplicate."""
    index = {}
    pending = list(orders)
    while pending:
        order = pending.pop()
        if not isinstance(order, dict):
            continue
        oid = str(order.get("orderId") or "")
        if oid:
            if oid in index and index[oid] != order:
                index[oid] = None
            else:
                index[oid] = order
        children = order.get("childOrderStrategies") or []
        if isinstance(children, list):
            pending.extend(children)
    return index


def compare_row(row, lookup):
    """Keep local ledger values distinct from fresh broker-derived calculations."""
    ticker = str(row.get("ticker") or "").upper()
    side = str(row.get("position_side") or "").upper()
    legacy_side = {"BUY": "LONG", "SELL": "SHORT"}.get(str(row.get("lado") or "").upper())
    if side not in {"LONG", "SHORT"}:
        side = ""  # do not infer missing order identity/direction from a price
    if legacy_side and side != legacy_side:
        side = ""
    instructions = {"LONG": ("BUY", "SELL"), "SHORT": ("SELL_SHORT", "BUY_TO_COVER")}.get(side, ("", ""))
    evidence = []
    for key, action in zip(("parent_order_id", "order_id"), instructions):
        oid = str(row.get(key) or "").strip()
        if not oid:
            item = {"verified": False, "reason": "Missing local order ID; no inferred match"}
        else:
            order, error = lookup(oid)
            item = fill_evidence(order, oid, ticker, action)
            if error:
                item = {**item, "verified": False, "reason": error}
        evidence.append(item)
    entry, close = evidence
    qty = number(row.get("cantidad"))
    full = (qty is not None and qty > 0 and all(
        e.get("verified") and abs(e["quantity"] - qty) <= 1e-6 for e in evidence
    ))
    gross = None
    if full:
        gross = (close["average_price"] - entry["average_price"]) * qty * (1 if side == "LONG" else -1)
    local_pnl = number(row.get("pnl_usd"))
    return {
        "ticker": ticker, "time_ny": row.get("hora_ny"),
        "local_record": {k: number(row.get(k)) for k in ("cantidad", "entrada", "salida", "pnl_usd")},
        "broker_entry": entry, "broker_final_close": close,
        "full_quantity_pair_verified": bool(full),
        "local_gross_calculated_from_broker_fills": round(gross, 6) if gross is not None else None,
        "difference_from_recorded_gross": round(gross - local_pnl, 6) if gross is not None and local_pnl is not None else None,
        "note": "Partial/multiple exits require all exact exit IDs; no whole-trade P/L inferred" if not full else
                "Gross calculation from fills; not Schwab/TOS account P/L or net of all fees",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="New York session YYYY-MM-DD")
    args = parser.parse_args(argv)
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    history = Path(resolve_runtime_path((cfg.get("logging") or {}).get(
        "historial_operaciones", "data/historial_operaciones.csv")))
    with history.open(encoding="utf-8-sig", newline="") as handle:
        rows = [r for r in csv.DictReader(handle) if mode_of(r) == "REAL"
                and str(r.get("evento") or "POSITION_CLOSE").upper() == "POSITION_CLOSE"
                and session_date(r) == args.date.isoformat()]
    if not rows:
        print("No REAL closes for this date; no broker calls made.")
        return 0
    # Import the client only on the explicit command path, never during tests/import.
    from api.schwab_client import SchwabClient, TRADER_URL
    account = os.getenv("SCHWAB_ACCOUNT_NUMBER", "").strip()
    if not account:
        print("Schwab account not configured; no account is selected by default.")
        return 2
    client = SchwabClient(cfg)
    account_hash = client.obtener_account_hash(account)
    if not account_hash:
        print("Exact configured Schwab account unavailable; audit aborted.")
        return 2
    base = f"{TRADER_URL}/accounts/{quote(str(account_hash), safe='')}/orders"

    def get_json(url, params=None):
        try:
            response = client._get_autorizado(url, params=params, timeout=20)
            if response is None:
                return None, "Broker authorization unavailable"
            if response.status_code != 200:
                return None, f"Broker GET HTTP {response.status_code}"
            return response.json(), ""
        except Exception:
            # Never print exceptions/URLs containing account identifiers or tokens.
            return None, "Broker GET failed; no fill inferred"

    ny = ZoneInfo("America/New_York")
    start = datetime.combine(args.date - timedelta(days=2), time.min, ny)
    end = datetime.combine(args.date + timedelta(days=1), time.min, ny)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    orders, list_error = get_json(base, {
        "fromEnteredTime": start.astimezone(timezone.utc).strftime(fmt),
        "toEnteredTime": end.astimezone(timezone.utc).strftime(fmt), "maxResults": 500,
    })
    if list_error in {"Broker GET HTTP 401", "Broker GET HTTP 403", "Broker authorization unavailable"}:
        print(list_error + "; audit aborted without changing history.")
        return 2
    valid_list = isinstance(orders, list) and all(isinstance(o, dict) for o in orders)
    index = order_index(orders) if valid_list else {}
    cache = {}

    def lookup(oid):
        if oid not in cache:
            if oid in index:
                cache[oid] = (index[oid], "Ambiguous broker order ID" if index[oid] is None else "")
            else:
                cache[oid] = get_json(base + "/" + quote(oid, safe=""))
        return cache[oid]

    results = [compare_row(row, lookup) for row in rows]
    counts = Counter()
    for result in results:
        for key in ("broker_entry", "broker_final_close"):
            counts[key + ("_verified" if result[key]["verified"] else "_unresolved")] += 1
        counts["full_quantity_pairs_verified"] += int(result["full_quantity_pair_verified"])
    print(json.dumps({
        "broker_operations": "GET_ONLY", "history_models_training_modified": False,
        "as_of_utc": datetime.now(timezone.utc).isoformat(), "session_ny": args.date.isoformat(),
        "is_broker_reported_account_pnl": False,
        "order_list_available": valid_list, "order_list_error": list_error,
        "order_list_may_be_truncated": bool(valid_list and len(orders) >= 500),
        "counts": dict(counts), "trades": results,
    }, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
