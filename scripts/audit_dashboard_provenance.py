#!/usr/bin/env python3
"""Report dashboard result sources; no broker calls or runtime-file writes."""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.dashboard_evidence import annotate_history, mode_of, session_date
from core.project_paths import resolve_runtime_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="", help="Optional New York session YYYY-MM-DD")
    args = parser.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    logging = cfg.get("logging", {}) or {}
    history = Path(resolve_runtime_path(logging.get("historial_operaciones", "data/historial_operaciones.csv")))
    audit = Path(resolve_runtime_path(logging.get("auditoria_ordenes", "data/auditoria_ordenes.csv")))
    if not history.exists():
        print("History unavailable; no results can be verified.")
        return 1
    with history.open(encoding="utf-8-sig", newline="") as handle:
        rows = [{**row, "evento": row.get("evento") or "POSITION_CLOSE"}
                for row in csv.DictReader(handle)]
    if audit.exists():
        with audit.open(encoding="utf-8-sig", newline="") as handle:
            rows = annotate_history(rows, csv.DictReader(handle))
    else:
        rows = annotate_history(rows)
    counts, reasons = Counter(), Counter()
    for row in rows:
        if row.get("evento") != "POSITION_CLOSE" or (args.date and session_date(row) != args.date):
            continue
        key = mode_of(row) + ("_verified" if row["performance_verified"] else "_unverified")
        counts[key] += 1
        if not row["performance_verified"]:
            reasons[row["performance_exclusion_reason"]] += 1
    print(json.dumps({
        "read_only": True, "session_ny": args.date or "ALL",
        "is_broker_reported": False, "counts": dict(counts),
        "exclusion_reasons": dict(reasons),
        "note": "PAPER is simulation; verified REAL is local gross fill calculation, not TOS account P/L.",
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
