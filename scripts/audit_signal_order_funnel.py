#!/usr/bin/env python3
"""Read-only GALA10 signal -> broker order-funnel audit.

Works from saved journal files, not a live Schwab API. AI approval is a
decision, not broker submission or execution. All order identities stay local;
stdout shows only aggregate event names/timestamps (no account/order IDs).
"""
from __future__ import annotations
import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.project_paths import resolve_runtime_path

EVENTS = {
    "ENTRY_SUBMITTED", "ENTRY_ACKNOWLEDGED", "ENTRY_RECONCILE_REQUIRED",
    "ENTRY_REJECTED", "ENTRY_FILLED", "ENTRY_BLOCKED",
    "ENTRY_TIMEOUT_CANCELED", "ENTRY_TERMINATED", "PENDING_ENTRY_CANCELED",
    "PENDING_ENTRY_CANCEL_FAILED", "REVERSAL_REQUESTED", "REVERSAL_ABORTED",
    "REVERSAL_ENTRY_FAILED", "REVERSAL_COMPLETED", "POSITION_CLOSE_FAILED",
    "EXIT_RECOVERED_FLAT", "EXIT_ORDER_ID_UNRESOLVED",
    "SIGNAL_EXECUTION_SKIPPED", "ENTRY_REQUESTED", "REVERSAL_CLOSE_ONLY",
}


def signal_date(row):
    date = str(row.get("date") or "").strip()[:10]
    if date:
        return date
    timestamp = str(row.get("timestamp_iso") or "").strip()
    return timestamp[:10]


def audit(signals_path, orders_path, date, tickers, learning_path=None):
    targets = set(tickers)
    # Each signal has a unique ID; no limit of 2000 UI signals is applied.
    signals = []
    if signals_path.exists():
        with signals_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(row, dict):
                    continue
                ticker = str(row.get("ticker") or "").upper()
                if ticker not in targets or signal_date(row) != date:
                    continue
                signals.append({
                    "ticker": ticker, "time_ny": str(row.get("timestamp") or "")[:8],
                    "signal_id": str(row.get("signal_id") or ""),
                    "signal": str(row.get("tipo") or "").upper(),
                    "decision": str(row.get("ai_decision") or "").upper(),
                    "mode": str(row.get("execution_mode") or "").upper(),
                })
    # Store only the user-selected signals' non-executed learning reasons.
    # These are execution-policy diagnoses, not broker-confirmed outcomes.
    learning_by_id = {}
    if learning_path is not None and learning_path.exists():
        signal_ids = {sig["signal_id"] for sig in signals if sig["signal_id"]}
        with learning_path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                signal_id = str(row.get("signal_id") or "").strip()
                if signal_id not in signal_ids:
                    continue
                if str(row.get("ticker") or "").upper() not in targets:
                    continue
                reason = str(row.get("reason") or "").strip()
                if reason:
                    # Arbitrary reason strings may contain sensitive backend
                    # details. Only structured status codes are exported.
                    reason = re.sub(r"[^A-Za-z0-9_:-]", "", reason)[:75]
                    learning_by_id[signal_id] = reason
    linked = defaultdict(Counter)
    linked_skip_reason = {}
    unlinked = Counter()
    # Old monitor-thread cancellation events do not always carry signal_id.
    # Link them only when an exact (ticker, date, Schwab order_id) belongs
    # uniquely to a previously linked ENTRY_SUBMITTED event. Never infer by
    # time proximity, price, or a shared ticker alone.
    entry_order_owners = {}
    ambiguous_orders = set()
    if orders_path.exists():
        with orders_path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                event = str(row.get("evento") or "").upper()
                ticker = str(row.get("ticker") or "").upper()
                if ticker not in targets or event not in EVENTS:
                    continue
                day = str(row.get("fecha") or "").strip()[:10]
                if day != date:
                    continue
                signal_id = str(row.get("signal_id") or "").strip()
                if signal_id:
                    linked[(ticker, signal_id)][event] += 1
                    if event == "ENTRY_SUBMITTED":
                        order_id = str(row.get("order_id") or "").strip()
                        if order_id:
                            key = (ticker, order_id)
                            previous = entry_order_owners.get(key)
                            if previous and previous != signal_id:
                                ambiguous_orders.add(key)
                            else:
                                entry_order_owners[key] = signal_id
                    if event == "SIGNAL_EXECUTION_SKIPPED":
                        reason = str(row.get("motivo") or "").strip()
                        linked_skip_reason[(ticker, signal_id)] = re.sub(
                            r"[^A-Za-z0-9_:-]", "", reason
                        )[:75]
                else:
                    unlinked[(ticker, event)] += 1
    if orders_path.exists() and entry_order_owners:
        with orders_path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if str(row.get("fecha") or "").strip()[:10] != date:
                    continue
                ticker = str(row.get("ticker") or "").upper()
                event = str(row.get("evento") or "").upper()
                if ticker not in targets or event not in {
                    "ENTRY_TIMEOUT_CANCELED", "ENTRY_TERMINATED",
                    "PENDING_ENTRY_CANCELED", "PENDING_ENTRY_CANCEL_FAILED",
                }:
                    continue
                if str(row.get("signal_id") or "").strip():
                    continue
                order_id = str(row.get("order_id") or "").strip()
                if not order_id or (ticker, order_id) in ambiguous_orders:
                    continue
                owner = entry_order_owners.get((ticker, order_id))
                if owner:
                    linked[(ticker, owner)][event] += 1
                    unlinked[(ticker, event)] -= 1
    output = []
    for signal in signals:
        event_counts = linked.get((signal["ticker"], signal["signal_id"]), {})
        event_names = sorted(event_counts)
        output.append({
            **signal,
            "broker_events": event_names,
            "execution_skip_reason": linked_skip_reason.get(
                (signal["ticker"], signal["signal_id"]), ""
            ),
            "learning_skip_reason": learning_by_id.get(signal["signal_id"], ""),
            "has_submission": any(
                e in event_counts for e in ("ENTRY_SUBMITTED", "ENTRY_ACKNOWLEDGED",
                                          "ENTRY_FILLED", "ENTRY_RECONCILE_REQUIRED")
            ),
            "has_fill": "ENTRY_FILLED" in event_counts,
            "has_block": any(e in event_counts for e in (
                "ENTRY_BLOCKED", "ENTRY_REJECTED", "REVERSAL_ABORTED",
                "POSITION_CLOSE_FAILED", "PENDING_ENTRY_CANCEL_FAILED"
            )),
        })
    return output, unlinked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="2026-10-22",
                        help="NY session date YYYY-MM-DD")
    parser.add_argument("--tickers", nargs="+",
                        default=["TSLA", "NKE", "BA", "META", "NVDA"])
    parser.add_argument("--runtime-root", type=Path, default=ROOT)
    args = parser.parse_args()
    date = datetime.strptime(args.date, "%Y-%m-%d").date().isoformat()
    runtime = args.runtime_root.resolve()
    cfg = yaml.safe_load((runtime / "config.yaml").read_text(encoding="utf-8")) or {}
    log_cfg = cfg.get("logging") or {}
    signals = Path(resolve_runtime_path(
        log_cfg.get("historial_senales", "data/historial_senales.jsonl"),
        project_root=runtime))
    orders = Path(resolve_runtime_path(
        log_cfg.get("auditoria_ordenes", "data/auditoria_ordenes.csv"),
        project_root=runtime))
    learning_cfg = ((cfg.get("agents") or {}).get("signal_learning") or {})
    learning = Path(resolve_runtime_path(
        learning_cfg.get("path", "data/signal_learning_observations.csv"),
        project_root=runtime))
    tickers = [str(t).strip().upper() for t in args.tickers]
    result, unlinked = audit(signals, orders, date, tickers, learning)
    print("READ ONLY: AI approval != order submitted != filled")
    print("NY date:", date, "| tickers:", ", ".join(tickers))
    live = cfg.get("live_trading") or {}
    print("REAL allow_short policy:", bool(live.get("allow_short", False)),
          "| per_ticker_execution:", bool(live.get("per_ticker_execution", False)))
    print("Signal archive:", signals.exists(), "| order audit:", orders.exists(),
          "| signal-learning ledger:", learning.exists())
    for ticker in tickers:
        rows = [row for row in result if row["ticker"] == ticker]
        print(f"\n=== {ticker}: {len(rows)} archived signals ===")
        for item in rows[-16:]:
            events = ", ".join(item["broker_events"]) or "no linked broker audit"
            # Do not print IDs or full arbitrary broker messages.
            reason = (
                item["execution_skip_reason"]
                or item["learning_skip_reason"]
            )
            print(f"  {item['time_ny']} {item['signal']:5s} "
                  f"AI={item['decision'] or 'N/A':18s} mode={item['mode'] or 'N/A':5s} "
                  f"order={events}"
                  + (f" | reason={reason}" if reason else ""))
        unlinked_count = sum(max(0, v) for (t, _), v in unlinked.items() if t == ticker)
        print("  Unlinked audit events:", unlinked_count,
              "(cannot automatically attribute to any one signal)")
    print("\nNo broker state inferred, no files/counters/orders changed.")


if __name__ == "__main__":
    main()
