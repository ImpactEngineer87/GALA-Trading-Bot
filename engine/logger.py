"""
=============================================================
MÓDULO: engine/logger.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Sistema de logging del bot. Registra todas las señales,
    órdenes, errores y eventos importantes en consola y archivo.

    Cada evento queda registrado con timestamp, ticker,
    tipo de señal y acción tomada para revisión posterior.
=============================================================
"""

import logging
import os
from datetime import datetime
from pathlib import Path


# =============================================================
# CONFIGURACIÓN DEL LOGGER
# =============================================================

def crear_logger(nombre: str = "chocoletra", nivel: str = "INFO",
                 log_a_archivo: bool = True, archivo: str = "logs/chocoletra.log") -> logging.Logger:
    """
    Crea y configura el logger principal del bot.

    Parámetros:
        nombre       → Nombre del logger
        nivel        → Nivel de log: DEBUG / INFO / WARNING / ERROR
        log_a_archivo → Si True, guarda también en archivo
        archivo      → Ruta del archivo de log

    Retorna:
        Logger configurado
    """
    logger = logging.getLogger(nombre)
    logger.setLevel(getattr(logging, nivel.upper(), logging.INFO))

    if logger.handlers:
        return logger  # Ya configurado

    formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Handler de consola
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Handler de archivo
    if log_a_archivo:
        Path(os.path.dirname(archivo)).mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(archivo, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


# =============================================================
# LOGGER GLOBAL DEL BOT
# =============================================================

logger = crear_logger()


def log_señal(ticker: str, tipo: str, precio: float, atr: float,
              motivo: str = ""):
    """
    Registra una señal generada por la estrategia.

    Ejemplos de salida:
        2026-06-19 09:30:01 | INFO     | [BUY]  BA @ $227.49 | ATR: 1.85
        2026-06-19 09:31:00 | INFO     | [OM-R] AAPL @ $185.20 | Rango insuficiente
    """
    icono = {"BUY": "🟢", "SELL": "🔴", "OM-R": "⚫", "OM-T": "🟠"}.get(tipo, "⚪")
    msg = f"{icono} [{tipo}] {ticker} @ ${precio:.2f} | ATR: {atr:.4f}"
    if motivo:
        msg += f" | {motivo}"
    logger.info(msg)


def log_orden(ticker: str, tipo: str, precio_limite: float,
              stop_loss: float, trailing_pct: float, cantidad: int):
    """
    Registra una orden enviada al broker.
    """
    logger.info(
        f"📤 ORDEN {tipo} | {ticker} | LIMIT: ${precio_limite:.2f} | "
        f"SL: ${stop_loss:.2f} | Trail: {trailing_pct:.2f}% | Qty: {cantidad}"
    )


def log_error(mensaje: str, ticker: str = ""):
    """
    Registra un error del bot.
    """
    prefijo = f"[{ticker}] " if ticker else ""
    logger.error(f"❌ {prefijo}{mensaje}")


def log_info(mensaje: str):
    """Registra un mensaje informativo general."""
    logger.info(f"ℹ️  {mensaje}")


def log_inicio_bot(tickers: list):
    """Registra el inicio del bot con la lista de tickers."""
    logger.info("=" * 60)
    logger.info("🤖 GALA10 CHOCOLETRA BOT — INICIANDO")
    logger.info(f"⏰ Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"📈 Tickers: {', '.join(tickers)}")
    logger.info("=" * 60)
