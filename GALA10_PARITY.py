"""
Exporta las señales y el estado interno de Gala10 para compararlos uno-a-uno
con ThinkOrSwim.

Ejemplo (PowerShell):
    python GALA10_PARITY.py --csv data/historico/MSFT.csv --ticker MSFT ^
      --from-et "2026-07-27 08:00" --to-et "2026-07-28 17:05"

El timestamp de barra es el inicio de la vela que muestra TOS. La decisión queda
disponible al cierre de esa vela, un minuto después.
"""

import argparse
import os

import pandas as pd
import yaml

from agents.historico_loader import _leer_csv_1m
from core.strategy import GALA10Strategy


PLOTS = [
    "buy",
    "sell",
    "omitida_buy_rango",
    "omitida_sell_rango",
    "omitida_buy_tendencia",
    "omitida_sell_tendencia",
]


def _args():
    parser = argparse.ArgumentParser(
        description="Exportar paridad Gala10 Python vs ThinkOrSwim")
    parser.add_argument("--csv", required=True, help="CSV OHLCV de 1 minuto")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--input-tz", default="UTC")
    parser.add_argument("--from-et", dest="from_et")
    parser.add_argument("--to-et", dest="to_et")
    parser.add_argument("--output")
    parser.add_argument(
        "--all-bars", action="store_true",
        help="Exportar todas las barras, no solo plots BUY/SELL/OM-*")
    return parser.parse_args()


def main() -> int:
    args = _args()
    with open(args.config, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}

    df = _leer_csv_1m(args.csv, args.input_tz)
    if df is None or len(df) < 35:
        raise SystemExit("CSV inválido o con menos de 35 velas utilizables")

    calc = GALA10Strategy(config).calcular(df)
    idx_et = calc.index.tz_localize("UTC").tz_convert("America/New_York")
    out = pd.DataFrame(index=calc.index)
    out["ticker"] = args.ticker.upper()
    out["bar_timestamp_et"] = idx_et.strftime("%Y-%m-%d %H:%M:%S%z")
    out["decision_available_et"] = (
        idx_et + pd.Timedelta(minutes=1)
    ).strftime("%Y-%m-%d %H:%M:%S%z")

    columnas = [
        "open", "high", "low", "close", "volume",
        "macd_line", "signal_line", "hist_line", "atr",
        "ema_trend", "ema_slope", "range_pct",
        "bull_cross", "bear_cross", "cross_high", "cross_low",
        "cross_bars", "direccion_activa",
        "buy_signal_raw", "sell_signal_raw",
        "barras_desde_buy", "barras_desde_sell",
        *PLOTS, "señal",
    ]
    for col in columnas:
        out[col] = calc[col].to_numpy()

    out.index = idx_et.tz_localize(None)
    if args.from_et:
        out = out.loc[pd.Timestamp(args.from_et):]
    if args.to_et:
        out = out.loc[:pd.Timestamp(args.to_et)]
    if not args.all_bars:
        out = out[out[PLOTS].any(axis=1)]

    ruta = args.output or os.path.join(
        "data", "reportes", f"paridad_gala10_{args.ticker.upper()}.csv")
    os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
    out.to_csv(ruta, index=False)
    print(f"[GALA10] Paridad exportada: {ruta} | filas={len(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
