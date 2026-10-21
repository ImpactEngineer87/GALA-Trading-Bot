"""Audit saved SmartExit recover-flat evidence without contacting Schwab or modifying files.

Checks whether the *recorded* execution-leg snapshot looked complete at the
recovery timestamp; this alone cannot prove a fill. Only a fresh authoritative
broker order read in the guarded SmartExit recovery path can confirm it.
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

# Allow both `python scripts/audit_recovered_flat_evidence.py` and
# `python -m scripts.audit_recovered_flat_evidence`, as with other audits.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.execution_v2 import SmartExit
from core.project_paths import resolve_runtime_path


def _positive(value):
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def main():
    cfg = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    path = Path(resolve_runtime_path((cfg.get("execution_engine", {}) or {}).get(
        "history_path", "data/execution_v2.jsonl"
    )))
    print(f"SmartExit historical journal: {path}")
    print("Read-only offline summary; no broker lookup or saved-data changes.")
    if not path.exists():
        print("Journal missing")
        return
    stats = defaultdict(Counter)
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict) or event.get("event") != "EXIT_RECOVERED_FLAT":
                continue
            ticker = str(event.get("ticker", "") or "").upper()
            if not ticker:
                continue
            row = stats[ticker]
            row["recovered_flat"] += 1
            legs = event.get("legs", []) or []
            if not legs:
                row["no_exit_legs"] += 1
            if any(not leg.get("order_id") for leg in legs if isinstance(leg, dict)):
                row["missing_order_identity"] += 1
            if any(_positive(leg.get("filled")) and not _positive(leg.get("avg_price"))
                   for leg in legs if isinstance(leg, dict)):
                row["positive_fills_missing_price"] += 1
            if SmartExit._verified_fill_legs(event):
                row["complete_snapshot_unverified"] += 1
            else:
                row["incomplete_snapshot"] += 1

    print("Ticker   FLAT events  Complete archived snapshot  Incomplete archived snapshot")
    for ticker, counts in sorted(stats.items()):
        print(f"{ticker:6} {counts['recovered_flat']:>10}"
              f" {counts['complete_snapshot_unverified']:>26}"
              f" {counts['incomplete_snapshot']:>28}")
    print("Archived snapshots may be stale and do not authorize changing past")
    print("execution_valid flags or manufacturing fills/prices.")


if __name__ == "__main__":
    main()
