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
}


def signal_date(row):
    date = str(row.get("date") or "").strip()[:10]
    if date:
        return date
    timestamp = str(row.get("timestamp_iso") or "").strip()
    return timestamp[:10]


def audit(signals_path, orders_path, date, tickers):
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
    linked = defaultdict(Counter)
    unlinked = Counter()
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
                else:
                    unlinked[(ticker, event)] += 1
    output = []
    for signal in signals:
        event_counts = linked.get((signal["ticker"], signal["signal_id"]), {})
        event_names = sorted(event_counts)
        output.append({
            **signal,
            "broker_events": event_names,
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
    tickers = [str(t).strip().upper() for t in args.tickers]
    result, unlinked = audit(signals, orders, date, tickers)
    print("READ ONLY: AI approval != order submitted != filled")
    print("NY date:", date, "| tickers:", ", ".join(tickers))
    live = cfg.get("live_trading") or {}
    print("REAL allow_short policy:", bool(live.get("allow_short", False)),
          "| per_ticker_execution:", bool(live.get("per_ticker_execution", False)))
    print("Signal archive:", signals.exists(), "| order audit:", orders.exists())
    for ticker in tickers:
        rows = [row for row in result if row["ticker"] == ticker]
        print(f"\n=== {ticker}: {len(rows)} archived signals ===")
        for item in rows[-16:]:
            events = ", ".join(item["broker_events"]) or "no linked broker audit"
            # Do not print IDs or full arbitrary broker messages.
            print(f"  {item['time_ny']} {item['signal']:5s} "
                  f"AI={item['decision'] or 'N/A':18s} mode={item['mode'] or 'N/A':5s} "
                  f"order={events}")
        unlinked_count = sum(v for (t, _), v in unlinked.items() if t == ticker)
        print("  Unlinked audit events:", unlinked_count,
              "(cannot automatically attribute to any one signal)")
    print("\nNo broker state inferred, no files/counters/orders changed.")


if __name__ == "__main__":
    main()
