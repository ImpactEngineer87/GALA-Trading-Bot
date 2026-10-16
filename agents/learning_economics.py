"""Cost-aware, chronological evidence for the AI veto filter.

Returns use planned risk units (R), not dollars or a simulated account balance.
Actual fill prices already contain execution slippage. The configured bps are
an additional fee/cost reserve, applied once to average round-trip notional.
Missing risk/cost evidence is never silently treated as a free trade.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def net_trade_returns(frame: pd.DataFrame, round_trip_cost_bps: float) -> np.ndarray:
    def numbers(name):
        return pd.to_numeric(frame.get(name, pd.Series(np.nan, index=frame.index)),
                             errors="coerce").to_numpy(dtype=float)

    risk = numbers("risk_per_share")
    entry, exit_price = numbers("precio_entrada"), numbers("precio_salida")
    sides = frame.get("tipo", pd.Series("", index=frame.index)).astype(str).str.upper()
    # Signal observations carry their original bracket rather than risk_per_share.
    bracket_risk = np.abs(entry - numbers("stop_loss"))
    risk = np.where(np.isfinite(risk) & (risk > 0), risk, bracket_risk)
    valid = (sides.isin(["BUY", "SELL"]).to_numpy() & np.isfinite(risk) & (risk > 0)
             & np.isfinite(entry) & (entry > 0)
             & np.isfinite(exit_price) & (exit_price > 0))
    result = np.full(len(frame), np.nan)
    direction = np.where(sides.to_numpy() == "BUY", 1., -1.)
    # Recompute from the recorded fills and risk. A stale or manually edited
    # gross-R column must not turn an actual losing close into a winning label.
    result[valid] = direction[valid] * (exit_price[valid] - entry[valid]) / risk[valid] - (
        (entry[valid] + exit_price[valid]) / 2.0
        * float(round_trip_cost_bps) / 10000.0 / risk[valid]
    )
    return result


def economic_metrics(returns, probabilities, threshold: float,
                     round_trip_cost_bps: float) -> dict:
    values = np.asarray(returns, dtype=float)
    approved = np.asarray(probabilities, dtype=float) >= float(threshold)
    selected = values[approved]
    missing = int((~np.isfinite(selected)).sum())
    usable = selected[np.isfinite(selected)]
    gains = float(usable[usable > 0].sum())
    losses = float(-usable[usable < 0].sum())
    equity = np.concatenate(([0.0], np.cumsum(usable)))
    drawdown = float((np.maximum.accumulate(equity) - equity).max())
    all_known = values[np.isfinite(values)]
    return {
        "evaluated": bool(len(selected) and missing == 0),
        "accepted_trades": int(len(selected)),
        "missing_returns": missing,
        "net_r": round(float(usable.sum()), 6),
        "expectancy_r": round(float(usable.mean()), 6) if len(usable) else None,
        # None with positive gains and no losses means unbounded; JSON Infinity
        # is deliberately avoided. no_loss_trades disambiguates missing data.
        "profit_factor": round(gains / losses, 6) if losses > 0 else None,
        "no_loss_trades": bool(len(usable) and losses == 0 and gains > 0),
        "max_drawdown_r": round(drawdown, 6),
        "win_rate": round(float((usable > 0).mean()), 6) if len(usable) else None,
        "round_trip_cost_bps": float(round_trip_cost_bps),
        "baseline_net_r": round(float(all_known.sum()), 6),
        "net_r_lift": round(float(usable.sum() - all_known.sum()), 6),
        "baseline_expectancy_r": (
            round(float(all_known.mean()), 6) if len(all_known) else None
        ),
        "baseline_missing_returns": int((~np.isfinite(values)).sum()),
        "cost_basis": "estimated_fee_reserve_actual_fill_slippage_already_in_prices",
    }


def economic_rejection_reasons(metrics: dict, *, min_trades: int,
                               min_expectancy_r: float, min_profit_factor: float,
                               max_drawdown_r: float, min_net_r_lift: float = 0.0) -> list[str]:
    reasons = []
    if not metrics.get("evaluated"):
        reasons.append("TEST económico sin retornos/costes verificables completos")
    if int(metrics.get("accepted_trades", 0)) < min_trades:
        reasons.append(f"TEST económico requiere {min_trades} operaciones aprobadas")
    expectancy = metrics.get("expectancy_r")
    if expectancy is None or expectancy <= min_expectancy_r:
        reasons.append(f"expectativa neta R debe superar {min_expectancy_r:.3f}")
    factor = metrics.get("profit_factor")
    if not metrics.get("no_loss_trades") and (factor is None or factor < min_profit_factor):
        reasons.append(f"profit factor neto inferior a {min_profit_factor:.3f}")
    if float(metrics.get("max_drawdown_r", 0)) > max_drawdown_r:
        reasons.append(f"drawdown TEST excede {max_drawdown_r:.3f} R")
    if float(metrics.get("net_r_lift", 0)) < min_net_r_lift:
        reasons.append(f"R neto TEST degrada estrategia sin filtro (mínimo lift {min_net_r_lift:.3f} R)")
    return reasons
