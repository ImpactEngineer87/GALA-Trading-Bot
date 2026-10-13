# -*- coding: utf-8 -*-
"""Read-only audit of the per-ticker AI retraining counter.

Example (run in the VPS project root, with the bot files backed up):
    python AUDITAR_CONTADOR_IA.py --ticker NVDA

x/50 counts execution-valid CLOSED trades, not BUY/SELL chart signals,
partial exits, counterfactuals or candidate-training samples.
This utility NEVER alters feedback, the model or the retraining state.
"""
import argparse
import collections
import csv
import json
from pathlib import Path


def read_json(path):
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8-sig") as fh:
        payload = json.load(fh)
    return payload if isinstance(payload, dict) else {}


def report(ticker, feedback_path, state_path, limit=12):
    ticker = str(ticker).strip().upper()
    if not ticker:
        raise ValueError("Specify --ticker")
    if not feedback_path.is_file():
        raise FileNotFoundError("Feedback file not found: %s" % feedback_path)

    valid, rejected, legacy = [], [], []
    exclusions = collections.Counter()
    with feedback_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        has_integrity = bool(reader.fieldnames and "execution_valid" in reader.fieldnames)
        for row in reader:
            if str(row.get("ticker") or "").strip().upper() != ticker:
                continue
            status = str(row.get("execution_valid") or "").strip()
            if status == "1":
                valid.append(row)
            elif status in ("",) and not has_integrity:
                legacy.append(row)
            elif status == "":
                # Compatibility with the existing RetrainingLifecycle counter:
                # blank execution_valid is treated as a legacy-eligible row.
                legacy.append(row)
            else:
                rejected.append(row)
                for reason in (row.get("exclusion_reason") or "NO_REASON").split(";"):
                    if reason.strip():
                        exclusions[reason.strip()] += 1

    state = read_json(state_path)
    record = (state.get("tickers") or {}).get(ticker) or {}
    last_attempt = record.get("last_attempt_count")
    eligible = len(valid) + len(legacy)
    progress = max(0, eligible - int(last_attempt)) if last_attempt is not None else None
    recent = sorted(
        (valid + legacy + rejected),
        key=lambda row: str(row.get("timestamp") or ""),
    )
    if limit == 0:
        recent = []
    else:
        recent = recent[-limit:]
    result = {
        "ticker": ticker,
        "feedback_file": str(feedback_path),
        "state_file": str(state_path),
        "execution_valid_closed": len(valid),
        "legacy_or_blank_execution_valid": len(legacy),
        "excluded_closed": len(rejected),
        "eligible_total": eligible,
        "last_attempt_count": last_attempt,
        "new_observations_x50": progress,
        "reported_new_observations": record.get("new_observations"),
        "last_result": record.get("last_result"),
        "status": record.get("status"),
        "exclusion_reasons": dict(exclusions),
        "recent_feedback": [
            {
                "timestamp": r.get("timestamp"),
                "mode": r.get("modo"),
                "execution_valid": r.get("execution_valid"),
                "exclusion_reason": r.get("exclusion_reason"),
                "has_entry_order_id": bool(r.get("entry_order_id")),
                "has_exit_order_id": bool(r.get("exit_order_id")),
                "has_position_id": bool(r.get("position_id")),
            }
            for r in recent
        ],
    }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="NVDA")
    parser.add_argument("--feedback", type=Path, default=Path("data/agentes_feedback.csv"))
    parser.add_argument("--state", type=Path, default=Path("data/retraining_state.json"))
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()
    data = report(args.ticker, args.feedback, args.state, max(0, args.limit))
    print(json.dumps(data, indent=2, ensure_ascii=False))
    if data["legacy_or_blank_execution_valid"]:
        print("[WARN] Legacy/blank execution_valid rows require manual audit.")
    if data["excluded_closed"]:
        print("[NOTE] Excluded trade rows cannot be made training-eligible "
              "without confirming missing broker and ledger evidence.")
    print("[NOTE] BUY/SELL signal markers alone never increment x/50.")


if __name__ == "__main__":
    main()
