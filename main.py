"""
=============================================================
ARCHIVO: main.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Punto de entrada principal del bot.
    Desde aquí se configura y se inicia el sistema.

CÓMO USAR:
    1. Instalar dependencias:
       pip install -r requirements.txt

    2. Crear archivo .env con tus credenciales Schwab:
       SCHWAB_CLIENT_ID=tu_client_id
       SCHWAB_CLIENT_SECRET=tu_client_secret

    3. Ejecutar en modo demo (paper trading):
       python main.py --modo demo

    4. Ejecutar con datos de prueba (CSV):
       python main.py --modo backtest --csv data/BA_1min.csv

    5. Ejecutar en modo real (requiere credenciales):
       python main.py --modo real --account 12345678
=============================================================
"""

import argparse
import os
import sys
# Consola UTF-8 segura (VPS Windows puede usar cp1252)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from engine.bot_engine import ChocoletraBot
from engine.logger import log_info, log_error
from api.broker_check import ejecutar_broker_check, ejecutar_broker_latency


def _configurar_modo_cli(bot, modo: str) -> bool:
    """
    Aplica el modo solicitado de forma explícita.

    ``demo`` y ``backtest`` SIEMPRE fuerzan simulación, aunque config.yaml haya
    quedado accidentalmente con paper_trading=false. ``real`` exige además que
    el operador haya habilitado el interruptor deliberado en config.yaml.
    """
    if modo in ("demo", "backtest"):
        bot.configurar_modo_ejecucion(paper_trading=True)
        return True

    if modo == "real":
        configurado_paper = bool(
            bot.config.get("risk", {}).get("paper_trading", True)
        )
        if configurado_paper:
            log_error(
                "Modo real bloqueado: config.yaml mantiene "
                "risk.paper_trading=true. Ejecute broker-check primero y cambie "
                "este interruptor manualmente solo durante la prueba supervisada."
            )
            return False
        bot.configurar_modo_ejecucion(paper_trading=False)
    return True


def main():
    """Función principal del bot."""

    # Parsear argumentos de línea de comandos
    parser = argparse.ArgumentParser(
        description="GALA10 Chocoletra — Bot de Trading Algorítmico"
    )
    parser.add_argument(
        "--modo",
        choices=["demo", "backtest", "broker-check", "broker-latency", "real"],
        default="demo",
        help=(
            "Modo: demo (simulado) | backtest (CSV) | "
            "broker-check (Schwab solo lectura) | "
            "broker-latency (benchmark GET) | real"
        )
    )
    parser.add_argument(
        "--csv",
        type=str,
        default="",
        help="Ruta al CSV de datos históricos (solo para modo backtest)"
    )
    parser.add_argument(
        "--account",
        type=str,
        default="",
        help="Número de cuenta Schwab (solo para modo real)"
    )
    parser.add_argument(
        "--tickers",
        type=str,
        default="AAPL,AMD,AMZN,BA,META,NVDA,RCL,UAL,TSLA,UNH,PINS,NCLH,MSFT,NKE,PYPL,NFLX,ADBE",
        help="Tickers separados por coma; por defecto usa los 17 confirmados"
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=10000.0,
        help="Capital disponible en USD (default: 10000)"
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=10,
        help="Muestras calientes para broker-latency (1-50, default: 10)"
    )
    args = parser.parse_args()
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    # Mostrar banner
    print("\n" + "=" * 60)
    print("  🤖 GALA10 CHOCOLETRA BOT")
    print("  Estrategia maestra: GALA10 + IA de veto")
    print("  Broker: Charles Schwab API")
    print("=" * 60)
    print(f"  Modo: {args.modo.upper()}")
    print(f"  Capital: ${args.capital:,.2f} USD")
    print("=" * 60 + "\n")

    # El preflight de broker es deliberadamente independiente del motor: solo
    # usa GET de cuenta/órdenes/datos y jamás construye un ejecutor de órdenes.
    if args.modo == "broker-check":
        account = args.account or os.getenv("SCHWAB_ACCOUNT_NUMBER", "")
        if not account:
            log_error(
                "Debes especificar --account o SCHWAB_ACCOUNT_NUMBER para broker-check"
            )
            sys.exit(2)
        ticker_check = tickers[0] if tickers else "MSFT"
        resultado = ejecutar_broker_check(
            config_path="config.yaml",
            account_number=account,
            ticker=ticker_check,
        )
        sys.exit(0 if resultado.get("ok") else 1)

    if args.modo == "broker-latency":
        account = args.account or os.getenv("SCHWAB_ACCOUNT_NUMBER", "")
        if not account:
            log_error(
                "Debes especificar --account o SCHWAB_ACCOUNT_NUMBER "
                "para broker-latency"
            )
            sys.exit(2)
        ticker_check = tickers[0] if tickers else "MSFT"
        resultado = ejecutar_broker_latency(
            config_path="config.yaml",
            account_number=account,
            ticker=ticker_check,
            samples=args.samples,
        )
        sys.exit(0 if resultado.get("ok") else 1)

    # Inicializar el bot
    try:
        bot = ChocoletraBot("config.yaml")
    except FileNotFoundError:
        log_error("Archivo config.yaml no encontrado. Asegúrate de ejecutar desde la carpeta correcta.")
        sys.exit(1)

    if not _configurar_modo_cli(bot, args.modo):
        sys.exit(2)

    # ===================================================
    # MODO DEMO (Paper Trading)
    # ===================================================
    if args.modo == "demo":
        log_info("Iniciando en modo PAPER TRADING (demo)")
        log_info("Las órdenes serán simuladas — no se opera con dinero real")
        bot.iniciar(
            tickers=tickers,
            account_number="DEMO",
            capital=args.capital
        )

    # ===================================================
    # MODO BACKTEST (CSV histórico)
    # ===================================================
    elif args.modo == "backtest":
        if not args.csv:
            log_error("Debes especificar un archivo CSV con --csv ruta/al/archivo.csv")
            sys.exit(1)

        ticker_test = tickers[0] if tickers else "TEST"
        log_info(f"Iniciando backtesting con: {args.csv}")
        resultado = bot.probar_con_csv(args.csv, ticker=ticker_test)

        # Guardar resultado en CSV
        output_path = f"data/resultado_backtest_{ticker_test}.csv"
        resultado.to_csv(output_path)
        log_info(f"📁 Resultado guardado en: {output_path}")

    # ===================================================
    # MODO REAL
    # ===================================================
    elif args.modo == "real":
        if not args.account:
            log_error("Debes especificar el número de cuenta con --account XXXXXXXX")
            sys.exit(1)

        # Preflight SOLO LECTURA obligatorio antes de armar el motor real.
        ticker_check = tickers[0] if tickers else "MSFT"
        preflight = ejecutar_broker_check(
            config_path="config.yaml",
            account_number=args.account,
            ticker=ticker_check,
        )
        if not preflight.get("ok"):
            log_error("Modo real bloqueado: broker-check no fue satisfactorio")
            sys.exit(2)

        print("\n⚠️  ADVERTENCIA: MODO REAL ACTIVO")
        print("⚠️  Las órdenes se enviarán al broker de forma real.")
        print("⚠️  Asegúrate de que las credenciales sean correctas.")
        sufijo = args.account[-4:]
        frase = f"REAL {sufijo}"
        confirmacion = input(f"Escribe exactamente '{frase}' para continuar: ")

        if confirmacion.strip() != frase:
            log_info("Operación cancelada por el usuario")
            sys.exit(0)

        log_info("Iniciando en MODO REAL con cuenta terminada en " + args.account[-4:])
        bot.iniciar(
            tickers=tickers,
            account_number=args.account,
            capital=args.capital
        )


if __name__ == "__main__":
    main()
