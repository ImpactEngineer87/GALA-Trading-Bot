"""
=============================================================
MÓDULO: core/strategy.py
PROYECTO: GALA10 Chocoletra Bot
DESCRIPCIÓN:
    Réplica de la estrategia maestra "Gala10" confirmada por el cliente
    (el encabezado interno del ThinkScript aún dice "GALA6 LINEAL
    CORREGIDO"). ThinkOrSwim/Gala10 es el disparador único; este módulo no
    añade condiciones de volumen ni decisiones propias de los agentes.

    Este módulo implementa EXACTAMENTE la misma lógica del script
    original, incluyendo:
      - Cálculo MACD (EMA rápida, lenta, señal, histograma)
      - Filtro ATR (método Wilder, período 14)
      - Tendencia EMA-11 con filtro de pendiente mínima
      - Memoria del cruce (crossHigh, crossLow, crossBars)
      - Filtro de compresión / rango (últimas 8 velas)
      - Fuerza del histograma (progresivo)
      - Calidad de vela (cuerpo impulso)
      - Ruptura del muro (breakout buffer)
      - Sistema de cooldown de 8 velas

AUTOR: Chocoletra Bot Dev Team
VERSIÓN: 1.0.0
=============================================================
"""

import numpy as np
import pandas as pd
from typing import Dict, Any


# =============================================================
# CLASE PRINCIPAL DE ESTRATEGIA
# =============================================================

class GALA10Strategy:
    """
    Implementa la estrategia maestra Gala10 confirmada en Python.

    Uso básico:
        strategy = GALA10Strategy(config)
        resultado = strategy.calcular(df)
        señales = resultado['señal']  # 'BUY', 'SELL', 'OM-R', 'OM-T', None
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Inicializa la estrategia con los parámetros del archivo config.yaml.

        Parámetros:
            config (dict): Diccionario con parámetros de la estrategia.
                           Se lee desde config.yaml sección 'strategy' y 'macd'.
        """
        # --- Parámetros MACD ---
        self.fast_length = config.get("macd", {}).get("fast_length", 12)
        self.slow_length = config.get("macd", {}).get("slow_length", 26)
        self.signal_length = config.get("macd", {}).get("signal_length", 9)

        # --- Parámetros de contexto ---
        s = config.get("strategy", {})
        self.ema_trend_length = s.get("ema_trend_length", 11)
        self.ema_slope_factor = s.get("ema_slope_factor", 0.02)
        self.range_lookback = s.get("range_lookback", 8)
        self.min_range_pct = s.get("min_range_pct", 0.18)
        self.breakout_buffer_factor = s.get("breakout_buffer_factor", 0.02)
        self.hist_threshold_factor = s.get("hist_threshold_factor", 0.04)
        self.impulse_body_factor = s.get("impulse_body_factor", 0.60)
        self.allow_fast_entry = s.get("allow_fast_entry", True)
        self.cooldown_bars = s.get("cooldown_bars", 8)

        # --- ATR ---
        self.atr_period = config.get("atr", {}).get("period", 14)

    # ==========================================================
    # MÉTODO PRINCIPAL — Calcula todos los indicadores y señales
    # ==========================================================

    def calcular(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Recibe un DataFrame con velas OHLCV y retorna el mismo DataFrame
        enriquecido con todos los indicadores y señales de la estrategia.

        Columnas requeridas en df:
            open, high, low, close, volume

        Columnas añadidas al resultado:
            macd_line, signal_line, hist_line  → Líneas MACD
            atr                                → ATR Wilder-14
            ema_trend, ema_slope               → EMA tendencia y pendiente
            valid_range                        → Filtro de rango (bool)
            cross_high, cross_low              → Máximo/mínimo del cruce
            cross_bars                         → Velas desde último cruce
            direccion_activa                   → 1=alcista, -1=bajista
            bull_hist_progressive              → Histograma progresivo bull
            bear_hist_progressive              → Histograma progresivo bear
            fast_bull_impulse                  → Impulso rápido alcista
            fast_bear_impulse                  → Impulso rápido bajista
            bull_break, bear_break             → Ruptura del muro
            buy_signal_raw                     → Señal buy sin cooldown
            sell_signal_raw                    → Señal sell sin cooldown
            buy, sell                          → plots finales independientes de TOS
            omitida_*                         → plots omitidos independientes de TOS
            señal                              → vista compacta BUY/SELL/OM-R/OM-T/None
        """
        df = df.copy()

        # Paso 1: Calcular MACD
        df = self._calcular_macd(df)

        # Paso 2: Calcular ATR (método Wilder)
        df = self._calcular_atr(df)

        # Paso 3: Calcular tendencia EMA
        df = self._calcular_ema_tendencia(df)

        # Paso 4: Memoria del cruce
        df = self._calcular_memoria_cruce(df)

        # Paso 5: Filtro de rango / compresión
        df = self._calcular_filtro_rango(df)

        # Paso 6: Fuerza del histograma
        df = self._calcular_histograma(df)

        # Paso 7: Calidad de vela / impulso
        df = self._calcular_impulso_vela(df)

        # Paso 8: Ruptura del muro
        df = self._calcular_breakout(df)

        # Paso 9: Señales base y filtros de contexto
        df = self._calcular_señales_base(df)

        # Paso 10: Cooldown y señales finales
        df = self._aplicar_cooldown(df)

        return df

    # ==========================================================
    # PASO 1 — MACD
    # ThinkScript equivalente:
    #   def fastEMA = ExpAverage(close, fast_length);
    #   def slowEMA = ExpAverage(close, slow_length);
    #   def macdLine = fastEMA - slowEMA;
    #   def signalLine = ExpAverage(macdLine, signal_length);
    #   def histLine = macdLine - signalLine;
    # ==========================================================

    def _calcular_macd(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Calcula las tres líneas del MACD:
          - macd_line:   EMA(close, 12) - EMA(close, 26)
          - signal_line: EMA(macd_line, 9)
          - hist_line:   macd_line - signal_line

        También detecta los cruces alcistas y bajistas de MACD.
        """
        fast_ema = df['close'].ewm(span=self.fast_length, adjust=False).mean()
        slow_ema = df['close'].ewm(span=self.slow_length, adjust=False).mean()

        df['macd_line'] = fast_ema - slow_ema
        df['signal_line'] = df['macd_line'].ewm(span=self.signal_length, adjust=False).mean()
        df['hist_line'] = df['macd_line'] - df['signal_line']
        # Contexto causal para el filtro IA. No participa en las ecuaciones
        # BUY/SELL; solo describe cómo progresa el histograma en la vela cerrada.
        df['hist_slope'] = df['hist_line'] - df['hist_line'].shift(1)

        # Detección de cruces
        # bull_cross: MACD cruza por encima de la línea de señal
        # bear_cross: MACD cruza por debajo de la línea de señal
        df['bull_cross'] = (
            (df['macd_line'] > df['signal_line']) &
            (df['macd_line'].shift(1) <= df['signal_line'].shift(1))
        )
        df['bear_cross'] = (
            (df['macd_line'] < df['signal_line']) &
            (df['macd_line'].shift(1) >= df['signal_line'].shift(1))
        )

        return df

    # ==========================================================
    # PASO 2 — ATR (Método Wilder)
    # ThinkScript equivalente:
    #   def tr = TrueRange(high, close, low);
    #   def atr = MovingAverage(AverageType.WILDERS, tr, 14);
    # ==========================================================

    def _calcular_atr(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Calcula el ATR (Average True Range) usando el método de Wilder.

        True Range = max(high-low, |high-close_prev|, |low-close_prev|)
        ATR Wilder = Media exponencial especial con factor 1/periodo
        """
        high = df['high']
        low = df['low']
        prev_close = df['close'].shift(1)

        # True Range: el mayor de los tres valores
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # ATR Wilder: media exponencial con alpha = 1/periodo
        df['atr'] = true_range.ewm(alpha=1.0 / self.atr_period, adjust=False).mean()
        # Régimen de volatilidad causal frente a las 100 velas disponibles hasta
        # el instante actual. No usa centered windows ni datos futuros.
        atr_mediana = df['atr'].rolling(100, min_periods=20).median()
        df['atr_regime_100'] = df['atr'] / atr_mediana.replace(0, np.nan)

        return df

    # ==========================================================
    # PASO 3 — TENDENCIA EMA
    # ThinkScript equivalente:
    #   def emaTrend = ExpAverage(close, emaTrendLength);
    #   def emaSlope = emaTrend - emaTrend[1];
    #   def minSlope = atr * emaSlopeFactor;
    #   def emaSubiendoFuerte = emaSlope > minSlope;
    #   def emaBajandoFuerte = emaSlope < -minSlope;
    # ==========================================================

    def _calcular_ema_tendencia(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Calcula la EMA de tendencia (período 11) y su pendiente.

        La pendiente debe superar un umbral mínimo basado en ATR
        para confirmar que la tendencia es lo suficientemente fuerte.

        ema_subiendo_fuerte = True cuando la pendiente es > ATR × 0.02
        ema_bajando_fuerte  = True cuando la pendiente es < -(ATR × 0.02)
        """
        df['ema_trend'] = df['close'].ewm(span=self.ema_trend_length, adjust=False).mean()
        df['ema_slope'] = df['ema_trend'] - df['ema_trend'].shift(1)
        df['min_slope'] = df['atr'] * self.ema_slope_factor

        df['ema_subiendo_fuerte'] = df['ema_slope'] > df['min_slope']
        df['ema_bajando_fuerte'] = df['ema_slope'] < -df['min_slope']

        return df

    # ==========================================================
    # PASO 4 — MEMORIA DEL CRUCE
    # ThinkScript equivalente:
    #   def crossHigh = if bullCross then high else crossHigh[1]
    #   def crossLow  = if bearCross then low  else crossLow[1]
    #   def crossBars = if bullCross or bearCross then 0 else crossBars[1]+1
    #   def direccionActiva = if bullCross then 1 else if bearCross then -1 else direccionActiva[1]
    # ==========================================================

    def _calcular_memoria_cruce(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Mantiene en memoria el estado del último cruce MACD:

        cross_high       → Máximo de la vela donde ocurrió el último cruce alcista
        cross_low        → Mínimo de la vela donde ocurrió el último cruce bajista
        cross_bars       → Cuántas velas han pasado desde el último cruce
        direccion_activa → Dirección del mercado: 1=alcista, -1=bajista
        """
        n = len(df)
        cross_high = np.full(n, np.nan)
        cross_low = np.full(n, np.nan)
        cross_bars = np.zeros(n, dtype=int)
        direccion = np.zeros(n, dtype=int)

        for i in range(n):
            if i == 0:
                cross_high[i] = df['high'].iloc[i]
                cross_low[i] = df['low'].iloc[i]
                cross_bars[i] = 0
                direccion[i] = 0
                continue

            bull = df['bull_cross'].iloc[i]
            bear = df['bear_cross'].iloc[i]

            # Actualizar cross_high cuando hay cruce alcista
            cross_high[i] = df['high'].iloc[i] if bull else cross_high[i - 1]
            # Actualizar cross_low cuando hay cruce bajista
            cross_low[i] = df['low'].iloc[i] if bear else cross_low[i - 1]

            # Reiniciar contador en cada cruce, incrementar en caso contrario
            cross_bars[i] = 0 if (bull or bear) else cross_bars[i - 1] + 1

            # Actualizar dirección activa
            if bull:
                direccion[i] = 1
            elif bear:
                direccion[i] = -1
            else:
                direccion[i] = direccion[i - 1]

        df['cross_high'] = cross_high
        df['cross_low'] = cross_low
        df['cross_bars'] = cross_bars
        df['direccion_activa'] = direccion

        return df

    # ==========================================================
    # PASO 5 — FILTRO DE RANGO / COMPRESIÓN
    # ThinkScript equivalente:
    #   def recentHigh = Highest(high[1], rangeLookback);
    #   def recentLow  = Lowest(low[1], rangeLookback);
    #   def recentRange = recentHigh - recentLow;
    #   def rangePct = (recentRange / close) * 100;
    #   def validRange = rangePct >= minRangePct;
    # ==========================================================

    def _calcular_filtro_rango(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Filtra señales en zonas de compresión (rango demasiado estrecho).

        Calcula el rango porcentual de las últimas 8 velas.
        Si el rango < 0.18% del precio actual → señal inválida (compresión/rango).

        valid_range = True  → Rango suficiente, señal puede operar
        valid_range = False → Zona de compresión, señal se omite (OM-R)
        """
        # Máximo y mínimo de las últimas N velas (excluyendo la vela actual)
        recent_high = df['high'].shift(1).rolling(window=self.range_lookback).max()
        recent_low = df['low'].shift(1).rolling(window=self.range_lookback).min()
        recent_range = recent_high - recent_low

        # Porcentaje del rango sobre el precio actual
        df['range_pct'] = (recent_range / df['close'].replace(0, np.nan)) * 100
        df['valid_range'] = df['range_pct'] >= self.min_range_pct

        return df

    # ==========================================================
    # PASO 6 — FUERZA DEL HISTOGRAMA
    # ThinkScript equivalente:
    #   def histThreshold = atr * histThresholdFactor;
    #   def bullHistProgressive = histLine > histLine[1] > histLine[2]
    #   def bearHistProgressive = histLine < histLine[1] < histLine[2]
    #   def bullHistStrong = histLine > histLine[1] and AbsValue(histLine) > histThreshold
    #   def bearHistStrong = histLine < histLine[1] and AbsValue(histLine) > histThreshold
    # ==========================================================

    def _calcular_histograma(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Mide la fuerza y dirección del histograma MACD.

        Histograma Progresivo: 3 velas consecutivas moviéndose en la misma dirección
        → Señal de mayor calidad (confirmación normal).

        Histograma Fuerte: Histograma moviéndose en una dirección y superando el umbral ATR
        → Necesario para entrada rápida (fast entry).
        """
        hist = df['hist_line']
        df['hist_threshold'] = df['atr'] * self.hist_threshold_factor

        # Progresivo: 3 velas consecutivas al alza o a la baja
        df['bull_hist_progressive'] = (hist > hist.shift(1)) & (hist.shift(1) > hist.shift(2))
        df['bear_hist_progressive'] = (hist < hist.shift(1)) & (hist.shift(1) < hist.shift(2))

        # Fuerte: en movimiento y superando el umbral
        df['bull_hist_strong'] = (hist > hist.shift(1)) & (hist.abs() > df['hist_threshold'])
        df['bear_hist_strong'] = (hist < hist.shift(1)) & (hist.abs() > df['hist_threshold'])

        return df

    # ==========================================================
    # PASO 7 — CALIDAD DE VELA / IMPULSO
    # ThinkScript equivalente:
    #   def candleBody = AbsValue(close - open);
    #   def strongBody = candleBody > (atr * impulseBodyFactor);
    #   def bullCloseStrong = close > open and close >= high - ((high-low)*0.25)
    #   def bearCloseStrong = close < open and close <= low + ((high-low)*0.25)
    # ==========================================================

    def _calcular_impulso_vela(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Evalúa la calidad de la vela para confirmar impulso real.

        Una vela de impulso válida debe tener:
          1. Cuerpo grande (> 60% del ATR)
          2. Cierre dominante (alcista: cierra en el 75% superior | bajista: 75% inferior)

        fast_bull_impulse = True → Vela de impulso alcista válida
        fast_bear_impulse = True → Vela de impulso bajista válida
        """
        candle_body = (df['close'] - df['open']).abs()
        candle_range = df['high'] - df['low']

        # Cuerpo grande suficiente
        strong_body = candle_body > (df['atr'] * self.impulse_body_factor)

        # Cierre dominante: alcista cierra en cuarto superior, bajista en cuarto inferior
        bull_close_strong = (df['close'] > df['open']) & \
                            (df['close'] >= df['high'] - candle_range * 0.25)
        bear_close_strong = (df['close'] < df['open']) & \
                            (df['close'] <= df['low'] + candle_range * 0.25)

        df['fast_bull_impulse'] = strong_body & bull_close_strong & df['bull_hist_strong']
        df['fast_bear_impulse'] = strong_body & bear_close_strong & df['bear_hist_strong']

        return df

    # ==========================================================
    # PASO 8 — RUPTURA DEL MURO (BREAKOUT)
    # ThinkScript equivalente:
    #   def breakoutBuffer = atr * breakoutBufferFactor;
    #   def bullBreak = close > crossHigh + breakoutBuffer;
    #   def bearBreak = close < crossLow  - breakoutBuffer;
    # ==========================================================

    def _calcular_breakout(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Confirma que el precio ha roto el nivel de resistencia/soporte
        establecido en el último cruce MACD.

        bull_break = True → Precio superó el máximo del cruce alcista + buffer
        bear_break = True → Precio cayó bajo el mínimo del cruce bajista - buffer

        El buffer (ATR × 0.02) evita falsas rupturas por ruido de mercado.
        """
        breakout_buffer = df['atr'] * self.breakout_buffer_factor

        df['breakout_buffer'] = breakout_buffer
        df['bull_break'] = df['close'] > (df['cross_high'] + breakout_buffer)
        df['bear_break'] = df['close'] < (df['cross_low'] - breakout_buffer)

        return df

    # ==========================================================
    # PASO 9 — SEÑALES BASE Y FILTROS DE CONTEXTO
    # ThinkScript equivalente:
    #   def normalBullConfirm = direccionActiva[1]==1 and crossBars>=2 and bullHistProgressive and bullBreak
    #   def fastBullConfirm   = allowFastEntry and direccionActiva[1]==1 and crossBars>=1 and fastBullImpulse and bullBreak
    #   def buySignalRaw  = (normalBullConfirm or fastBullConfirm) and emaSubiendoFuerte and validRange
    # ==========================================================

    def _calcular_señales_base(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Combina todos los filtros para generar señales base (sin cooldown).

        Existen dos tipos de confirmación para cada dirección:
          → Normal: espera 2+ velas desde el cruce, histograma progresivo + breakout
          → Rápida: espera 1+ vela, pero requiere vela de impulso fuerte

        Las señales raw aún no tienen el filtro de cooldown aplicado.
        """
        dir_prev = df['direccion_activa'].shift(1)
        bars = df['cross_bars']

        # --- Confirmaciones alcistas ---
        normal_bull = (
            (dir_prev == 1) &
            (bars >= 2) &
            df['bull_hist_progressive'] &
            df['bull_break']
        )
        fast_bull = pd.Series(False, index=df.index)
        if self.allow_fast_entry:
            fast_bull = (
                (dir_prev == 1) &
                (bars >= 1) &
                df['fast_bull_impulse'] &
                df['bull_break']
            )

        # --- Confirmaciones bajistas ---
        normal_bear = (
            (dir_prev == -1) &
            (bars >= 2) &
            df['bear_hist_progressive'] &
            df['bear_break']
        )
        fast_bear = pd.Series(False, index=df.index)
        if self.allow_fast_entry:
            fast_bear = (
                (dir_prev == -1) &
                (bars >= 1) &
                df['fast_bear_impulse'] &
                df['bear_break']
            )

        # Se exportan por separado para que la IA sepa si una señal maestra fue
        # normal o rápida. Estos plots auxiliares no cambian la lógica GALA10.
        df['normal_bull_confirm'] = normal_bull
        df['normal_bear_confirm'] = normal_bear
        df['fast_bull_confirm'] = fast_bull
        df['fast_bear_confirm'] = fast_bear

        # Señal disparadora base
        df['disparo_buy_base'] = normal_bull | fast_bull
        df['disparo_sell_base'] = normal_bear | fast_bear

        # Aplicar filtros de contexto: tendencia EMA + rango válido
        df['buy_signal_raw'] = df['disparo_buy_base'] & df['ema_subiendo_fuerte'] & df['valid_range']
        df['sell_signal_raw'] = df['disparo_sell_base'] & df['ema_bajando_fuerte'] & df['valid_range']

        # Señales omitidas (para logs y análisis)
        df['omitida_buy_rango'] = df['disparo_buy_base'] & ~df['valid_range']
        df['omitida_sell_rango'] = df['disparo_sell_base'] & ~df['valid_range']
        df['omitida_buy_tendencia'] = df['disparo_buy_base'] & df['valid_range'] & ~df['ema_subiendo_fuerte']
        df['omitida_sell_tendencia'] = df['disparo_sell_base'] & df['valid_range'] & ~df['ema_bajando_fuerte']

        return df

    # ==========================================================
    # PASO 10 — COOLDOWN DE 8 VELAS
    # ThinkScript equivalente:
    #   def barrasDesdeUltimoBuy  = if buySignalRaw[1] then 1 else barrasDesdeUltimoBuy[1]+1
    #   def barrasDesdeUltimoSell = if sellSignalRaw[1] then 1 else barrasDesdeUltimoSell[1]+1
    #   plot Buy  = buySignalRaw  and barrasDesdeUltimoBuy  > cooldownBars;
    #   plot Sell = sellSignalRaw and barrasDesdeUltimoSell > cooldownBars;
    # ==========================================================

    def _aplicar_cooldown(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Aplica el sistema de bloqueo de 8 velas (cooldown).

        El contador se reinicia con CADA señal raw de la dirección, incluso
        si esa señal raw fue suprimida por el propio cooldown. Es exactamente
        la recursión del ThinkScript recibido:
            if buySignalRaw[1] then 1 else contador[1] + 1

        Esto evita señales repetidas en la misma zona de precio y
        protege contra el sobre-trading.

        Columna final 'señal':
          'BUY'  → Señal de compra válida
          'SELL' → Señal de venta válida
          'OM-R' → Señal omitida por rango insuficiente
          'OM-T' → Señal omitida por tendencia insuficiente
          None   → Sin señal
        """
        n = len(df)
        barras_buy = np.full(n, 100)   # Inicializar con valor alto
        barras_sell = np.full(n, 100)
        señal = [None] * n
        buy = np.zeros(n, dtype=bool)
        sell = np.zeros(n, dtype=bool)

        for i in range(n):
            if i == 0:
                barras_buy[i] = 100
                barras_sell[i] = 100
            else:
                # Contador buy: reinicia a 1 si hubo señal raw en vela anterior
                barras_buy[i] = 1 if df['buy_signal_raw'].iloc[i - 1] else barras_buy[i - 1] + 1
                # Contador sell: reinicia a 1 si hubo señal raw en vela anterior
                barras_sell[i] = 1 if df['sell_signal_raw'].iloc[i - 1] else barras_sell[i - 1] + 1

            # Señal BUY válida: señal raw activa Y han pasado más de N velas
            buy_final = bool(
                df['buy_signal_raw'].iloc[i]
                and barras_buy[i] > self.cooldown_bars
            )
            sell_final = bool(
                df['sell_signal_raw'].iloc[i]
                and barras_sell[i] > self.cooldown_bars
            )
            buy[i] = buy_final
            sell[i] = sell_final

            # Asignar señal final con prioridad: BUY > SELL > OM-R > OM-T > None
            if buy_final:
                señal[i] = 'BUY'
            elif sell_final:
                señal[i] = 'SELL'
            elif df['omitida_buy_rango'].iloc[i] or df['omitida_sell_rango'].iloc[i]:
                señal[i] = 'OM-R'
            elif df['omitida_buy_tendencia'].iloc[i] or df['omitida_sell_tendencia'].iloc[i]:
                señal[i] = 'OM-T'

        df['barras_desde_buy'] = barras_buy
        df['barras_desde_sell'] = barras_sell
        # Mantener los plots separados permite comparar uno-a-uno con el
        # ThinkScript. La columna compacta "señal" se conserva por compatibilidad
        # con el motor y los reportes existentes.
        df['buy'] = buy
        df['sell'] = sell
        df['señal'] = señal

        return df


# Compatibilidad con el resto del proyecto y con integraciones existentes.
# El nombre canónico nuevo es GALA10Strategy; no se duplica ni altera la lógica.
GALA6Strategy = GALA10Strategy
