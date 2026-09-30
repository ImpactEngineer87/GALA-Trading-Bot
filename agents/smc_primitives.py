"""
=============================================================
MÓDULO: agents/smc_primitives.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Primitivas de cálculo Smart Money Concepts (SMC) compartidas
    por los agentes 1 y 2. Centralizar estas funciones garantiza
    que todos los agentes usan EXACTAMENTE la misma definición de
    FVG, liquidez, swings y estructura.

    PRINCIPIO CLAVE — SIN LOOK-AHEAD (sin mirar el futuro):
        Todas las funciones usan únicamente velas ya cerradas.
        Un swing solo se confirma cuando ya pasaron 'derecha' velas
        después de él; un FVG solo se evalúa como mitigado con velas
        posteriores reales. Ninguna función usa información que no
        estaría disponible en tiempo real en la vela actual.
=============================================================
"""

from datetime import datetime
from typing import List, Optional

import numpy as np
import pandas as pd

from agents.contracts import NivelLiquidez, FairValueGap


# =============================================================
# ATR (reutiliza la definición Wilder de core/strategy.py)
# =============================================================

def atr_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    ATR de Wilder. Idéntico a core/strategy.py para consistencia
    entre la estrategia matemática y los agentes SMC.
    """
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(alpha=1.0 / period, adjust=False).mean()


def atr_relativo_actual(df: pd.DataFrame, period: int = 14) -> float:
    """ATR de la última vela como porcentaje del precio de cierre."""
    if df is None or len(df) < period + 1:
        return 0.0
    atr = atr_wilder(df, period)
    precio = float(df["close"].iloc[-1])
    val = float(atr.iloc[-1])
    if precio <= 0:
        return 0.0
    return val / precio * 100.0


# =============================================================
# FAIR VALUE GAPS (FVG)
# =============================================================

def detectar_fvg(df: pd.DataFrame, tf: str,
                 solo_direccion: Optional[str] = None) -> List[FairValueGap]:
    """
    Detecta todas las Brechas de Valor Justo (FVG) de 3 velas.

    FVG alcista  → low[i] > high[i-2]   (hueco de impulso al alza)
    FVG bajista  → high[i] < low[i-2]   (hueco de impulso a la baja)

    Parámetros:
        df             → DataFrame OHLCV (velas cerradas)
        tf             → etiqueta de temporalidad ("15m", "5m", ...)
        solo_direccion → "alcista", "bajista" o None (ambas)

    Retorna FVGs ordenados de más antiguo a más reciente.
    """
    out: List[FairValueGap] = []
    if df is None or len(df) < 3:
        return out

    highs = df["high"].values
    lows = df["low"].values
    idx = df.index

    for i in range(2, len(df)):
        # --- FVG alcista: la vela i deja un hueco por encima de la i-2 ---
        if lows[i] > highs[i - 2]:
            if solo_direccion in (None, "alcista"):
                out.append(FairValueGap(
                    tf=tf, direccion="alcista",
                    fondo=float(highs[i - 2]), tope=float(lows[i]),
                    indice=i - 1,
                    timestamp=_as_dt(idx[i - 1]),
                ))
        # --- FVG bajista: la vela i deja un hueco por debajo de la i-2 ---
        elif highs[i] < lows[i - 2]:
            if solo_direccion in (None, "bajista"):
                out.append(FairValueGap(
                    tf=tf, direccion="bajista",
                    fondo=float(highs[i]), tope=float(lows[i - 2]),
                    indice=i - 1,
                    timestamp=_as_dt(idx[i - 1]),
                ))
    return out


def marcar_fvg_mitigados(fvgs: List[FairValueGap], df: pd.DataFrame) -> List[FairValueGap]:
    """
    Marca como mitigado cualquier FVG en el que el precio haya vuelto
    a entrar DESPUÉS de que el patrón de 3 velas se completara.

    El patrón ocupa las velas (indice-1, indice, indice+1); por eso
    solo se consideran velas a partir de indice+2 para la mitigación.
    """
    if df is None or len(df) == 0:
        return fvgs
    lows = df["low"].values
    highs = df["high"].values
    n = len(df)
    for g in fvgs:
        inicio = g.indice + 2
        for j in range(inicio, n):
            # Solapamiento entre el rango de la vela j y el hueco
            if lows[j] <= g.tope and highs[j] >= g.fondo:
                g.mitigado = True
                break
    return fvgs


def fvgs_validos(df: pd.DataFrame, tf: str, tipo_operacion: str,
                 atr_val: float, max_edad_velas: int,
                 min_tamano_atr: float, en_formacion: bool = True) -> List[FairValueGap]:
    """
    Devuelve los FVG NO mitigados, con antigüedad y tamaño suficientes,
    alineados con el tipo de operación buscado.

    Un BUY busca FVGs alcistas (zonas de demanda debajo del precio);
    un SELL busca FVGs bajistas (zonas de oferta encima del precio).

    SIN LOOK-AHEAD: si 'en_formacion' es True, la última vela representa el
    periodo aún abierto (high/low todavía se mueven), por lo que se EXCLUYE
    de la detección — un FVG cuya vela de impulso es la vela en curso no
    existe todavía en tiempo real y no debe generar entradas.

    Parámetros:
        tipo_operacion → "BUY" o "SELL"
        atr_val        → ATR absoluto de la temporalidad (para filtrar por tamaño)
        max_edad_velas → antigüedad máxima aceptada (en velas)
        min_tamano_atr → tamaño mínimo del hueco relativo al ATR
        en_formacion   → True si la última vela aún no ha cerrado

    Retorna los FVG válidos, el más reciente al final.
    """
    dir_smc = "alcista" if tipo_operacion == "BUY" else "bajista"
    # Trabajar solo sobre velas cerradas
    df_cerrado = df.iloc[:-1] if (en_formacion and len(df) > 3) else df
    gaps = detectar_fvg(df_cerrado, tf, dir_smc)
    gaps = marcar_fvg_mitigados(gaps, df_cerrado)
    n = len(df_cerrado)
    res: List[FairValueGap] = []
    for g in gaps:
        if g.mitigado:
            continue
        edad = (n - 1) - g.indice
        if edad > max_edad_velas:
            continue
        if atr_val > 0 and g.tamano < (min_tamano_atr * atr_val):
            continue
        res.append(g)
    return res


# =============================================================
# SWINGS Y LIQUIDEZ
# =============================================================

def swing_highs(df: pd.DataFrame, izquierda: int = 5, derecha: int = 5) -> List[int]:
    """
    Índices de swing highs CONFIRMADOS (fractales).

    Un swing high en i requiere que high[i] sea el máximo de la ventana
    [i-izquierda, i+derecha]. Solo se devuelven swings ya confirmados,
    es decir, con 'derecha' velas posteriores reales (sin look-ahead
    sobre la vela actual).
    """
    if df is None or len(df) < izquierda + derecha + 1:
        return []
    highs = df["high"].values
    n = len(highs)
    res = []
    for i in range(izquierda, n - derecha):
        ventana = highs[i - izquierda: i + derecha + 1]
        if highs[i] == ventana.max() and np.argmax(ventana) == izquierda:
            res.append(i)
    return res


def swing_lows(df: pd.DataFrame, izquierda: int = 5, derecha: int = 5) -> List[int]:
    """Índices de swing lows CONFIRMADOS (fractales). Ver swing_highs."""
    if df is None or len(df) < izquierda + derecha + 1:
        return []
    lows = df["low"].values
    n = len(lows)
    res = []
    for i in range(izquierda, n - derecha):
        ventana = lows[i - izquierda: i + derecha + 1]
        if lows[i] == ventana.min() and np.argmin(ventana) == izquierda:
            res.append(i)
    return res


def niveles_swing_liquidez(df: pd.DataFrame, lookback: int = 5,
                           max_niveles: int = 3) -> List[NivelLiquidez]:
    """
    Extrae los swing highs/lows más recientes como pools de liquidez
    (SWH = liquidez de compradores arriba, SWL = liquidez de vendedores abajo).
    """
    niveles: List[NivelLiquidez] = []
    sh = swing_highs(df, lookback, lookback)
    sl = swing_lows(df, lookback, lookback)
    for i in sh[-max_niveles:]:
        niveles.append(NivelLiquidez("SWH", float(df["high"].iloc[i]), "resistencia"))
    for i in sl[-max_niveles:]:
        niveles.append(NivelLiquidez("SWL", float(df["low"].iloc[i]), "soporte"))
    return niveles


def equal_highs_lows(df: pd.DataFrame, lookback: int = 5,
                     tolerancia_pct: float = 0.05) -> List[NivelLiquidez]:
    """
    Detecta Equal Highs (EQH) y Equal Lows (EQL): dos o más swings
    al mismo nivel (dentro de 'tolerancia_pct'), que marcan liquidez
    agrupada — objetivos frecuentes de los movimientos institucionales.
    """
    niveles: List[NivelLiquidez] = []
    sh = swing_highs(df, lookback, lookback)
    sl = swing_lows(df, lookback, lookback)

    def _agrupar(indices, col, nombre, tipo):
        vals = [float(df[col].iloc[i]) for i in indices]
        usados = [False] * len(vals)
        for a in range(len(vals)):
            if usados[a]:
                continue
            for b in range(a + 1, len(vals)):
                if usados[b] or vals[a] <= 0:
                    continue
                if abs(vals[a] - vals[b]) / vals[a] * 100.0 <= tolerancia_pct:
                    nivel = (vals[a] + vals[b]) / 2.0
                    niveles.append(NivelLiquidez(nombre, nivel, tipo))
                    usados[a] = usados[b] = True
                    break

    _agrupar(sh, "high", "EQH", "resistencia")
    _agrupar(sl, "low", "EQL", "soporte")
    return niveles


# =============================================================
# NIVELES DE PERIODO PREVIO (PDH/PDL/PDC, PWH/PWL/PWC)
# =============================================================

def _fila_periodo_previo(df: pd.DataFrame, actual_en_formacion: bool) -> Optional[pd.Series]:
    """
    Devuelve la fila del último periodo COMPLETO.

    Si 'actual_en_formacion' es True, la última vela representa el
    periodo en curso (aún abierto), así que el periodo previo es iloc[-2].
    Si es False (mercado cerrado / sin vela en curso), el periodo
    previo es la última vela cerrada iloc[-1].
    """
    if df is None or len(df) == 0:
        return None
    if actual_en_formacion and len(df) >= 2:
        return df.iloc[-2]
    return df.iloc[-1]


def niveles_dia_previo(df_diario: pd.DataFrame,
                       actual_en_formacion: bool = True) -> List[NivelLiquidez]:
    """Previous Day High / Low / Close (PDH / PDL / PDC)."""
    fila = _fila_periodo_previo(df_diario, actual_en_formacion)
    if fila is None:
        return []
    return [
        NivelLiquidez("PDH", float(fila["high"]), "resistencia"),
        NivelLiquidez("PDL", float(fila["low"]), "soporte"),
        NivelLiquidez("PDC", float(fila["close"]), "referencia"),
    ]


def niveles_semana_previa(df_semanal: pd.DataFrame,
                          actual_en_formacion: bool = True) -> List[NivelLiquidez]:
    """Previous Week High / Low / Close (PWH / PWL / PWC)."""
    fila = _fila_periodo_previo(df_semanal, actual_en_formacion)
    if fila is None:
        return []
    return [
        NivelLiquidez("PWH", float(fila["high"]), "resistencia"),
        NivelLiquidez("PWL", float(fila["low"]), "soporte"),
        NivelLiquidez("PWC", float(fila["close"]), "referencia"),
    ]


def hueco_apertura_pct(df_diario: pd.DataFrame,
                       actual_en_formacion: bool = True) -> Optional[float]:
    """
    Brecha de apertura: apertura del periodo actual vs cierre del previo,
    en porcentaje. Positivo = gap up, negativo = gap down.
    """
    if df_diario is None or len(df_diario) < 2:
        return None
    actual = df_diario.iloc[-1]
    previo = df_diario.iloc[-2]
    if float(previo["close"]) <= 0:
        return None
    return (float(actual["open"]) - float(previo["close"])) / float(previo["close"]) * 100.0


# =============================================================
# SESGO DE ESTRUCTURA (para el Agente 1)
# =============================================================

def sesgo_estructura(df: pd.DataFrame, lookback: int = 5) -> float:
    """
    Puntúa la estructura de mercado de una temporalidad en [-1, 1].

    +1 → estructura claramente alcista (Higher Highs + Higher Lows)
    -1 → estructura claramente bajista (Lower Highs + Lower Lows)
     0 → sin sesgo definido

    Combina tres señales: relación entre los dos últimos swing highs,
    entre los dos últimos swing lows, y la pendiente de una EMA.
    """
    if df is None or len(df) < (lookback * 2 + 2):
        return 0.0

    puntajes = []

    sh = swing_highs(df, lookback, lookback)
    if len(sh) >= 2:
        puntajes.append(1.0 if df["high"].iloc[sh[-1]] > df["high"].iloc[sh[-2]] else -1.0)

    sl = swing_lows(df, lookback, lookback)
    if len(sl) >= 2:
        puntajes.append(1.0 if df["low"].iloc[sl[-1]] > df["low"].iloc[sl[-2]] else -1.0)

    span = min(20, max(5, len(df) // 3))
    ema = df["close"].ewm(span=span, adjust=False).mean()
    if len(ema) >= 3:
        pendiente = float(ema.iloc[-1]) - float(ema.iloc[-3])
        if pendiente > 0:
            puntajes.append(1.0)
        elif pendiente < 0:
            puntajes.append(-1.0)
        else:
            puntajes.append(0.0)

    if not puntajes:
        return 0.0
    return float(np.mean(puntajes))


# =============================================================
# UTILIDADES
# =============================================================

def marcar_niveles_por_precio(niveles: List[NivelLiquidez],
                              precio_actual: float) -> List[NivelLiquidez]:
    """
    Reclasifica cada nivel como 'resistencia' (por encima del precio)
    o 'soporte' (por debajo), respetando la etiqueta 'referencia'.
    """
    for n in niveles:
        if n.tipo == "referencia":
            continue
        n.tipo = "resistencia" if n.precio >= precio_actual else "soporte"
    return niveles


def objetivo_liquidez_mas_cercano(niveles: List[NivelLiquidez], precio_actual: float,
                                  tipo_operacion: str) -> Optional[float]:
    """
    Elige el nivel de liquidez objetivo más cercano en la dirección de
    la operación: para un BUY, la resistencia inmediata superior; para
    un SELL, el soporte inmediato inferior.
    """
    if not niveles or precio_actual <= 0:
        return None
    if tipo_operacion == "BUY":
        arriba = [n.precio for n in niveles if n.precio > precio_actual]
        return min(arriba) if arriba else None
    else:
        abajo = [n.precio for n in niveles if n.precio < precio_actual]
        return max(abajo) if abajo else None


def _as_dt(valor) -> datetime:
    """Convierte un índice temporal de pandas a datetime nativo de forma segura."""
    try:
        if isinstance(valor, datetime):
            return valor
        return pd.Timestamp(valor).to_pydatetime()
    except Exception:
        return datetime(1970, 1, 1)
