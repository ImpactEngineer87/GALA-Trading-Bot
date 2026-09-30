"""
=============================================================
MÓDULO: pipeline/backtest.py  (C3.4 del PLAN_DE_CONSTRUCCION)
PROYECTO: GALA6 Chocoletra Bot — canalización de datos
=============================================================
DESCRIPCIÓN:
    Backtest BASELINE de la estrategia GALA6 sobre el histórico de 2 años
    (1-min, RTH). Responde la pregunta más importante del proyecto:
    ¿la estrategia base tiene ventaja (gana) ANTES de meter IA encima?

    CLAVE: reutiliza el MISMO código que opera en vivo —
      - core.strategy.GALA6Strategy  → genera las señales BUY/SELL
      - core.risk_manager.GestorDeRiesgo → entrada/SL/TP/TP1 y sizing 70/30
    para que el resultado refleje la lógica real, no una re-implementación.

    MECÁNICA (fiel al motor en vivo, sin look-ahead):
      - Señal en el cierre de la barra i → ENTRADA en la apertura de i+1.
      - Una posición por ticker; señal opuesta = reversión (cierra y voltea).
      - Gestión 70/30: 70% sale en TP1, el 30% pasa a breakeven; cierre por
        SL / TP final. Resolución intrabar conservadora (SL tiene prioridad).
      - Riesgo por operación: 1% del capital (como risk_manager).

    MÉTRICAS (en dólares y en R-múltiplos, que son agnósticos al capital):
      total trades, win%, R total, R medio, profit factor, max drawdown.

USO:
    python -m pipeline.backtest                 # todos los tickers
    python -m pipeline.backtest --tickers NVDA TSLA
    python -m pipeline.backtest --capital 10000 --riesgo-pct 1.0
=============================================================
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
_V2 = _REPO.parent
DEFAULT_PARQUET = _V2 / "dataset" / "historico_1min_parquet"
DEFAULT_OUT = _V2 / "dataset" / "backtest"

from core.strategy import GALA6Strategy          # noqa: E402
from core.risk_manager import GestorDeRiesgo      # noqa: E402


@dataclass
class Trade:
    ticker: str
    tipo: str            # BUY / SELL
    entrada: float
    salida: float
    qty: int
    pnl: float
    r_multiple: float
    entrada_ts: pd.Timestamp
    salida_ts: pd.Timestamp
    motivo: str          # TP1+TPfinal / SL / breakeven / reversión / fin-datos


def _simular_ticker(df: pd.DataFrame, ticker: str, estrategia: GALA6Strategy,
                    gestor: GestorDeRiesgo, capital: float, riesgo_pct: float) -> list[Trade]:
    """Simula la estrategia sobre un ticker. Devuelve la lista de trades cerrados."""
    calc = estrategia.calcular(df.copy())
    señales = calc["señal"].to_numpy(dtype=object)
    atr = calc["atr"].to_numpy(dtype=float) if "atr" in calc else np.zeros(len(calc))
    o = calc["open"].to_numpy(float); h = calc["high"].to_numpy(float)
    l = calc["low"].to_numpy(float);  c = calc["close"].to_numpy(float)
    ts = calc["datetime"].to_numpy()
    n = len(calc)

    trades: list[Trade] = []
    pos = None   # dict con estado de la posición abierta

    def abrir(i_entrada: int, tipo: str):
        """Abre en la apertura de la barra i_entrada usando ATR de la barra previa."""
        entrada = o[i_entrada]
        atr_val = atr[i_entrada - 1] if i_entrada > 0 else atr[i_entrada]
        if not np.isfinite(entrada) or entrada <= 0 or not np.isfinite(atr_val) or atr_val <= 0:
            return None
        orden = gestor.calcular_orden(ticker=ticker, tipo=tipo, precio_actual=float(entrada),
                                      atr=float(atr_val), capital_disponible=capital,
                                      riesgo_por_operacion_pct=riesgo_pct)
        if orden is None:
            return None
        return {
            "tipo": tipo, "entrada": float(entrada), "entrada_ts": ts[i_entrada],
            "sl": orden.stop_loss, "sl_actual": orden.stop_loss,
            "tp1": orden.take_profit_parcial, "tp_final": orden.take_profit,
            "qty": orden.cantidad, "qty_parcial": orden.cantidad_parcial,
            "qty_resto": orden.cantidad_resto, "parcial_on": orden.salida_parcial_activa,
            "tp1_hit": False, "modo_resto": orden.modo_resto,
            "riesgo_accion": abs(float(entrada) - orden.stop_loss), "pnl_realizado": 0.0,
        }

    def cerrar(precio_salida: float, i: int, motivo: str):
        nonlocal pos
        p = pos
        qresto = p["qty_resto"] if p["tp1_hit"] else p["qty"]
        if p["tipo"] == "BUY":
            pnl_resto = (precio_salida - p["entrada"]) * qresto
        else:
            pnl_resto = (p["entrada"] - precio_salida) * qresto
        pnl_total = p["pnl_realizado"] + pnl_resto
        riesgo_total = p["riesgo_accion"] * p["qty"]
        r_mult = pnl_total / riesgo_total if riesgo_total > 0 else 0.0
        trades.append(Trade(ticker, p["tipo"], p["entrada"], float(precio_salida), p["qty"],
                            round(pnl_total, 2), round(r_mult, 3),
                            pd.Timestamp(p["entrada_ts"]), pd.Timestamp(ts[i]), motivo))
        pos = None

    def parcial():
        """Cierra el 70% en TP1 y pasa el 30% a breakeven (o mantiene SL)."""
        p = pos
        if p["tipo"] == "BUY":
            p["pnl_realizado"] += (p["tp1"] - p["entrada"]) * p["qty_parcial"]
        else:
            p["pnl_realizado"] += (p["entrada"] - p["tp1"]) * p["qty_parcial"]
        p["tp1_hit"] = True
        p["sl_actual"] = p["entrada"] if p["modo_resto"] == "breakeven" else p["sl"]

    for i in range(n):
        s = señales[i]

        # --- Gestión de la posición abierta (intrabar, SL prioritario) ---
        if pos is not None:
            p = pos
            if p["tipo"] == "BUY":
                if l[i] <= p["sl_actual"]:
                    cerrar(p["sl_actual"], i, "SL" if not p["tp1_hit"] else "breakeven");
                elif p["parcial_on"] and not p["tp1_hit"] and h[i] >= p["tp1"]:
                    parcial()
                    if h[i] >= p["tp_final"]:
                        cerrar(p["tp_final"], i, "TP1+TPfinal")
                elif p["tp1_hit"] and h[i] >= p["tp_final"]:
                    cerrar(p["tp_final"], i, "TP1+TPfinal")
                elif not p["parcial_on"] and h[i] >= p["tp_final"]:
                    cerrar(p["tp_final"], i, "TPfinal")
            else:  # SELL
                if h[i] >= p["sl_actual"]:
                    cerrar(p["sl_actual"], i, "SL" if not p["tp1_hit"] else "breakeven")
                elif p["parcial_on"] and not p["tp1_hit"] and l[i] <= p["tp1"]:
                    parcial()
                    if l[i] <= p["tp_final"]:
                        cerrar(p["tp_final"], i, "TP1+TPfinal")
                elif p["tp1_hit"] and l[i] <= p["tp_final"]:
                    cerrar(p["tp_final"], i, "TP1+TPfinal")
                elif not p["parcial_on"] and l[i] <= p["tp_final"]:
                    cerrar(p["tp_final"], i, "TPfinal")

        # --- Señal: apertura o reversión (entra en la barra SIGUIENTE) ---
        if s in ("BUY", "SELL") and i + 1 < n:
            tipo = s
            if pos is None:
                nueva = abrir(i + 1, tipo)
                if nueva:
                    pos = nueva
            elif pos["tipo"] != tipo:
                # reversión: cerrar en la apertura siguiente y voltear
                cerrar(o[i + 1], i + 1, "reversión")
                nueva = abrir(i + 1, tipo)
                if nueva:
                    pos = nueva

    # Cierre forzado al final de los datos
    if pos is not None:
        cerrar(c[n - 1], n - 1, "fin-datos")
    return trades


def _metricas(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0}
    r = np.array([t.r_multiple for t in trades])
    pnl = np.array([t.pnl for t in trades])
    wins = pnl > 0
    ganancias = pnl[wins].sum()
    perdidas = -pnl[~wins].sum()
    # Curva de equity realizada (por orden de cierre) para el drawdown
    ordenados = sorted(trades, key=lambda t: t.salida_ts)
    equity = np.cumsum([t.pnl for t in ordenados])
    pico = np.maximum.accumulate(equity)
    dd = equity - pico
    max_dd = float(dd.min()) if len(dd) else 0.0
    base = 10000.0
    max_dd_pct = float((dd / (pico + base)).min() * 100) if len(dd) else 0.0
    return {
        "trades": len(trades),
        "win_pct": round(float(wins.mean() * 100), 1),
        "pnl_total": round(float(pnl.sum()), 2),
        "r_total": round(float(r.sum()), 2),
        "r_medio": round(float(r.mean()), 3),
        "profit_factor": round(float(ganancias / perdidas), 2) if perdidas > 0 else float("inf"),
        "max_drawdown_usd": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 1),
        "mejor": round(float(pnl.max()), 2),
        "peor": round(float(pnl.min()), 2),
    }


def main():
    ap = argparse.ArgumentParser(description="Backtest baseline de la estrategia GALA6")
    ap.add_argument("--parquet", default=str(DEFAULT_PARQUET))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--tickers", nargs="*", default=None)
    ap.add_argument("--capital", type=float, default=10000.0)
    ap.add_argument("--riesgo-pct", type=float, default=1.0)
    ap.add_argument("--config", default=str(_REPO / "config.yaml"))
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    estrategia = GALA6Strategy(config)
    gestor = GestorDeRiesgo(config)

    parts = sorted(glob.glob(str(Path(args.parquet) / "ticker=*")))
    if args.tickers:
        want = {t.upper() for t in args.tickers}
        parts = [p for p in parts if Path(p).name.split("=")[1] in want]
    if not parts:
        raise SystemExit(f"[ERROR] Sin datos en {args.parquet}")

    todos: list[Trade] = []
    por_ticker = {}
    print(f"[backtest] {len(parts)} tickers | riesgo {args.riesgo_pct}% de ${args.capital:,.0f}\n")
    print(f"{'TICKER':7} {'trades':>7} {'win%':>6} {'PnL$':>11} {'R tot':>8} {'R med':>7} {'PF':>6} {'maxDD%':>7}")
    print("-" * 66)
    for p in parts:
        ticker = Path(p).name.split("=")[1]
        df = pd.read_parquet(Path(p) / "data.parquet")
        df = df[df["is_rth"]].reset_index(drop=True)
        tr = _simular_ticker(df, ticker, estrategia, gestor, args.capital, args.riesgo_pct)
        todos.extend(tr)
        m = _metricas(tr)
        por_ticker[ticker] = m
        if m["trades"]:
            print(f"{ticker:7} {m['trades']:>7} {m['win_pct']:>6} {m['pnl_total']:>11,.0f} "
                  f"{m['r_total']:>8.1f} {m['r_medio']:>7.3f} {m['profit_factor']:>6} {m['max_drawdown_pct']:>7}")
        else:
            print(f"{ticker:7} {'sin trades':>7}")

    glob_m = _metricas(todos)
    print("-" * 66)
    print(f"{'TOTAL':7} {glob_m.get('trades',0):>7} {glob_m.get('win_pct',0):>6} "
          f"{glob_m.get('pnl_total',0):>11,.0f} {glob_m.get('r_total',0):>8.1f} "
          f"{glob_m.get('r_medio',0):>7.3f} {glob_m.get('profit_factor',0):>6} "
          f"{glob_m.get('max_drawdown_pct',0):>7}")

    Path(args.out).mkdir(parents=True, exist_ok=True)
    with open(Path(args.out) / "resultado_baseline.json", "w", encoding="utf-8") as f:
        json.dump({"global": glob_m, "por_ticker": por_ticker,
                   "config": {"capital": args.capital, "riesgo_pct": args.riesgo_pct}},
                  f, indent=2, ensure_ascii=False)
    # Guardar trades para análisis posterior
    if todos:
        pd.DataFrame([t.__dict__ for t in todos]).to_csv(
            Path(args.out) / "trades_baseline.csv", index=False)
    print(f"\n[backtest] resultado → {Path(args.out) / 'resultado_baseline.json'}")
    print(f"[backtest] {len(todos):,} trades → {Path(args.out) / 'trades_baseline.csv'}")


if __name__ == "__main__":
    main()
