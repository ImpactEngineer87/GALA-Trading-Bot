"""
=============================================================
MÓDULO: pipeline/features.py  (C3.2 del PLAN_DE_CONSTRUCCION)
PROYECTO: GALA6 Chocoletra Bot — canalización de datos
=============================================================
DESCRIPCIÓN:
    Genera la "ficha" (vector de features) por barra a partir del histórico
    Parquet. Reutiliza los indicadores de core.strategy.GALA6Strategy (el MISMO
    cálculo que opera en vivo) y añade features de volumen, volatilidad y hora.

    TODAS las features son CAUSALES (solo usan pasado): MACD/EMA/ATR son
    recursivas hacia atrás, y las medias/retornos usan ventanas cerradas en la
    barra actual. Sin look-ahead.

    Features producidas (una fila por barra RTH):
      Precio/tendencia:  macd_line, signal_line, hist_line, ema_trend_dist
      Volatilidad:       atr, atr_rel (atr/close*100)
      Volumen:           vol_sma20 (volumen / media 20), vol_z (z-score 20)
      Momento:           ret_1, ret_5, ret_15 (retornos pasados)
      Contexto:          rango_pct (rango de las últimas N velas)
      Tiempo:            minute_of_day (ET), es_apertura, es_cierre
      Señal cruda:       senal (BUY/SELL/OM-R/OM-T/None de la estrategia)

    Salida: <out>/ticker=<SYM>/data.parquet

USO:
    python -m pipeline.features
    python -m pipeline.features --tickers NVDA TSLA
=============================================================
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
_V2 = _REPO.parent
DEFAULT_IN = _V2 / "dataset" / "historico_1min_parquet"
DEFAULT_OUT = _V2 / "dataset" / "features"

from core.strategy import GALA6Strategy   # noqa: E402

FEATURE_COLS = [
    "macd_line", "signal_line", "hist_line", "ema_trend_dist",
    "atr", "atr_rel", "vol_sma20", "vol_z",
    "ret_1", "ret_5", "ret_15", "rango_pct",
    "minute_of_day", "es_apertura", "es_cierre",
]


def resample_intradia(df: pd.DataFrame, regla: str) -> pd.DataFrame:
    """
    Re-muestrea velas de 1-min a una temporalidad mayor (ej. '5min', '15min').
    Solo sesión regular (RTH). Los buckets nocturnos quedan vacíos (calendario
    fijo) → nunca se mezclan datos a través del cierre. Recalcula las banderas.
    """
    d = df[df["is_rth"]].copy().set_index("datetime").sort_index()
    agg = d.resample(regla, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open", "high", "low", "close"]).reset_index()
    agg["ticker"] = df["ticker"].iloc[0] if "ticker" in df else ""
    dia = agg["datetime"].dt.date
    agg["new_session"] = dia.ne(dia.shift(1)).fillna(True)
    m = agg["datetime"].dt.hour * 60 + agg["datetime"].dt.minute - 4 * 60
    agg["minute_of_day"] = (m % (24 * 60)).astype(int)
    agg["is_rth"] = True
    return agg


def construir_features(df: pd.DataFrame, estrategia: GALA6Strategy,
                       rango_lookback: int = 8) -> pd.DataFrame:
    """Devuelve el DataFrame con indicadores de estrategia + features añadidas."""
    calc = estrategia.calcular(df.copy())

    close = calc["close"]
    # Volatilidad relativa
    calc["atr_rel"] = (calc["atr"] / close * 100).replace([np.inf, -np.inf], np.nan)
    # Distancia a la EMA de tendencia (si la estrategia la expone)
    ema_col = next((c for c in calc.columns if "ema" in c and "trend" in c.lower()), None)
    if ema_col is None:
        ema_col = next((c for c in calc.columns if c.startswith("ema")), None)
    calc["ema_trend_dist"] = ((close - calc[ema_col]) / close * 100) if ema_col else 0.0
    # Volumen relativo (causal: media/desv de las 20 anteriores, sin incluir la actual)
    vol = calc["volume"]
    vol_media = vol.rolling(20, min_periods=5).mean().shift(1)
    vol_std = vol.rolling(20, min_periods=5).std().shift(1)
    calc["vol_sma20"] = (vol / vol_media).replace([np.inf, -np.inf], np.nan)
    calc["vol_z"] = ((vol - vol_media) / vol_std).replace([np.inf, -np.inf], np.nan)
    # Retornos pasados
    calc["ret_1"] = close.pct_change(1)
    calc["ret_5"] = close.pct_change(5)
    calc["ret_15"] = close.pct_change(15)
    # Rango de las últimas N velas (%)
    hi = calc["high"].rolling(rango_lookback, min_periods=2).max()
    lo = calc["low"].rolling(rango_lookback, min_periods=2).min()
    calc["rango_pct"] = ((hi - lo) / close * 100).replace([np.inf, -np.inf], np.nan)
    # Tiempo (minute_of_day ya viene del ingest; marcar apertura/cierre)
    if "minute_of_day" not in calc.columns:
        m = calc["datetime"].dt.hour * 60 + calc["datetime"].dt.minute - 4 * 60
        calc["minute_of_day"] = m % (24 * 60)
    calc["es_apertura"] = (calc["minute_of_day"] < 570 + 30).astype(int)   # primeros 30 min RTH
    calc["es_cierre"] = (calc["minute_of_day"] >= 960 - 30).astype(int)    # últimos 30 min RTH

    # Renombrar la columna de señal para el pipeline
    calc = calc.rename(columns={"señal": "senal"})
    return calc


def main():
    ap = argparse.ArgumentParser(description="Genera features por barra")
    ap.add_argument("--in", dest="entrada", default=str(DEFAULT_IN))
    ap.add_argument("--out", dest="salida", default=str(DEFAULT_OUT))
    ap.add_argument("--tickers", nargs="*", default=None)
    ap.add_argument("--resample", default=None, help="Re-muestrear a esta temporalidad (ej. 5min, 15min)")
    ap.add_argument("--config", default=str(_REPO / "config.yaml"))
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    estrategia = GALA6Strategy(config)
    rango_lb = config.get("strategy", {}).get("range_lookback", 8)

    parts = sorted(glob.glob(str(Path(args.entrada) / "ticker=*")))
    if args.tickers:
        want = {t.upper() for t in args.tickers}
        parts = [p for p in parts if Path(p).name.split("=")[1] in want]
    if not parts:
        raise SystemExit(f"[ERROR] Sin datos en {args.entrada}")

    salida = Path(args.salida); salida.mkdir(parents=True, exist_ok=True)
    print(f"[features] {len(parts)} tickers → {salida}")
    base_cols = ["datetime", "ticker", "open", "high", "low", "close", "volume",
                 "is_rth", "new_session", "senal"]
    total = 0
    for p in parts:
        ticker = Path(p).name.split("=")[1]
        df = pd.read_parquet(Path(p) / "data.parquet")
        if args.resample:
            df = resample_intradia(df, args.resample)
        feats = construir_features(df, estrategia, rango_lb)
        feats = feats[feats["is_rth"]].copy()
        cols = [c for c in base_cols if c in feats.columns] + FEATURE_COLS
        out = feats[cols].reset_index(drop=True)
        dest = salida / f"ticker={ticker}"; dest.mkdir(parents=True, exist_ok=True)
        out.to_parquet(dest / "data.parquet", engine="pyarrow", compression="snappy", index=False)
        n_sig = int(out["senal"].isin(["BUY", "SELL"]).sum())
        total += len(out)
        print(f"  [ok] {ticker:5} {len(out):>7,} barras | {n_sig:>5} señales BUY/SELL "
              f"| NaN en features: {int(out[FEATURE_COLS].isna().any(axis=1).sum())}")
    print(f"[features] LISTO: {total:,} filas de features")


if __name__ == "__main__":
    main()
