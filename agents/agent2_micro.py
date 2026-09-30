"""
=============================================================
MÓDULO: agents/agent2_micro.py
PROYECTO: GALA6 Chocoletra Bot
AGENTE 2 — EL EJECUTOR MICRO / FRANCOTIRADOR
=============================================================
DESCRIPCIÓN:
    Recibe la Dirección del Día del Agente 1 y baja a 15m y 5m
    para buscar Brechas de Valor Justo (FVG) y desequilibrios no
    mitigados ALINEADOS con la dirección macro.

    Solo genera una propuesta cuando encuentra un FVG válido con
    una Relación Beneficio/Riesgo (R/B) óptima:

      ENTRADA   → borde proximal del FVG (limit dentro del hueco)
      STOP LOSS → borde distal del FVG + colchón (ATR × factor)
      TAKE PROFIT→ objetivo de liquidez del Agente 1 (o R/B objetivo)

    Si ningún FVG cumple el R/B mínimo, no propone nada (None).
=============================================================
"""

from datetime import datetime
from typing import List, Optional

import pandas as pd

from agents.contracts import (
    Direccion, DatosMultiTemporal, DireccionDia, PropuestaOperacion, FairValueGap,
)
from agents import smc_primitives as smc


class AgenteMicroFrancotirador:
    """Agente 2 del sistema SMC: convierte la dirección macro en una
    propuesta de entrada de precisión basada en FVG."""

    def __init__(self, config: dict):
        cfg = config.get("agents", {}).get("agente2", {})
        self.timeframes: List[str] = cfg.get("timeframes", ["15m", "5m"])
        self.ratio_rb_minimo: float = cfg.get("ratio_rb_minimo", 1.8)
        self.buffer_sl_atr: float = cfg.get("buffer_sl_atr", 0.5)
        self.max_edad_fvg: int = cfg.get("max_edad_fvg_velas", 60)
        self.min_tamano_fvg_atr: float = cfg.get("min_tamano_fvg_atr", 0.15)
        self.atr_period: int = config.get("atr", {}).get("period", 14)

    # =========================================================
    # ANÁLISIS PRINCIPAL
    # =========================================================

    def analizar(self, datos: DatosMultiTemporal, direccion_dia: DireccionDia,
                 precio_actual: float,
                 ahora: Optional[datetime] = None,
                 en_formacion: bool = True) -> Optional[PropuestaOperacion]:
        """
        Busca la mejor propuesta de operación alineada con la dirección macro.

        Retorna PropuestaOperacion o None si no hay un setup válido.
        """
        ts = ahora or datetime.now()

        # La dirección macro debe permitir operar
        if direccion_dia.direccion == Direccion.ALCISTA:
            tipo = "BUY"
        elif direccion_dia.direccion == Direccion.BAJISTA:
            tipo = "SELL"
        else:
            return None  # NEUTRAL → sin ejecución

        if precio_actual <= 0:
            return None

        candidatas: List[PropuestaOperacion] = []

        for tf in self.timeframes:
            df = datos.tf(tf)
            if df is None or len(df) < self.atr_period + 3:
                continue

            atr_series = smc.atr_wilder(df, self.atr_period)
            atr_val = float(atr_series.iloc[-1]) if len(atr_series) else 0.0
            if atr_val <= 0:
                continue

            fvgs = smc.fvgs_validos(
                df, tf, tipo, atr_val,
                max_edad_velas=self.max_edad_fvg,
                min_tamano_atr=self.min_tamano_fvg_atr,
                en_formacion=en_formacion,
            )
            # Evaluar del más reciente al más antiguo
            for fvg in reversed(fvgs):
                propuesta = self._construir_propuesta(
                    datos.ticker, tipo, fvg, atr_val, precio_actual,
                    direccion_dia.objetivo_liquidez, tf, ts,
                )
                if propuesta is not None:
                    candidatas.append(propuesta)

        if not candidatas:
            return None

        # Elegir la propuesta con mejor R/B
        candidatas.sort(key=lambda p: p.ratio_rb, reverse=True)
        return candidatas[0]

    # =========================================================
    # CONSTRUCCIÓN DE LA PROPUESTA
    # =========================================================

    def _construir_propuesta(self, ticker: str, tipo: str, fvg: FairValueGap,
                             atr_val: float, precio_actual: float,
                             objetivo_liquidez: Optional[float], tf: str,
                             ts: datetime) -> Optional[PropuestaOperacion]:
        """
        Deriva entrada / SL / TP a partir de un FVG y valida el R/B.
        """
        buffer = atr_val * self.buffer_sl_atr

        if tipo == "BUY":
            # FVG alcista (zona de demanda). Entrada al borde superior (proximal
            # desde arriba); SL bajo el borde inferior con colchón.
            entrada = fvg.tope
            stop_loss = fvg.fondo - buffer
            # El precio debe seguir por encima del hueco (entrada aún pendiente
            # de retroceso) o dentro de él; si ya cayó por debajo, el setup murió.
            if precio_actual < fvg.fondo:
                return None
            # Objetivo: liquidez macro por encima; si no sirve, R/B objetivo
            take_profit = self._resolver_tp(tipo, entrada, stop_loss, objetivo_liquidez)
            if take_profit <= entrada:
                return None
        else:  # SELL
            # FVG bajista (zona de oferta). Entrada al borde inferior (proximal
            # desde abajo); SL sobre el borde superior con colchón.
            entrada = fvg.fondo
            stop_loss = fvg.tope + buffer
            if precio_actual > fvg.tope:
                return None
            take_profit = self._resolver_tp(tipo, entrada, stop_loss, objetivo_liquidez)
            if take_profit >= entrada:
                return None

        riesgo = abs(entrada - stop_loss)
        beneficio = abs(take_profit - entrada)
        if riesgo <= 0:
            return None
        ratio_rb = beneficio / riesgo

        if ratio_rb < self.ratio_rb_minimo:
            return None

        razones = [
            f"FVG {fvg.direccion} en {tf} [{fvg.fondo:.2f}-{fvg.tope:.2f}] no mitigado",
            f"Entrada ${entrada:.2f} | SL ${stop_loss:.2f} | TP ${take_profit:.2f}",
            f"R/B {ratio_rb:.2f} (mín {self.ratio_rb_minimo})",
        ]

        return PropuestaOperacion(
            ticker=ticker,
            tipo=tipo,
            precio_entrada=round(entrada, 4),
            stop_loss=round(stop_loss, 4),
            take_profit=round(take_profit, 4),
            ratio_rb=round(ratio_rb, 3),
            fvg=fvg,
            razones=razones,
            timestamp=ts,
        )

    def _resolver_tp(self, tipo: str, entrada: float, stop_loss: float,
                     objetivo_liquidez: Optional[float]) -> float:
        """
        Elige el take profit. Prioriza el objetivo de liquidez del Agente 1
        si está en la dirección correcta; si no, usa un objetivo por R/B.
        """
        riesgo = abs(entrada - stop_loss)
        tp_por_rb = (entrada + riesgo * self.ratio_rb_minimo) if tipo == "BUY" \
            else (entrada - riesgo * self.ratio_rb_minimo)

        if objetivo_liquidez is not None:
            if tipo == "BUY" and objetivo_liquidez > entrada:
                return objetivo_liquidez
            if tipo == "SELL" and objetivo_liquidez < entrada:
                return objetivo_liquidez
        return tp_por_rb
