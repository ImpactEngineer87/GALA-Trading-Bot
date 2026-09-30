"""Per-ticker dashboard performance aggregation for GALA10 v2.9.8.

The helper is intentionally pure so it can be tested without Flask or broker access.
It combines:
- today's persisted signals,
- today's verified position closes from trade history,
- the engine's active-position snapshot, and
- the persistent per-ticker PAPER/REAL registry.

It does not mutate trading state or training data.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping, Sequence


def _ticker(value) -> str:
    return str(value or "").strip().upper()


def _date_text(row: Mapping) -> str:
    value = str(row.get("date") or row.get("fecha") or "").strip()
    if value:
        return value[:10]
    value = str(row.get("timestamp_iso") or row.get("timestamp_utc") or "").strip()
    return value[:10] if len(value) >= 10 else ""


def _number(value, default=0.0) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return float(default)


def _integer(value, default=0) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return int(default)


def build_ticker_performance(
    tickers: Iterable[str],
    *,
    signal_rows: Sequence[Mapping] = (),
    history_rows: Sequence[Mapping] = (),
    positions: Sequence[Mapping] = (),
    execution_rows: Sequence[Mapping] = (),
    session_date: str = "",
):
    """Return deterministic per-ticker session metrics.

    ``session_date`` must be ``YYYY-MM-DD`` in New York time.  History PnL is
    counted only from POSITION_CLOSE rows, so historical ENTRY rows cannot inflate
    either trade counts or PnL.
    """
    session_date = str(session_date or "")[:10]
    ordered = []
    seen = set()
    for value in tickers or []:
        t = _ticker(value)
        if t and t not in seen:
            seen.add(t)
            ordered.append(t)

    mode_by_ticker = {}
    shares_by_ticker = {}
    for row in execution_rows or []:
        t = _ticker(row.get("ticker"))
        if not t:
            continue
        mode_by_ticker[t] = str(
            row.get("effective_mode") or row.get("mode") or "PAPER"
        ).upper()
        shares_by_ticker[t] = _integer(row.get("shares"), 1)

    stats = defaultdict(lambda: {
        "signals": 0,
        "buy_signals": 0,
        "sell_signals": 0,
        "vetoed_signals": 0,
        "approved_signals": 0,
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "open_positions": 0,
    })

    for row in signal_rows or []:
        if session_date and _date_text(row) != session_date:
            continue
        t = _ticker(row.get("ticker"))
        if not t:
            continue
        kind = str(row.get("tipo") or "").upper()
        decision = str(row.get("ai_decision") or "").upper()
        stats[t]["signals"] += 1
        if kind == "BUY":
            stats[t]["buy_signals"] += 1
        elif kind == "SELL":
            stats[t]["sell_signals"] += 1
        if "VETO" in decision:
            stats[t]["vetoed_signals"] += 1
        if decision == "APPROVED":
            stats[t]["approved_signals"] += 1

    for row in history_rows or []:
        if session_date and _date_text(row) != session_date:
            continue
        if str(row.get("evento") or "POSITION_CLOSE").upper() != "POSITION_CLOSE":
            continue
        t = _ticker(row.get("ticker"))
        if not t:
            continue
        pnl = _number(row.get("pnl_usd"))
        stats[t]["closed_trades"] += 1
        stats[t]["realized_pnl"] += pnl
        if pnl > 0:
            stats[t]["wins"] += 1
        elif pnl < 0:
            stats[t]["losses"] += 1

    for row in positions or []:
        t = _ticker(row.get("ticker"))
        if not t:
            continue
        status = str(row.get("status") or "OPEN").upper()
        if status not in ("OPEN", "PARTIAL", "PENDING"):
            continue
        stats[t]["open_positions"] += 1
        stats[t]["unrealized_pnl"] += _number(
            row.get("floating_pnl", row.get("unrealized_pnl", 0.0))
        )

    # Include tickers that appear in live state/history even if they are not in
    # the configured ordering. This avoids hiding an adopted/reconciled broker
    # position from the summary.
    discovered = set(stats) | set(mode_by_ticker)
    for t in sorted(discovered):
        if t not in seen:
            seen.add(t)
            ordered.append(t)

    result = []
    for t in ordered:
        row = stats[t]
        realized = round(row["realized_pnl"], 2)
        unrealized = round(row["unrealized_pnl"], 2)
        result.append({
            "ticker": t,
            "mode": mode_by_ticker.get(t, "PAPER"),
            "configured_shares": shares_by_ticker.get(t, 1),
            "signals": int(row["signals"]),
            "buy_signals": int(row["buy_signals"]),
            "sell_signals": int(row["sell_signals"]),
            "approved_signals": int(row["approved_signals"]),
            "vetoed_signals": int(row["vetoed_signals"]),
            "closed_trades": int(row["closed_trades"]),
            "wins": int(row["wins"]),
            "losses": int(row["losses"]),
            "open_positions": int(row["open_positions"]),
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_pnl": round(realized + unrealized, 2),
        })
    return result
