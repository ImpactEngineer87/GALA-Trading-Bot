"""
=============================================================
MÓDULO: pipeline/ingest.py  (C3.1 del PLAN_DE_CONSTRUCCION)
PROYECTO: GALA6 Chocoletra Bot — canalización de datos
=============================================================
DESCRIPCIÓN:
    Ingesta el histórico de 1 minuto (CSV OHLCV por ticker) y lo normaliza
    a Parquet particionado por ticker, listo para features/etiquetado/backtest.

    Qué hace, por columna añadida:
      - ticker          → símbolo (desde el nombre del fichero)
      - session         → "pre" | "rth" | "post"  (sesión de mercado, hora UTC)
      - is_rth          → True si la barra está en sesión regular (RTH)
      - minute_of_day   → minuto del día en ET (para features horarias)
      - new_session     → True en la PRIMERA barra de cada día de trading
                          (marca el gap nocturno: NO calcular retornos "a través" de él)
      - gap_pct         → variación vs. cierre anterior (0.0 en la primera barra del día)

    Los datos vienen en UTC (13:30 UTC = 09:30 ET, apertura regular). El volumen
    viene AJUSTADO por un proveedor (no entero) — se conserva tal cual y se anota
    en el manifiesto para que el backtest de ejecución lo tenga en cuenta.

    Salida:
      <out>/ticker=<SYM>/data.parquet   (uno por ticker)
      <out>/_manifest.json              (procedencia, rangos, conteos, avisos)

USO:
    python -m pipeline.ingest                      # rutas por defecto
    python -m pipeline.ingest --in <dir> --out <dir>
=============================================================
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# --- Rutas por defecto: V2/dataset/... (fuera del repo, no se versiona) ---
_REPO = Path(__file__).resolve().parents[1]      # .../V2/gala6-bot
_V2 = _REPO.parent                                 # .../V2
DEFAULT_IN = _V2 / "dataset" / "historico_1min_raw"
DEFAULT_OUT = _V2 / "dataset" / "historico_1min_parquet"

# --- Sesión de mercado en minutos desde medianoche UTC ---
# RTH (sesión regular): 09:30–16:00 ET = 13:30–20:00 UTC
RTH_INICIO_UTC = 13 * 60 + 30     # 13:30 UTC
RTH_FIN_UTC = 20 * 60             # 20:00 UTC
# Desfase ET respecto a UTC durante el horario de mercado (EDT = UTC-4).
# Suficiente para minute_of_day en features; el backtest usa el timestamp UTC crudo.
ET_OFFSET_MIN = 4 * 60

COLS = ["open", "high", "low", "close", "volume"]


def _clasificar_sesion(min_utc: pd.Series) -> pd.Series:
    """pre / rth / post según el minuto del día en UTC."""
    s = pd.Series("post", index=min_utc.index, dtype="object")
    s[min_utc < RTH_INICIO_UTC] = "pre"
    s[(min_utc >= RTH_INICIO_UTC) & (min_utc < RTH_FIN_UTC)] = "rth"
    return s


def procesar_ticker(csv_path: str) -> tuple[pd.DataFrame, dict]:
    """Lee un CSV OHLCV y devuelve (DataFrame normalizado, resumen del ticker)."""
    ticker = os.path.basename(csv_path)[:-4].upper()
    df = pd.read_csv(csv_path)

    # Normalizar nombres y tipos
    df.columns = [c.strip().lower() for c in df.columns]
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce", utc=False)
    df = df.dropna(subset=["datetime"])
    df[COLS] = df[COLS].astype(float)

    # Orden y deduplicado por timestamp
    n0 = len(df)
    df = df.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    dups = n0 - len(df)

    # Columnas derivadas
    df["ticker"] = ticker
    min_utc = df["datetime"].dt.hour * 60 + df["datetime"].dt.minute
    df["session"] = _clasificar_sesion(min_utc)
    df["is_rth"] = df["session"].eq("rth")
    df["minute_of_day"] = (min_utc - ET_OFFSET_MIN) % (24 * 60)   # minuto del día en ET

    # Primera barra de cada día de trading (marca el gap nocturno)
    dia = df["datetime"].dt.date
    df["new_session"] = dia.ne(dia.shift(1)).fillna(True)

    # Variación vs. cierre anterior; 0 en el arranque de cada día (no cruzar el gap)
    gap = df["close"].pct_change().fillna(0.0)
    gap[df["new_session"]] = 0.0
    df["gap_pct"] = gap.round(6)

    resumen = {
        "ticker": ticker,
        "filas": int(len(df)),
        "duplicados_eliminados": int(dups),
        "desde": df["datetime"].min().isoformat(),
        "hasta": df["datetime"].max().isoformat(),
        "dias_trading": int(dia.nunique()),
        "barras_rth": int(df["is_rth"].sum()),
        "barras_fuera_rth": int((~df["is_rth"]).sum()),
        "volumen_no_entero": int((df["volume"] % 1 != 0).sum()),
    }
    return df, resumen


def main():
    ap = argparse.ArgumentParser(description="Ingesta histórico 1-min CSV -> Parquet")
    ap.add_argument("--in", dest="entrada", default=str(DEFAULT_IN),
                    help=f"Directorio de CSV crudos (def: {DEFAULT_IN})")
    ap.add_argument("--out", dest="salida", default=str(DEFAULT_OUT),
                    help=f"Directorio de salida Parquet (def: {DEFAULT_OUT})")
    args = ap.parse_args()

    entrada, salida = Path(args.entrada), Path(args.salida)
    csvs = sorted(glob.glob(str(entrada / "*.csv")))
    if not csvs:
        raise SystemExit(f"[ERROR] No se encontraron CSV en {entrada}")

    salida.mkdir(parents=True, exist_ok=True)
    print(f"[ingest] {len(csvs)} ficheros desde {entrada}")

    resumenes = []
    total_filas = 0
    for csv in csvs:
        df, resumen = procesar_ticker(csv)
        destino = salida / f"ticker={resumen['ticker']}"
        destino.mkdir(parents=True, exist_ok=True)
        df.to_parquet(destino / "data.parquet", engine="pyarrow",
                      compression="snappy", index=False)
        resumenes.append(resumen)
        total_filas += resumen["filas"]
        print(f"  [ok] {resumen['ticker']:5} {resumen['filas']:>7,} filas "
              f"| {resumen['desde'][:10]}→{resumen['hasta'][:10]} "
              f"| dup {resumen['duplicados_eliminados']}")

    manifest = {
        "generado": datetime.now(timezone.utc).isoformat(),
        "fuente": str(entrada),
        "salida": str(salida),
        "granularidad": "1min",
        "zona_horaria_datos": "UTC (13:30 UTC = 09:30 ET apertura regular)",
        "tickers": len(resumenes),
        "filas_totales": total_filas,
        "avisos": [
            "El VOLUMEN viene ajustado/normalizado por un proveedor (valores no enteros): "
            "consistente para entrenar, pero los precios están retro-ajustados y NO coinciden "
            "con el precio absoluto operado en vivo. Tenerlo en cuenta en el backtest de ejecución.",
            "Incluye barras de pre-market; usar is_rth para filtrar a sesión regular.",
            "El último día del histórico puede estar parcial (descarga incompleta).",
        ],
        "detalle": resumenes,
    }
    with open(salida / "_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"[ingest] LISTO: {total_filas:,} filas en {len(resumenes)} tickers → {salida}")
    print(f"[ingest] manifiesto: {salida / '_manifest.json'}")


if __name__ == "__main__":
    main()
