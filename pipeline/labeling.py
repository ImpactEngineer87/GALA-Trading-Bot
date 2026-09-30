"""
=============================================================
MÓDULO: pipeline/labeling.py  (C3.3 del PLAN_DE_CONSTRUCCION)
PROYECTO: GALA6 Chocoletra Bot — canalización de datos
=============================================================
DESCRIPCIÓN:
    Etiqueta CADA señal BUY/SELL con el resultado que habría tenido, medido en
    R-múltiplo (ganancia/pérdida ÷ riesgo). Es la etiqueta que el modelo aprende
    a predecir → filtrar las señales por su probabilidad de dar R positivo.

    D1 (decidido con el cliente): "ganar" = ganancia ACUMULADA → se etiqueta por
    MAGNITUD (R-múltiplo), no por acertar/fallar binario.

    Cada señal se evalúa de forma INDEPENDIENTE (no respeta 'una posición a la
    vez'), porque queremos la calidad intrínseca de cada señal para el filtro:
      - Entrada en la apertura de la barra siguiente a la señal.
      - SL/TP/70-30 calculados por GestorDeRiesgo (mismo que en vivo).
      - Se resuelve mirando hasta H barras adelante (SL prioritario intrabar).
      - Sin look-ahead en las FEATURES (se toman en la barra de la señal).

    Salida: <out>/ticker=<SYM>/data.parquet  (features + tipo + r_multiple + win)

USO:
    python -m pipeline.labeling
    python -m pipeline.labeling --tickers NVDA --horizonte 120
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
DEFAULT_IN = _V2 / "dataset" / "features"
DEFAULT_OUT = _V2 / "dataset" / "labeled"

from core.risk_manager import GestorDeRiesgo   # noqa: E402
from pipeline.features import FEATURE_COLS       # noqa: E402


def _resolver_senal(tipo, entrada, sl, tp1, tpf, qty, qparc, qresto, parcial_on,
                    modo_resto, h, l, i_ini, horizonte, riesgo_accion):
    """Simula una señal hacia adelante y devuelve su R-múltiplo (o None si no resuelve)."""
    n = len(h)
    fin = min(i_ini + horizonte, n)
    pnl_real = 0.0
    tp1_hit = False
    sl_act = sl
    for j in range(i_ini, fin):
        if tipo == "BUY":
            if l[j] <= sl_act:
                pnl_real += (sl_act - entrada) * (qresto if tp1_hit else qty)
                return pnl_real / (riesgo_accion * qty)
            if parcial_on and not tp1_hit and h[j] >= tp1:
                pnl_real += (tp1 - entrada) * qparc
                tp1_hit = True
                sl_act = entrada if modo_resto == "breakeven" else sl
                if h[j] >= tpf:
                    pnl_real += (tpf - entrada) * qresto
                    return pnl_real / (riesgo_accion * qty)
            elif tp1_hit and h[j] >= tpf:
                pnl_real += (tpf - entrada) * qresto
                return pnl_real / (riesgo_accion * qty)
            elif not parcial_on and h[j] >= tpf:
                return ((tpf - entrada) * qty) / (riesgo_accion * qty)
        else:  # SELL
            if h[j] >= sl_act:
                pnl_real += (entrada - sl_act) * (qresto if tp1_hit else qty)
                return pnl_real / (riesgo_accion * qty)
            if parcial_on and not tp1_hit and l[j] <= tp1:
                pnl_real += (entrada - tp1) * qparc
                tp1_hit = True
                sl_act = entrada if modo_resto == "breakeven" else sl
                if l[j] <= tpf:
                    pnl_real += (entrada - tpf) * qresto
                    return pnl_real / (riesgo_accion * qty)
            elif tp1_hit and l[j] <= tpf:
                pnl_real += (entrada - tpf) * qresto
                return pnl_real / (riesgo_accion * qty)
            elif not parcial_on and l[j] <= tpf:
                return ((entrada - tpf) * qty) / (riesgo_accion * qty)
    # No resolvió en el horizonte: cerrar a mercado (cierre de la última barra)
    cierre = (h[fin - 1] + l[fin - 1]) / 2
    qr = qresto if tp1_hit else qty
    pnl_real += (cierre - entrada) * qr if tipo == "BUY" else (entrada - cierre) * qr
    return pnl_real / (riesgo_accion * qty)


def etiquetar_ticker(df: pd.DataFrame, ticker: str, gestor: GestorDeRiesgo,
                     horizonte: int, capital: float) -> pd.DataFrame:
    o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float); atr = df["atr"].to_numpy(float)
    senal = df["senal"].to_numpy(object)
    n = len(df)
    idx_sig = np.where(np.isin(senal, ["BUY", "SELL"]))[0]

    filas = []
    for i in idx_sig:
        if i + 1 >= n:
            continue
        entrada = o[i + 1]; atr_val = atr[i]
        if not (np.isfinite(entrada) and entrada > 0 and np.isfinite(atr_val) and atr_val > 0):
            continue
        orden = gestor.calcular_orden(ticker=ticker, tipo=senal[i], precio_actual=float(entrada),
                                      atr=float(atr_val), capital_disponible=capital)
        if orden is None:
            continue
        riesgo_accion = abs(float(entrada) - orden.stop_loss)
        if riesgo_accion <= 0:
            continue
        r = _resolver_senal(senal[i], float(entrada), orden.stop_loss, orden.take_profit_parcial,
                            orden.take_profit, orden.cantidad, orden.cantidad_parcial,
                            orden.cantidad_resto, orden.salida_parcial_activa, orden.modo_resto,
                            h, l, i + 1, horizonte, riesgo_accion)
        if r is None or not np.isfinite(r):
            continue
        fila = {c: df[c].iloc[i] for c in FEATURE_COLS}
        fila["ticker"] = ticker
        fila["datetime"] = df["datetime"].iloc[i]
        fila["tipo"] = 1 if senal[i] == "BUY" else 0
        fila["r_multiple"] = round(float(r), 4)   # R BRUTO (sin costes)
        fila["win"] = int(r > 0)
        # Datos para calcular costes reales (slippage/comisión) en el entrenamiento:
        fila["precio_entrada"] = round(float(entrada), 4)
        fila["riesgo_accion"] = round(float(riesgo_accion), 6)
        # nº de fills = entrada + salidas (3 con salida parcial 70/30; si no, 2)
        fila["n_fills"] = 3 if orden.salida_parcial_activa else 2
        filas.append(fila)
    return pd.DataFrame(filas)


def main():
    ap = argparse.ArgumentParser(description="Etiquetado de señales por R-múltiplo")
    ap.add_argument("--in", dest="entrada", default=str(DEFAULT_IN))
    ap.add_argument("--out", dest="salida", default=str(DEFAULT_OUT))
    ap.add_argument("--tickers", nargs="*", default=None)
    ap.add_argument("--horizonte", type=int, default=None, help="Barras futuras máximas")
    ap.add_argument("--capital", type=float, default=10000.0)
    ap.add_argument("--config", default=str(_REPO / "config.yaml"))
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    gestor = GestorDeRiesgo(config)
    horizonte = args.horizonte or config.get("agents", {}).get("backtest", {}).get("max_barras_futuras", 120)

    parts = sorted(glob.glob(str(Path(args.entrada) / "ticker=*")))
    if args.tickers:
        want = {t.upper() for t in args.tickers}
        parts = [p for p in parts if Path(p).name.split("=")[1] in want]
    if not parts:
        raise SystemExit(f"[ERROR] Sin features en {args.entrada}. Corre pipeline.features primero.")

    salida = Path(args.salida); salida.mkdir(parents=True, exist_ok=True)
    print(f"[labeling] {len(parts)} tickers | horizonte {horizonte} barras")
    total = 0; total_r = 0.0; total_win = 0
    for p in parts:
        ticker = Path(p).name.split("=")[1]
        df = pd.read_parquet(Path(p) / "data.parquet")
        lab = etiquetar_ticker(df, ticker, gestor, horizonte, args.capital)
        if lab.empty:
            print(f"  [--] {ticker}: sin señales etiquetables"); continue
        dest = salida / f"ticker={ticker}"; dest.mkdir(parents=True, exist_ok=True)
        lab.to_parquet(dest / "data.parquet", engine="pyarrow", compression="snappy", index=False)
        r_sum = lab["r_multiple"].sum(); wr = lab["win"].mean() * 100
        total += len(lab); total_r += r_sum; total_win += lab["win"].sum()
        print(f"  [ok] {ticker:5} {len(lab):>6} señales | win {wr:4.1f}% | R total {r_sum:>8.1f} "
              f"| R medio {lab['r_multiple'].mean():+.3f}")
    print(f"[labeling] LISTO: {total:,} señales | win global {total_win/max(total,1)*100:.1f}% "
          f"| R total {total_r:,.0f}")


if __name__ == "__main__":
    main()
