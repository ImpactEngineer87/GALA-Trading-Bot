"""Mode-separated dashboard results. Local strategy P/L is never broker account P/L."""
from __future__ import annotations

from collections import defaultdict
from core.dashboard_evidence import mode_of, number, session_date as row_date


def _bucket():
    return dict(closed_trades=0, wins=0, losses=0, realized_pnl=0.0,
                unrealized_pnl=0.0, open_positions=0, excluded_closed_trades=0,
                unknown_open_pnl=0)


def build_ticker_performance(tickers, *, signal_rows=(), history_rows=(),
                             positions=(), execution_rows=(), session_date=""):
    modes, shares = {}, {}
    ordered = list(dict.fromkeys(str(t).strip().upper() for t in tickers if str(t).strip()))
    signals = defaultdict(lambda: dict(signals=0, buy_signals=0, sell_signals=0,
                                      approved_signals=0, vetoed_signals=0))
    stats = defaultdict(_bucket)
    for row in execution_rows:
        t = str(row.get("ticker") or "").strip().upper()
        if not t:
            continue
        modes[t] = str(row.get("effective_mode") or row.get("mode") or "UNKNOWN").upper()
        shares[t] = row.get("shares", 1)
    for row in signal_rows:
        if session_date and row_date(row) != session_date:
            continue
        t = str(row.get("ticker") or "").strip().upper()
        if not t:
            continue
        s = signals[t]
        s["signals"] += 1
        kind = str(row.get("tipo") or "").upper()
        if kind in {"BUY", "SELL"}:
            s[kind.lower() + "_signals"] += 1
        decision = str(row.get("ai_decision") or "").upper()
        s["approved_signals"] += int(decision == "APPROVED")
        s["vetoed_signals"] += int("VETO" in decision)
    for row in history_rows:
        if str(row.get("evento") or "").upper() != "POSITION_CLOSE":
            continue
        if session_date and row_date(row) != session_date:
            continue
        t, mode = str(row.get("ticker") or "").strip().upper(), mode_of(row)
        if not t:
            continue
        s = stats[t, mode]
        pnl = number(row.get("pnl_usd"))
        valid = row.get("performance_verified", mode == "PAPER") is True
        if not valid or pnl is None:
            s["excluded_closed_trades"] += 1
            continue
        s["closed_trades"] += 1
        s["realized_pnl"] += pnl
        s["wins"] += int(pnl > 0)
        s["losses"] += int(pnl < 0)
    for row in positions:
        # A submitted order is not a filled/open position.
        if str(row.get("status") or "").upper() not in {"OPEN", "PARTIAL"}:
            continue
        t, mode = str(row.get("ticker") or "").strip().upper(), mode_of(row)
        if not t:
            continue
        s = stats[t, mode]
        s["open_positions"] += 1
        pnl = number(row.get("floating_pnl"))
        # REAL headlines never combine confirmed closes with a candle estimate.
        # Schwab position-day P/L has its own explicitly scoped broker query.
        if mode == "REAL" or pnl is None or row.get("valuation_available") is False:
            s["unknown_open_pnl"] += 1
        else:
            s["unrealized_pnl"] += pnl
    discovered = set(signals) | set(modes) | {t for t, _ in stats}
    ordered += sorted(discovered - set(ordered))
    result = []
    for t in ordered:
        breakdown = {}
        for mode in ("PAPER", "REAL", "UNKNOWN"):
            s = dict(stats[t, mode])
            s["known_realized_pnl"] = round(s["realized_pnl"], 2)
            s["known_unrealized_pnl"] = round(s["unrealized_pnl"], 2)
            s["realized_pnl"] = None if s["excluded_closed_trades"] else s["known_realized_pnl"]
            s["unrealized_pnl"] = None if s["unknown_open_pnl"] else s["known_unrealized_pnl"]
            s["total_pnl"] = (
                round(s["realized_pnl"] + s["unrealized_pnl"], 2)
                if s["realized_pnl"] is not None and s["unrealized_pnl"] is not None else None
            )
            s["pnl_source"] = (
                "PAPER_SIMULATION" if mode == "PAPER" else
                "LOCAL_RECORDED_FILLS_GROSS" if mode == "REAL" else "UNVERIFIED"
            )
            if mode == "REAL" and not s["closed_trades"] and not s["open_positions"]:
                s["realized_pnl"] = None
                s["unrealized_pnl"] = None
                s["total_pnl"] = None
            s["is_broker_reported"] = False
            breakdown[mode] = s
        mode = modes.get(t, "UNKNOWN")
        if mode not in breakdown:
            mode = "UNKNOWN"
        result.append(dict(
            ticker=t, mode=mode, configured_shares=shares.get(t, 1),
            **signals[t], **breakdown[mode], mode_breakdown=breakdown,
        ))
    return result

