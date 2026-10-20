#!/usr/bin/env python3
"""Read-only audit of persisted signal learning, counters, and train settings.

Does NOT train, change a model, write CSV, or call Schwab. The audit deliberately
distinguishes all recorded signals, resolved signals, FIT candidates, and
execution-verified closes (the x/50 trigger).
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from core.project_paths import resolve_runtime_path

TICKERS = ("AAPL", "AMD", "AMZN", "BA", "META", "NVDA", "RCL", "UAL",
           "TSLA", "UNH", "PINS", "NCLH", "MSFT", "NKE", "PYPL", "NFLX", "ADBE")


def main():
    import yaml
    config = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    cfg = (config.get("agents") or {})
    training = cfg.get("entrenamiento", {}) or {}
    learning = cfg.get("signal_learning", {}) or {}
    auto = training.get("automatic_retraining", {}) or {}
    signal_path = Path(resolve_runtime_path(learning.get("path", "data/signal_learning_observations.csv")))
    feedback_path = Path(resolve_runtime_path(training.get("feedback_path", "data/agentes_feedback.csv")))
    state_path = Path(resolve_runtime_path(auto.get("state_path", "data/retraining_state.json")))
    print(f"Signal ledger: {signal_path} exists={signal_path.exists()}")
    print(f"Feedback ledger: {feedback_path} exists={feedback_path.exists()}")
    print(f"Retraining state: {state_path} exists={state_path.exists()}")
    print(f"Signal learning enabled={learning.get('enabled', True)}, use_for_training={learning.get('use_for_training', True)}")
    print(f"AI filter activo={training.get('activo', True)}, auto_retraining={auto.get('enabled')}, after_session={auto.get('only_after_session')}")
    print("All counts below are read-only, based on the current persisted CSV.")
    # Keep at most one small terminal status per observation ID. A signal may
    # have OPEN and RESOLVED events; counting raw CSV rows would double count.
    statuses = {}
    if signal_path.exists():
        with signal_path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                oid = str(row.get("observation_id") or "").strip()
                if not oid:
                    continue
                ticker = str(row.get("ticker") or "").strip().upper()
                if ticker not in TICKERS:
                    continue
                event = str(row.get("event") or "").strip().upper()
                if event == "RESOLVED" or oid not in statuses:
                    statuses[oid] = (
                        ticker, event, str(row.get("training_eligible") or ""),
                        str(row.get("source_kind") or ""),
                    )
    by_ticker = defaultdict(lambda: [0, 0, 0, 0])
    for ticker, event, eligible, _ in statuses.values():
        item = by_ticker[ticker]
        item[0] += 1
        if event == "RESOLVED":
            item[1] += 1
            if eligible == "1":
                item[2] += 1
        else:
            item[3] += 1
    print(f"{'Ticker':6} {'Signals':>8} {'Resolved':>9} {'FIT usable':>11} {'Open':>7}")
    for ticker in TICKERS:
        a,b,c,d = by_ticker[ticker]
        print(f"{ticker:6} {a:8} {b:9} {c:11} {d:7}")
    print("Note: FIT usable supplemental signals never count toward verified closed-trade x/50 or economic TEST.")


if __name__ == "__main__":
    main()
