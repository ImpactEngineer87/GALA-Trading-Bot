"""
=============================================================
MÓDULO: agents/historico_loader.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Carga histórico de MINUTOS EN BLOQUE desde archivos CSV locales
    (p. ej. FirstRate Data) para PRE-ENTRENAR los modelos por acción con
    ~2 años de datos, en lugar de los ~8 días que da Schwab por API.

    Por qué: con 8 días la IA no ve suficientes ciclos, rangos ni trampas,
    y en real se tragaría señales falsas. Con 2 años de velas de 1m tiene
    una base sólida ANTES de operar con dinero real.

    Uso:
        - Poné un CSV por acción en data/historico/ (config: ai.historico_dir).
          Nombres aceptados: AAPL.csv, AAPL_1min.csv, AAPL_1minute.csv, ...
        - Formato típico (FirstRate): fecha/hora, open, high, low, close, volume.
          Se autodetectan las columnas y se acepta con o sin cabecera.
        - Se asume hora de Nueva York (ET) en las marcas de tiempo (config:
          ai.historico_tz), y se normaliza a UTC-naive igual que los datos de
          Schwab, para que TODO el pipeline (incluida la feature 'hora' en ET)
          funcione idéntico.

    El pre-entrenamiento usa este histórico si existe el archivo; si no, cae
    a Schwab automáticamente.
=============================================================
"""

import os
import glob
from typing import Optional

import numpy as np
import pandas as pd


# Temporalidades que construimos (mismas claves que obtener_multitemporal).
# Todas se derivan por remuestreo del 1m (una sola fuente = coherencia total).
_REMUESTREOS = {
    "5m": "5min", "15m": "15min", "1H": "1h", "4H": "4h", "1D": "1D", "1W": "1W",
}
_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}

_COLS = {
    "open":   ("open", "o", "apertura"),
    "high":   ("high", "h", "max", "maximo", "máximo"),
    "low":    ("low", "l", "min", "minimo", "mínimo"),
    "close":  ("close", "c", "cierre", "last"),
    "volume": ("volume", "vol", "v", "volumen"),
}


def _ruta_csv(ticker: str, carpeta: str) -> Optional[str]:
    """Busca el CSV del ticker con varios nombres posibles (sin distinguir may/min)."""
    if not os.path.isdir(carpeta):
        return None
    patrones = [f"{ticker}.csv", f"{ticker}_*.csv", f"{ticker}-*.csv"]
    for pat in patrones:
        for ruta in glob.glob(os.path.join(carpeta, pat)):
            return ruta
        # búsqueda sin distinguir mayúsculas
        for f in os.listdir(carpeta):
            base = f.lower()
            if base == f"{ticker.lower()}.csv" or base.startswith(f"{ticker.lower()}_") \
               or base.startswith(f"{ticker.lower()}-"):
                if base.endswith(".csv"):
                    return os.path.join(carpeta, f)
    return None


def _detectar_columna(df: pd.DataFrame, alias: tuple) -> Optional[str]:
    lower = {str(c).strip().lower(): c for c in df.columns}
    for a in alias:
        if a in lower:
            return lower[a]
    return None


def _leer_csv_1m(ruta: str, tz: str) -> Optional[pd.DataFrame]:
    """Lee el CSV, detecta columnas, normaliza a 1m OHLCV con índice UTC-naive."""
    # Intento 1: con cabecera. Si las columnas OHLC no se detectan, reintenta sin cabecera.
    df = pd.read_csv(ruta)
    o = _detectar_columna(df, _COLS["open"])
    if o is None:
        df = pd.read_csv(ruta, header=None)
        # Formato FirstRate sin cabecera: datetime, open, high, low, close, volume
        n = df.shape[1]
        nombres = ["datetime", "open", "high", "low", "close", "volume"][:n]
        df.columns = nombres + list(df.columns[len(nombres):])

    col_o = _detectar_columna(df, _COLS["open"])
    col_h = _detectar_columna(df, _COLS["high"])
    col_l = _detectar_columna(df, _COLS["low"])
    col_c = _detectar_columna(df, _COLS["close"])
    col_v = _detectar_columna(df, _COLS["volume"])
    if not all([col_o, col_h, col_l, col_c]):
        return None

    # Columna(s) de fecha/hora
    col_dt = _detectar_columna(df, ("datetime", "timestamp", "date_time", "fecha_hora"))
    if col_dt is not None:
        idx = pd.to_datetime(df[col_dt], errors="coerce")
    else:
        col_fecha = _detectar_columna(df, ("date", "fecha", "day"))
        col_hora = _detectar_columna(df, ("time", "hora"))
        if col_fecha is not None and col_hora is not None:
            idx = pd.to_datetime(df[col_fecha].astype(str) + " " + df[col_hora].astype(str),
                                 errors="coerce")
        elif col_fecha is not None:
            idx = pd.to_datetime(df[col_fecha], errors="coerce")
        else:
            # última opción: primera columna
            idx = pd.to_datetime(df.iloc[:, 0], errors="coerce")

    out = pd.DataFrame({
        "open":  pd.to_numeric(df[col_o], errors="coerce"),
        "high":  pd.to_numeric(df[col_h], errors="coerce"),
        "low":   pd.to_numeric(df[col_l], errors="coerce"),
        "close": pd.to_numeric(df[col_c], errors="coerce"),
        "volume": pd.to_numeric(df[col_v], errors="coerce") if col_v else 0.0,
    })
    out.index = idx
    out = out[~out.index.isna()].dropna(subset=["open", "high", "low", "close"])
    if len(out) == 0:
        return None
    out = out[~out.index.duplicated(keep="first")].sort_index()

    # Normalizar zona horaria: las marcas vienen en ET → llevar a UTC-naive
    # (misma convención que Schwab, epoch-ms). Así la feature 'hora' (que el
    # backtest reconvierte a ET) queda idéntica a la operativa en vivo.
    try:
        if out.index.tz is None:
            out.index = out.index.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward")
        out.index = out.index.tz_convert("UTC").tz_localize(None)
        out = out[~out.index.isna()]
    except Exception:
        pass  # si ya viene sin tz o falla, se deja tal cual (mejor algo que nada)
    return out


def cargar_historico_local(ticker: str, config: dict):
    """
    Devuelve un DatosMultiTemporal construido desde el CSV histórico local del
    ticker (todas las temporalidades derivadas del 1m), o None si no hay archivo.
    """
    from agents.contracts import DatosMultiTemporal

    cfg_ai = (config or {}).get("ai", {})
    carpeta = cfg_ai.get("historico_dir", "data/historico")
    # Por defecto UTC: el descargador de Massive guarda en UTC. (Para FirstRate,
    # que viene en ET, poné historico_tz: "America/New_York" en config.yaml.)
    tz = cfg_ai.get("historico_tz", "UTC")

    ruta = _ruta_csv(ticker, carpeta)
    if ruta is None:
        return None
    try:
        m1 = _leer_csv_1m(ruta, tz)
    except Exception as e:
        print(f"[HISTORICO] {ticker}: error leyendo {ruta}: {e}")
        return None
    if m1 is None or len(m1) < 60:
        print(f"[HISTORICO] {ticker}: CSV sin datos utilizables ({ruta})")
        return None

    # La sesión forma parte de la definición de Gala10: usar un histórico RTH
    # cuando TOS calcula también pre/after-market cambia todas las recurrencias
    # (EMA/MACD/ATR/cross/cooldown). Fallar de forma explícita evita entrenar un
    # modelo aparentemente válido pero desalineado.
    incluir_extended = bool(
        (config or {}).get("market", {}).get(
            "include_extended_hours_data", False))
    try:
        idx_et = m1.index.tz_localize("UTC").tz_convert("America/New_York")
        es_rth = (
            (idx_et.weekday < 5)
            & (
                (idx_et.hour > 9)
                | ((idx_et.hour == 9) & (idx_et.minute >= 30))
            )
            & (idx_et.hour < 16)
        )
        tiene_extended = bool((~es_rth).any())
        if incluir_extended and not tiene_extended:
            print(
                f"[HISTORICO] {ticker}: CSV solo RTH, incompatible con "
                "Gala10/TOS extended. Re-descargue con "
                "ai.forzar_descarga=true; no se usará para entrenar."
            )
            return None
        if not incluir_extended and tiene_extended:
            m1 = m1[es_rth]
    except Exception as exc:
        print(f"[HISTORICO] {ticker}: no se pudo validar la sesión: {exc}")
        return None

    velas = {"1m": m1}
    for clave, regla in _REMUESTREOS.items():
        try:
            r = m1.resample(regla, label="right", closed="right").agg(_AGG).dropna(subset=["close"])
            if len(r):
                velas[clave] = r
        except Exception:
            continue

    dias = (m1.index[-1] - m1.index[0]).days if len(m1) > 1 else 0
    print(f"[HISTORICO] {ticker}: {len(m1)} velas 1m locales | cobertura ~{dias} días ({os.path.basename(ruta)})")
    return DatosMultiTemporal(ticker=ticker, velas=velas, sintetico=False)
