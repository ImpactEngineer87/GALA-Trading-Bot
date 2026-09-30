"""Market-regime context for GALA10 AI veto decisions.

This module does **not** generate BUY/SELL signals and does not replace GALA10.
It evaluates the already-confirmed micro price history to determine whether the
market is trending, range-bound/choppy, unusually volatile, or uncertain.

PAPER/REAL execution routing is independent from this policy layer.  The
market-regime guard receives the ticker's effective execution mode and applies
an explicit per-mode policy (off / observe / enforce).  This avoids treating
Shadow as a third trading mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from agents.contracts import DatosMultiTemporal


REGIME_TREND_UP = "TREND_UP"
REGIME_TREND_DOWN = "TREND_DOWN"
REGIME_RANGE = "RANGE"
REGIME_HIGH_VOLATILITY = "HIGH_VOLATILITY"
REGIME_UNCERTAIN = "UNCERTAIN"
REGIME_INSUFFICIENT = "INSUFFICIENT_DATA"


@dataclass
class MarketRegimeDecision:
    regime: str = REGIME_INSUFFICIENT
    confidence: float = 0.0
    range_pct: float = float("nan")
    efficiency_ratio: float = float("nan")
    flip_ratio: float = float("nan")
    net_move_atr: float = float("nan")
    atr_regime: float = float("nan")
    ema_slope_atr: float = float("nan")
    breakout_atr: float = float("nan")
    signal_aligned: Optional[bool] = None
    would_block: bool = False
    reason: str = "REGIME_DATA_INSUFFICIENT"
    reasons: List[str] = field(default_factory=list)


class AgenteRegimenMercado:
    """Classifies micro context and recommends an AI veto when appropriate.

    The classifier intentionally uses simple, causal statistics so its behavior
    is explainable in audit logs.  It is a context guard, not a second entry
    strategy.
    """

    def __init__(self, config: dict):
        cfg = (config.get("agents", {}) or {}).get("market_regime", {}) or {}
        self.enabled = bool(cfg.get("enabled", True))

        # v3.1.1: PAPER and REAL are execution modes.  Regime behavior is a
        # separate policy for each execution mode: off / observe / enforce.
        # ``enforcement`` is retained as a compatibility fallback for v3.1.0.
        legacy = str(cfg.get("enforcement", "shadow") or "shadow").strip().lower()
        if legacy not in {"off", "shadow", "real"}:
            legacy = "shadow"
        self.enforcement = legacy
        raw_policy = cfg.get("execution_policy")
        if isinstance(raw_policy, dict):
            self.execution_policy = {
                "PAPER": self._normalize_policy(raw_policy.get("PAPER", raw_policy.get("paper", "observe"))),
                "REAL": self._normalize_policy(raw_policy.get("REAL", raw_policy.get("real", "observe"))),
            }
        elif legacy == "off":
            self.execution_policy = {"PAPER": "off", "REAL": "off"}
        elif legacy == "real":
            # Preserve v3.1.0 semantics: observe PAPER, enforce REAL.
            self.execution_policy = {"PAPER": "observe", "REAL": "enforce"}
        else:
            self.execution_policy = {"PAPER": "observe", "REAL": "observe"}

        self.lookback = max(12, int(cfg.get("lookback_bars", 24) or 24))
        self.min_bars = max(12, int(cfg.get("min_bars", 20) or 20))
        self.atr_period = max(5, int(cfg.get("atr_period", config.get("atr", {}).get("period", 14)) or 14))
        self.ema_period = max(5, int(cfg.get("ema_period", 11) or 11))

        self.range_efficiency_max = float(cfg.get("range_efficiency_max", 0.32))
        self.range_flip_ratio_min = float(cfg.get("range_flip_ratio_min", 0.45))
        self.range_net_move_atr_max = float(cfg.get("range_net_move_atr_max", 1.60))
        self.range_width_pct_max = float(cfg.get("range_width_pct_max", 0.85))

        self.trend_efficiency_min = float(cfg.get("trend_efficiency_min", 0.38))
        self.trend_net_move_atr_min = float(cfg.get("trend_net_move_atr_min", 1.20))
        self.trend_slope_atr_min = float(cfg.get("trend_slope_atr_min", 0.015))

        self.breakout_confirm_atr = float(cfg.get("breakout_confirm_atr", 0.20))
        self.high_volatility_atr_regime = float(cfg.get("high_volatility_atr_regime", 1.80))

        self.block_range = bool(cfg.get("block_range", True))
        self.block_countertrend = bool(cfg.get("block_countertrend", False))
        self.block_high_volatility = bool(cfg.get("block_high_volatility", False))

    @staticmethod
    def _normalize_policy(value: object) -> str:
        value = str(value or "observe").strip().lower()
        aliases = {"shadow": "observe", "real": "enforce", "production": "enforce"}
        value = aliases.get(value, value)
        return value if value in {"off", "observe", "enforce"} else "observe"

    def policy_for(self, execution_mode: str) -> str:
        mode = str(execution_mode or "PAPER").strip().upper()
        if mode not in {"PAPER", "REAL"}:
            mode = "PAPER"
        if not self.enabled:
            return "off"
        return self.execution_policy.get(mode, "observe")

    @property
    def shadow_active(self) -> bool:
        return bool(self.enabled and any(v in {"observe", "enforce"} for v in self.execution_policy.values()))

    @property
    def production_active(self) -> bool:
        return bool(self.enabled and self.execution_policy.get("REAL") == "enforce")

    def evaluar(
        self,
        datos: DatosMultiTemporal,
        senal: str,
        gala10_contexto: Optional[Dict[str, float]] = None,
        actual_en_formacion: bool = False,
    ) -> MarketRegimeDecision:
        if not self.enabled:
            return MarketRegimeDecision(
                regime=REGIME_UNCERTAIN,
                confidence=0.0,
                would_block=False,
                reason="REGIME_FILTER_OFF",
                reasons=["Filtro de régimen desactivado"],
            )

        df = self._micro_frame(datos, actual_en_formacion)
        if df is None or len(df) < self.min_bars:
            return MarketRegimeDecision(
                regime=REGIME_INSUFFICIENT,
                confidence=0.0,
                would_block=False,
                reason="REGIME_DATA_INSUFFICIENT",
                reasons=["Velas micro insuficientes para clasificar régimen"],
            )

        needed = {"high", "low", "close"}
        if not needed.issubset(df.columns):
            return MarketRegimeDecision(
                regime=REGIME_INSUFFICIENT,
                confidence=0.0,
                would_block=False,
                reason="REGIME_COLUMNS_MISSING",
                reasons=["Faltan columnas OHLC para clasificar régimen"],
            )

        # Keep up to 100 historical bars for the ATR-regime baseline while the
        # actual trend/range classification still uses only ``lookback`` bars.
        frame = df.tail(max(self.lookback, self.atr_period + 2, 100)).copy()
        close = pd.to_numeric(frame["close"], errors="coerce")
        high = pd.to_numeric(frame["high"], errors="coerce")
        low = pd.to_numeric(frame["low"], errors="coerce")
        valid = close.notna() & high.notna() & low.notna()
        frame = frame.loc[valid]
        close = close.loc[valid]
        high = high.loc[valid]
        low = low.loc[valid]
        if len(frame) < self.min_bars:
            return MarketRegimeDecision(
                regime=REGIME_INSUFFICIENT,
                confidence=0.0,
                would_block=False,
                reason="REGIME_DATA_INSUFFICIENT",
                reasons=["Velas OHLC válidas insuficientes"],
            )

        analysis = frame.tail(self.lookback)
        c = pd.to_numeric(analysis["close"], errors="coerce")
        h = pd.to_numeric(analysis["high"], errors="coerce")
        l = pd.to_numeric(analysis["low"], errors="coerce")
        last_close = float(c.iloc[-1])
        if not math.isfinite(last_close) or last_close <= 0:
            return MarketRegimeDecision(reason="REGIME_INVALID_PRICE")

        atr_series = self._atr_wilder(frame)
        atr = float(atr_series.iloc[-1]) if len(atr_series) else float("nan")
        if not math.isfinite(atr) or atr <= 0:
            return MarketRegimeDecision(reason="REGIME_ATR_UNAVAILABLE")

        atr_median = float(atr_series.tail(min(100, len(atr_series))).median())
        atr_regime = atr / atr_median if math.isfinite(atr_median) and atr_median > 0 else 1.0

        price_range = float(h.max() - l.min())
        range_pct = price_range / last_close * 100.0

        deltas = c.diff().dropna()
        path = float(deltas.abs().sum())
        net = float(c.iloc[-1] - c.iloc[0])
        efficiency = abs(net) / path if path > 0 else 0.0
        net_move_atr = abs(net) / atr

        signs = np.sign(deltas.to_numpy(dtype=float))
        signs = signs[signs != 0]
        if len(signs) >= 2:
            flip_ratio = float(np.mean(signs[1:] != signs[:-1]))
        else:
            flip_ratio = 0.0

        ema = c.ewm(span=self.ema_period, adjust=False).mean()
        slope_window = ema.diff().tail(3).dropna()
        ema_slope = float(slope_window.mean()) if len(slope_window) else 0.0
        ema_slope_atr = ema_slope / atr

        context = gala10_contexto or {}
        breakout_atr = self._finite(context.get("gala_breakout_atr"))

        low_eff = efficiency <= self.range_efficiency_max
        high_flips = flip_ratio >= self.range_flip_ratio_min
        low_net = net_move_atr <= self.range_net_move_atr_max
        bounded_width = range_pct <= self.range_width_pct_max
        range_votes = int(low_eff) + int(high_flips) + int(low_net) + int(bounded_width)

        high_vol = atr_regime >= self.high_volatility_atr_regime
        trend_up = (
            net > 0
            and efficiency >= self.trend_efficiency_min
            and net_move_atr >= self.trend_net_move_atr_min
            and ema_slope_atr >= self.trend_slope_atr_min
        )
        trend_down = (
            net < 0
            and efficiency >= self.trend_efficiency_min
            and net_move_atr >= self.trend_net_move_atr_min
            and ema_slope_atr <= -self.trend_slope_atr_min
        )

        if high_vol:
            regime = REGIME_HIGH_VOLATILITY
        elif range_votes >= 3:
            regime = REGIME_RANGE
        elif trend_up:
            regime = REGIME_TREND_UP
        elif trend_down:
            regime = REGIME_TREND_DOWN
        else:
            regime = REGIME_UNCERTAIN

        confidence = self._confidence(
            regime=regime,
            efficiency=efficiency,
            flip_ratio=flip_ratio,
            net_move_atr=net_move_atr,
            atr_regime=atr_regime,
            range_votes=range_votes,
        )

        signal = str(senal or "").upper()
        signal_dir = 1 if signal == "BUY" else -1 if signal == "SELL" else 0
        regime_dir = 1 if regime == REGIME_TREND_UP else -1 if regime == REGIME_TREND_DOWN else 0
        aligned = None if signal_dir == 0 or regime_dir == 0 else bool(signal_dir == regime_dir)

        breakout_confirmed = math.isfinite(breakout_atr) and breakout_atr >= self.breakout_confirm_atr
        would_block = False
        reason = f"REGIME_{regime}"
        reasons: List[str] = [
            f"regime={regime}",
            f"efficiency={efficiency:.3f}",
            f"flip_ratio={flip_ratio:.3f}",
            f"net_move_atr={net_move_atr:.3f}",
            f"range_pct={range_pct:.3f}",
            f"atr_regime={atr_regime:.3f}",
        ]

        if regime == REGIME_RANGE and self.block_range:
            if breakout_confirmed:
                reason = "REGIME_RANGE_BREAKOUT_CONFIRMED"
                reasons.append(f"breakout_atr={breakout_atr:.3f} confirmado")
            else:
                would_block = True
                reason = "REGIME_RANGE_CHOP"
                if math.isfinite(breakout_atr):
                    reasons.append(f"breakout_atr={breakout_atr:.3f} insuficiente")
                else:
                    reasons.append("breakout_atr=N/A")
        elif regime == REGIME_HIGH_VOLATILITY and self.block_high_volatility:
            would_block = True
            reason = "REGIME_HIGH_VOLATILITY"
        elif self.block_countertrend and aligned is False:
            would_block = True
            reason = "REGIME_COUNTERTREND"

        return MarketRegimeDecision(
            regime=regime,
            confidence=round(confidence, 4),
            range_pct=round(range_pct, 6),
            efficiency_ratio=round(efficiency, 6),
            flip_ratio=round(flip_ratio, 6),
            net_move_atr=round(net_move_atr, 6),
            atr_regime=round(atr_regime, 6),
            ema_slope_atr=round(ema_slope_atr, 6),
            breakout_atr=breakout_atr,
            signal_aligned=aligned,
            would_block=would_block,
            reason=reason,
            reasons=reasons,
        )

    def _micro_frame(self, datos: DatosMultiTemporal, actual_en_formacion: bool):
        # Prefer 1m because GALA10 is evaluated on the confirmed 1m close.
        first = None
        for tf in ("1m", "5m", "15m", "1H"):
            df = datos.tf(tf)
            if df is None:
                continue
            candidate = df.iloc[:-1] if actual_en_formacion and len(df) > 1 else df
            if first is None:
                first = candidate
            if len(candidate) >= self.min_bars:
                return candidate
        return first

    def _atr_wilder(self, df: pd.DataFrame) -> pd.Series:
        high = pd.to_numeric(df["high"], errors="coerce")
        low = pd.to_numeric(df["low"], errors="coerce")
        close = pd.to_numeric(df["close"], errors="coerce")
        prev = close.shift(1)
        tr = pd.concat(
            [(high - low), (high - prev).abs(), (low - prev).abs()], axis=1
        ).max(axis=1)
        return tr.ewm(alpha=1.0 / self.atr_period, adjust=False).mean()

    @staticmethod
    def _finite(value) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return float("nan")
        return number if math.isfinite(number) else float("nan")

    @staticmethod
    def _confidence(
        regime: str,
        efficiency: float,
        flip_ratio: float,
        net_move_atr: float,
        atr_regime: float,
        range_votes: int,
    ) -> float:
        if regime == REGIME_RANGE:
            return min(1.0, 0.50 + 0.125 * range_votes)
        if regime in {REGIME_TREND_UP, REGIME_TREND_DOWN}:
            return min(1.0, 0.45 + 0.35 * efficiency + 0.05 * min(net_move_atr, 4.0))
        if regime == REGIME_HIGH_VOLATILITY:
            return min(1.0, 0.60 + 0.15 * max(0.0, atr_regime - 1.0))
        return max(0.25, min(0.55, 0.45 - 0.15 * flip_ratio + 0.10 * efficiency))
