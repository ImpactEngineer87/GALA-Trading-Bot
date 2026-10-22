"""Read-only presentation of Schwab-reported P/L on currently open positions.

Broker `currentDayProfitLoss` is not equivalent to the bot's closed-trade
realized P/L, and it may not equal TOS account-wide P/L Day (which can include
closed trades and positions not tracked by GALA). Never fill missing fields
with estimated prices or count them as zero.
"""
from __future__ import annotations

import math


def open_position_pnl_snapshot(positions):
    if positions is None:
        return {
            "available": False, "source": "SCHWAB_OPEN_POSITIONS",
            "positions": [], "known_count": 0, "unknown_count": 0,
            "position_day_pnl": None,
        }
    result = []
    known = 0
    partial_sum = 0.0
    for position in positions:
        if not isinstance(position, dict):
            continue
        symbol = str(position.get("symbol") or "").strip().upper()
        side = str(position.get("tipo") or "").upper()
        if not symbol or side not in {"BUY", "SELL"}:
            continue
        try:
            quantity = int(position.get("cantidad", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if quantity <= 0:
            continue
        raw = position.get("broker_position_day_pnl")
        pnl = None
        if raw is not None and raw != "":
            try:
                value = float(raw)
                if math.isfinite(value):
                    pnl = round(value, 2)
                    partial_sum += value
                    known += 1
            except (TypeError, ValueError, OverflowError):
                pass
        result.append({
            "ticker": symbol,
            "side": side,
            "quantity": quantity,
            "broker_position_day_pnl": pnl,
        })
    unknown = len(result) - known
    return {
        "available": True, "source": "SCHWAB_OPEN_POSITIONS",
        "positions": result,
        "known_count": known, "unknown_count": unknown,
        "position_day_pnl": round(partial_sum, 2) if not unknown else None,
        "partial_known_pnl": round(partial_sum, 2) if known else None,
    }
