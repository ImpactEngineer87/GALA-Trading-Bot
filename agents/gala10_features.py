"""Causal GALA10 context features shared by training and live inference.

The ThinkScript remains the only source of BUY/SELL entries.  These values do
not create or alter a signal; they describe the state of the already-confirmed
closed candle so the AI can decide whether to approve or veto that signal.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Dict, Optional

import numpy as np
import pandas as pd


GALA10_FEATURE_NAMES = [
    "gala_hist_strength_atr",
    "gala_hist_progress_atr",
    "gala_ema_slope_atr",
    "gala_range_pct",
    "gala_cross_bars",
    "gala_fast_entry",
    "gala_breakout_atr",
    "gala_atr_regime_100",
    "gala_body_atr",
    "gala_close_location",
    "gala_session_minute",
]


def _finite(value, default=float("nan")) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _session_minute(timestamp) -> float:
    """Minutes from 09:30 New York, preserving negative pre-market values.

    DataFrame indices in this project use UTC-naive timestamps (Schwab epoch and
    the normalized Massive loader). Timezone-aware inputs are respected.
    """
    if timestamp is None:
        return float("nan")
    try:
        ts = pd.Timestamp(timestamp)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        ts = ts.tz_convert("America/New_York")
        return float(ts.hour * 60 + ts.minute - (9 * 60 + 30))
    except Exception:
        if isinstance(timestamp, datetime):
            return float(timestamp.hour * 60 + timestamp.minute - (9 * 60 + 30))
        return float("nan")


def empty_gala10_features() -> Dict[str, float]:
    return {name: float("nan") for name in GALA10_FEATURE_NAMES}


def extraer_features_gala10(
    resultado: pd.DataFrame,
    posicion: int = -1,
    senal: Optional[str] = None,
    timestamp=None,
) -> Dict[str, float]:
    """Extract features from one fully calculated GALA10 candle.

    Only the selected row and earlier rows are read.  No future price, order
    result, TP, or stop information participates in these values.
    """
    if resultado is None or len(resultado) == 0:
        return empty_gala10_features()
    try:
        pos = posicion if posicion >= 0 else len(resultado) + posicion
        if pos < 0 or pos >= len(resultado):
            return empty_gala10_features()
        row = resultado.iloc[pos]
        prev = resultado.iloc[pos - 1] if pos > 0 else None
    except Exception:
        return empty_gala10_features()

    signal = str(senal or row.get("señal") or "").upper()
    if signal not in ("BUY", "SELL"):
        return empty_gala10_features()
    direction = 1.0 if signal == "BUY" else -1.0

    atr = _finite(row.get("atr"))
    if not math.isfinite(atr) or atr <= 0:
        return empty_gala10_features()

    hist = _finite(row.get("hist_line"))
    hist_prev = _finite(prev.get("hist_line")) if prev is not None else float("nan")
    ema_slope = _finite(row.get("ema_slope"))
    close = _finite(row.get("close"))
    open_ = _finite(row.get("open"))
    high = _finite(row.get("high"))
    low = _finite(row.get("low"))
    cross_high = _finite(row.get("cross_high"))
    cross_low = _finite(row.get("cross_low"))
    breakout_buffer = _finite(row.get("breakout_buffer"), 0.0)

    if signal == "BUY":
        wall = cross_high + breakout_buffer
        fast_entry = bool(row.get("fast_bull_confirm", False))
    else:
        wall = cross_low - breakout_buffer
        fast_entry = bool(row.get("fast_bear_confirm", False))

    breakout_atr = (
        direction * (close - wall) / atr
        if all(math.isfinite(v) for v in (close, wall))
        else float("nan")
    )
    hist_progress = (
        direction * (hist - hist_prev) / atr
        if all(math.isfinite(v) for v in (hist, hist_prev))
        else float("nan")
    )
    candle_range = high - low if all(math.isfinite(v) for v in (high, low)) else 0.0
    if candle_range > 0 and math.isfinite(close):
        raw_location = (close - low) / candle_range
        close_location = raw_location if signal == "BUY" else 1.0 - raw_location
    else:
        close_location = float("nan")

    ts = timestamp if timestamp is not None else getattr(row, "name", None)
    values = {
        "gala_hist_strength_atr": abs(hist) / atr if math.isfinite(hist) else float("nan"),
        "gala_hist_progress_atr": hist_progress,
        "gala_ema_slope_atr": (
            direction * ema_slope / atr
            if math.isfinite(ema_slope) else float("nan")
        ),
        "gala_range_pct": _finite(row.get("range_pct")),
        "gala_cross_bars": _finite(row.get("cross_bars")),
        "gala_fast_entry": 1.0 if fast_entry else 0.0,
        "gala_breakout_atr": breakout_atr,
        "gala_atr_regime_100": _finite(row.get("atr_regime_100")),
        "gala_body_atr": (
            abs(close - open_) / atr
            if all(math.isfinite(v) for v in (close, open_))
            else float("nan")
        ),
        "gala_close_location": close_location,
        "gala_session_minute": _session_minute(ts),
    }
    return {
        name: round(_finite(values.get(name)), 8)
        if math.isfinite(_finite(values.get(name)))
        else float("nan")
        for name in GALA10_FEATURE_NAMES
    }
