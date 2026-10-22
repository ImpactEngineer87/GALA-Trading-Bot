#!/usr/bin/env python3
"""Read-only recent REAL close blockers and AI learning state.

No broker API requests, no orders/cancellations, no config edits, no training.
Do not print broker account numbers, credentials, raw order IDs, or URLs.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.project_paths import resolve_runtime_path

TICKERS = ("BA", "META", "NVDA", "TSLA", "NKE", "RCL")
AUDIT_EVENTS = {
    "POSITION_CLOSE_FAILED", "EXIT_RECOVERED_FLAT",
    "EXIT_PENDING_CONFIRMATION", "EXIT_RECONCILE_REQUIRED",
    "EXIT_RECOVERED_FLAT", "EXIT_FLAT_PRICE_UNVERIFIED",
    "EXIT_ORDER_ID_UNRESOLVED", "EXIT_REJECTED",
    "EXIT_FILLED", "STALE_EXIT_RETIRED_BROKER_FLAT",
    "STALE_EXIT_RETIRED_FOR_EMERGENCY",
    "STALE_EXIT_RETIRED_FOR_NORMAL_CLOSE",
}


def _path(value):
    return Path(resolve_runtime_path(value))


def _positive(value):
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _safe_reason(value):
    # Legacy broker exception descriptions occasionally include request URLs
    # and account IDs. Limit operator output to recognized static SmartExit
    # reasons, not arbitrary user/server errors or sensitive strings.
    reason = str(value or "").strip()
    safe_markers = (
        "position", "Position", "Schwab", "exit", "Exit", "WORKING",
        "working", "Broker", "broker", "cierre", "Cierre",
        "Prior", "prior", "Ticker", "ticker", "Ambiguous",
    )
    if any(reason.startswith(marker) for marker in safe_markers):
        if "http" in reason.lower() or "token" in reason.lower():
            return "(see private broker audit)"
        return re.sub(r"\\b\\d{4,}\\b", "[REDACTED]", " ".join(reason.split()))[:150]
    return "(see private broker audit)" if reason else "(no reason recorded)"


def inspect(config):
    log_path = _path((config.get("logging") or {}).get(
        "auditoria_ordenes", "data/auditoria_ordenes.csv"
    ))
    history_path = _path((config.get("execution_engine") or {}).get(
        "history_path", "data/execution_v2.jsonl"
    ))
    auto = (((config.get("agents") or {}).get("entrenamiento") or {})
            .get("automatic_retraining") or {})
    state_path = _path(auto.get("state_path", "data/retraining_state.json"))
    recent = {t: deque(maxlen=5) for t in TICKERS}
    recovery = {t: deque(maxlen=4) for t in TICKERS}

    if log_path.exists():
        with log_path.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                ticker = str(row.get("ticker", "")).strip().upper()
                event = str(row.get("evento", "")).strip().upper()
                if ticker not in recent or event not in AUDIT_EVENTS:
                    continue
                if str(row.get("modo", "")).upper() != "REAL":
                    continue
                recent[ticker].append({
                    "time": str(row.get("timestamp_utc", ""))[:24],
                    "event": event,
                    "state": str(row.get("estado", ""))[:35],
                    "reason": _safe_reason(row.get("motivo", "")),
                    "has_order_id": bool(str(row.get("order_id", "")).strip()),
                    "has_position_id": bool(str(row.get("position_id", "")).strip()),
                })

    if history_path.exists():
        with history_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(row, dict):
                    continue
                ticker = str(row.get("ticker", "")).strip().upper()
                event = str(row.get("event", "")).strip().upper()
                if ticker not in recovery or event not in {
                    "EXIT_RECOVERED_FLAT", "EXIT_COMPLETED",
                    "STALE_EXIT_RETIRED_FOR_EMERGENCY",
                    "STALE_EXIT_RETIRED_FOR_NORMAL_CLOSE",
                }:
                    continue
                legs = [leg for leg in row.get("legs", [])
                        if isinstance(leg, dict)] if isinstance(row.get("legs", []), list) else []
                recovery[ticker].append({
                    "time": str(row.get("created_utc", ""))[:24],
                    "event": event,
                    "qty": row.get("quantity", ""),
                    "legs": len(legs),
                    "filled_legs": sum(
                        1 for leg in legs if _positive(leg.get("filled"))
                    ),
                    "priced_legs": sum(
                        1 for leg in legs if _positive(leg.get("avg_price"))
                    ),
                    "position_id_present": bool(row.get("position_id")),
                })

    train_state = {}
    if state_path.exists():
        try:
            contents = json.loads(state_path.read_text(encoding="utf-8"))
            train_state = contents.get("tickers", contents)
            if not isinstance(train_state, dict):
                train_state = {}
        except (OSError, ValueError):
            pass

    print("READ-ONLY reconciliation and AI snapshot — NO broker requests/writes")
    print("Order audit present:", log_path.exists(),
          "| Exit journal present:", history_path.exists(),
          "| AI state present:", state_path.exists())
    for ticker in TICKERS:
        saved = train_state.get(ticker) or {}
        print("\n=== " + ticker + " ===")
        print("AI verified closes:", saved.get("total_observations", "?"),
              "| new x/50:", saved.get("new_observations", "?"),
              "| status:", saved.get("status", "?"))
        print("Last REAL audit events:")
        for event in recent[ticker]:
            print(" ", event)
        print("Last SmartExit events:")
        for event in recovery[ticker]:
            print(" ", event)
    print("\nNever change x/50 or execution_valid based on this log alone.")


if __name__ == "__main__":
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    inspect(cfg)
