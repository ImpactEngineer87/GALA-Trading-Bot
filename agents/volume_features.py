"""Causal one-minute volume and liquidity features for the AI veto models."""

from __future__ import annotations

import math
from typing import Dict

import numpy as np

from agents.contracts import DatosMultiTemporal


VOLUME_FEATURE_NAMES = [
    "volumen_relativo_20",
    "volumen_zscore_20",
    "liquidez_dolar_log",
    "spread_proxy_pct",
    "impacto_amihud_20",
]


def _empty() -> Dict[str, float]:
    return {name: float("nan") for name in VOLUME_FEATURE_NAMES}


def calcular_features_volumen(
    datos: DatosMultiTemporal,
    actual_en_formacion: bool,
    lookback: int = 20,
) -> Dict[str, float]:
    """
    Build features using only information available at the decision timestamp.

    In live mode Schwab normally includes the current 1m candle, so it is removed.
    Historical snapshots end exactly at a closed candle and keep their last row.
    No future return or execution result is used.
    """
    df = datos.tf("1m")
    if df is None:
        df = datos.tf("5m")
    if df is None:
        return _empty()
    if actual_en_formacion and len(df) > 1:
        df = df.iloc[:-1]
    if len(df) < max(5, lookback + 1):
        return _empty()

    required = {"high", "low", "close", "volume"}
    if not required.issubset(df.columns):
        return _empty()

    window = df.iloc[-(lookback + 1):].copy()
    close = window["close"].astype(float)
    volume = window["volume"].astype(float)
    valid = (
        np.isfinite(close.to_numpy())
        & np.isfinite(volume.to_numpy())
        & (close.to_numpy() > 0)
        & (volume.to_numpy() > 0)
    )
    if int(valid.sum()) < max(5, lookback // 2):
        return _empty()

    current_volume = float(volume.iloc[-1])
    previous_volume = volume.iloc[:-1].replace([np.inf, -np.inf], np.nan).dropna()
    previous_volume = previous_volume[previous_volume > 0]
    if current_volume <= 0 or len(previous_volume) < 5:
        return _empty()

    mean_volume = float(previous_volume.mean())
    std_volume = float(previous_volume.std(ddof=0))
    relative_volume = current_volume / mean_volume if mean_volume > 0 else float("nan")
    zscore_volume = (
        (current_volume - mean_volume) / std_volume
        if std_volume > 0 else 0.0
    )

    dollar_volume = (close * volume).replace([np.inf, -np.inf], np.nan)
    dollar_volume = dollar_volume[dollar_volume > 0]
    mean_dollar = float(dollar_volume.tail(lookback).mean()) if len(dollar_volume) else 0.0
    liquidity_log = math.log10(mean_dollar) if mean_dollar > 0 else float("nan")

    last_close = float(close.iloc[-1])
    spread_proxy = (
        (float(window["high"].iloc[-1]) - float(window["low"].iloc[-1]))
        / last_close * 100.0
        if last_close > 0 else float("nan")
    )

    returns = close.pct_change().abs()
    amihud = (returns / (close * volume)).replace([np.inf, -np.inf], np.nan).dropna()
    # Scale the tiny raw ratio to a numerically readable range.
    amihud_scaled = float(amihud.tail(lookback).mean() * 1e9) if len(amihud) else float("nan")

    return {
        "volumen_relativo_20": round(float(relative_volume), 6),
        "volumen_zscore_20": round(float(zscore_volume), 6),
        "liquidez_dolar_log": round(float(liquidity_log), 6),
        "spread_proxy_pct": round(float(spread_proxy), 6),
        "impacto_amihud_20": round(float(amihud_scaled), 6),
    }
