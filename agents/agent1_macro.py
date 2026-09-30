"""
=============================================================
MÓDULO: agents/agent1_macro.py
PROYECTO: GALA6 Chocoletra Bot
AGENTE 1 — EL ANALISTA MACRO Y DE LIQUIDEZ
=============================================================
DESCRIPCIÓN:
    Analiza el contexto mayor en temporalidades síncronas
    (Semanal, Diaria, 4H y 1H) para emitir la "Dirección del Día":
    Vía Libre ALCISTA, BAJISTA o NEUTRAL.

    Rastrea además la liquidez del mercado:
      - Niveles del día previo    (PDH / PDL / PDC)
      - Niveles de la semana previa(PWH / PWL / PWC)
      - Equal Highs / Lows        (EQH / EQL — liquidez agrupada)
      - Swing Highs / Lows        (SWH / SWL)
      - Brecha de apertura (opening gap)

    LÓGICA DE DIRECCIÓN:
      Cada temporalidad aporta un sesgo de estructura en [-1, 1]
      (Higher-Highs/Higher-Lows = alcista). Se combinan con los
      pesos de config.yaml (mayor peso a las temporalidades altas).
      Si el sesgo neto supera el umbral → ALCISTA/BAJISTA; si no,
      NEUTRAL (no operar).
=============================================================
"""

from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from agents.contracts import (
    Direccion, DatosMultiTemporal, DireccionDia, NivelLiquidez,
)
from agents import smc_primitives as smc


class AgenteMacroLiquidez:
    """
    Agente 1 del sistema SMC. Determina la dirección permitida del
    día y construye el mapa de liquidez que usarán los agentes 2 y 3.
    """

    def __init__(self, config: dict):
        cfg_root = config.get("agents", {})
        cfg = cfg_root.get("agente1", {})
        self.timeframes: List[str] = cfg.get("timeframes", ["1W", "1D", "4H", "1H"])
        self.pesos: Dict[str, float] = cfg.get(
            "pesos", {"1W": 0.35, "1D": 0.35, "4H": 0.20, "1H": 0.10}
        )
        self.umbral_direccion: float = cfg.get("umbral_direccion", 0.15)
        self.lookback_swing: int = cfg.get("lookback_swing", 5)
        self.tolerancia_eq_pct: float = cfg.get("tolerancia_eq_pct", 0.05)

    # =========================================================
    # ANÁLISIS PRINCIPAL
    # =========================================================

    def analizar(self, datos: DatosMultiTemporal,
                 precio_actual: Optional[float] = None,
                 actual_en_formacion: bool = True,
                 ahora: Optional[datetime] = None) -> DireccionDia:
        """
        Ejecuta el análisis macro completo.

        Parámetros:
            datos               → velas multi-temporal del ticker
            precio_actual       → precio de referencia (si None, se toma
                                   del cierre de la TF más baja disponible)
            actual_en_formacion → True si la última vela diaria/semanal
                                   representa el periodo aún en curso
            ahora               → timestamp de la decisión (inyectable para tests)

        Retorna:
            DireccionDia con dirección, confianza, niveles y objetivo.
        """
        ts = ahora or datetime.now()
        precio = precio_actual if precio_actual is not None else self._precio_referencia(datos)
        razones: List[str] = []

        # -----------------------------------------------------
        # 1) Sesgo ponderado de estructura en las TF mayores
        # -----------------------------------------------------
        sesgo_neto = 0.0
        peso_total = 0.0
        detalles = []
        for tf in self.timeframes:
            df = datos.tf(tf)
            if df is None:
                continue
            sesgo = smc.sesgo_estructura(df, self.lookback_swing)
            peso = float(self.pesos.get(tf, 0.0))
            if peso <= 0:
                continue
            sesgo_neto += sesgo * peso
            peso_total += peso
            detalles.append(f"{tf}:{sesgo:+.2f}")

        if peso_total > 0:
            sesgo_neto /= peso_total
        else:
            # Sin datos de temporalidades mayores → no se puede opinar
            return DireccionDia(
                ticker=datos.ticker, direccion=Direccion.NEUTRAL, confianza=0.0,
                razones=["Sin datos suficientes en temporalidades mayores"],
                timestamp=ts,
            )

        razones.append("Sesgo estructura [" + ", ".join(detalles) + f"] → neto {sesgo_neto:+.3f}")

        # -----------------------------------------------------
        # 2) Determinar la dirección del día
        # -----------------------------------------------------
        if sesgo_neto >= self.umbral_direccion:
            direccion = Direccion.ALCISTA
        elif sesgo_neto <= -self.umbral_direccion:
            direccion = Direccion.BAJISTA
        else:
            direccion = Direccion.NEUTRAL
            razones.append(f"Sesgo neto |{sesgo_neto:+.3f}| < umbral {self.umbral_direccion} → NEUTRAL")

        # Confianza: magnitud del sesgo, saturada a 1.0
        confianza = min(1.0, abs(sesgo_neto) / max(self.umbral_direccion, 1e-6) * 0.5 + abs(sesgo_neto) * 0.5)
        confianza = round(min(1.0, confianza), 3)

        # -----------------------------------------------------
        # 3) Construir el mapa de liquidez
        # -----------------------------------------------------
        niveles = self._construir_liquidez(datos, precio, actual_en_formacion)

        # -----------------------------------------------------
        # 4) Brecha de apertura (opening gap) desde el diario
        # -----------------------------------------------------
        hueco = None
        df_d = datos.tf("1D")
        if df_d is not None:
            hueco = smc.hueco_apertura_pct(df_d, actual_en_formacion)
            if hueco is not None:
                razones.append(f"Brecha de apertura: {hueco:+.2f}%")

        # -----------------------------------------------------
        # 5) Objetivo de liquidez en la dirección
        # -----------------------------------------------------
        objetivo = None
        if direccion == Direccion.ALCISTA:
            objetivo = smc.objetivo_liquidez_mas_cercano(niveles, precio, "BUY")
        elif direccion == Direccion.BAJISTA:
            objetivo = smc.objetivo_liquidez_mas_cercano(niveles, precio, "SELL")
        if objetivo is not None:
            razones.append(f"Objetivo de liquidez: ${objetivo:.2f}")

        return DireccionDia(
            ticker=datos.ticker,
            direccion=direccion,
            confianza=confianza,
            niveles_liquidez=niveles,
            hueco_apertura_pct=hueco,
            objetivo_liquidez=objetivo,
            razones=razones,
            timestamp=ts,
        )

    # =========================================================
    # AUXILIARES
    # =========================================================

    def _construir_liquidez(self, datos: DatosMultiTemporal, precio: float,
                            actual_en_formacion: bool) -> List[NivelLiquidez]:
        """Reúne todos los niveles de liquidez y los clasifica por precio."""
        niveles: List[NivelLiquidez] = []

        df_d = datos.tf("1D")
        if df_d is not None:
            niveles += smc.niveles_dia_previo(df_d, actual_en_formacion)

        df_w = datos.tf("1W")
        if df_w is not None:
            niveles += smc.niveles_semana_previa(df_w, actual_en_formacion)

        # Liquidez intradía / estructura: preferir 1H, luego 4H
        df_estruct = datos.tf("1H")
        if df_estruct is None:
            df_estruct = datos.tf("4H")
        if df_estruct is not None:
            niveles += smc.equal_highs_lows(df_estruct, self.lookback_swing, self.tolerancia_eq_pct)
            niveles += smc.niveles_swing_liquidez(df_estruct, self.lookback_swing, max_niveles=2)

        # Clasificar resistencia/soporte respecto al precio actual
        if precio and precio > 0:
            niveles = smc.marcar_niveles_por_precio(niveles, precio)
        return niveles

    def _precio_referencia(self, datos: DatosMultiTemporal) -> float:
        """Toma el cierre más reciente de la TF más baja disponible."""
        for tf in ("1m", "5m", "15m", "1H", "4H", "1D", "1W"):
            df = datos.tf(tf)
            if df is not None:
                return float(df["close"].iloc[-1])
        return 0.0
