# -*- coding: utf-8 -*-
"""Read-only readiness check for GALA10 v2.9.2 per-ticker execution.

No order is submitted, modified, or cancelled.  Only tickers explicitly marked
REAL in data/ticker_execution.json are validated against the live guard.
"""

from pathlib import Path
import os

import yaml
from dotenv import load_dotenv

from agents.training import EntrenadorAgentes
from api.broker_check import ejecutar_broker_check
from core.live_guard import validate_live_start
from core.ticker_execution import TickerExecutionRegistry


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
ENV_PATH = BASE_DIR / ".env"


def _configured_tickers(cfg: dict):
    ticker_file = BASE_DIR / "data" / "tickers_activos.json"
    if ticker_file.exists():
        try:
            import json
            values = json.loads(ticker_file.read_text(encoding="utf-8-sig"))
            if isinstance(values, list) and values:
                return list(dict.fromkeys(
                    str(x).strip().upper() for x in values if str(x).strip()
                ))
        except Exception:
            pass
    return list(dict.fromkeys(
        str(x).strip().upper()
        for x in ((cfg.get("market", {}) or {}).get("tickers") or [])
        if str(x).strip()
    ))


def main() -> int:
    load_dotenv(ENV_PATH, override=False)
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8-sig")) or {}
    live = cfg.get("live_trading", {}) or {}
    risk = cfg.get("risk", {}) or {}
    tickers = _configured_tickers(cfg)

    registry = TickerExecutionRegistry(
        str(live.get("ticker_execution_path", "data/ticker_execution.json")),
        default_mode="PAPER",
        default_shares=max(1, int(risk.get("fixed_shares", 1) or 1)),
    )
    rows = registry.ensure_tickers(tickers, persist=True)
    settings = {row["ticker"]: row for row in rows}
    real_tickers = [row["ticker"] for row in rows if row["mode"] == "REAL"]

    trainer = EntrenadorAgentes(cfg)

    print("\n" + "=" * 76)
    print("  GALA10 v2.9.2 LIVE READINESS — READ ONLY")
    print("=" * 76)
    print("Per-ticker execution:", bool(live.get("per_ticker_execution", False)))
    print("Legacy risk.paper_trading:", bool(risk.get("paper_trading", True)))
    print("Execution routing:")
    for row in rows:
        ticker = row["ticker"]
        model_text = (
            f"PRODUCTION={trainer.modelo_disponible(ticker)} "
            f"threshold={trainer._umbrales.get(ticker)}"
            if row["mode"] == "REAL"
            else "PAPER"
        )
        print(
            f"  {ticker:5s} {row['mode']:5s} shares={int(row['shares']):>3d} | "
            f"{model_text}"
        )

    if not bool(live.get("per_ticker_execution", False)):
        print("[BLOCKED] live_trading.per_ticker_execution no está habilitado")
        print("RESULT: BLOCKED")
        print("=" * 76 + "\n")
        return 1

    if not real_tickers:
        print("\nNo hay tickers en REAL. Ninguna orden puede llegar a Schwab por la ruta REAL.")
        print("RESULT: SAFE_PAPER_ONLY")
        print("=" * 76 + "\n")
        return 0

    def model_checker(allowed):
        return trainer.modelos_faltantes(allowed)

    def broker_checker(account_number, ticker):
        return ejecutar_broker_check(
            str(CONFIG_PATH), account_number, ticker=ticker
        )

    account_number = os.getenv("SCHWAB_ACCOUNT_NUMBER", "").strip()
    ok, errors, checked = validate_live_start(
        cfg,
        get_env=lambda key, default="": os.getenv(key, default),
        account_number=account_number,
        model_checker=model_checker,
        broker_checker=broker_checker,
        real_tickers=real_tickers,
        ticker_settings=settings,
    )

    print("\nREAL tickers checked:", ", ".join(checked) if checked else "NONE")
    print("Approved production models:")
    for ticker in checked:
        print(
            f"  {ticker}: production_loaded={trainer.modelo_disponible(ticker)} "
            f"threshold={trainer._umbrales.get(ticker)} "
            f"shares={settings[ticker]['shares']}"
        )
    if errors:
        for error in errors:
            print("[BLOCKED]", error)
    print("RESULT:", "READY" if ok else "BLOCKED")
    print("=" * 76 + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
