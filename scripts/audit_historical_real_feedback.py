#!/usr/bin/env python3
"""Read-only audit of REAL feedback exclusions against recorded exit-fill events.

Does not access Schwab, mutate CSVs, alter training counters or models.
Local EXIT_FILLED entries are *candidates for broker verification*, not proof
that historical excluded observations should be promoted into training.
Uses bounded per-position order indexes; never loads 100MB feedback as a list.
"""
from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.project_paths import resolve_runtime_path

TICKERS = ("BA", "META", "NVDA", "TSLA", "NKE")


def _nonempty(value):
    return str(value or "").strip()


def _truth(value):
    return _nonempty(value).lower() in ("1", "true", "yes")


def _positive(value):
    try:
        return float(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def audit(feedback_path, order_audit_path, tickers=TICKERS):
    wanted = {t.upper() for t in tickers}
    confirmed = defaultdict(list)
    # The audit ledger can be sizeable. Retain only minimal metadata for
    # executed REAL exits belonging to our target tickers.
    if order_audit_path.exists():
        with order_audit_path.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if (_nonempty(row.get("ticker")).upper() not in wanted
                        or _nonempty(row.get("evento")).upper() != "EXIT_FILLED"
                        or _nonempty(row.get("modo")).upper() != "REAL"
                        or not _positive(row.get("precio"))
                        or not _positive(row.get("cantidad"))):
                    continue
                oid = _nonempty(row.get("order_id"))
                if not oid:
                    continue
                confirmed[oid].append((
                    _nonempty(row.get("position_id")),
                    _nonempty(row.get("execution_id")),
                    _nonempty(row.get("ticker")).upper(),
                    _nonempty(row.get("estado")).upper(),
                ))

    stats = {t: Counter() for t in tickers}
    with feedback_path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            t = _nonempty(row.get("ticker")).upper()
            if t not in stats or _nonempty(row.get("modo")).upper() != "REAL":
                continue
            s = stats[t]
            if _truth(row.get("execution_valid")):
                s["already_valid"] += 1
                continue
            s["excluded"] += 1
            reasons = [x for x in _nonempty(row.get("exclusion_reason")).split(";") if x]
            for reason in reasons:
                s["reason:" + reason] += 1
            oid = _nonempty(row.get("exit_order_id"))
            if not oid:
                s["missing_exit_id"] += 1
                continue
            s["exit_id_present"] += 1
            matches = confirmed.get(oid, ())
            if not matches:
                s["no_local_EXIT_FILLED_match"] += 1
                continue
            position = _nonempty(row.get("position_id"))
            execution = _nonempty(row.get("execution_id"))
            strong = [
                match for match in matches
                if match[2] == t and match[3] == "FILLED"
                and ((position and match[0] == position)
                     or (execution and match[1] == execution))
            ]
            if len(strong) == 1:
                s["local_fill_link_candidate"] += 1
            else:
                s["ambiguous_or_unlinked_local_fill"] += 1
    return stats


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    feedback = Path(resolve_runtime_path(
        ((cfg.get("agents") or {}).get("entrenamiento") or {})
        .get("feedback_path", "data/agentes_feedback.csv")
    ))
    audit_file = Path(resolve_runtime_path(
        (cfg.get("logging") or {}).get(
            "auditoria_ordenes", "data/auditoria_ordenes.csv"
        )
    ))
    print("READ-ONLY historic REAL evidence inspection — no Schwab calls or writes")
    print("Feedback exists:", feedback.exists())
    print("Order audit exists:", audit_file.exists())
    if not feedback.exists():
        print("Feedback file missing; cannot audit")
        return 1
    stats = audit(feedback, audit_file)
    print(f"{'Ticker':<7}{'Valid':>7}{'Excluded':>11}{'Exit ID':>10}"
          f"{'Local linked':>14}{'Needs broker verification':>27}")
    for ticker in TICKERS:
        s = stats[ticker]
        print(f"{ticker:<7}{s['already_valid']:>7}{s['excluded']:>11}"
              f"{s['exit_id_present']:>10}"
              f"{s['local_fill_link_candidate']:>14}"
              f"{s['excluded']:>27}")
        print("  Reasons:", {key[7:]: value for key, value in sorted(s.items())
                             if key.startswith("reason:")})
    print("A local match is NOT proof of a broker fill. No training counts were changed.")
    print("Never retroactively flip execution_valid without independently confirming")
    print("the exact Schwab fill quantity, side, price, order and position identity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
