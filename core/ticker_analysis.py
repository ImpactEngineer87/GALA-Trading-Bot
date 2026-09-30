"""Automatic per-ticker evidence analysis for GALA10 v2.9.9.

This module ranks tickers using *actual closed-trade outcomes*, not the AI model's
historical balanced-accuracy score.  The ranking deliberately penalizes small
samples with a Wilson lower confidence bound so a 2/2 ticker cannot outrank a
stable 30/40 ticker merely because its raw win rate is 100%.

The helper is pure/read-only.  It never changes PAPER/REAL routing, position
size, model artifacts, retraining state, or broker orders.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from math import sqrt
from typing import Iterable, Mapping, Sequence


def _ticker(value) -> str:
    return str(value or "").strip().upper()


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


def _row_date(row: Mapping):
    raw = str(
        row.get("fecha")
        or row.get("date")
        or row.get("timestamp_utc")
        or row.get("timestamp_iso")
        or ""
    ).strip()
    if len(raw) < 10:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _wilson_lower_bound(wins: int, trials: int, z: float = 1.96) -> float:
    """95% Wilson lower bound for a Bernoulli success rate."""
    wins = max(0, int(wins))
    trials = max(0, int(trials))
    if trials <= 0:
        return 0.0
    p = wins / trials
    z2 = z * z
    denom = 1.0 + z2 / trials
    center = p + z2 / (2.0 * trials)
    margin = z * sqrt((p * (1.0 - p) + z2 / (4.0 * trials)) / trials)
    return max(0.0, min(1.0, (center - margin) / denom))


def partial_share_plan(shares: int, pct: float = 0.70, enabled: bool = True) -> dict:
    """Mirror the engine's integer 70/30 split without mutating runtime state.

    GALA10 intentionally requires at least three shares for a partial exit.  With
    three shares, 70/30 becomes 2 + 1 (about 67/33) because equity shares are
    integer units in this strategy.
    """
    qty = max(1, _integer(shares, 1))
    pct = max(0.0, min(1.0, _number(pct, 0.70)))
    if not enabled or qty < 3:
        return {
            "enabled": False,
            "shares": qty,
            "partial_shares": 0,
            "runner_shares": qty,
            "configured_pct": round(pct, 4),
            "actual_partial_pct": 0.0,
            "actual_runner_pct": 1.0,
        }
    partial = int(round(qty * pct))
    partial = max(1, min(partial, qty - 1))
    runner = qty - partial
    return {
        "enabled": True,
        "shares": qty,
        "partial_shares": partial,
        "runner_shares": runner,
        "configured_pct": round(pct, 4),
        "actual_partial_pct": round(partial / qty, 4),
        "actual_runner_pct": round(runner / qty, 4),
    }


def build_ticker_accuracy_analysis(
    tickers: Iterable[str],
    *,
    history_rows: Sequence[Mapping] = (),
    execution_rows: Sequence[Mapping] = (),
    ai_rows: Sequence[Mapping] = (),
    veto_rows: Sequence[Mapping] = (),
    as_of_date: str = "",
    lookback_days: int = 30,
    min_closed_trades: int = 10,
):
    """Rank tickers from recent *executed and closed* trade evidence.

    Ranking order for statistically eligible tickers:
      1. Wilson lower bound of observed win rate (sample-size aware)
      2. Net return on gross entry notional (share-size normalized)
      3. Profit factor
      4. Net PnL

    Model BA and veto outcomes are returned as context only and never determine
    the ranking.  This avoids confusing historical model-validation accuracy with
    real/paper execution performance.
    """
    try:
        end_date = datetime.strptime(str(as_of_date)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        end_date = date.today()
    lookback_days = max(1, int(lookback_days or 30))
    min_closed_trades = max(1, int(min_closed_trades or 10))
    start_date = end_date - timedelta(days=lookback_days - 1)

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
        shares_by_ticker[t] = max(1, _integer(row.get("shares"), 1))

    ai_by_ticker = {_ticker(row.get("ticker")): row for row in (ai_rows or []) if _ticker(row.get("ticker"))}
    veto_by_ticker = {_ticker(row.get("ticker")): row for row in (veto_rows or []) if _ticker(row.get("ticker"))}

    stats = defaultdict(lambda: {
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "neutral": 0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "net_pnl": 0.0,
        "entry_notional": 0.0,
        "shares_closed": 0,
        "paper_closed": 0,
        "real_closed": 0,
        "paper_pnl": 0.0,
        "real_pnl": 0.0,
    })

    for row in history_rows or []:
        if str(row.get("evento") or "POSITION_CLOSE").upper() != "POSITION_CLOSE":
            continue
        d = _row_date(row)
        if d is None or d < start_date or d > end_date:
            continue
        t = _ticker(row.get("ticker"))
        if not t:
            continue
        pnl = _number(row.get("pnl_usd"), 0.0)
        qty = max(0, _integer(row.get("cantidad"), 0))
        entry = abs(_number(row.get("entrada"), 0.0))
        mode = str(row.get("modo") or "PAPER").upper()
        s = stats[t]
        s["closed_trades"] += 1
        s["net_pnl"] += pnl
        s["shares_closed"] += qty
        s["entry_notional"] += entry * qty
        if pnl > 0:
            s["wins"] += 1
            s["gross_profit"] += pnl
        elif pnl < 0:
            s["losses"] += 1
            s["gross_loss"] += abs(pnl)
        else:
            s["neutral"] += 1
        if mode == "REAL":
            s["real_closed"] += 1
            s["real_pnl"] += pnl
        else:
            s["paper_closed"] += 1
            s["paper_pnl"] += pnl

    discovered = set(stats) | set(mode_by_ticker) | set(ai_by_ticker) | set(veto_by_ticker)
    for t in sorted(discovered):
        if t not in seen:
            seen.add(t)
            ordered.append(t)

    rows = []
    for t in ordered:
        s = stats[t]
        decisive = s["wins"] + s["losses"]
        win_rate = (s["wins"] / decisive) if decisive else None
        wilson = _wilson_lower_bound(s["wins"], decisive) if decisive else 0.0
        profit_factor = (
            s["gross_profit"] / s["gross_loss"]
            if s["gross_loss"] > 0
            else (999.0 if s["gross_profit"] > 0 else None)
        )
        avg_pnl = s["net_pnl"] / s["closed_trades"] if s["closed_trades"] else 0.0
        pnl_per_share = s["net_pnl"] / s["shares_closed"] if s["shares_closed"] else 0.0
        net_return_pct = (
            (s["net_pnl"] / s["entry_notional"]) * 100.0
            if s["entry_notional"] > 0 else 0.0
        )
        eligible = s["closed_trades"] >= min_closed_trades and decisive >= min_closed_trades
        ai = ai_by_ticker.get(t, {}) or {}
        veto = veto_by_ticker.get(t, {}) or {}
        row = {
            "ticker": t,
            "mode": mode_by_ticker.get(t, "PAPER"),
            "configured_shares": shares_by_ticker.get(t, 1),
            "closed_trades": int(s["closed_trades"]),
            "wins": int(s["wins"]),
            "losses": int(s["losses"]),
            "neutral": int(s["neutral"]),
            "decisive_trades": int(decisive),
            "win_rate_pct": round(win_rate * 100.0, 2) if win_rate is not None else None,
            "win_rate_wilson_lb_pct": round(wilson * 100.0, 2),
            "gross_profit": round(s["gross_profit"], 2),
            "gross_loss": round(s["gross_loss"], 2),
            "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
            "net_pnl": round(s["net_pnl"], 2),
            "avg_pnl": round(avg_pnl, 2),
            "pnl_per_share": round(pnl_per_share, 4),
            "net_return_pct": round(net_return_pct, 4),
            "shares_closed": int(s["shares_closed"]),
            "paper_closed": int(s["paper_closed"]),
            "real_closed": int(s["real_closed"]),
            "paper_pnl": round(s["paper_pnl"], 2),
            "real_pnl": round(s["real_pnl"], 2),
            "sample_eligible": bool(eligible),
            "sample_status": "ELEGIBLE" if eligible else "MUESTRA_BAJA",
            "min_closed_trades": min_closed_trades,
            "model_candidate_ba_pct": (
                round(_number(ai.get("last_balanced_accuracy")) * 100.0, 2)
                if ai.get("last_balanced_accuracy") is not None else None
            ),
            "model_incumbent_ba_pct": (
                round(_number(ai.get("last_incumbent_balanced_accuracy")) * 100.0, 2)
                if ai.get("last_incumbent_balanced_accuracy") is not None else None
            ),
            "production_model_loaded": bool(ai.get("production_model_loaded")),
            "veto_total": _integer(veto.get("veto_total"), 0),
            "veto_good": _integer(veto.get("veto_good"), 0),
            "veto_bad": _integer(veto.get("veto_bad"), 0),
            "rank": None,
            "review_candidate": False,
        }
        rows.append(row)

    eligible_rows = [r for r in rows if r["sample_eligible"]]
    eligible_rows.sort(
        key=lambda r: (
            r["win_rate_wilson_lb_pct"],
            r["net_return_pct"],
            (r["profit_factor"] if r["profit_factor"] is not None else 0.0),
            r["net_pnl"],
        ),
        reverse=True,
    )
    for rank, row in enumerate(eligible_rows, start=1):
        row["rank"] = rank
        row["review_candidate"] = bool(
            row["net_pnl"] > 0
            and row["win_rate_pct"] is not None
            and row["win_rate_pct"] >= 50.0
            and (row["profit_factor"] is None or row["profit_factor"] > 1.0)
        )

    # Return ranked eligible rows first, then low-sample rows by evidence volume.
    return sorted(
        rows,
        key=lambda r: (
            0 if r["rank"] is not None else 1,
            r["rank"] if r["rank"] is not None else -r["closed_trades"],
            r["ticker"],
        ),
    )
