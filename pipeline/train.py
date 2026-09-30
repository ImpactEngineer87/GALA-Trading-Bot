"""
=============================================================
MÓDULO: pipeline/train.py  (C3.5 del PLAN_DE_CONSTRUCCION)
PROYECTO: GALA6 Chocoletra Bot — canalización de datos
=============================================================
DESCRIPCIÓN:
    Entrena el FILTRO predictivo con validación TEMPORAL (walk-forward) y
    responde la pregunta decisiva del proyecto:

      ¿Filtrar las señales por la probabilidad del modelo convierte el baseline
       negativo en R POSITIVO out-of-sample, con drawdown tolerable?

    Método (sin look-ahead, honesto):
      - Split por TIEMPO: entrena con < 2026-01-01, valida con >= 2026-01-01.
        (El modelo NUNCA ve el periodo de validación.)
      - Modelo: XGBoost (clasificador) → probabilidad de que la señal gane.
      - Barrido de umbral D3 (0.50→0.80): para cada umbral, se opera SOLO las
        señales con prob >= umbral y se mide, sobre el periodo de validación:
        nº trades, win%, R total, R medio, y max drawdown en R.
      - Se compara contra el baseline (operar TODAS las señales de validación).

    Salida:
      data/modelo_filtro.pkl        (modelo entrenado, versionado)
      <out>/tabla_umbral.json + .csv (la tabla umbral × resultado para el cliente)
      importancia de features (qué pesa en la decisión — explicabilidad)

USO:
    python -m pipeline.train
    python -m pipeline.train --corte 2026-01-01
=============================================================
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
_V2 = _REPO.parent
DEFAULT_IN = _V2 / "dataset" / "labeled"
DEFAULT_OUT = _V2 / "dataset" / "modelo"

from pipeline.features import FEATURE_COLS   # noqa: E402

X_COLS = FEATURE_COLS + ["tipo"]


def _max_dd_R(r_series: np.ndarray) -> float:
    """Max drawdown en R sobre la curva acumulada (orden cronológico)."""
    if len(r_series) == 0:
        return 0.0
    eq = np.cumsum(r_series)
    pico = np.maximum.accumulate(eq)
    return float((eq - pico).min())


def main():
    ap = argparse.ArgumentParser(description="Entrenamiento walk-forward del filtro")
    ap.add_argument("--in", dest="entrada", default=str(DEFAULT_IN))
    ap.add_argument("--out", dest="salida", default=str(DEFAULT_OUT))
    ap.add_argument("--corte", default="2026-01-01", help="Fecha de corte train/validación")
    args = ap.parse_args()

    parts = sorted(glob.glob(str(Path(args.entrada) / "ticker=*")))
    if not parts:
        raise SystemExit(f"[ERROR] Sin datos etiquetados en {args.entrada}. Corre pipeline.labeling.")
    df = pd.concat([pd.read_parquet(Path(p) / "data.parquet") for p in parts], ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.dropna(subset=X_COLS + ["r_multiple", "win"]).sort_values("datetime").reset_index(drop=True)

    corte = pd.Timestamp(args.corte)
    tr = df[df["datetime"] < corte]
    va = df[df["datetime"] >= corte]
    print(f"[train] total {len(df):,} señales | train {len(tr):,} (< {args.corte}) "
          f"| validación {len(va):,} (>= {args.corte})")
    if len(tr) < 500 or len(va) < 200:
        raise SystemExit("[ERROR] Datos insuficientes para un split temporal fiable.")

    from xgboost import XGBClassifier
    Xtr, ytr = tr[X_COLS].to_numpy(float), tr["win"].to_numpy(int)
    Xva, yva = va[X_COLS].to_numpy(float), va["win"].to_numpy(int)
    r_va = va["r_multiple"].to_numpy(float)

    modelo = XGBClassifier(
        n_estimators=300, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8, eval_metric="logloss",
        n_jobs=-1, random_state=42,
    )
    modelo.fit(Xtr, ytr)
    prob = modelo.predict_proba(Xva)[:, 1]

    # --- Baseline: operar TODAS las señales de validación ---
    base_r = float(r_va.sum()); base_wr = float((r_va > 0).mean() * 100)
    base_dd = _max_dd_R(r_va)
    print(f"\n[VALIDACIÓN 2026] Baseline (sin filtro): {len(r_va):,} señales | "
          f"win {base_wr:.1f}% | R total {base_r:,.0f} | maxDD {base_dd:,.0f} R")

    # --- Barrido de umbral (la tabla para el cliente) ---
    print(f"\n{'umbral':>7} {'señales':>8} {'% del tot':>9} {'win%':>6} {'R total':>9} {'R medio':>8} {'maxDD R':>9}")
    print("-" * 62)
    tabla = []
    for thr in [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]:
        mask = prob >= thr
        n = int(mask.sum())
        if n == 0:
            print(f"{thr:>7.2f} {0:>8} {'-':>9} {'-':>6} {'-':>9} {'-':>8} {'-':>9}")
            tabla.append({"umbral": thr, "senales": 0})
            continue
        rr = r_va[mask]
        fila = {
            "umbral": thr, "senales": n, "pct_del_total": round(n / len(r_va) * 100, 1),
            "win_pct": round(float((rr > 0).mean() * 100), 1),
            "r_total": round(float(rr.sum()), 1), "r_medio": round(float(rr.mean()), 4),
            "max_dd_R": round(_max_dd_R(rr), 1),
        }
        tabla.append(fila)
        print(f"{thr:>7.2f} {n:>8,} {fila['pct_del_total']:>8}% {fila['win_pct']:>6} "
              f"{fila['r_total']:>9,.0f} {fila['r_medio']:>+8.4f} {fila['max_dd_R']:>9,.0f}")

    # --- Sensibilidad a COSTES (slippage + comisión) ---
    # Schwab cobra $0 de comisión en acciones online → el coste real es el SLIPPAGE.
    # Coste por operación en R = n_fills × (slippage×precio + comisión) / riesgo_accion.
    costes = None
    if all(c in va.columns for c in ["precio_entrada", "riesgo_accion", "n_fills"]):
        precio = va["precio_entrada"].to_numpy(float)
        riesgo = va["riesgo_accion"].to_numpy(float)
        nf = va["n_fills"].to_numpy(float)
        comision = 0.0
        thr_ref = 0.55
        mref = prob >= thr_ref
        print(f"\n[COSTES] Conjunto filtrado umbral {thr_ref:.2f} ({int(mref.sum()):,} trades) — "
              f"R neto según slippage (comisión Schwab = $0):")
        print(f"{'slippage':>10} {'coste medio R/trade':>21} {'R neto':>9} {'¿positivo?':>11}")
        print("-" * 54)
        costes = []
        for slbps in [0, 1, 2, 3, 5, 10]:
            slip = slbps / 10000.0
            coste_R = nf * (slip * precio + comision) / riesgo
            r_net = r_va - coste_R
            rr = r_net[mref]
            fila = {"slippage_bps": slbps, "coste_medio_R": round(float(coste_R[mref].mean()), 4),
                    "r_neto": round(float(rr.sum()), 1)}
            costes.append(fila)
            print(f"{slbps:>8}bp {fila['coste_medio_R']:>21.4f} {fila['r_neto']:>9,.0f} "
                  f"{'sí' if fila['r_neto'] > 0 else 'NO':>11}")

    # --- Importancia de features (explicabilidad) ---
    imp = sorted(zip(X_COLS, modelo.feature_importances_), key=lambda x: -x[1])
    print("\n[importancia] top features:")
    for name, w in imp[:8]:
        print(f"    {name:16} {w:.3f}")

    # --- Guardar ---
    Path(args.salida).mkdir(parents=True, exist_ok=True)
    import joblib
    (_REPO / "data").mkdir(exist_ok=True)
    joblib.dump({"modelo": modelo, "features": X_COLS, "corte": args.corte},
                _REPO / "data" / "modelo_filtro.pkl")
    resultado = {
        "corte": args.corte,
        "baseline_validacion": {"senales": len(r_va), "win_pct": round(base_wr, 1),
                                "r_total": round(base_r, 1), "max_dd_R": round(base_dd, 1)},
        "tabla_umbral": tabla,
        "sensibilidad_costes": costes,
        "importancia": [{"feature": n, "peso": round(float(w), 4)} for n, w in imp],
    }
    with open(Path(args.salida) / "tabla_umbral.json", "w", encoding="utf-8") as f:
        json.dump(resultado, f, indent=2, ensure_ascii=False)
    pd.DataFrame(tabla).to_csv(Path(args.salida) / "tabla_umbral.csv", index=False)
    print(f"\n[train] modelo → {_REPO / 'data' / 'modelo_filtro.pkl'}")
    print(f"[train] tabla umbral → {Path(args.salida) / 'tabla_umbral.json'}")

    # --- Veredicto automático ---
    mejor = max((f for f in tabla if f.get("senales", 0) > 50),
                key=lambda x: x.get("r_total", -1e9), default=None)
    print("\n" + "=" * 62)
    if mejor and mejor["r_total"] > 0 and base_r <= mejor["r_total"]:
        print(f"VEREDICTO: el filtro MEJORA. Mejor umbral {mejor['umbral']:.2f} → "
              f"R {mejor['r_total']:,.0f} (baseline {base_r:,.0f}) con {mejor['senales']:,} trades.")
    else:
        print(f"VEREDICTO: con estas features el filtro NO logra R positivo claro out-of-sample. "
              f"Revisar features/estrategia base (ver HALLAZGO_BACKTEST_BASELINE).")
    print("=" * 62)


if __name__ == "__main__":
    main()
