#!/usr/bin/env python3
"""Inspect a saved private broker packet and exact local audit links offline.

No broker client, network calls, ledger constructor/migration, writes, repairs,
training, order or service operations. Mask broker/position identifiers and
redact private identifiers in rejection descriptions. A selected audit record
or a broker replacement edge does not automatically establish trade ownership.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import deque
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TARGETS = {"BA", "NKE", "META"}


def ref(value):
    value = str(value or "").strip()
    return hashlib.sha256(value.encode()).hexdigest()[:12] if value else None


def timestamp(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(ZoneInfo("America/New_York")).isoformat() if stamp.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def walk(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            label = (path + "." if path else "") + str(key)
            yield str(key), item, label
            yield from walk(item, label)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from walk(item, path + "[" + str(i) + "]")


def order_refs(value):
    return sorted({ref(item) for key, item, _ in walk(value)
                   if key.lower() in {"orderid", "order_id"}
                   and isinstance(item, (str, int)) and not isinstance(item, bool) and str(item).strip()})


def safe_description(value, secrets):
    text = str(value or "")
    if "token" in text.lower() or "bearer" in text.lower():
        return "See private evidence file"
    text = re.sub(r"https?://\S+", "[URL]", text)
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= 4:
            text = text.replace(secret, "[ID]")
    text = re.sub(r"\b[0-9A-Fa-f]{16,}\b|\b\d{4,}\b", "[ID]", text)
    return " ".join(text.split())[:260]


def inspect(packet, root, day):
    import yaml
    from core.dashboard_evidence import mode_of, session_date, number
    from core.project_paths import resolve_runtime_path
    from scripts.audit_schwab_execution_prices import fill_evidence, order_index

    if packet.get("session_ny") != day or not isinstance(packet.get("orders"), list):
        raise ValueError("Evidence session or orders invalid")
    state = packet.get("accounting_state_at_read") or {}
    unresolved = state.get("unresolved")
    if not isinstance(unresolved, dict) or any(not isinstance(v, dict) for v in unresolved.values()):
        raise ValueError("Saved accounting snapshot invalid")
    runtime_keys = {key for key, row in unresolved.items()
                    if not str(key).startswith("history:") and row.get("ticker") in TARGETS}
    history_keys = {key for key, row in unresolved.items()
                    if str(key).startswith("history:") and row.get("ticker") in TARGETS}
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) or {}
    log = cfg.get("logging") or {}
    def path(value):
        return Path(resolve_runtime_path(value, project_root=root))
    index = order_index(packet["orders"])
    secrets = {str(item) for key, item, _ in walk(packet["orders"])
               if key.lower() in {"orderid", "accountnumber", "accounthash"}
               and isinstance(item, (str, int)) and not isinstance(item, bool)}
    secrets.update(str(key) for key in unresolved)
    known_orders = {str(row.get(field)) for row in unresolved.values()
                    for field in ("entry_order_id", "exit_order_id") if row.get(field)}
    broker = []
    selected_ids = set()
    for oid, order in index.items():
        if not isinstance(order, dict):
            continue
        legs = order.get("orderLegCollection")
        if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], dict):
            continue
        leg = legs[0]
        instrument = leg.get("instrument") or {}
        ticker = instrument.get("symbol") if isinstance(instrument, dict) else None
        if ticker not in TARGETS:
            continue
        entered = timestamp(order.get("enteredTime"))
        activity_times = {timestamp(item) for key, item, _ in walk(order.get("orderActivityCollection") or [])
                          if key == "time"}
        if oid not in known_orders and not any(stamp and stamp[:10] == day for stamp in activity_times | {entered}):
            continue
        selected_ids.add(oid)
        relationships = {}
        for key, value in order.items():
            if any(term in key.lower() for term in ("replac", "parent", "child")):
                refs = order_refs(value)
                if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value) in index:
                    refs = sorted(set(refs + [ref(value)]))
                if isinstance(value, list):
                    refs = sorted(set(refs + [ref(v) for v in value
                                             if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v) in index]))
                relationships[key] = {"value_type": type(value).__name__, "order_refs": refs}
        evidence = fill_evidence(order, oid, ticker, leg.get("instruction"))
        broker.append({"broker_order_ref": ref(oid), "ticker": ticker,
                       "entered_ny": entered, "status": evidence.get("status"),
                       "instruction": evidence.get("instruction"), "order_type": evidence.get("order_type"),
                       "broker_reported_filled_quantity": number(order.get("filledQuantity")),
                       "broker_fill_verified": evidence["verified"],
                       "whole_order_average_fill": evidence.get("average_price"),
                       "native_relationship_fields": relationships,
                       "order_fields_present": sorted(order),
                       "rejection_description_redacted": safe_description(order.get("statusDescription"), secrets)})
    history_path = path(log.get("historial_operaciones", "data/historial_operaciones.csv"))
    history = []
    with history_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if mode_of(row) != "REAL" or session_date(row) != day:
                continue
            history_key = "history:" + day + ":" + str(row.get("history_id") or row.get("position_id") or "")
            if row.get("position_id") not in runtime_keys and history_key not in history_keys:
                continue
            history.append({"ticker": row.get("ticker"), "position_ref": ref(row.get("position_id")),
                            "execution_ref": ref(row.get("execution_id")),
                            "entry_order_ref": ref(row.get("parent_order_id")), "exit_order_ref": ref(row.get("order_id")),
                            "event": row.get("evento"), "time_ny": row.get("hora_ny"),
                            "side": row.get("position_side"), "quantity": number(row.get("cantidad")),
                            "local_recorded_entry": number(row.get("entrada")),
                            "local_recorded_exit": number(row.get("salida")), "local_recorded_pnl": number(row.get("pnl_usd")),
                            "scope": "Local record only; values are not fresh fill confirmation"})
    audit_path = path(log.get("auditoria_ordenes", "data/auditoria_ordenes.csv"))
    events = deque(maxlen=100)
    count = 0
    flag_keys = {"broker_price_verified", "broker_confirmed", "fill_price_verified", "entry_confirmed", "tp1_ejecutado"}
    with audit_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if mode_of(row) != "REAL" or session_date(row) != day or row.get("ticker") not in TARGETS:
                continue
            by_position = row.get("position_id") in runtime_keys
            by_order = row.get("order_id") in selected_ids or row.get("parent_order_id") in selected_ids
            if not by_position and not by_order:
                continue
            try:
                details = json.loads(row.get("detalle") or "{}")
            except (ValueError, TypeError):
                details = {}
            detail_refs = [{"field": label, "ref": ref(value)} for key, value, label in walk(details)
                           if isinstance(value, (str, int)) and not isinstance(value, bool)
                           and (("order" in key.lower() and key.lower().endswith("id"))
                                or key.lower() in {"sl_id", "tp1_id"})]
            flags = {label: value for key, value, label in walk(details)
                     if key in flag_keys and isinstance(value, bool)}
            events.append({"ticker": row.get("ticker"), "time_ny": row.get("hora_ny"),
                           "event": row.get("evento"), "state": row.get("estado"),
                           "selected_by": "exact_position_id" if by_position else "exact_broker_order_id_only",
                           "position_ref": ref(row.get("position_id")), "execution_ref": ref(row.get("execution_id")),
                           "order_ref": ref(row.get("order_id")), "parent_ref": ref(row.get("parent_order_id")),
                           "details_order_refs": detail_refs, "recorded_flags": flags})
            count += 1
    return {"offline_read_only": True, "broker_calls": 0, "accounting_history_modified": False,
            "session_ny": day, "evidence_as_of_utc": packet.get("as_of_utc"),
            "scope": "Native relationship fields and exact local ID selections only; no automatic trade attribution or repair",
            "broker_order_relationships": broker, "local_history_records": history,
            "local_audit_events": list(events), "omitted_audit_events": max(0, count - len(events))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)
    root = args.runtime_root.resolve()
    sys.path.insert(0, str(root))
    packet = json.loads(args.evidence.read_text(encoding="utf-8"))
    print(json.dumps(inspect(packet, root, args.date.isoformat()), indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Offline evidence inspection unavailable; no files or orders changed. Check the evidence path, session and local ledger access.", file=sys.stderr)
        raise SystemExit(2)
