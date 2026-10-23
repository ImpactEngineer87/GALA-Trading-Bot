# -*- coding: utf-8 -*-
"""
=============================================================
ARCHIVO: server.py  (PRODUCTION VERSION)
PROYECTO: GALA6 Chocoletra Bot
=============================================================
Bot loop + Flask Dashboard ek hi process mein.
Signals/Trades seedha BOT_STATE mein jaate hain
aur dashboard par real-time dikhte hain.
=============================================================
"""

import os
import sys
import json
import queue
import sqlite3
import yaml
import hashlib
import hmac
import secrets
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from api.schwab_rate_guard import guarded_market_get
from flask import Flask, send_from_directory, jsonify, request, session
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash

# ── Force UTF-8 on Windows console
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ('utf-8', 'utf8'):
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from core.daily_loss_guard import DailyLossGuard
from core.ticker_execution import TickerExecutionRegistry
from core.live_capacity import max_real_tickers as real_ticker_limit
from core.runtime_reliability import (
    infer_closed_bar_epoch_ms,
    marker_within_1m_candle_span,
)
from agents.veto_observation_store import VetoObservationStore
from agents.signal_learning_store import SignalLearningStore
from api.schwab_oauth import (
    DEFAULT_REDIRECT_URI as SCHWAB_REDIRECT_URI,
    OAUTH_ROTATION_LOCK,
    SchwabOAuthFailure,
    SchwabOAuthCooldown,
    build_safe_diagnostic,
    code_fingerprint,
    exchange_authorization_code,
    refresh_oauth_token,
)

# Respect credentials injected by the operating system, service manager, or
# tests. Values from .env are only used when the process environment does not
# already define them.
load_dotenv(os.path.join(BASE_DIR, ".env"), override=False)

# ── Flask
app = Flask(__name__, static_folder='dashboard', static_url_path='')
app.secret_key = os.getenv('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(hours=12)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.getenv("DASHBOARD_HTTPS", "false").lower() == "true",
    # Background polls must not overwrite a newer OAuth session cookie with
    # the older cookie they received before the operator opened Schwab login.
    SESSION_REFRESH_EACH_REQUEST=False,
)

# ── Schwab endpoints
SCHWAB_TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
SCHWAB_AUTH_URL  = "https://api.schwabapi.com/v1/oauth/authorize"
SCHWAB_MKT_URL   = "https://api.schwabapi.com/marketdata/v1"
SCHWAB_TRADE_URL = "https://api.schwabapi.com/trader/v1"
SCHWAB_AUTH_NOTIFICATION_SECONDS = 5 * 60
SCHWAB_STATUS_CACHE_SECONDS = 20

# =============================================================
# SHARED STATE  (Bot engine reads/writes this directly)
# =============================================================
BOT_STATE = {
    "running":   False,
    "mode":      "paper",
    "tickers":   [
        "AAPL", "AMD", "AMZN", "BA", "META", "NVDA", "RCL", "UAL",
        "TSLA", "UNH", "PINS", "NCLH", "MSFT", "NKE", "PYPL", "NFLX",
        "ADBE",
    ],
    "signals":   [],
    "trades":    [],  # execution events for the current dashboard process
    "active_positions": {},  # fallback only; engine snapshot is authoritative
    "logs":      [],
    "stats": {
        "buy": 0, "sell": 0, "omr": 0, "omt": 0,
        "trades_open": 0, "trades_closed": 0,
        "pnl_total": 0.0,
    },
    "schwab_token":         None,
    "schwab_token_expiry":  None,
    "schwab_refresh_token": None,
    "schwab_auth_state":    "unknown",
    "schwab_auth_message":  "",
    "schwab_auth_verified": False,
    "schwab_auth_verified_at": None,
    "schwab_auth_next_check_monotonic": 0.0,
    "schwab_reauthorization_required": False,
    "schwab_oauth_diagnostic": None,
    "schwab_api_diagnostic": None,
    "schwab_oauth_last_code_fingerprint": "",
    "account_number":       os.getenv("SCHWAB_ACCOUNT_NUMBER", ""),
}

CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
ENV_PATH    = os.path.join(BASE_DIR, ".env")
# Lista de activos persistente. Se guarda aparte (no en config.yaml) para no
# perder los comentarios del config al reescribirlo. Sobrevive a los reinicios.
TICKERS_PATH = os.path.join(BASE_DIR, "data", "tickers_activos.json")
_SIGNAL_LOCK = threading.RLock()
_SIGNAL_MEMORY_LIMIT = 5000
_CANDLE_LOCK = threading.RLock()
_CANDLE_PRIMED = set()
_CANDLE_ARCHIVE_QUEUE = queue.Queue(maxsize=64)
_CANDLE_ARCHIVE_WORKER = None
_SCHWAB_AUTH_LOCK = threading.RLock()
_SERVER_LOG_LOCK = threading.RLock()
_SERVER_LOG_PATH = os.path.join(BASE_DIR, "logs", "dashboard_server.log")


def _order_audit_path():
    """Returns the same persistent audit path used by the trading engine."""
    path = "data/auditoria_ordenes.csv"
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        path = (cfg.get("logging", {}) or {}).get("auditoria_ordenes", path)
    except Exception:
        pass
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    return os.path.normpath(path)


def _execution_quality_path():
    """Returns the separate signal/limit/fill slippage ledger path."""

    path = "data/slippage_entradas.csv"
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        path = (cfg.get("logging", {}) or {}).get("slippage_entradas", path)
    except Exception:
        pass
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    return os.path.normpath(path)

def _history_path():
    """Return the persistent closed-trade history path from config.yaml."""
    path = "data/historial_operaciones.csv"
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        path = (cfg.get("logging", {}) or {}).get("historial_operaciones", path)
    except Exception:
        pass
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    return os.path.normpath(path)


def _signal_history_path():
    """Return the append-only signal ledger configured for the dashboard."""
    path = "data/historial_senales.jsonl"
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        path = (cfg.get("logging", {}) or {}).get("historial_senales", path)
    except Exception:
        pass
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    return os.path.normpath(path)


def _candle_history_path():
    """Return the durable SQLite archive used by historical dashboard charts."""
    path = "data/historial_velas.sqlite3"
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        path = (cfg.get("logging", {}) or {}).get("historial_velas", path)
    except Exception:
        pass
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    return os.path.normpath(path)


def _local_history_settings():
    """Return the existing training-history directory and its source timezone."""
    folder = "data/historico"
    source_tz = "UTC"
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        ai_cfg = cfg.get("ai", {}) or {}
        folder = ai_cfg.get("historico_dir", folder)
        source_tz = ai_cfg.get("historico_tz", source_tz)
    except Exception:
        pass
    if not os.path.isabs(folder):
        folder = os.path.join(BASE_DIR, folder)
    return os.path.normpath(folder), str(source_tz or "UTC")


def _open_candle_db():
    """Open and initialize the local one-minute candle archive."""
    path = _candle_history_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS candles (
            ticker TEXT NOT NULL,
            epoch_ms INTEGER NOT NULL,
            timestamp_iso TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'local',
            PRIMARY KEY (ticker, epoch_ms)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_candles_range "
        "ON candles(ticker, epoch_ms)"
    )
    return connection


def _normalise_chart_candle(candle: dict):
    """Validate one chart candle and return the stable persisted representation."""
    try:
        epoch_ms = int(candle["epoch_ms"])
        candle_ny = datetime.fromtimestamp(
            epoch_ms / 1000.0, tz=timezone.utc
        ).astimezone(_ahora_ny_datetime().tzinfo)
        return {
            "epoch_ms": epoch_ms,
            "timestamp_iso": candle_ny.isoformat(timespec="seconds"),
            "open": round(float(candle["open"]), 4),
            "high": round(float(candle["high"]), 4),
            "low": round(float(candle["low"]), 4),
            "close": round(float(candle["close"]), 4),
            "volume": int(float(candle.get("volume", 0) or 0)),
        }
    except (KeyError, TypeError, ValueError, OSError):
        return None


def _store_chart_candles(ticker: str, candles, source: str = "local"):
    """Upsert real candles so later chart clicks do not depend on Schwab."""
    ticker = str(ticker or "").strip().upper()
    records = []
    for candle in candles or []:
        row = _normalise_chart_candle(candle)
        if row is not None:
            records.append((
                ticker, row["epoch_ms"], row["timestamp_iso"], row["open"],
                row["high"], row["low"], row["close"], row["volume"], source,
            ))
    if not ticker or not records:
        return 0
    with _CANDLE_LOCK:
        with _open_candle_db() as connection:
            connection.executemany(
                """
                INSERT INTO candles (
                    ticker, epoch_ms, timestamp_iso, open, high, low, close,
                    volume, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, epoch_ms) DO UPDATE SET
                    timestamp_iso=excluded.timestamp_iso,
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    source=excluded.source
                """,
                records,
            )
    return len(records)


def _read_cached_chart_candles(ticker: str, start_ny: datetime, end_ny: datetime):
    """Read a historical chart window from the durable local SQLite archive."""
    path = _candle_history_path()
    if not os.path.exists(path):
        return []
    ticker = str(ticker or "").strip().upper()
    start_ms = int(start_ny.timestamp() * 1000)
    end_ms = int(end_ny.timestamp() * 1000)
    with _CANDLE_LOCK:
        with _open_candle_db() as connection:
            records = connection.execute(
                """
                SELECT epoch_ms, timestamp_iso, open, high, low, close, volume
                FROM candles
                WHERE ticker = ? AND epoch_ms BETWEEN ? AND ?
                ORDER BY epoch_ms
                """,
                (ticker, start_ms, end_ms),
            ).fetchall()
    return [
        {
            "epoch_ms": row[0], "timestamp_iso": row[1], "open": row[2],
            "high": row[3], "low": row[4], "close": row[5], "volume": row[6],
        }
        for row in records
    ]


def _dataframe_chart_candles(frame):
    """Convert the bot's UTC-indexed OHLCV DataFrame into dashboard rows."""
    if frame is None or len(frame) == 0:
        return []
    import pandas as pd

    rows = []
    for index, candle in frame.iterrows():
        try:
            stamp = pd.Timestamp(index)
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize("UTC")
            else:
                stamp = stamp.tz_convert("UTC")
            row = _normalise_chart_candle({
                "epoch_ms": int(stamp.timestamp() * 1000),
                "open": candle["open"], "high": candle["high"],
                "low": candle["low"], "close": candle["close"],
                "volume": candle.get("volume", 0),
            })
            if row is not None:
                rows.append(row)
        except (KeyError, TypeError, ValueError, OSError):
            continue
    rows.sort(key=lambda row: row["epoch_ms"])
    return rows


def _read_training_history_candles(
    ticker: str, start_ny: datetime, end_ny: datetime
):
    """Read the requested window from data/historico/<TICKER>.csv."""
    from agents.historico_loader import _leer_csv_1m, _ruta_csv

    folder, source_tz = _local_history_settings()
    path = _ruta_csv(ticker, folder)
    if path is None:
        return []
    frame = _leer_csv_1m(path, source_tz)
    if frame is None or len(frame) == 0:
        return []
    start_utc = start_ny.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_ny.astimezone(timezone.utc).replace(tzinfo=None)
    frame = frame[(frame.index >= start_utc) & (frame.index <= end_utc)]
    return _dataframe_chart_candles(frame)


def _load_local_chart_candles(
    ticker: str, start_ny: datetime, end_ny: datetime
):
    """Use archived live candles first, then the existing local training CSV."""
    cached = _read_cached_chart_candles(ticker, start_ny, end_ny)
    if len(cached) >= 2:
        return cached, "local_archive"
    try:
        historical = _read_training_history_candles(ticker, start_ny, end_ny)
    except Exception as exc:
        _log("WARN", f"[Gráfico] Histórico local {ticker} no legible: {exc}")
        historical = []
    if historical:
        _store_chart_candles(ticker, historical, source="historico_csv")
    combined = {row["epoch_ms"]: row for row in cached}
    combined.update({row["epoch_ms"]: row for row in historical})
    rows = [combined[key] for key in sorted(combined)]
    return rows, ("historico_csv" if historical else "")


def _read_signal_history(*, ticker: str = "", limit: int = None):
    """Read valid signal rows oldest-first without losing earlier good rows.

    JSON Lines keeps each signal independent: an interrupted final write can be
    skipped without making the rest of the ledger unreadable.
    """
    path = _signal_history_path()
    if not os.path.exists(path):
        return []
    wanted = str(ticker or "").strip().upper()
    rows = []
    with _SIGNAL_LOCK:
        with open(path, "r", encoding="utf-8-sig") as fh:
            for raw in fh:
                try:
                    row = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(row, dict):
                    continue
                if wanted and str(row.get("ticker", "")).upper() != wanted:
                    continue
                # v3.3.1: old v3.3.0 rows could persist ai_threshold from the
                # production trainer even when the decision actually used a
                # SHADOW model.  The immutable decision snapshot is authoritative
                # and also repairs historical display rows (e.g. UAL 41%, not 0%).
                context = row.get("ai_context") or {}
                if isinstance(context, dict) and context.get("threshold") not in (None, ""):
                    try:
                        row["ai_threshold"] = round(float(context["threshold"]), 4)
                    except (TypeError, ValueError):
                        pass
                bar_epoch = (
                    context.get("signal_bar_epoch_ms")
                    if isinstance(context, dict) else None
                )
                if bar_epoch in (None, ""):
                    bar_epoch = infer_closed_bar_epoch_ms(row.get("epoch_ms"))
                try:
                    if bar_epoch is not None:
                        row["signal_bar_epoch_ms"] = int(bar_epoch)
                        row["chart_epoch_ms"] = int(bar_epoch)
                except (TypeError, ValueError):
                    pass
                rows.append(row)
    if limit is not None:
        rows = rows[-max(0, int(limit)):]
    return rows


def _append_signal_history(signal: dict):
    """Durably append one complete signal before exposing it to the UI."""
    path = _signal_history_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = json.dumps(signal, ensure_ascii=False, separators=(",", ":"))
    with _SIGNAL_LOCK:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(payload + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def _signal_counts(rows):
    counts = {"buy": 0, "sell": 0, "omr": 0, "omt": 0}
    keys = {"BUY": "buy", "SELL": "sell", "OM-R": "omr", "OM-T": "omt"}
    for row in rows:
        key = keys.get(str(row.get("tipo", "")).upper())
        if key:
            counts[key] += 1
    return counts


def _restore_signal_history():
    """Restore dashboard state and counters after a service restart."""
    rows = _read_signal_history()
    BOT_STATE["signals"] = rows[-_SIGNAL_MEMORY_LIMIT:]
    BOT_STATE["stats"].update(_signal_counts(rows))


def _read_history_rows(*, newest_first: bool = True):
    from core.trade_history import read_trade_history

    return read_trade_history(
        _history_path(), newest_first=newest_first, limit=None
    )


_DASHBOARD_HISTORY_CACHE = {}
_DASHBOARD_HISTORY_LOCK = threading.RLock()


def _dashboard_history_rows(*, newest_first=True):
    """Read-only provenance view, cached until either source ledger changes."""
    import csv
    from core.dashboard_evidence import annotate_history
    from core.trade_history import LEGACY_HISTORY_FIELDS, HISTORY_FIELDS

    history_path, audit_path = _history_path(), _order_audit_path()
    def signature(path):
        try:
            st = os.stat(path)
            return (path, st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            return (path, None)

    key = (signature(history_path), signature(audit_path))
    with _DASHBOARD_HISTORY_LOCK:
        if _DASHBOARD_HISTORY_CACHE.get("key") != key:
            rows = []
            if os.path.exists(history_path):
                with open(history_path, encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    fields = list(reader.fieldnames or [])
                    if fields and fields not in (LEGACY_HISTORY_FIELDS, HISTORY_FIELDS):
                        raise ValueError("Esquema histórico no reconocido; no se calculan resultados")
                    rows = list(reader)
                    if fields == LEGACY_HISTORY_FIELDS:
                        rows = [{**r, "evento": "POSITION_CLOSE"} for r in rows]
            try:
                with open(audit_path, encoding="utf-8-sig", newline="") as handle:
                    annotated = annotate_history(rows, csv.DictReader(handle))
            except (OSError, csv.Error):
                annotated = annotate_history(rows)
            _DASHBOARD_HISTORY_CACHE.clear()
            _DASHBOARD_HISTORY_CACHE.update(key=key, rows=annotated)
        rows = _DASHBOARD_HISTORY_CACHE["rows"]
        return [dict(row) for row in (reversed(rows) if newest_first else rows)]


def _closed_history_rows(rows):
    """Only closes affect PnL/counts; v2.9 also stores transparent open rows."""

    return [
        row for row in rows
        if str(row.get("evento", "POSITION_CLOSE") or "POSITION_CLOSE").upper()
        == "POSITION_CLOSE"
    ]


def _history_summary():
    """Persistent closed count/PnL, so dashboard stats survive restarts."""
    from core.dashboard_evidence import mode_of, session_date
    try:
        rows = _dashboard_history_rows(newest_first=False)
    except Exception:
        return {
            "available": False, "excluded_closed_count": 0,
            "is_broker_reported": False,
            "pnl_source": "UNAVAILABLE",
            "closed_count": 0, "pnl_total": None,
            "paper_pnl": None, "real_pnl": None,
            "paper_closed_count": 0, "real_closed_count": 0,
            "paper_realized_today": None, "real_realized_today": None,
            "paper_closed_today": 0, "real_closed_today": 0,
        }
    all_closed = _closed_history_rows(rows)
    excluded = sum(not r.get("performance_verified", False) for r in all_closed)
    closed = [r for r in all_closed if r.get("performance_verified")]
    by_mode = {"PAPER": 0.0, "REAL": 0.0}
    counts = {"PAPER": 0, "REAL": 0}
    today_pnl = {"PAPER": 0.0, "REAL": 0.0}
    today_counts = {"PAPER": 0, "REAL": 0}
    today_ny = _ahora_ny_datetime().strftime("%Y-%m-%d")
    for row in closed:
        try:
            value = float(row.get("pnl_usd", 0.0) or 0.0)
            mode = str(row.get("modo", "PAPER") or "PAPER").upper()
            if mode not in by_mode:
                mode = "PAPER"
            by_mode[mode] += value
            counts[mode] += 1
            if session_date(row) == today_ny:
                today_pnl[mode] += value
                today_counts[mode] += 1
        except (TypeError, ValueError):
            pass
    incomplete = {mode: any(not r.get("performance_verified") and mode_of(r) == mode
                            for r in all_closed) for mode in ("PAPER", "REAL")}
    incomplete_today = {mode: any(not r.get("performance_verified") and mode_of(r) == mode
                                  and session_date(r) == today_ny for r in all_closed)
                        for mode in ("PAPER", "REAL")}
    return {
        "available": True,
        "excluded_closed_count": excluded,
        "pnl_source": "LOCAL_RECORDED_FILLS_AND_PAPER_SIMULATION",
        "is_broker_reported": False,
        "closed_count": len(closed), "pnl_total": None,
        "paper_pnl": None if incomplete["PAPER"] else round(by_mode["PAPER"], 2),
        "real_pnl": None if incomplete["REAL"] else round(by_mode["REAL"], 2),
        "paper_closed_count": counts["PAPER"],
        "real_closed_count": counts["REAL"],
        "paper_realized_today": None if incomplete_today["PAPER"] else round(today_pnl["PAPER"], 2),
        "real_realized_today": None if incomplete_today["REAL"] else round(today_pnl["REAL"], 2),
        "paper_closed_today": today_counts["PAPER"],
        "real_closed_today": today_counts["REAL"],
    }


def _position_pnl_summary(positions):
    """Aggregate only currently open engine positions by execution mode."""
    summary = {
        "paper_open_count": 0, "real_open_count": 0,
        "paper_unrealized_pnl": 0.0, "real_unrealized_pnl": 0.0,
    }
    from core.dashboard_evidence import mode_of, number
    incomplete = set()
    for row in positions or []:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status", "OPEN") or "OPEN").upper()
        if status not in {"OPEN", "PARTIAL"}:
            continue
        mode = mode_of(row)
        if mode not in {"PAPER", "REAL"}:
            continue
        key = mode.lower()
        summary[f"{key}_open_count"] += 1
        value = number(row.get("floating_pnl"))
        if value is None or row.get("valuation_available") is False:
            incomplete.add(key)
        else:
            summary[f"{key}_unrealized_pnl"] += value
    for mode in ("paper", "real"):
        key = f"{mode}_unrealized_pnl"
        summary[key] = None if mode in incomplete else round(summary[key], 2)
    return summary


def _pnl_sum(*values):
    from core.dashboard_evidence import number
    parsed = [number(v) for v in values]
    return round(sum(parsed), 2) if all(v is not None for v in parsed) else None


def _engine_positions_snapshot():
    """Current positions from the trading engine, never inferred from history."""
    bot = globals().get("_ENGINE")
    if bot is not None and hasattr(bot, "dashboard_snapshot"):
        try:
            return bot.dashboard_snapshot()
        except Exception as exc:
            _log("WARN", f"[Dashboard] No se pudo leer posiciones del motor: {exc}")
    return list((BOT_STATE.get("active_positions") or {}).values())


def _broker_reconciliation_state_path(cfg: dict = None) -> str:
    if cfg is None:
        reader = globals().get("_read_config_runtime")
        cfg = reader() if callable(reader) else {}
    live = (cfg.get("live_trading", {}) or {}) if isinstance(cfg, dict) else {}
    path = str(live.get(
        "broker_reconciliation_state_path",
        "data/broker_reconciliation_state.json",
    ))
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    return os.path.normpath(path)


def _engine_broker_reconciliation_snapshot():
    """Broker/local coordination status, including persisted stopped state."""
    bot = globals().get("_ENGINE")
    if bot is not None and hasattr(bot, "broker_reconciliation_snapshot"):
        try:
            return bot.broker_reconciliation_snapshot()
        except Exception as exc:
            _log("WARN", f"[Dashboard] No se pudo leer reconciliación broker: {exc}")
    path = _broker_reconciliation_state_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                return raw
    except Exception as exc:
        _log("WARN", f"[Dashboard] Estado broker persistido ilegible: {exc}")
    return {
        "ok": True, "flat": True, "context": "NOT_STARTED",
        "message": "Sin reconciliación REAL pendiente",
        "positions": [], "open_order_symbols": [],
        "last_order_failures": {},
    }


def _refresh_broker_reconciliation_read_only():
    """Refresh positions/orders with Schwab GET calls only.

    This intentionally does not clear execution_pending.json and never submits,
    changes, or cancels an order. It lets the operator prove the current broker
    exposure while the daily-loss lock keeps the engine stopped.
    """
    bot = globals().get("_ENGINE")
    if bot is not None and hasattr(bot, "_refresh_broker_reconciliation"):
        state = bot._refresh_broker_reconciliation(
            context="DASHBOARD_READ_ONLY", require_flat=False
        )
        sync = {}
        reconcile_local = getattr(
            bot, "synchronize_stopped_real_positions_from_broker", None
        )
        if callable(reconcile_local):
            sync = reconcile_local(state)
            # The local sync can clear resolved failure banners and amend the
            # persisted message, so return the post-sync snapshot.
            try:
                state = bot.broker_reconciliation_snapshot()
            except Exception:
                pass
        state = dict(state or {})
        state["local_sync"] = sync
        return state

    from api.schwab_client import SchwabClient

    cfg = _read_config_runtime()
    account = str(
        BOT_STATE.get("account_number", "")
        or get_env("SCHWAB_ACCOUNT_NUMBER", "")
        or ""
    ).strip()
    if not account:
        raise RuntimeError("SCHWAB_ACCOUNT_NUMBER está vacío")
    client = SchwabClient(cfg)
    if not client._asegurar_token():
        raise RuntimeError("OAuth de Schwab no disponible; vuelva a conectar Schwab")
    account_hash = client.obtener_account_hash(account)
    if not account_hash:
        raise RuntimeError("No se pudo resolver el hash de la cuenta Schwab")
    raw_positions = client.obtener_posiciones(account_hash)
    raw_orders = client.obtener_ordenes_abiertas(account_hash)
    if raw_positions is None or raw_orders is None:
        raise RuntimeError("Schwab no confirmó posiciones y órdenes")

    cfg_real = set(_real_tickers_runtime(cfg))
    positions = []
    for row in raw_positions:
        ticker = str(row.get("symbol", "") or "").upper()
        qty = int(row.get("cantidad", 0) or 0)
        side = str(row.get("tipo", "") or "").upper()
        if ticker in cfg_real and qty > 0:
            positions.append({"ticker": ticker, "side": side, "quantity": qty})

    def iter_orders(rows):
        for order in rows or []:
            if not isinstance(order, dict):
                continue
            yield order
            yield from iter_orders(order.get("childOrderStrategies") or [])

    order_symbols = []
    for order in iter_orders(raw_orders):
        for leg in order.get("orderLegCollection") or []:
            ticker = str((leg.get("instrument") or {}).get("symbol", "") or "").upper()
            if ticker in cfg_real:
                order_symbols.append(ticker)

    previous = _engine_broker_reconciliation_snapshot()
    flat = not positions and not order_symbols
    state = {
        "ok": True,
        "flat": flat,
        "context": "DASHBOARD_READ_ONLY",
        "message": "Broker confirmado plano" if flat else "Broker sincronizado; exposición REAL conocida",
        "positions": positions,
        "open_order_symbols": sorted(set(order_symbols)),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "last_order_failures": dict(previous.get("last_order_failures") or {}),
        "read_only": True,
    }
    path = _broker_reconciliation_state_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    os.replace(tmp, path)
    return state


def _dashboard_stats():
    history = _history_summary()
    positions = _engine_positions_snapshot()
    stats = dict(BOT_STATE.get("stats") or {})
    broker = _engine_broker_reconciliation_snapshot()
    broker_positions = list(broker.get("positions") or [])
    live = _position_pnl_summary(positions)
    stats["trades_open"] = len(positions) if positions else len(broker_positions)
    stats["broker_positions_open"] = len(broker_positions)
    stats["pnl_source"] = history["pnl_source"]
    stats["is_broker_reported"] = False
    stats["excluded_closed_count"] = history["excluded_closed_count"]
    stats["trades_closed"] = history["closed_count"]
    stats["pnl_total"] = history["pnl_total"]
    stats["paper_pnl"] = history["paper_pnl"]
    stats["real_pnl"] = history["real_pnl"]
    stats["paper_closed_count"] = history["paper_closed_count"]
    stats["real_closed_count"] = history["real_closed_count"]
    stats.update(live)
    stats["paper_realized_today"] = history["paper_realized_today"]
    stats["real_realized_today"] = history["real_realized_today"]
    stats["paper_closed_today"] = history["paper_closed_today"]
    stats["real_closed_today"] = history["real_closed_today"]
    stats["paper_session_pnl"] = _pnl_sum(
        history["paper_realized_today"], live["paper_unrealized_pnl"]
    )
    stats["real_session_pnl"] = _pnl_sum(
        history["real_realized_today"], live["real_unrealized_pnl"]
    )
    return stats


def _engine_daily_risk_snapshot():
    """Daily net-loss state from the engine or persistent state when stopped."""
    bot = globals().get("_ENGINE")
    if bot is not None and hasattr(bot, "daily_risk_snapshot"):
        try:
            return bot.daily_risk_snapshot()
        except Exception as exc:
            _log("WARN", f"[Dashboard] No se pudo leer riesgo diario: {exc}")

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        paper = bool((cfg.get("risk", {}) or {}).get("paper_trading", True))
        guard = DailyLossGuard(
            cfg,
            history_path=_history_path(),
        )
        return guard.snapshot(paper, unrealized_pnl=0.0)
    except Exception:
        return {
            "enabled": False, "blocked": False, "mode": "PAPER",
            "limit_usd": 0.0, "realized_pnl": 0.0,
            "unrealized_pnl": 0.0, "net_pnl": 0.0,
            "remaining_to_limit_usd": 0.0,
        }


def _cargar_tickers_guardados():
    """Devuelve la lista de activos a monitorear. Prioridad: 1) el archivo
    persistente del dashboard, 2) market.tickers de config.yaml. Así la lista NO
    se reinicia a 5 en cada arranque y los tickers que agrega el cliente
    sobreviven al reinicio (antes solo vivían en memoria y se borraban)."""
    try:
        if os.path.exists(TICKERS_PATH):
            with open(TICKERS_PATH, "r", encoding="utf-8-sig") as f:
                t = json.load(f)
            if isinstance(t, list) and t:
                return [str(x).strip().upper() for x in t if str(x).strip()]
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        t = (cfg.get("market", {}) or {}).get("tickers")
        if t:
            return [str(x).strip().upper() for x in t if str(x).strip()]
    except Exception as e:
        print(f"[INIT] No se pudieron cargar tickers guardados: {e}")
    return None

_tickers_guardados = _cargar_tickers_guardados()
if _tickers_guardados:
    BOT_STATE["tickers"] = _tickers_guardados

# =============================================================
# LOGGING HELPER
# =============================================================
def _hora_ny(fmt: str = "%H:%M:%S") -> str:
    """Hora actual en Nueva York (hora del MERCADO), independiente de la zona
    horaria del VPS y del navegador, para que logs, señales y panel muestren
    siempre la misma hora que el mercado y el broker."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime(fmt)
    except Exception:
        return datetime.now().strftime(fmt)


def _ahora_ny_datetime():
    """Timezone-aware New York time used by persistent signal identifiers."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now().astimezone()


def _log(nivel: str, mensaje: str):
    """Thread-safe dashboard/control log in memory, console, and disk.

    v3.3.1 persists control-plane messages so a later incident review can prove
    whether a stop came from the dashboard, scheduler, or a startup guard.
    """
    ts = _hora_ny()
    BOT_STATE["logs"].append({
        "nivel":     nivel,
        "mensaje":   mensaje,
        "timestamp": ts,
    })
    if len(BOT_STATE["logs"]) > 2000:
        BOT_STATE["logs"] = BOT_STATE["logs"][-1000:]
    try:
        os.makedirs(os.path.dirname(_SERVER_LOG_PATH), exist_ok=True)
        with _SERVER_LOG_LOCK:
            with open(_SERVER_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(f"[{datetime.now(timezone.utc).isoformat(timespec='milliseconds')}] "
                         f"[NY {ts}] {nivel}: {mensaje}\n")
    except Exception:
        # Logging must never take down trading/control endpoints.
        pass
    try:
        print(f"[{ts}] {nivel}: {mensaje}")
    except UnicodeEncodeError:
        pass

agregar_log = _log  # alias for backward compat

# =============================================================
# ENV HELPERS
# =============================================================
def reload_env(*, override: bool = True):
    """Reload .env after an explicit dashboard save.

    Normal reads must not reload the file on every request because doing so
    would unexpectedly replace credentials supplied by the host environment.
    """
    load_dotenv(ENV_PATH, override=override)

def read_env_dict() -> dict:
    d = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    d[k.strip()] = v.strip()
    return d

def write_env_dict(d: dict):
    lines_new = []
    written = set()
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("#") or s == "":
                    lines_new.append(line if line.endswith("\n") else line + "\n")
                elif "=" in s:
                    k = s.split("=", 1)[0].strip()
                    if k in d:
                        lines_new.append(f"{k}={d[k]}\n")
                        written.add(k)
                    else:
                        lines_new.append(line if line.endswith("\n") else line + "\n")
    for k, v in d.items():
        if k not in written:
            lines_new.append(f"{k}={v}\n")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines_new)

def get_env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _clear_schwab_oauth_tokens(reason: str = "Credenciales OAuth modificadas"):
    """Forget tokens bound to old app credentials without exposing them."""
    env_d = read_env_dict()
    env_d["SCHWAB_ACCESS_TOKEN"] = ""
    env_d["SCHWAB_REFRESH_TOKEN"] = ""
    env_d["SCHWAB_TOKEN_EXPIRES_AT"] = ""
    write_env_dict(env_d)
    for key in ("SCHWAB_ACCESS_TOKEN", "SCHWAB_REFRESH_TOKEN", "SCHWAB_TOKEN_EXPIRES_AT"):
        os.environ.pop(key, None)
    BOT_STATE["schwab_token"] = None
    BOT_STATE["schwab_refresh_token"] = None
    BOT_STATE["schwab_token_expiry"] = None
    BOT_STATE["schwab_reauthorization_required"] = True
    BOT_STATE["schwab_oauth_diagnostic"] = None
    BOT_STATE["schwab_oauth_last_code_fingerprint"] = ""
    _set_schwab_auth_state(
        "not_connected",
        f"{reason}. Complete una autorización OAuth nueva.",
        verified=True,
        cache_seconds=0,
    )
    engine = globals().get("_ENGINE")
    if engine is not None and getattr(engine, "schwab", None) is not None:
        try:
            engine.schwab.access_token = None
            engine.schwab.refresh_token = None
            engine.schwab.token_expiry = None
            engine.schwab.token_es_usuario = False
            engine.schwab.reauthorization_required = True
            engine.schwab.auth_error = reason
            engine.schwab._session.headers.pop("Authorization", None)
        except Exception:
            pass


def _parse_schwab_token_expiry(raw_value):
    """Parse the persisted OAuth expiry into the naive local format in use here."""
    raw = str(raw_value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        expiry = datetime.fromisoformat(raw)
        if expiry.tzinfo is not None:
            expiry = expiry.astimezone().replace(tzinfo=None)
        return expiry
    except (TypeError, ValueError):
        return None


def _load_schwab_auth_from_env():
    """Restore OAuth metadata so the first dashboard check sees the real state."""
    access_token = get_env("SCHWAB_ACCESS_TOKEN")
    refresh_token = get_env("SCHWAB_REFRESH_TOKEN")
    expiry = _parse_schwab_token_expiry(get_env("SCHWAB_TOKEN_EXPIRES_AT"))
    BOT_STATE["schwab_token"] = access_token or None
    BOT_STATE["schwab_refresh_token"] = refresh_token or None
    BOT_STATE["schwab_token_expiry"] = expiry
    if access_token or refresh_token:
        BOT_STATE["schwab_auth_message"] = (
            "Credenciales Schwab guardadas; esperando verificación en vivo."
        )


def _persist_schwab_credentials(access_token: str, refresh_token: str,
                                expiry):
    """Persist rotated credentials and update this process without a restart."""
    updates = {
        "SCHWAB_ACCESS_TOKEN": access_token or "",
        "SCHWAB_REFRESH_TOKEN": refresh_token or "",
        "SCHWAB_TOKEN_EXPIRES_AT": (
            expiry.isoformat(timespec="seconds") if expiry else ""
        ),
    }
    env_d = read_env_dict()
    env_d.update(updates)
    # load_dotenv() does not overwrite an already-running process. Keeping these
    # values aligned also prevents reuse if disk persistence fails.
    os.environ.update(updates)
    write_env_dict(env_d)


_load_schwab_auth_from_env()

def get_dashboard_user():     return get_env("DASHBOARD_USERNAME", "")
def get_dashboard_password(): return get_env("DASHBOARD_PASSWORD", "")
def get_dashboard_password_hash(): return get_env("DASHBOARD_PASSWORD_HASH", "")


def _dashboard_password_ok(candidate: str) -> bool:
    """Accept a password hash first; plaintext remains a migration fallback."""
    stored_hash = get_dashboard_password_hash()
    if stored_hash:
        try:
            return bool(check_password_hash(stored_hash, candidate))
        except (TypeError, ValueError):
            return False
    legacy = get_dashboard_password()
    return bool(legacy) and hmac.compare_digest(candidate, legacy)


_LOGIN_ATTEMPTS = defaultdict(list)
_LOGIN_ATTEMPTS_LOCK = threading.Lock()
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_FAILURES = 5


def _login_client_key() -> str:
    # The dashboard binds to localhost by default. Do not trust X-Forwarded-For
    # unless a separately configured reverse proxy validates it.
    return request.remote_addr or "unknown"


def _login_retry_after(client_key: str) -> int:
    now = time.monotonic()
    cutoff = now - _LOGIN_WINDOW_SECONDS
    with _LOGIN_ATTEMPTS_LOCK:
        recent = [t for t in _LOGIN_ATTEMPTS[client_key] if t >= cutoff]
        _LOGIN_ATTEMPTS[client_key] = recent
        if len(recent) < _LOGIN_MAX_FAILURES:
            return 0
        return max(1, int(_LOGIN_WINDOW_SECONDS - (now - recent[0])))


def _register_login_failure(client_key: str):
    with _LOGIN_ATTEMPTS_LOCK:
        _LOGIN_ATTEMPTS[client_key].append(time.monotonic())


def _clear_login_failures(client_key: str):
    with _LOGIN_ATTEMPTS_LOCK:
        _LOGIN_ATTEMPTS.pop(client_key, None)

# =============================================================
# SCHWAB AUTH
# =============================================================
def _set_schwab_auth_state(state: str, message: str = "", *,
                           verified: bool = False,
                           cache_seconds: int = SCHWAB_STATUS_CACHE_SECONDS):
    """Store a non-sensitive broker-auth result for the dashboard."""
    now = datetime.now(timezone.utc)
    BOT_STATE["schwab_auth_state"] = state
    BOT_STATE["schwab_auth_message"] = message
    BOT_STATE["schwab_auth_verified"] = bool(verified)
    BOT_STATE["schwab_auth_verified_at"] = (
        now.isoformat(timespec="seconds") if verified else None
    )
    BOT_STATE["schwab_auth_next_check_monotonic"] = (
        time.monotonic() + max(0, int(cache_seconds))
    )
    if state == "connected":
        BOT_STATE["schwab_reauthorization_required"] = False
    elif state == "expired":
        BOT_STATE["schwab_reauthorization_required"] = True


def _schwab_auth_snapshot() -> dict:
    """Return the dashboard contract without exposing OAuth credentials."""
    state = str(BOT_STATE.get("schwab_auth_state") or "unknown")
    expiry = BOT_STATE.get("schwab_token_expiry")
    secs = None
    if expiry:
        try:
            secs = max(0, int((expiry - datetime.now()).total_seconds()))
        except (TypeError, ValueError):
            secs = None
    next_check = max(
        0,
        int(
            float(BOT_STATE.get("schwab_auth_next_check_monotonic") or 0.0)
            - time.monotonic()
        ),
    )
    return {
        "status": "ok",
        "connected": state == "connected",
        "auth_state": state,
        "auth_expired": state == "expired",
        "verified": bool(BOT_STATE.get("schwab_auth_verified")),
        "verified_at": BOT_STATE.get("schwab_auth_verified_at"),
        "message": BOT_STATE.get("schwab_auth_message") or "",
        "account_number": BOT_STATE.get("account_number", ""),
        "expires_in_seconds": secs,
        "token_expires_at": (
            expiry.isoformat(timespec="seconds") if expiry else None
        ),
        "next_verification_in_seconds": next_check,
        "notification_interval_seconds": SCHWAB_AUTH_NOTIFICATION_SECONDS,
        "oauth_diagnostic": BOT_STATE.get("schwab_oauth_diagnostic"),
        "api_diagnostic": BOT_STATE.get("schwab_api_diagnostic"),
    }


def _current_schwab_access_token() -> str:
    """Return a token only while reauthorization has not been confirmed."""
    if BOT_STATE.get("schwab_reauthorization_required"):
        return ""
    return BOT_STATE.get("schwab_token") or get_env("SCHWAB_ACCESS_TOKEN")


def _mark_schwab_expired(message: str):
    BOT_STATE["schwab_token"] = None
    BOT_STATE["schwab_token_expiry"] = None
    _set_schwab_auth_state(
        "expired",
        message,
        verified=True,
        cache_seconds=SCHWAB_AUTH_NOTIFICATION_SECONDS,
    )


def schwab_get_token_cc() -> bool:
    """Alias legado: Schwab Trader API requiere OAuth authorization_code."""
    _log(
        "WARN",
        "[Schwab] No hay OAuth de usuario; conecte la cuenta desde el dashboard",
    )
    return False

def schwab_ensure_token():
    """Renueva el token si esta por expirar."""
    if BOT_STATE.get("schwab_reauthorization_required"):
        return False
    expiry = BOT_STATE.get("schwab_token_expiry")
    if not expiry or datetime.now() >= expiry:
        # Intentar refresh primero
        refresh = BOT_STATE.get("schwab_refresh_token") or get_env("SCHWAB_REFRESH_TOKEN")
        if refresh:
            if not schwab_refresh(refresh):
                # Do not hand an expired bearer token to any market-data,
                # order or chart request after a failed refresh.
                return False
        else:
            _mark_schwab_expired(
                "No hay una autorización Schwab vigente ni refresh_token. "
                "Abra Schwab OAuth nuevamente desde este dashboard."
            )
            return False
    return bool(_current_schwab_access_token())

def _sync_schwab_token_to_engine(access_token: str, refresh_token: str,
                                 expires_in: int = 1800):
    """Keep a running engine aligned when the dashboard rotates OAuth tokens."""
    engine = globals().get("_ENGINE")
    if engine is None or not access_token:
        return
    try:
        engine.schwab.autenticar_con_token(
            access_token,
            refresh_token,
            expires_in=expires_in,
            expires_at=BOT_STATE.get("schwab_token_expiry"),
        )
        engine.ejecutor_hibrido.set_token(access_token)
    except Exception as exc:
        _log(
            "WARN",
            f"[Schwab] Token renovado, pero no se pudo sincronizar el motor: "
            f"{type(exc).__name__}",
        )


def schwab_refresh(refresh_token: str, *, rejected_access_token: str = "") -> bool:
    """Serialize refresh-token rotation across dashboard and bot threads."""
    with _SCHWAB_AUTH_LOCK:
        return _schwab_refresh_locked(
            refresh_token, rejected_access_token=rejected_access_token
        )


def _schwab_refresh_locked(refresh_token: str, *, rejected_access_token: str = "") -> bool:
    try:
        with OAUTH_ROTATION_LOCK:
            # Adopt a refresh already published by the engine, but never reuse
            # the bearer Schwab just rejected with 401.
            latest_access = get_env("SCHWAB_ACCESS_TOKEN").strip()
            latest_expiry = _parse_schwab_token_expiry(
                get_env("SCHWAB_TOKEN_EXPIRES_AT")
            )
            if (
                latest_access and latest_access != rejected_access_token
                and latest_expiry and datetime.now() < latest_expiry
            ):
                BOT_STATE["schwab_token"] = latest_access
                BOT_STATE["schwab_refresh_token"] = get_env("SCHWAB_REFRESH_TOKEN")
                BOT_STATE["schwab_token_expiry"] = latest_expiry
                return True
            cid = get_env("SCHWAB_CLIENT_ID").strip()
            csec = get_env("SCHWAB_CLIENT_SECRET").strip()
            # A different SchwabClient may have rotated the refresh token just
            # before this call. Re-read the process value while holding the
            # shared OAuth lock so an older token is never replayed.
            refresh_token = get_env("SCHWAB_REFRESH_TOKEN").strip() or refresh_token
            d = refresh_oauth_token(
                cid,
                csec,
                refresh_token,
                redirect_uri=SCHWAB_REDIRECT_URI,
                timeout=15,
                http_post=requests.post,
            )
            new_access_token = d.get("access_token", "")
            expires_in = int(d.get("expires_in", 1800))
            BOT_STATE["schwab_token"] = new_access_token
            BOT_STATE["schwab_refresh_token"] = d.get("refresh_token", refresh_token)
            BOT_STATE["schwab_token_expiry"] = datetime.now() + timedelta(
                seconds=max(30, expires_in - 60)
            )
            BOT_STATE["schwab_oauth_diagnostic"] = None
            # Publish rotated tokens before releasing the shared lock. Otherwise
            # a ticker worker can replay the previous refresh token.
            try:
                _persist_schwab_credentials(
                    BOT_STATE["schwab_token"],
                    BOT_STATE["schwab_refresh_token"],
                    BOT_STATE["schwab_token_expiry"],
                )
            except OSError as exc:
                _log(
                    "WARN",
                    f"[Schwab] Token renovado pero no persistido: {type(exc).__name__}",
                )
        _sync_schwab_token_to_engine(
            BOT_STATE["schwab_token"],
            BOT_STATE["schwab_refresh_token"],
            expires_in,
        )
        _set_schwab_auth_state(
            "connected",
            "Autorización Schwab renovada correctamente.",
            verified=True,
        )
        _log("INFO", "[Schwab] Token renovado correctamente")
        return True
    except SchwabOAuthCooldown as exc:
        # A previous refresh got 403/429. Do not POST again or claim that the
        # access token was successfully refreshed. Operator can reconnect via
        # authorization_code, which clears the gate on success.
        _set_schwab_auth_state(
            "unavailable",
            "Renovación OAuth pausada tras rechazo HTTP %s. Revise permisos y "
            "estado de la aplicación Schwab." % (exc.last_status or "?"),
            verified=False,
            cache_seconds=max(30, int(exc.remaining_seconds)),
        )
        return False
    except SchwabOAuthFailure as exc:
        diag = dict(exc.diagnostic or {})
        BOT_STATE["schwab_oauth_diagnostic"] = diag
        oauth_error = str(diag.get("oauth_error") or "").lower()
        if oauth_error in {"invalid_grant", "invalid_token", "invalid_client"}:
            _mark_schwab_expired(
                "Schwab rechazó la renovación OAuth. Se requiere una nueva autorización."
            )
        else:
            _set_schwab_auth_state(
                "unavailable",
                f"No se pudo renovar Schwab (HTTP {diag.get('http_status') or '?'}).",
                verified=False,
            )
        _log_schwab_diagnostic("WARN", "Refresh rechazado", diag)
        return False
    except requests.RequestException as exc:
        _set_schwab_auth_state(
            "unavailable",
            "No se pudo verificar la renovación por un error de red.",
            verified=False,
        )
        _log("WARN", f"[Schwab] Refresh no verificable: {type(exc).__name__}")
        return False
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        _set_schwab_auth_state(
            "unavailable",
            "Schwab devolvió una respuesta de renovación inválida.",
            verified=False,
        )
        _log("WARN", f"[Schwab] Refresh inválido: {type(exc).__name__}")
        return False

def schwab_headers() -> dict:
    if not schwab_ensure_token():
        return {}
    token = _current_schwab_access_token()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _log_schwab_diagnostic(level: str, event: str, diagnostic: dict):
    """Persist only sanitized diagnostic fields, never response bodies/tokens."""
    fields = (
        "phase", "http_status", "oauth_error", "recommended_action",
        "client_id_tail", "content_type", "response_kind", "request_id", "description",
    )
    safe = {key: diagnostic[key] for key in fields if diagnostic.get(key)}
    _log(level, f"[Schwab][OAuth] {event} {json.dumps(safe, ensure_ascii=True)}")


def _verify_schwab_connection(*, force: bool = False) -> dict:
    """Verify OAuth against Schwab, refreshing once after a rejected token.

    Only a confirmed 401 is reported as ``expired``. A 403 can also mean
    app/endpoint permission rejection: don't rotate OAuth for a 403.
    Timeouts, rate limits, and Schwab/server failures are ``unavailable`` and therefore
    never trigger the five-minute expiration notification.
    """
    with _SCHWAB_AUTH_LOCK:
        if (
            not force
            and time.monotonic()
            < float(BOT_STATE.get("schwab_auth_next_check_monotonic") or 0.0)
        ):
            return _schwab_auth_snapshot()

        access_token = BOT_STATE.get("schwab_token") or get_env(
            "SCHWAB_ACCESS_TOKEN"
        )
        refresh_token = BOT_STATE.get("schwab_refresh_token") or get_env(
            "SCHWAB_REFRESH_TOKEN"
        )
        if not access_token and refresh_token:
            if schwab_refresh(refresh_token):
                access_token = BOT_STATE.get("schwab_token") or ""
            else:
                return _schwab_auth_snapshot()
        if not access_token:
            _set_schwab_auth_state(
                "not_connected",
                "Schwab no está conectado. Complete OAuth desde el dashboard.",
                verified=False,
            )
            return _schwab_auth_snapshot()

        if not schwab_ensure_token():
            return _schwab_auth_snapshot()
        access_token = _current_schwab_access_token()
        refresh_token = BOT_STATE.get("schwab_refresh_token") or get_env("SCHWAB_REFRESH_TOKEN")

        url = f"{SCHWAB_MKT_URL}/quotes"
        params = {"symbols": "BA"}

        def verify_request(token: str):
            return guarded_market_get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=10,
            )

        try:
            response = verify_request(access_token)
            if response.status_code == 401:
                if refresh_token and schwab_refresh(
                    refresh_token, rejected_access_token=access_token
                ):
                    access_token = BOT_STATE.get("schwab_token") or ""
                    response = verify_request(access_token)
                elif BOT_STATE.get("schwab_auth_state") == "expired":
                    # invalid_grant/invalid_token was confirmed by OAuth.
                    return _schwab_auth_snapshot()
                elif refresh_token:
                    _set_schwab_auth_state(
                        "unavailable",
                        "Schwab rechazó el access token, pero la renovación no "
                        "pudo verificarse por ahora.",
                        verified=False,
                    )
                    return _schwab_auth_snapshot()
                else:
                    _mark_schwab_expired(
                        "Schwab rechazó la autorización y no fue posible renovarla. "
                        "Vuelva a autorizar OAuth desde el dashboard."
                    )
                    return _schwab_auth_snapshot()

            if response.ok:
                BOT_STATE["schwab_api_diagnostic"] = None
                _set_schwab_auth_state(
                    "connected",
                    "Conexión Schwab verificada.",
                    verified=True,
                )
            elif response.status_code == 401:
                _mark_schwab_expired(
                    "Schwab rechazó la autorización después del intento de renovación. "
                    "Vuelva a autorizar OAuth desde el dashboard."
                )
            else:
                diag = build_safe_diagnostic(
                    response, phase="market_data_verification",
                    client_id=get_env("SCHWAB_CLIENT_ID"),
                    redirect_uri=SCHWAB_REDIRECT_URI,
                    secrets_to_redact=(
                        access_token, refresh_token, get_env("SCHWAB_CLIENT_SECRET"),
                    ),
                )
                BOT_STATE["schwab_api_diagnostic"] = diag
                _log_schwab_diagnostic("WARN", "Verificación rechazada", diag)
                _set_schwab_auth_state(
                    "unavailable",
                    f"Schwab no pudo verificarse (HTTP {response.status_code}).",
                    verified=False,
                )
        except requests.RequestException as exc:
            _set_schwab_auth_state(
                "unavailable",
                "No se pudo verificar Schwab por un error temporal de red.",
                verified=False,
            )
            _log("WARN", f"[Schwab] Verificación no disponible: {type(exc).__name__}")
        return _schwab_auth_snapshot()

def schwab_get_candles(ticker: str, cantidad: int = 200):
    """Only Schwab candles. Missing data never becomes fabricated market data."""
    schwab_ensure_token()
    hdrs = schwab_headers()
    if not hdrs:
        return None
    try:
        r = guarded_market_get(
            f"{SCHWAB_MKT_URL}/pricehistory",
            params={"symbol": ticker, "periodType": "day", "period": 5,
                    "frequencyType": "minute", "frequency": 1,
                    "needExtendedHoursData": False},
            headers=hdrs, timeout=15
        )
        if r.status_code == 401:
            refresh = BOT_STATE.get("schwab_refresh_token") or get_env(
                "SCHWAB_REFRESH_TOKEN"
            )
            if refresh:
                schwab_refresh(refresh)
            hdrs = schwab_headers()
            if not hdrs:
                return None
            r = guarded_market_get(
                f"{SCHWAB_MKT_URL}/pricehistory",
                params={"symbol": ticker, "periodType": "day", "period": 5,
                        "frequencyType": "minute", "frequency": 1,
                        "needExtendedHoursData": False},
                headers=hdrs, timeout=15
            )
        r.raise_for_status()
        datos = r.json()
        if "candles" not in datos or not datos["candles"]:
            return None
        import pandas as pd
        df = pd.DataFrame(datos["candles"])
        df["datetime"] = pd.to_datetime(df["datetime"], unit="ms", utc=True)
        df.set_index("datetime", inplace=True)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        df.sort_index(inplace=True)
        return df.tail(cantidad)
    except Exception as e:
        _log("WARN", f"[Schwab] Velas no disponibles para {ticker}: {type(e).__name__}; sin datos de sustitución")
        return None

def _signal_datetime(signal: dict):
    """Parse new and legacy signal timestamps as timezone-aware NY datetimes."""
    if signal.get("epoch_ms") is not None:
        try:
            return datetime.fromtimestamp(
                float(signal["epoch_ms"]) / 1000.0, tz=timezone.utc
            ).astimezone(_ahora_ny_datetime().tzinfo)
        except (TypeError, ValueError, OSError):
            pass
    raw_iso = str(signal.get("timestamp_iso") or "").strip()
    if raw_iso:
        try:
            parsed = datetime.fromisoformat(raw_iso.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_ahora_ny_datetime().tzinfo)
            return parsed.astimezone(_ahora_ny_datetime().tzinfo)
        except ValueError:
            pass
    raw_date = str(signal.get("date") or "").strip()
    raw_time = str(signal.get("timestamp") or "").strip()
    if raw_date and raw_time:
        try:
            parsed = datetime.fromisoformat(f"{raw_date}T{raw_time}")
            return parsed.replace(tzinfo=_ahora_ny_datetime().tzinfo)
        except ValueError:
            pass
    return None


def _fetch_chart_candles(
    ticker: str,
    start_ny: datetime,
    end_ny: datetime,
    *,
    include_extended: bool = False,
):
    """Fetch real Schwab 1-minute candles for an exact New York window."""
    headers = schwab_headers()
    if not headers:
        raise RuntimeError("Schwab no está conectado; vuelva a autorizar OAuth")
    params = {
        "symbol": ticker,
        "frequencyType": "minute",
        "frequency": 1,
        "startDate": int(start_ny.timestamp() * 1000),
        "endDate": int(end_ny.timestamp() * 1000),
        "needExtendedHoursData": bool(include_extended),
        "needPreviousClose": True,
    }

    response = guarded_market_get(
        f"{SCHWAB_MKT_URL}/pricehistory",
        params=params,
        headers=headers,
        timeout=15,
    )
    if response.status_code == 401:
        refresh = BOT_STATE.get("schwab_refresh_token") or get_env(
            "SCHWAB_REFRESH_TOKEN"
        )
        if refresh:
            schwab_refresh(refresh)
        headers = schwab_headers()
        if not headers:
            raise RuntimeError("La sesión Schwab expiró; vuelva a autorizar OAuth")
        response = guarded_market_get(
            f"{SCHWAB_MKT_URL}/pricehistory",
            params=params,
            headers=headers,
            timeout=15,
        )
    response.raise_for_status()
    payload = response.json()
    rows = []
    ny_tz = _ahora_ny_datetime().tzinfo
    for candle in payload.get("candles") or []:
        try:
            epoch_ms = int(candle["datetime"])
            candle_ny = datetime.fromtimestamp(
                epoch_ms / 1000.0, tz=timezone.utc
            ).astimezone(ny_tz)
            if candle_ny < start_ny or candle_ny > end_ny:
                continue
            rows.append({
                "epoch_ms": epoch_ms,
                "timestamp_iso": candle_ny.isoformat(timespec="seconds"),
                "open": round(float(candle["open"]), 4),
                "high": round(float(candle["high"]), 4),
                "low": round(float(candle["low"]), 4),
                "close": round(float(candle["close"]), 4),
                "volume": int(float(candle.get("volume", 0) or 0)),
            })
        except (KeyError, TypeError, ValueError, OSError):
            continue
    rows.sort(key=lambda row: row["epoch_ms"])
    return rows


def _archive_engine_candles(ticker: str, frame):
    """Persist the engine's real one-minute frame without slowing every cycle."""
    ticker = str(ticker or "").strip().upper()
    if frame is None or len(frame) == 0 or not ticker:
        return
    try:
        with _CANDLE_LOCK:
            first_snapshot = ticker not in _CANDLE_PRIMED
        rows = _dataframe_chart_candles(frame if first_snapshot else frame.tail(3))
        if rows:
            _store_chart_candles(ticker, rows, source="engine_1m")
        with _CANDLE_LOCK:
            _CANDLE_PRIMED.add(ticker)
    except Exception as exc:
        _log("WARN", f"[Velas] No se pudo archivar {ticker}: {exc}")


def _candle_archive_worker():
    """Serialize disk writes outside the latency-sensitive trading workers."""
    while True:
        ticker, frame = _CANDLE_ARCHIVE_QUEUE.get()
        try:
            _archive_engine_candles(ticker, frame)
        finally:
            _CANDLE_ARCHIVE_QUEUE.task_done()


def _ensure_candle_archive_worker():
    global _CANDLE_ARCHIVE_WORKER
    with _CANDLE_LOCK:
        if _CANDLE_ARCHIVE_WORKER is not None and _CANDLE_ARCHIVE_WORKER.is_alive():
            return
        _CANDLE_ARCHIVE_WORKER = threading.Thread(
            target=_candle_archive_worker,
            daemon=True,
            name="CandleArchive",
        )
        _CANDLE_ARCHIVE_WORKER.start()

# =============================================================
# BUILT-IN BOT LOOP (runs in background thread)
# =============================================================
_bot_thread: threading.Thread = None
_bot_stop_event = threading.Event()

def _register_signal(
    tipo, ticker, precio, atr, prob_ia=None, motivo="",
    ai_threshold=None, execution_mode="", ai_decision="", ai_context=None,
):
    """Persist one signal plus its AI/policy decision context."""
    now_ny = _ahora_ny_datetime()
    sig = {
        "signal_id": uuid.uuid4().hex,
        "tipo":      str(tipo or "").strip().upper(),
        "ticker":    str(ticker or "").strip().upper(),
        "precio":    round(float(precio), 2),
        "atr":       round(float(atr), 5),
        "timestamp": now_ny.strftime("%H:%M:%S"),
        "date":      now_ny.strftime("%Y-%m-%d"),
        "timestamp_iso": now_ny.isoformat(timespec="seconds"),
        "epoch_ms": int(now_ny.timestamp() * 1000),
    }
    # Always expose the field. ``null`` means no compatible model was loaded
    # and the dashboard renders it explicitly as ``N/A`` instead of hiding it
    # or inventing a 50% score.
    sig["prob_ia"] = (
        round(float(prob_ia), 3) if prob_ia is not None else None
    )
    if motivo:
        sig["motivo"] = str(motivo)
    sig["ai_threshold"] = (
        round(float(ai_threshold), 4) if ai_threshold is not None else None
    )
    sig["execution_mode"] = str(execution_mode or "").upper()
    sig["ai_decision"] = str(ai_decision or "").upper()
    sig["ai_context"] = ai_context or {}
    try:
        signal_bar_epoch = (sig["ai_context"] or {}).get("signal_bar_epoch_ms")
        if signal_bar_epoch not in (None, ""):
            sig["signal_bar_epoch_ms"] = int(signal_bar_epoch)
            sig["chart_epoch_ms"] = int(signal_bar_epoch)
    except (TypeError, ValueError, AttributeError):
        pass
    try:
        _append_signal_history(sig)
    except Exception as exc:
        _log("ERROR", f"[Señales] No se pudo persistir {sig['ticker']}: {exc}")
    with _SIGNAL_LOCK:
        BOT_STATE["signals"].append(sig)
        if len(BOT_STATE["signals"]) > _SIGNAL_MEMORY_LIMIT:
            BOT_STATE["signals"] = BOT_STATE["signals"][-_SIGNAL_MEMORY_LIMIT:]
    stat_key = {
        "BUY": "buy", "SELL": "sell", "OM-R": "omr", "OM-T": "omt",
    }.get(sig["tipo"])
    if stat_key:
        BOT_STATE["stats"][stat_key] += 1
    return sig


_restore_signal_history()

def _register_trade(tipo, ticker, precio, cantidad, estado="OPEN", pnl=0.0):
    """Register an execution event without confusing it with a live position.

    ``OPEN`` is an ENTRY event, not a permanent dashboard status. The current
    position list comes from ``ChocoletraBot.dashboard_snapshot()``. A fallback
    map is maintained only while the engine snapshot is temporarily unavailable.
    """
    estado = str(estado or "OPEN").upper()
    ticker = str(ticker or "").upper()
    event_label = {
        "OPEN": "ENTRY",
        "PARTIAL": "TP1_70_PERCENT",
        "CLOSED": "CLOSE",
    }.get(estado, estado)
    trade = {
        "tipo": tipo,
        "ticker": ticker,
        "precio": round(float(precio), 2),
        "acciones": int(cantidad),
        "estado": estado,      # backwards compatibility
        "evento": event_label, # unambiguous dashboard label
        "pnl": round(float(pnl), 2),
        "timestamp": _hora_ny(),
        "date": _hora_ny("%Y-%m-%d"),
    }
    BOT_STATE["trades"].append(trade)
    if len(BOT_STATE["trades"]) > 500:
        BOT_STATE["trades"] = BOT_STATE["trades"][-300:]

    active = BOT_STATE.setdefault("active_positions", {})
    if estado == "OPEN":
        active[ticker] = {
            "ticker": ticker,
            "side": tipo,
            "status": "OPEN",
            "initial_quantity": int(cantidad),
            "remaining_quantity": int(cantidad),
            "partial_quantity_executed": 0,
            "planned_partial_quantity": 0,
            "planned_remaining_quantity": int(cantidad),
            "entry_price": round(float(precio), 4),
            "stop_initial": 0.0,
            "stop_current": 0.0,
            "tp1_price": 0.0,
            "final_target": 0.0,
            "tp1_hit": False,
            "breakeven_active": False,
            "protection_mode": "",
            "realized_pnl": 0.0,
            "opened_at": f"{_hora_ny('%Y-%m-%d')}T{_hora_ny()}",
            "mode": str(BOT_STATE.get("mode", "paper")).upper(),
        }
    elif estado == "PARTIAL":
        pos = active.get(ticker)
        if pos:
            executed = int(cantidad)
            pos["partial_quantity_executed"] = (
                int(pos.get("partial_quantity_executed", 0) or 0) + executed
            )
            pos["remaining_quantity"] = max(
                0,
                int(pos.get("initial_quantity", 0) or 0)
                - int(pos["partial_quantity_executed"]),
            )
            pos["status"] = "PARTIAL"
            pos["tp1_hit"] = True
            pos["realized_pnl"] = round(
                float(pos.get("realized_pnl", 0.0) or 0.0) + float(pnl or 0.0),
                2,
            )
    elif estado == "CLOSED":
        active.pop(ticker, None)
        BOT_STATE["stats"]["trades_closed"] += 1
        BOT_STATE["stats"]["pnl_total"] += float(pnl or 0.0)

    BOT_STATE["stats"]["trades_open"] = len(active)

def _bot_cycle():
    """Un ciclo de procesamiento del bot para todos los tickers."""
    import numpy as np

    tickers = list(BOT_STATE["tickers"])
    _log("INFO", f"[Bot] Ciclo iniciado | {len(tickers)} tickers | {_hora_ny()}")

    for ticker in tickers:
        if not BOT_STATE["running"] or _bot_stop_event.is_set():
            break
        try:
            df = schwab_get_candles(ticker, cantidad=200)
            if df is None or len(df) < 35:
                _log("WARN", f"[Bot] Datos insuficientes para {ticker}")
                continue

            # ── Calcular MACD + EMA simple
            close = df["close"]
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            macd  = ema12 - ema26
            signal= macd.ewm(span=9, adjust=False).mean()
            hist  = macd - signal

            # EMA de tendencia
            ema20 = close.ewm(span=20, adjust=False).mean()

            # ATR
            high, low = df["high"], df["low"]
            tr = (high-low).combine((high-close.shift()).abs(), max).combine(
                 (low-close.shift()).abs(), max)
            atr = tr.ewm(span=14, adjust=False).mean()

            # Ultima vela
            precio_actual = float(close.iloc[-1])
            atr_actual    = float(atr.iloc[-1])
            hist_prev     = float(hist.iloc[-2])
            hist_curr     = float(hist.iloc[-1])
            tendencia_bull= float(close.iloc[-1]) > float(ema20.iloc[-1])

            # ── Logica de senal simple pero efectiva
            tipo = None
            if hist_prev < 0 and hist_curr > 0 and tendencia_bull:
                tipo = "BUY"
            elif hist_prev > 0 and hist_curr < 0 and not tendencia_bull:
                tipo = "SELL"
            elif abs(hist_curr) < atr_actual * 0.05:
                tipo = "OM-R"  # rango lateral
            else:
                # No signal this cycle — OK
                _log("INFO", f"[Bot] {ticker} @ ${precio_actual:.2f} | MACD: {hist_curr:.4f} | Sin senal")
                continue

            # Registrar senal
            _register_signal(tipo, ticker, precio_actual, atr_actual)
            _log("INFO" if tipo in ("BUY","SELL") else "INFO",
                 f"[Bot] [{tipo}] {ticker} @ ${precio_actual:.2f} | ATR: {atr_actual:.4f}")

            # ── Ejecutar orden (paper trading)
            if tipo in ("BUY", "SELL"):
                capital  = float(BOT_STATE.get("capital", 10000))
                riesgo   = capital * 0.02  # 2% riesgo por operacion
                cantidad = max(1, int(riesgo / (atr_actual * 2)))
                _register_trade(tipo, ticker, precio_actual, cantidad, "OPEN", 0.0)
                _log("INFO", f"[Bot] [TRADE] {tipo} {cantidad}x{ticker} @ ${precio_actual:.2f} (PAPER)")

        except Exception as e:
            _log("ERROR", f"[Bot] Error en {ticker}: {e}")

def _bot_loop():
    """Loop principal del bot — corre en hilo de background."""
    _log("INFO", "[Bot] Motor de trading iniciado")
    while not _bot_stop_event.is_set():
        if BOT_STATE["running"]:
            try:
                _bot_cycle()
            except Exception as e:
                _log("ERROR", f"[Bot] Error en ciclo: {e}")
        else:
            time.sleep(1)
            continue

        # Esperar el intervalo configurado
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            interval = cfg.get("engine", {}).get("loop_interval_sec", 60)
        except Exception:
            interval = 60

        # Esperar en chunks de 1s para poder detener rapidamente
        for _ in range(int(interval)):
            if _bot_stop_event.is_set() or not BOT_STATE["running"]:
                break
            time.sleep(1)

    _log("INFO", "[Bot] Motor de trading detenido")

# =============================================================
# AUTH MIDDLEWARE
# =============================================================
@app.before_request
def check_auth():
    public = {'/api/login', '/api/auth/status', '/'}
    if request.path.startswith('/api/') and request.path not in public:
        if not session.get('logged_in'):
            return jsonify({"status": "error", "message": "No autorizado"}), 401
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            expected = session.get("csrf_token", "")
            supplied = request.headers.get("X-CSRF-Token", "")
            if not expected or not supplied or not hmac.compare_digest(expected, supplied):
                return jsonify({
                    "status": "error",
                    "message": "Token CSRF inválido o ausente",
                }), 403


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.path.startswith("/api/") or response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store"
    if response.mimetype == "text/html":
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# =============================================================
# AUTH ROUTES
# =============================================================
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    user = data.get('username', '').strip()
    pwd  = data.get('password', '').strip()
    client_key = _login_client_key()
    retry_after = _login_retry_after(client_key)
    if retry_after:
        return jsonify({
            "status": "error",
            "message": "Demasiados intentos. Inténtelo nuevamente más tarde.",
            "retry_after_seconds": retry_after,
        }), 429

    expected_user = get_dashboard_user()
    if not expected_user or not (
        get_dashboard_password_hash() or get_dashboard_password()
    ):
        return jsonify({
            "status": "error",
            "message": "Credenciales del dashboard no configuradas en .env",
        }), 503
    user_ok = hmac.compare_digest(user, expected_user)
    pwd_ok = _dashboard_password_ok(pwd)
    if user_ok and pwd_ok:
        _clear_login_failures(client_key)
        session.clear()
        session.permanent = True
        session['logged_in'] = True
        session['username']  = user
        session['csrf_token'] = secrets.token_urlsafe(32)
        _log("INFO", f"Login exitoso - Usuario: {user}")
        return jsonify({
            "status": "ok",
            "message": "Login exitoso",
            "csrf_token": session["csrf_token"],
        })
    _register_login_failure(client_key)
    _log("WARN", f"Login fallido - Usuario: '{user}'")
    return jsonify({"status": "error", "message": "Usuario o contrasena incorrectos"}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"status": "ok"})

@app.route('/api/auth/status')
def auth_status():
    logged_in = bool(session.get("logged_in"))
    if logged_in and not session.get("csrf_token"):
        # Upgrade sessions created by an earlier dashboard version.
        session["csrf_token"] = secrets.token_urlsafe(32)
    return jsonify({
        "logged_in": logged_in,
        "username": session.get("username", ""),
        "csrf_token": session.get("csrf_token", "") if logged_in else "",
    })

# =============================================================
# MAIN ROUTE
# =============================================================
@app.route('/')
def dashboard():
    return send_from_directory('dashboard', 'index.html', conditional=False, max_age=0)

# =============================================================
# SCHWAB ROUTES
# =============================================================
@app.route('/api/schwab/auth-url')
def schwab_auth_url_route():
    cid = get_env("SCHWAB_CLIENT_ID").strip()
    if not cid:
        return jsonify({"status": "error", "message": "Falta SCHWAB_CLIENT_ID"}), 400
    oauth_state = secrets.token_urlsafe(24)
    session["schwab_oauth_state"] = oauth_state
    session["schwab_oauth_state_created_at"] = int(time.time())
    params = {
        "client_id": cid,
        "redirect_uri": SCHWAB_REDIRECT_URI,
        "response_type": "code",
        "state": oauth_state,
    }
    url = f"{SCHWAB_AUTH_URL}?{urlencode(params)}"
    return jsonify({
        "status": "ok",
        "auth_url": url,
        "client_id_tail": cid[-4:] if cid else "",
        "redirect_uri": SCHWAB_REDIRECT_URI,
    })


@app.route('/api/schwab/connect', methods=['POST'])
def schwab_connect():
    from api.schwab_oauth import parse_authorization_input

    data = request.get_json() or {}
    raw_input = str(data.get("auth_code", "") or "").strip()
    try:
        parsed = parse_authorization_input(
            raw_input, expected_redirect_uri=SCHWAB_REDIRECT_URI
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    auth_code = parsed["code"]
    returned_state = parsed.get("state", "")
    expected_state = str(session.get("schwab_oauth_state") or "")
    created_at = int(session.get("schwab_oauth_state_created_at") or 0)
    state_age = int(time.time()) - created_at
    if returned_state and (not expected_state or not created_at):
        return jsonify({
            "status": "error",
            "message": "La sesión OAuth no está disponible en este dashboard (pudo reiniciarse "
                       "el servidor o cambiar el navegador). Abra Schwab OAuth nuevamente aquí "
                       "y pegue la URL de retorno inmediatamente en esta misma sesión.",
            "diagnostic": {
                "phase": "callback_validation",
                "oauth_error": "oauth_session_missing",
                "recommended_action": "OPEN_NEW_AUTHORIZATION",
                "redirect_uri": SCHWAB_REDIRECT_URI,
            },
        }), 400
    if returned_state and expected_state and not hmac.compare_digest(returned_state, expected_state):
        return jsonify({
            "status": "error",
            "message": "La respuesta OAuth no pertenece a la autorización abierta por este dashboard.",
            "diagnostic": {
                "phase": "callback_validation",
                "oauth_error": "state_mismatch",
                "recommended_action": "OPEN_NEW_AUTHORIZATION",
                "redirect_uri": SCHWAB_REDIRECT_URI,
            },
        }), 400
    if expected_state and (state_age > 15 * 60 or state_age < 0):
        return jsonify({
            "status": "error",
            "message": "La sesión OAuth del dashboard venció. Abra Schwab OAuth nuevamente, "
                       "complete el acceso y pegue la URL de retorno inmediatamente aquí. "
                       "Use la URL de la nueva autorización.",
            "diagnostic": {
                "phase": "callback_validation",
                "oauth_error": "state_expired",
                "recommended_action": "OPEN_NEW_AUTHORIZATION",
                "redirect_uri": SCHWAB_REDIRECT_URI,
            },
        }), 400

    cid = get_env("SCHWAB_CLIENT_ID").strip()
    csec = get_env("SCHWAB_CLIENT_SECRET").strip()
    if not cid or not csec:
        return jsonify({
            "status": "error",
            "message": "Faltan Client ID o Client Secret de Schwab.",
        }), 400

    fingerprint = code_fingerprint(auth_code)
    with _SCHWAB_AUTH_LOCK:
        if fingerprint and fingerprint == BOT_STATE.get("schwab_oauth_last_code_fingerprint"):
            return jsonify({
                "status": "error",
                "message": "Este código OAuth ya fue utilizado. Abra Schwab OAuth y copie una URL nueva.",
                "diagnostic": {
                    "phase": "authorization_code_exchange",
                    "oauth_error": "authorization_code_reused",
                    "recommended_action": "NEW_AUTHORIZATION_CODE",
                    "client_id_tail": cid[-4:],
                    "redirect_uri": SCHWAB_REDIRECT_URI,
                },
            }), 400
        try:
            persistence_warning = ""
            with OAUTH_ROTATION_LOCK:
                d = exchange_authorization_code(
                    cid,
                    csec,
                    auth_code,
                    redirect_uri=SCHWAB_REDIRECT_URI,
                    timeout=15,
                    http_post=requests.post,
                )
                BOT_STATE["schwab_token"] = d.get("access_token", "")
                BOT_STATE["schwab_refresh_token"] = d.get("refresh_token", "")
                expires_in = int(d.get("expires_in", 1800))
                BOT_STATE["schwab_token_expiry"] = datetime.now() + timedelta(
                    seconds=max(30, expires_in - 60)
                )
                BOT_STATE["schwab_reauthorization_required"] = False
                BOT_STATE["schwab_oauth_diagnostic"] = None
                BOT_STATE["schwab_api_diagnostic"] = None
                BOT_STATE["schwab_oauth_last_code_fingerprint"] = fingerprint
                session.pop("schwab_oauth_state", None)
                session.pop("schwab_oauth_state_created_at", None)
                _set_schwab_auth_state(
                    "connected",
                    "Autorización Schwab completada correctamente.",
                    verified=True,
                )

                try:
                    _persist_schwab_credentials(
                        BOT_STATE["schwab_token"],
                        BOT_STATE["schwab_refresh_token"],
                        BOT_STATE["schwab_token_expiry"],
                    )
                except OSError as exc:
                    persistence_warning = (
                        "Schwab conectado, pero no se pudo guardar OAuth para el próximo "
                        "reinicio. Revise los permisos del archivo .env."
                    )
                    _log("WARN", f"[Schwab] OAuth no persistido: {type(exc).__name__}")
            _sync_schwab_token_to_engine(
                BOT_STATE["schwab_token"],
                BOT_STATE["schwab_refresh_token"],
                expires_in,
            )

            # Best-effort account discovery. This happens only after token exchange.
            try:
                hdrs = schwab_headers()
                ra = requests.get(f"{SCHWAB_TRADE_URL}/accounts", headers=hdrs, timeout=10)
                if ra.ok:
                    accs = ra.json()
                    if accs and isinstance(accs, list):
                        acc = accs[0].get("securitiesAccount", {}).get("accountNumber", "")
                        if acc:
                            BOT_STATE["account_number"] = acc
                            env_d = read_env_dict()
                            env_d["SCHWAB_ACCOUNT_NUMBER"] = acc
                            write_env_dict(env_d)
                            os.environ["SCHWAB_ACCOUNT_NUMBER"] = acc
            except Exception:
                pass

            _log(
                "INFO",
                f"[Schwab][OAuth] Conectado authorization_code client=****{cid[-4:]}",
            )
            return jsonify({
                "status": "ok",
                "message": "Schwab conectado",
                "warning": persistence_warning or None,
                "account": BOT_STATE.get("account_number", ""),
                "client_id_tail": cid[-4:],
            })
        except SchwabOAuthFailure as exc:
            diag = dict(exc.diagnostic or {})
            BOT_STATE["schwab_oauth_diagnostic"] = diag
            BOT_STATE["schwab_reauthorization_required"] = True
            oauth_error = str(diag.get("oauth_error") or "").lower()
            if oauth_error == "invalid_client":
                message = "Schwab rechazó el Client ID/Client Secret (invalid_client)."
            elif oauth_error == "invalid_grant":
                message = "Schwab rechazó el código OAuth (invalid_grant). Abra OAuth y use una URL nueva."
            elif oauth_error:
                message = f"Schwab rechazó OAuth: {oauth_error} (HTTP {diag.get('http_status')})."
            else:
                message = f"Schwab rechazó el intercambio OAuth (HTTP {diag.get('http_status')})."
            _set_schwab_auth_state(
                "expired" if oauth_error in {"invalid_grant", "invalid_client"} else "unavailable",
                message,
                verified=True,
                cache_seconds=0,
            )
            _log_schwab_diagnostic("ERROR", "Token exchange rechazado", diag)
            return jsonify({
                "status": "error",
                "message": message,
                "diagnostic": diag,
            }), 400
        except requests.RequestException as exc:
            _set_schwab_auth_state(
                "unavailable",
                "No se pudo contactar el endpoint OAuth de Schwab.",
                verified=False,
                cache_seconds=0,
            )
            _log("WARN", f"[Schwab][OAuth] Error de red: {type(exc).__name__}")
            return jsonify({
                "status": "error",
                "message": "No se pudo contactar Schwab para intercambiar el token.",
                "diagnostic": {
                    "phase": "authorization_code_exchange",
                    "oauth_error": "network_error",
                    "recommended_action": "RETRY_LATER",
                    "client_id_tail": cid[-4:],
                    "redirect_uri": SCHWAB_REDIRECT_URI,
                },
            }), 503
        except (TypeError, ValueError, KeyError) as exc:
            _set_schwab_auth_state(
                "unavailable",
                "Schwab devolvió una respuesta OAuth inválida.",
                verified=False,
                cache_seconds=0,
            )
            _log("WARN", f"[Schwab][OAuth] Respuesta inválida: {type(exc).__name__}")
            return jsonify({
                "status": "error",
                "message": "Schwab devolvió una respuesta OAuth inválida.",
            }), 502

@app.route('/api/schwab/status')
def schwab_status():
    return jsonify(_verify_schwab_connection())

@app.route('/api/schwab/test')
def schwab_test():
    hdrs = schwab_headers()
    if not hdrs:
        return jsonify({
            "status": "error",
            "message": BOT_STATE.get("schwab_auth_message") or "Sin token Schwab",
            "diagnostic": BOT_STATE.get("schwab_oauth_diagnostic"),
        }), 400
    try:
        r = guarded_market_get(f"{SCHWAB_MKT_URL}/quotes", params={"symbols":"BA"}, headers=hdrs, timeout=10)
        if r.ok:
            price = r.json().get("BA",{}).get("quote",{}).get("lastPrice","N/A")
            _log("INFO", f"[Schwab] Test OK - BA: ${price}")
            return jsonify({"status":"ok","ticker":"BA","price":price})
        diag = build_safe_diagnostic(
            r, phase="market_data_test", client_id=get_env("SCHWAB_CLIENT_ID"),
            redirect_uri=SCHWAB_REDIRECT_URI,
            secrets_to_redact=(
                _current_schwab_access_token(), get_env("SCHWAB_REFRESH_TOKEN"),
                get_env("SCHWAB_CLIENT_SECRET"),
            ),
        )
        BOT_STATE["schwab_api_diagnostic"] = diag
        _log_schwab_diagnostic("WARN", "Test rechazado", diag)
        return jsonify({
            "status": "error", "message": f"Schwab rechazó el test (HTTP {r.status_code}).",
            "diagnostic": diag,
        }), 400
    except Exception as e:
        return jsonify({
            "status": "error", "message": f"Test Schwab no disponible ({type(e).__name__}).",
            "diagnostic": BOT_STATE.get("schwab_api_diagnostic"),
        }), 503

# =============================================================
# ENV
# =============================================================
SENSITIVE = {"SCHWAB_CLIENT_SECRET","SCHWAB_ACCESS_TOKEN","SCHWAB_REFRESH_TOKEN",
             "SCHWAB_ACCOUNT_NUMBER","TELEGRAM_BOT_TOKEN","DASHBOARD_PASSWORD",
             "DASHBOARD_PASSWORD_HASH","FLASK_SECRET_KEY"}

@app.route('/api/env')
def get_env_route():
    env_d = read_env_dict()
    result = {}
    for key in ["SCHWAB_CLIENT_ID","SCHWAB_CLIENT_SECRET","SCHWAB_ACCOUNT_NUMBER",
                "TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID",
                "DASHBOARD_USERNAME","DASHBOARD_PASSWORD","GOOGLE_SHEET_ID"]:
        val = env_d.get(key, "")
        if key == "DASHBOARD_PASSWORD" and env_d.get("DASHBOARD_PASSWORD_HASH"):
            result[key] = "************"
        elif key in SENSITIVE and val:
            result[key] = "****" + val[-4:]
        else:
            result[key] = val
    return jsonify({"status":"ok","env":result})

@app.route('/api/env', methods=['POST'])
def save_env_route():
    data = request.get_json() or {}
    env_d = read_env_dict()
    before = dict(env_d)
    allowed = ["SCHWAB_CLIENT_ID", "SCHWAB_CLIENT_SECRET", "SCHWAB_ACCOUNT_NUMBER",
               "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
               "DASHBOARD_USERNAME", "DASHBOARD_PASSWORD", "GOOGLE_SHEET_ID"]
    for key in allowed:
        val = data.get(key, "")
        if isinstance(val, str) and key.startswith("SCHWAB_"):
            val = val.strip()
        if key == "DASHBOARD_PASSWORD" and val and "*" not in val and len(val) < 12:
            return jsonify({
                "status": "error",
                "message": "La contraseña del dashboard debe tener al menos 12 caracteres",
            }), 400
        if key == "DASHBOARD_PASSWORD" and val and "*" not in val:
            password_hash = generate_password_hash(val)
            env_d["DASHBOARD_PASSWORD_HASH"] = password_hash
            env_d.pop("DASHBOARD_PASSWORD", None)
            os.environ["DASHBOARD_PASSWORD_HASH"] = password_hash
            os.environ.pop("DASHBOARD_PASSWORD", None)
        elif val and "*" not in str(val):
            env_d[key] = val

    schwab_credentials_changed = any(
        str(env_d.get(key, "") or "").strip() != str(before.get(key, "") or "").strip()
        for key in ("SCHWAB_CLIENT_ID", "SCHWAB_CLIENT_SECRET")
    )
    if schwab_credentials_changed:
        # Tokens are cryptographically bound to the old OAuth app credentials.
        # Never attempt to refresh/reuse them after an app-key rotation.
        env_d["SCHWAB_ACCESS_TOKEN"] = ""
        env_d["SCHWAB_REFRESH_TOKEN"] = ""
        env_d["SCHWAB_TOKEN_EXPIRES_AT"] = ""

    write_env_dict(env_d)
    reload_env()
    BOT_STATE["account_number"] = env_d.get("SCHWAB_ACCOUNT_NUMBER", "")

    if schwab_credentials_changed:
        _clear_schwab_oauth_tokens("Client ID/Client Secret de Schwab modificados")
        _log(
            "INFO",
            "Credenciales Schwab rotadas; tokens anteriores invalidados y OAuth nuevo requerido",
        )
        return jsonify({
            "status": "ok",
            "message": "Credenciales guardadas. Se invalidaron los tokens anteriores; complete OAuth nuevamente.",
            "schwab_reauthorization_required": True,
        })

    _log("INFO", "Credenciales guardadas y recargadas")
    return jsonify({"status":"ok","message":"Credenciales guardadas"})


# =============================================================
# CONFIG
# =============================================================
@app.route('/api/config')
def get_config():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        return jsonify({"status":"ok","config":cfg})
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500

@app.route('/api/config', methods=['POST'])
def save_config():
    try:
        nueva = request.get_json() or {}
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        scheduler_changed = False
        for sec in ["macd","strategy","risk","market","engine","scheduler","kill_switch"]:
            if sec in nueva and isinstance(nueva[sec], dict):
                entrante = dict(nueva[sec])
                # v3.0.6 LIVE CONTROL: validate MACD values before persisting.
                if sec == "macd":
                    current_macd = cfg.get("macd", {}) or {}
                    try:
                        fast = int(entrante.get("fast_length", current_macd.get("fast_length", 12)))
                        slow = int(entrante.get("slow_length", current_macd.get("slow_length", 26)))
                        signal = int(entrante.get("signal_length", current_macd.get("signal_length", 9)))
                    except (TypeError, ValueError):
                        return jsonify({
                            "status": "error",
                            "message": "Los parámetros MACD deben ser enteros",
                        }), 400
                    if not (2 <= fast < slow <= 100):
                        return jsonify({
                            "status": "error",
                            "message": "MACD requiere 2 <= fast < slow <= 100",
                        }), 400
                    if not 2 <= signal <= 50:
                        return jsonify({
                            "status": "error",
                            "message": "MACD signal debe estar entre 2 y 50",
                        }), 400
                    entrante["fast_length"] = fast
                    entrante["slow_length"] = slow
                    entrante["signal_length"] = signal

                # SEGURIDAD: el interruptor de dinero real NO se puede cambiar
                # desde el dashboard; solo editando config.yaml directamente.
                if sec == "risk":
                    entrante.pop("paper_trading", None)
                    if "sizing_mode" in entrante:
                        mode = str(entrante["sizing_mode"]).strip().lower()
                        if mode not in ("atr", "fixed"):
                            return jsonify({
                                "status": "error",
                                "message": "sizing_mode debe ser 'atr' o 'fixed'",
                            }), 400
                        entrante["sizing_mode"] = mode
                    if "fixed_shares" in entrante:
                        shares = int(entrante["fixed_shares"])
                        if not 1 <= shares <= 1_000_000:
                            return jsonify({
                                "status": "error",
                                "message": "fixed_shares fuera de rango",
                            }), 400
                        entrante["fixed_shares"] = shares
                    if "operating_capital_usd" in entrante:
                        capital = float(entrante["operating_capital_usd"])
                        if not 100.0 <= capital <= 1_000_000_000.0:
                            return jsonify({
                                "status": "error",
                                "message": "operating_capital_usd fuera de rango",
                            }), 400
                        entrante["operating_capital_usd"] = capital
                    if "risk_per_trade_pct" in entrante:
                        risk_pct = float(entrante["risk_per_trade_pct"])
                        if not 0.01 <= risk_pct <= 10.0:
                            return jsonify({
                                "status": "error",
                                "message": "risk_per_trade_pct fuera de rango",
                            }), 400
                        entrante["risk_per_trade_pct"] = risk_pct
                    current_risk = cfg.get("risk", {}) or {}
                    stop_factor = float(entrante.get(
                        "stop_loss_factor",
                        current_risk.get("stop_loss_factor", 2.0),
                    ))
                    take_factor = float(entrante.get(
                        "take_profit_factor",
                        current_risk.get("take_profit_factor", stop_factor * 2.0),
                    ))
                    if not 0.5 <= stop_factor <= 5.0:
                        return jsonify({
                            "status": "error",
                            "message": "stop_loss_factor debe estar entre 0.5 y 5.0",
                        }), 400
                    if not 0.5 <= take_factor <= 8.0:
                        return jsonify({
                            "status": "error",
                            "message": "take_profit_factor debe estar entre 0.5 y 8.0",
                        }), 400
                    if take_factor <= stop_factor:
                        return jsonify({
                            "status": "error",
                            "message": "Take Profit ATR debe ser mayor que Stop Loss ATR",
                        }), 400
                    if "stop_loss_factor" in entrante:
                        entrante["stop_loss_factor"] = stop_factor
                    if "take_profit_factor" in entrante:
                        entrante["take_profit_factor"] = take_factor
                # v3.0.6 LIVE CONTROL: nested daily-loss limits are real runtime config.
                if sec == "kill_switch":
                    current_ks = cfg.get("kill_switch", {}) or {}
                    incoming_daily = entrante.get("daily_loss", {}) or {}
                    if not isinstance(incoming_daily, dict):
                        return jsonify({
                            "status": "error",
                            "message": "kill_switch.daily_loss debe ser un objeto",
                        }), 400
                    current_daily = current_ks.get("daily_loss", {}) or {}
                    merged_daily = dict(current_daily)
                    for key in ("paper_limit_usd", "live_limit_usd"):
                        if key in incoming_daily:
                            try:
                                value = abs(float(incoming_daily[key]))
                            except (TypeError, ValueError):
                                return jsonify({
                                    "status": "error",
                                    "message": f"{key} debe ser numérico",
                                }), 400
                            if not 1.0 <= value <= 1_000_000_000.0:
                                return jsonify({
                                    "status": "error",
                                    "message": f"{key} fuera de rango",
                                }), 400
                            merged_daily[key] = value
                    merged_ks = dict(current_ks)
                    merged_ks.update({k: v for k, v in entrante.items() if k != "daily_loss"})
                    merged_ks["daily_loss"] = merged_daily
                    entrante = merged_ks

                if sec == "scheduler":
                    modo = str(entrante.get(
                        "modo",
                        (cfg.get("scheduler", {}) or {}).get("modo", "manual"),
                    )).strip().lower()
                    if modo == "automatic":
                        modo = "automatico"
                    if modo not in ("manual", "automatico"):
                        return jsonify({
                            "status": "error",
                            "message": "scheduler.modo debe ser manual o automatico",
                        }), 400

                    import re
                    hora_inicio = str(entrante.get(
                        "hora_inicio",
                        (cfg.get("scheduler", {}) or {}).get(
                            "hora_inicio", "09:15"),
                    ))
                    hora_fin = str(entrante.get(
                        "hora_fin",
                        (cfg.get("scheduler", {}) or {}).get(
                            "hora_fin", "15:45"),
                    ))
                    patron_hora = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
                    if not patron_hora.fullmatch(hora_inicio) \
                       or not patron_hora.fullmatch(hora_fin):
                        return jsonify({
                            "status": "error",
                            "message": "Las horas deben usar el formato HH:MM",
                        }), 400
                    if hora_inicio >= hora_fin:
                        return jsonify({
                            "status": "error",
                            "message": "La hora de inicio debe ser anterior a la hora final",
                        }), 400
                    if modo == "automatico" and _real_tickers_runtime(cfg):
                        return jsonify({
                            "status": "error",
                            "message": (
                                "El encendido automático está bloqueado en modo REAL mientras "
                                "haya ticker(s) en REAL; cambie esos tickers a PAPER "
                                "o mantenga el scheduler MANUAL"
                            ),
                        }), 400

                    entrante["modo"] = modo
                    entrante["hora_inicio"] = hora_inicio
                    entrante["hora_fin"] = hora_fin
                    entrante["zona_horaria"] = "America/New_York"
                    entrante["automatico_solo_paper"] = True
                    entrante["activo"] = modo == "automatico"
                    scheduler_changed = True
                cfg.setdefault(sec, {}).update(entrante)
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        if scheduler_changed:
            _configure_scheduler_service()
        _log("INFO", "Configuracion guardada desde dashboard")
        return jsonify({
            "status": "ok",
            "engine_restart_required": any(sec in nueva for sec in ("risk", "macd", "strategy", "kill_switch")),
        })
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500

# =============================================================
# BOT CONTROL
# =============================================================
@app.route('/api/status')
def get_status():
    auth_state = BOT_STATE.get("schwab_auth_state")
    schwab_ok = (
        auth_state == "connected"
        if auth_state not in {None, "unknown"}
        else bool(_current_schwab_access_token())
    )
    return jsonify({
        "status": "ok",
        "bot": {
            "running":          BOT_STATE["running"],
            "mode":             BOT_STATE["mode"],
            "tickers":          BOT_STATE["tickers"],
            "timestamp":        datetime.now().isoformat(),
            "schwab_connected": schwab_ok,
            "account_number":   BOT_STATE.get("account_number",""),
        },
        "stats": _dashboard_stats(),
        "daily_risk": _engine_daily_risk_snapshot(),
        "scheduler": _scheduler_status(),
        "broker_reconciliation": _engine_broker_reconciliation_snapshot(),
    })


@app.route('/api/broker/reconcile-read-only', methods=['POST'])
def reconcile_broker_read_only():
    try:
        state = _refresh_broker_reconciliation_read_only()
        # A completed GET workflow is not proof that Schwab confirmed the
        # actual positions and orders. Do not show a false success toast if
        # the broker returned None (or another fail-closed result).
        if not isinstance(state, dict) or not bool(state.get("ok")):
            message = (
                str(state.get("message") or "Schwab no confirmó posiciones y órdenes")
                if isinstance(state, dict)
                else "Schwab no confirmó posiciones y órdenes"
            )
            _log("WARN", "[Schwab] Reconciliación manual pendiente: broker no confirmado")
            return jsonify({
                "status": "error",
                "message": message,
                "broker_reconciliation": state,
            }), 409
        _log("INFO", "[Schwab] Reconciliación manual de solo lectura completada")
        return jsonify({"status": "ok", "broker_reconciliation": state})
    except Exception as exc:
        _log("ERROR", f"[Schwab] Reconciliación de solo lectura falló: {exc}")
        return jsonify({"status": "error", "message": str(exc)}), 409

# =============================================================
# MOTOR REAL (engine/bot_engine.ChocoletraBot)
# El botón de encendido del Dashboard arranca este motor real.
# =============================================================
_ENGINE = None
_engine_thread = None
_ENGINE_CONTROL_LOCK = threading.RLock()
_STARTUP_QUARANTINE = {}
_SCHEDULER = None
_AI_STATUS_TRAINER = None
_AI_STATUS_SIGNATURE = None
_AI_STATUS_LOCK = threading.RLock()
_VETO_STATUS_STORE = None
_LEARNING_STATUS_STORE = None

def _hook_signal(
    tipo, ticker, precio, atr, prob=None, motivo="",
    ai_threshold=None, execution_mode="", ai_decision="", ai_context=None,
):
    return _register_signal(
        tipo, ticker, precio, atr, prob, motivo,
        ai_threshold, execution_mode, ai_decision, ai_context=ai_context,
    )

def _hook_candles(ticker, frame):
    _ensure_candle_archive_worker()
    try:
        _CANDLE_ARCHIVE_QUEUE.put_nowait((ticker, frame.copy(deep=False)))
    except queue.Full:
        _log("WARN", f"[Velas] Cola local llena; se omitió snapshot de {ticker}")

def _hook_trade(tipo, ticker, precio, cantidad, estado="OPEN", pnl=0.0):
    _register_trade(tipo, ticker, precio, cantidad, estado, pnl)

def _hook_log(nivel, mensaje):
    _log(nivel, mensaje)

def _start_engine(mode: str, capital: float):
    """Instancia el motor real y lo arranca en un hilo de background."""
    global _ENGINE, _engine_thread
    from engine.bot_engine import ChocoletraBot

    bot = ChocoletraBot(CONFIG_PATH)
    bot._ticker_quarantine.update(_STARTUP_QUARANTINE)

    # v2.9.2: the persistent per-ticker registry is the execution authority when
    # enabled.  The start guard has already preflighted every REAL-routed ticker.
    real_tickers = _real_tickers_runtime(bot.config)
    if real_tickers:
        _log(
            "WARN",
            "[Motor] EJECUCIÓN MIXTA ACTIVA — REAL: " + ", ".join(real_tickers),
        )

    # Reflejar el estado en el Dashboard
    bot.set_hooks(
        on_signal=_hook_signal,
        on_trade=_hook_trade,
        on_log=_hook_log,
        on_candles=_hook_candles,
    )

    # Inyectar los tokens OAuth2 ya conectados de Schwab
    token = _current_schwab_access_token() if schwab_ensure_token() else ""
    if token:
        try:
            bot.schwab.autenticar_con_token(
                token, BOT_STATE.get("schwab_refresh_token") or get_env("SCHWAB_REFRESH_TOKEN"),
                expires_at=BOT_STATE.get("schwab_token_expiry"),
            )
            bot.ejecutor_hibrido.set_token(token)
        except Exception as e:
            _log("WARN", f"[Motor] No se pudo inyectar token al motor: {e}")

    bot.account_number = BOT_STATE.get("account_number", "") or get_env("SCHWAB_ACCOUNT_NUMBER", "")
    tickers = list(BOT_STATE.get("tickers") or bot.TICKERS_DEFAULT)

    _ENGINE = bot

    def _runner():
        try:
            bot.iniciar(tickers=tickers, account_number=bot.account_number, capital=capital)
        except Exception as e:
            _log("ERROR", f"[Motor] Terminó con error: {e}")
        finally:
            BOT_STATE["running"] = False

    _engine_thread = threading.Thread(target=_runner, daemon=True, name="ChocoletraEngine")
    _engine_thread.start()


def _read_config_runtime() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception as exc:
        _log("ERROR", f"[Scheduler] No se pudo leer config.yaml: {exc}")
        return {}


def _ticker_execution_registry(cfg: dict = None) -> TickerExecutionRegistry:
    cfg = cfg or _read_config_runtime()
    live = cfg.get("live_trading", {}) or {}
    risk = cfg.get("risk", {}) or {}
    return TickerExecutionRegistry(
        str(live.get("ticker_execution_path", "data/ticker_execution.json")),
        default_mode="PAPER",
        initial_real_tickers=live.get("real_tickers", []),
        default_shares=max(1, int(risk.get("fixed_shares", 1) or 1)),
    )


def _ticker_execution_rows(cfg: dict = None, *, persist_missing: bool = True):
    cfg = cfg or _read_config_runtime()
    tickers = list(BOT_STATE.get("tickers") or [])
    live = cfg.get("live_trading", {}) or {}
    per_ticker = bool(live.get("per_ticker_execution", False))
    registry = _ticker_execution_registry(cfg)
    if persist_missing:
        rows = registry.ensure_tickers(tickers, persist=True)
    else:
        rows = registry.snapshot(tickers)
    if per_ticker:
        for row in rows:
            row["effective_mode"] = row["mode"]
    else:
        global_mode = (
            "PAPER" if bool((cfg.get("risk", {}) or {}).get("paper_trading", True))
            else "REAL"
        )
        for row in rows:
            row["effective_mode"] = global_mode
    return rows


def _real_tickers_runtime(cfg: dict = None) -> list:
    cfg = cfg or _read_config_runtime()
    live = cfg.get("live_trading", {}) or {}
    if bool(live.get("per_ticker_execution", False)):
        return [
            row["ticker"] for row in _ticker_execution_rows(
                cfg, persist_missing=False
            )
            if row.get("mode") == "REAL"
        ]
    paper = bool((cfg.get("risk", {}) or {}).get("paper_trading", True))
    return [] if paper else list(BOT_STATE.get("tickers") or [])


def _scheduler_mode() -> str:
    modo = str(
        (_read_config_runtime().get("scheduler", {}) or {}).get(
            "modo", "manual")
    ).strip().lower()
    return "automatico" if modo in ("automatico", "automatic") else "manual"


def _scheduler_status() -> dict:
    if _SCHEDULER is not None:
        try:
            estado = _SCHEDULER.estado()
            estado["paper_only"] = True
            return estado
        except Exception:
            pass
    cfg = _read_config_runtime().get("scheduler", {}) or {}
    modo = str(cfg.get("modo", "manual")).strip().lower()
    if modo == "automatic":
        modo = "automatico"
    return {
        "modo": modo if modo in ("manual", "automatico") else "manual",
        "activo": False,
        "hora_inicio": str(cfg.get("hora_inicio", "09:15")),
        "hora_fin": str(cfg.get("hora_fin", "15:45")),
        "zona_horaria": "America/New_York",
        "en_horario": False,
        "suspendido_hoy": False,
        "hora_actual": _hora_ny(),
        "paper_only": True,
    }


def _validate_live_start(cfg: dict):
    """Validate only tickers explicitly routed to REAL in v2.9.2."""
    from core.live_guard import validate_live_start

    real_tickers = _real_tickers_runtime(cfg)
    rows = _ticker_execution_rows(cfg, persist_missing=False)
    settings = {row["ticker"]: row for row in rows}

    def _model_checker(allowed):
        from agents.training import EntrenadorAgentes
        trainer = EntrenadorAgentes(cfg)
        return trainer.modelos_faltantes(allowed)

    def _broker_checker(account_number, ticker):
        from api.broker_check import ejecutar_broker_check
        return ejecutar_broker_check(CONFIG_PATH, account_number, ticker=ticker)

    account_number = str(
        BOT_STATE.get("account_number", "")
        or get_env("SCHWAB_ACCOUNT_NUMBER", "")
        or ""
    ).strip()
    return validate_live_start(
        cfg,
        get_env=get_env,
        account_number=account_number,
        model_checker=_model_checker,
        broker_checker=_broker_checker,
        real_tickers=real_tickers,
        ticker_settings=settings,
    )


def _validate_live_start_partitioned(cfg: dict):
    """v3.2 mixed-mode validation with per-ticker REAL quarantine.

    Global safety conditions still fail closed. Broker/model/share failures that
    belong to one ticker are returned separately so the caller can move only that
    ticker back to PAPER and allow the rest of the engine to start.
    """
    from core.live_guard import validate_live_start_partitioned

    real_tickers = _real_tickers_runtime(cfg)
    rows = _ticker_execution_rows(cfg, persist_missing=False)
    settings = {row["ticker"]: row for row in rows}

    def _model_checker(allowed):
        from agents.training import EntrenadorAgentes
        trainer = EntrenadorAgentes(cfg)
        return trainer.modelos_faltantes(allowed)

    def _broker_checker(account_number, ticker):
        from api.broker_check import ejecutar_broker_check
        return ejecutar_broker_check(CONFIG_PATH, account_number, ticker=ticker)

    account_number = str(
        BOT_STATE.get("account_number", "")
        or get_env("SCHWAB_ACCOUNT_NUMBER", "")
        or ""
    ).strip()
    live = cfg.get("live_trading", {}) or {}
    return validate_live_start_partitioned(
        cfg,
        get_env=get_env,
        account_number=account_number,
        model_checker=_model_checker,
        broker_checker=_broker_checker,
        real_tickers=real_tickers,
        ticker_settings=settings,
        broker_retry_attempts=max(
            1, int(live.get("broker_preflight_retry_attempts", 3) or 3)
        ),
        broker_retry_delay_sec=max(
            0.0, float(live.get("broker_preflight_retry_delay_sec", 0.75) or 0.75)
        ),
    )


def _start_engine_controlled(source: str, capital: float = None):
    """Single entry point for Dashboard and scheduler; prevents double starts."""
    global _ENGINE
    with _ENGINE_CONTROL_LOCK:
        origen_req = "horario automático" if source == "scheduler" else "dashboard"
        _log("INFO", f"[Motor] SOLICITUD DE ARRANQUE desde {origen_req}")
        if BOT_STATE["running"] and _ENGINE is not None:
            return True, "El motor ya está en ejecución"

        # v3.3.2: a previous stop may have retained a stopped engine because
        # Schwab still showed managed REAL exposure.  Retry that emergency
        # flatten before allowing a fresh engine instance/new entries.
        if not BOT_STATE["running"] and _ENGINE is not None:
            previous = _ENGINE
            try:
                recovery = previous.detener() or {}
            except Exception as exc:
                _log("ERROR", f"[Motor] Recuperación previa al arranque falló: {exc}")
                return False, "Arranque bloqueado: exposición REAL previa no reconciliada"
            if isinstance(recovery, dict) and not bool(recovery.get("broker_flat", True)):
                message = str(recovery.get("message", "") or "Schwab no confirmó plano")
                _log("ERROR", f"[Motor] ARRANQUE BLOQUEADO por exposición REAL previa: {message}")
                return False, (
                    "Arranque bloqueado: todavía existe exposición REAL no resuelta. "
                    "Use PARAR/reconciliación y confirme Schwab plano antes de nuevas entradas."
                )
            _ENGINE = None

        _STARTUP_QUARANTINE.clear()
        cfg = _read_config_runtime()
        guard = DailyLossGuard(cfg, history_path=_history_path())
        if guard.enabled and guard.is_blocked():
            risk = guard.snapshot(bool((cfg.get("risk", {}) or {}).get("paper_trading", True)))
            reset = str(risk.get("next_reset_at_ny", "") or "")
            mensaje = (
                f"BOT BLOQUEADO POR PÉRDIDA DIARIA ({risk.get('trigger_mode') or risk.get('mode')} "
                f"{float(risk.get('trigger_net_pnl', 0.0)):.2f} USD). "
                f"No se puede reiniciar ni borrar manualmente; próximo reinicio NY: {reset}."
            )
            _log("WARN", f"[DailyLossGuard] {mensaje}")
            return False, mensaje
        real_tickers = _real_tickers_runtime(cfg)
        if source == "scheduler" and real_tickers:
            mensaje = (
                "Encendido automático BLOQUEADO en modo REAL: hay ticker(s) en REAL. "
                "El dinero real requiere activación manual."
            )
            _log("ERROR", f"[Scheduler] {mensaje}")
            return False, mensaje

        fallback_notes = []
        if real_tickers:
            live_cfg = cfg.get("live_trading", {}) or {}
            isolate = bool(
                live_cfg.get("preflight_isolate_ticker_failures", True)
            ) and bool(live_cfg.get("per_ticker_execution", False))

            if isolate:
                validation = _validate_live_start_partitioned(cfg)
                global_errors = list(validation.get("global_errors") or [])
                if global_errors:
                    mensaje = "MODO REAL BLOQUEADO: " + " | ".join(global_errors)
                    _log("ERROR", f"[LiveGuard] {mensaje}")
                    return False, mensaje

                blocked = list(validation.get("blocked_real") or [])
                approved = list(validation.get("approved_real") or [])
                if blocked:
                    registry = _ticker_execution_registry(cfg)
                    for ticker in blocked:
                        reasons = list(
                            (validation.get("ticker_errors") or {}).get(ticker) or []
                        )
                        detail = "; ".join(reasons) or "preflight local falló"
                        # v3.3.6: ticker-local preflight failures are temporary
                        # runtime quarantines. Never persist REAL -> PAPER here.
                        _STARTUP_QUARANTINE[ticker] = detail
                        fallback_notes.append(
                            f"{ticker}: REAL bloqueado temporalmente ({detail})"
                        )
                        _log(
                            "ERROR",
                            f"[LiveGuard] {ticker} REAL EN CUARENTENA TEMPORAL: "
                            f"{detail} | selección REAL conservada",
                        )

                if approved:
                    _log(
                        "WARN",
                        "[LiveGuard] Preflight aprobado. Tickers REAL: "
                        + ", ".join(approved),
                    )
                elif blocked:
                    _log(
                        "WARN",
                        "[LiveGuard] Ningún ticker quedó habilitado en REAL; "
                        "el motor continuará en PAPER.",
                    )
            else:
                live_ok, live_errors, live_tickers = _validate_live_start(cfg)
                if not live_ok:
                    mensaje = "MODO REAL BLOQUEADO: " + " | ".join(live_errors)
                    _log("ERROR", f"[LiveGuard] {mensaje}")
                    return False, mensaje
                _log(
                    "WARN",
                    "[LiveGuard] Preflight aprobado. Tickers REAL: "
                    + ", ".join(live_tickers),
                )

        total = len(BOT_STATE.get("tickers") or [])
        modo = (
            "mixed" if real_tickers and len(real_tickers) < total
            else "real" if real_tickers
            else "paper"
        )
        capital_real = float(
            capital if capital is not None
            else BOT_STATE.get("capital", 10000)
        )
        BOT_STATE["mode"] = modo
        BOT_STATE["capital"] = capital_real
        _start_engine(modo, capital_real)
        BOT_STATE["running"] = True
        origen = "horario automático" if source == "scheduler" else "dashboard"
        _log(
            "INFO",
            f"[Motor] ARRANCADO desde {origen} (modo: {modo.upper()})",
        )
        if fallback_notes:
            return True, (
                "Motor arrancado. REAL aislado por seguridad: "
                + " | ".join(fallback_notes)
            )
        return True, "Motor arrancado"


def _stop_engine_controlled(source: str):
    """Stop engine and distinguish STOPPED from BROKER-FLAT.

    v3.3.2 deliberately keeps the stopped engine object available when Schwab
    still reports managed REAL exposure, so another stop/recovery request can
    retry liquidation instead of silently discarding the only recovery context.
    """
    global _ENGINE
    with _ENGINE_CONTROL_LOCK:
        origen = "horario automático" if source == "scheduler" else "dashboard"
        _log("WARN", f"[Motor] SOLICITUD DE PARADA desde {origen}")
        BOT_STATE["running"] = False
        bot = _ENGINE
        result = {}
        if bot is not None:
            try:
                try:
                    result = bot.detener(fin_sesion=(source == "scheduler")) or {}
                except TypeError:
                    # Backwards compatibility for test doubles/legacy engines.
                    result = bot.detener() or {}
            except Exception as exc:
                _log("ERROR", f"[Motor] Error al detener: {exc}")
                return False, str(exc)

        broker_flat = True
        if isinstance(result, dict) and "broker_flat" in result:
            broker_flat = bool(result.get("broker_flat"))
        if broker_flat:
            _ENGINE = None
            _log(
                "WARN",
                f"[Motor] DETENIDO desde {origen} — broker plano confirmado",
            )
            return True, "Motor detenido; broker plano confirmado"

        # Keep a reference to the stopped engine for an explicit recovery retry.
        _ENGINE = bot
        message = str(
            (result.get("message") if isinstance(result, dict) else "")
            or "Schwab no confirmó cuenta plana"
        )
        _log(
            "ERROR",
            f"[Motor] DETENIDO desde {origen}, PERO QUEDA EXPOSICIÓN REAL: {message}",
        )
        return False, (
            "Motor detenido, pero Schwab NO confirmó posiciones REAL planas. "
            "No iniciar nuevas entradas; vuelva a ejecutar PARAR para reintentar "
            "la recuperación o revise Schwab."
        )


def _scheduler_start_callback():
    ok, _ = _start_engine_controlled("scheduler")
    return ok


def _scheduler_stop_callback():
    _stop_engine_controlled("scheduler")


def _configure_scheduler_service():
    """Reloads schedule changes without restarting Flask or the trading engine."""
    global _SCHEDULER
    from engine.scheduler import PlanificadorHorario

    with _ENGINE_CONTROL_LOCK:
        anterior = _SCHEDULER
        if anterior is not None:
            anterior.detener(apagar_bot=False)
        cfg = _read_config_runtime()
        nuevo = PlanificadorHorario(cfg)
        _SCHEDULER = nuevo
        real_tickers = _real_tickers_runtime(cfg)
        if nuevo.modo == "automatico" and real_tickers:
            _log(
                "ERROR",
                "[Scheduler] Configuración automática ignorada: hay ticker(s) REAL; "
                "el dinero real siempre requiere encendido manual.",
            )
            return nuevo
        nuevo.iniciar(
            callback_encender=_scheduler_start_callback,
            callback_apagar=_scheduler_stop_callback,
            callback_estado=lambda: bool(BOT_STATE.get("running")),
        )
    return nuevo

@app.route('/api/bot/start', methods=['POST'])
def start_bot():
    # force=True + silent=True: accept ANY content-type (fixes HTTP 415)
    data = request.get_json(force=True, silent=True) or {}
    if _scheduler_mode() == "automatico":
        return jsonify({
            "status": "error",
            "running": bool(BOT_STATE["running"]),
            "message": (
                "El horario está en modo AUTOMÁTICO. Cambie a MANUAL "
                "para encender el motor con el botón."
            ),
        }), 409

    BOT_STATE["mode"] = data.get("mode", "paper")
    runtime_cfg = _read_config_runtime()
    configured_capital = float(
        ((runtime_cfg.get("risk", {}) or {}).get("operating_capital_usd", 10000) or 10000)
    )
    BOT_STATE["capital"] = configured_capital

    if BOT_STATE["running"] and _ENGINE is not None:
        return jsonify({"status": "ok", "running": True, "mode": BOT_STATE["mode"],
                        "message": "El motor ya está en ejecución"})

    try:
        ok, mensaje = _start_engine_controlled(
            "dashboard", BOT_STATE["capital"])
        if not ok:
            return jsonify({
                "status": "error", "message": mensaje,
            }), 400
        return jsonify({
            "status": "ok",
            "running": True,
            "mode": BOT_STATE["mode"],
            "message": mensaje,
        })
    except Exception as e:
        _log("ERROR", f"[Motor] No se pudo arrancar: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/bot/stop', methods=['POST'])
def stop_bot():
    if _SCHEDULER is not None and _scheduler_mode() == "automatico":
        _SCHEDULER.suspender_sesion_actual()
    ok, mensaje = _stop_engine_controlled("dashboard")
    return jsonify({
        "status": "ok" if ok else "error",
        "running": False,
        "message": mensaje,
        "automatic_resume": "next_session"
        if _scheduler_mode() == "automatico" else None,
    }), (200 if ok else 500)

# =============================================================
# SIGNALS
# =============================================================
@app.route('/api/signals')
def get_signals():
    try:
        limit = max(1, min(int(request.args.get("limit", 500)), 2000))
    except (TypeError, ValueError):
        limit = 500
    ticker = str(request.args.get("ticker", "") or "").strip().upper()
    all_rows = _read_signal_history(ticker=ticker)
    if not all_rows and not ticker:
        with _SIGNAL_LOCK:
            all_rows = list(BOT_STATE["signals"])
    return jsonify({
        "status":  "ok",
        "signals": all_rows[-limit:],
        "total": len(all_rows),
        "persistent": True,
        "counts": _signal_counts(all_rows),
    })


def _chart_trade_events(ticker: str, start_ny: datetime, end_ny: datetime):
    """Persistent ENTRY/70%/final markers for a ticker and chart interval."""

    from core.order_audit import read_order_audit

    event_map = {
        "ENTRY_FILLED": "ENTRY",
        "TP1_FILLED_70_PERCENT": "PARTIAL_70",
        "POSITION_CLOSED": "FINAL_EXIT",
    }
    start_ms = int(start_ny.timestamp() * 1000)
    end_ms = int(end_ny.timestamp() * 1000)
    rows = []
    try:
        audit_rows = read_order_audit(
            _order_audit_path(), limit=5000, newest_first=False
        )
    except Exception:
        audit_rows = []

    for row in audit_rows:
        event_name = str(row.get("evento", "")).upper()
        kind = event_map.get(event_name)
        if kind is None or str(row.get("ticker", "")).upper() != ticker:
            continue
        try:
            event_dt = datetime.fromisoformat(
                str(row.get("timestamp_utc", "")).replace("Z", "+00:00")
            )
            epoch_ms = int(event_dt.timestamp() * 1000)
        except (TypeError, ValueError):
            continue
        if not start_ms <= epoch_ms <= end_ms:
            continue
        try:
            detail = json.loads(row.get("detalle", "") or "{}")
            if not isinstance(detail, dict):
                detail = {}
        except (TypeError, ValueError):
            detail = {}
        if str(row.get("modo", "")).upper() == "REAL":
            if kind == "ENTRY" and detail.get("broker_price_verified") is not True:
                continue
            if kind == "PARTIAL_70" and str(row.get("estado", "")).upper() != "FILLED":
                continue
        price = row.get("precio", 0)
        if kind == "FINAL_EXIT":
            if str(row.get("modo", "")).upper() == "REAL" and not (
                detail.get("broker_confirmed") is True
                and detail.get("fill_price_verified") is True
            ):
                continue
            price = detail.get("salida_real")
        try:
            price = float(price or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            continue
        rows.append({
            "event_id": row.get("event_id", ""),
            "kind": kind,
            "epoch_ms": epoch_ms,
            "timestamp_iso": event_dt.isoformat(),
            "price": price,
            "side": str(row.get("lado", "")).upper(),
            "quantity": int(float(row.get("cantidad", 0) or 0)),
            "pnl_usd": float(row.get("pnl_usd", 0) or 0),
            "label": {
                "ENTRY": "ENTRADA",
                "PARTIAL_70": "SALIDA 70%",
                "FINAL_EXIT": "CIERRE FINAL",
            }[kind],
        })
    rows.sort(key=lambda item: (item["epoch_ms"], item["kind"]))
    return rows


@app.route('/api/signals/chart')
def get_signal_chart():
    ticker = str(request.args.get("ticker", "") or "").strip().upper()
    if not ticker or not ticker.replace(".", "").replace("-", "").isalnum():
        return jsonify({"status": "error", "message": "Ticker inválido"}), 400
    try:
        window = max(15, min(int(request.args.get("window", 60)), 240))
    except (TypeError, ValueError):
        window = 60
    signal_id = str(request.args.get("signal_id", "") or "").strip()
    selected_date_text = str(request.args.get("date", "") or "").strip()
    view_mode = str(request.args.get("view", "signal") or "signal").strip().lower()
    selected_date = None
    if selected_date_text:
        try:
            selected_date = datetime.strptime(
                selected_date_text, "%Y-%m-%d"
            ).date()
        except ValueError:
            return jsonify({
                "status": "error", "message": "Fecha inválida; use AAAA-MM-DD",
            }), 400
    if view_mode not in ("signal", "date", "session", "live"):
        return jsonify({"status": "error", "message": "Vista inválida"}), 400
    ticker_signals = _read_signal_history(ticker=ticker)
    if not ticker_signals:
        with _SIGNAL_LOCK:
            ticker_signals = [
                row for row in BOT_STATE["signals"]
                if str(row.get("ticker", "")).upper() == ticker
            ]
    selected = None
    if signal_id:
        selected = next(
            (row for row in ticker_signals if row.get("signal_id") == signal_id),
            None,
        )
        if selected is None:
            return jsonify({
                "status": "error", "message": "La señal seleccionada no existe",
            }), 404
    elif ticker_signals and view_mode == "signal":
        if selected_date is not None:
            on_date = [
                row for row in ticker_signals
                if (_signal_datetime(row) is not None
                    and _signal_datetime(row).date() == selected_date)
            ]
            selected = on_date[-1] if on_date else None
        else:
            selected = ticker_signals[-1]

    now_ny = _ahora_ny_datetime()
    ny_tz = now_ny.tzinfo
    selected_time = _signal_datetime(selected or {})
    if selected is not None and selected_time is None:
        selected = None
    if selected_date is None and selected_time is not None:
        selected_date = selected_time.date()
    if view_mode == "live":
        selected = None
        selected_time = None
        selected_date = now_ny.date()
    bounds_date = selected_date or now_ny.date()

    session_start_ny = datetime.combine(
        bounds_date, datetime.min.time()
    ).replace(tzinfo=ny_tz) + timedelta(hours=9, minutes=15)
    session_end_ny = datetime.combine(
        bounds_date, datetime.min.time()
    ).replace(tzinfo=ny_tz) + timedelta(hours=15, minutes=45)

    if view_mode in ("session", "live"):
        selected_date = bounds_date
        start_ny = session_start_ny
        if view_mode == "live":
            if now_ny <= session_start_ny:
                end_ny = min(session_start_ny + timedelta(minutes=1), session_end_ny)
            else:
                end_ny = min(now_ny, session_end_ny)
        else:
            end_ny = session_end_ny
    elif view_mode == "date" and selected_date is not None:
        start_ny = datetime.combine(selected_date, datetime.min.time()).replace(
            tzinfo=ny_tz
        )
        end_ny = start_ny + timedelta(days=1) - timedelta(microseconds=1)
        selected = None
    else:
        if selected_time is not None:
            center = selected_time
        elif selected_date is not None:
            center = datetime.combine(
                selected_date, datetime.min.time()
            ).replace(tzinfo=ny_tz) + timedelta(hours=12)
        else:
            center = _ahora_ny_datetime()
            selected_date = center.date()
        start_ny = center - timedelta(minutes=window)
        end_ny = center + timedelta(minutes=window)

    candles, source = _load_local_chart_candles(ticker, start_ny, end_ny)
    broker_error = None
    broker_verified = False
    if view_mode in ("session", "live") or len(candles) < 2:
        try:
            broker_rows = _fetch_chart_candles(
                ticker,
                start_ny,
                end_ny,
                include_extended=view_mode in ("session", "live"),
            )
            broker_verified = True
            if broker_rows:
                _store_chart_candles(ticker, broker_rows, source="schwab")
                combined = {row["epoch_ms"]: row for row in candles}
                combined.update({row["epoch_ms"]: row for row in broker_rows})
                candles = [combined[key] for key in sorted(combined)]
                source = "schwab_live" if view_mode == "live" else "schwab"
        except Exception as exc:
            broker_error = exc
            _log("WARN", f"[Gráfico] No se pudo cargar {ticker} desde Schwab: {exc}")
    if len(candles) < 2 and broker_error is not None:
        return jsonify({
            "status": "error",
            "message": (
                f"No hay velas locales para {ticker} el {selected_date.isoformat()} "
                f"y Schwab no está disponible: {broker_error}"
            ),
            "ticker": ticker,
            "selected_signal": selected,
        }), 503
    if len(candles) < 2:
        return jsonify({
            "status": "error",
            "message": (
                f"No hay velas locales ni de Schwab para {ticker} "
                f"el {selected_date.isoformat()}"
            ),
            "ticker": ticker,
            "selected_signal": selected,
        }), 404

    start_ms = candles[0]["epoch_ms"]
    end_ms = candles[-1]["epoch_ms"]
    markers = []
    for row in ticker_signals:
        marker_time = _signal_datetime(row)
        if marker_time is None:
            continue
        decision_ms = int(marker_time.timestamp() * 1000)
        marker_ms = row.get("signal_bar_epoch_ms", row.get("chart_epoch_ms"))
        if marker_ms in (None, ""):
            marker_ms = infer_closed_bar_epoch_ms(decision_ms)
        try:
            marker_ms = int(marker_ms)
        except (TypeError, ValueError):
            marker_ms = decision_ms
        # Plot against the source closed bar, while retaining the actual decision
        # timestamp for the history row and tooltip.
        if marker_within_1m_candle_span(marker_ms, start_ms, end_ms):
            marker_row = dict(row)
            marker_row["chart_epoch_ms"] = marker_ms
            markers.append(marker_row)
    if selected is not None and selected.get("chart_epoch_ms") in (None, ""):
        selected = dict(selected)
        selected["chart_epoch_ms"] = selected.get(
            "signal_bar_epoch_ms", infer_closed_bar_epoch_ms(selected.get("epoch_ms"))
        )

    active_position = next((
        dict(position) for position in _engine_positions_snapshot()
        if str(position.get("ticker", "")).upper() == ticker
        and str(position.get("status", "")).upper() in ("OPEN", "PARTIAL")
    ), None)
    trade_events = _chart_trade_events(ticker, start_ny, end_ny)
    return jsonify({
        "status": "ok",
        "ticker": ticker,
        "source": source or "local",
        "selected_date": selected_date.isoformat(),
        "view": view_mode,
        "window_minutes": window,
        "live": view_mode == "live",
        "live_status": (
            "connected" if view_mode == "live" and broker_verified
            else "stale" if view_mode == "live" else None
        ),
        "live_message": str(broker_error or ""),
        "refresh_seconds": 5 if view_mode == "live" else None,
        "server_time_epoch_ms": int(now_ny.timestamp() * 1000),
        "last_closed_epoch_ms": int(
            (now_ny.replace(second=0, microsecond=0) - timedelta(minutes=1))
            .timestamp() * 1000
        ),
        "session_start_epoch_ms": int(session_start_ny.timestamp() * 1000),
        "session_end_epoch_ms": int(session_end_ny.timestamp() * 1000),
        "selected_signal": selected,
        "candles": candles,
        "signals": markers,
        "active_position": active_position,
        "trade_events": trade_events,
    })

@app.route('/api/signals', methods=['POST'])
def add_signal():
    data = request.get_json() or {}
    _register_signal(data.get("tipo",""), data.get("ticker",""),
                     data.get("precio",0), data.get("atr",0),
                     data.get("prob_ia"))
    return jsonify({"status":"ok"})

# =============================================================
# TRADES
# =============================================================
@app.route('/api/broker/open-position-pnl')
def broker_open_position_pnl():
    """Explicit read-only Schwab P/L lookup; never route orders or simulate data.

    This is not the account-wide P/L Day: closed positions and manual holdings
    may have different accounting. The operator initiates the API call from
    Trades; it is not repeated on each dashboard polling cycle.
    """
    from core.broker_pnl import open_position_pnl_snapshot
    bot = globals().get("_ENGINE")
    account = str(getattr(bot, "account_number", "") or
                  BOT_STATE.get("account_number", "") or
                  get_env("SCHWAB_ACCOUNT_NUMBER", "")).strip()
    try:
        if not account:
            raise RuntimeError("Cuenta Schwab no configurada")
        if bot is not None and getattr(bot, "schwab", None) is not None:
            positions = bot.schwab.obtener_posiciones(account)
        else:
            from api.schwab_client import SchwabClient
            client = SchwabClient(_read_config_runtime())
            account_hash = client.obtener_account_hash(account)
            positions = client.obtener_posiciones(account_hash) if account_hash else None
    except Exception:
        positions = None
    summary = open_position_pnl_snapshot(positions)
    if not summary["available"]:
        return jsonify({
            "status": "unavailable", **summary,
            "message": "Schwab no confirmó las posiciones; no se calcula P/L",
        }), 503
    return jsonify({
        "status": "ok",
        **summary,
        "as_of_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": (
            "P/L Day informado por Schwab para posiciones ABIERTAS disponibles; "
            "no incluye todos los cierres de hoy y NO equivale al P/L de la estrategia GALA"
        ),
    })


@app.route('/api/trades')
def get_trades():
    positions = _engine_positions_snapshot()
    history = _history_summary()
    live = _position_pnl_summary(positions)
    events = BOT_STATE["trades"][-100:]
    return jsonify({
        "status": "ok",
        "display_version": 3,
        "pnl_source": history["pnl_source"],
        "is_broker_reported": False,
        "excluded_closed_count": history["excluded_closed_count"],
        "positions": positions,
        "events": events,
        # Compatibility for older dashboard builds.
        "trades": events,
        "open_count": len(positions),
        "closed_count": history["closed_count"],
        "pnl_total": history["pnl_total"],
        "paper_pnl": history["paper_pnl"],
        "real_pnl": history["real_pnl"],
        "paper_closed_count": history["paper_closed_count"],
        "real_closed_count": history["real_closed_count"],
        "paper_realized_today": history["paper_realized_today"],
        "real_realized_today": history["real_realized_today"],
        **live,
        "paper_session_pnl": _pnl_sum(
            history["paper_realized_today"], live["paper_unrealized_pnl"]
        ),
        "real_session_pnl": _pnl_sum(
            history["real_realized_today"], live["real_unrealized_pnl"]
        ),
        "daily_risk": _engine_daily_risk_snapshot(),
    })

@app.route('/api/trades', methods=['POST'])
def add_trade():
    data = request.get_json() or {}
    _register_trade(data.get("tipo","BUY"), data.get("ticker",""),
                    data.get("precio",0), data.get("acciones",1),
                    data.get("estado","OPEN"), float(data.get("pnl",0)))
    return jsonify({"status":"ok"})


@app.route('/api/ticker-performance')
def get_ticker_performance():
    """Today's signal/trade/PnL summary per ticker (New York session date).

    This is a read-only dashboard view.  Realized PnL comes only from persisted
    POSITION_CLOSE rows; unrealized PnL comes from the engine's active-position
    snapshot, so the table never infers an open position from old history.
    """
    from core.ticker_performance import build_ticker_performance

    cfg = _read_config_runtime()
    selected_date = str(
        request.args.get("date")
        or _ahora_ny_datetime().strftime("%Y-%m-%d")
    )[:10]

    tickers = list(BOT_STATE.get("tickers") or [])
    if not tickers:
        tickers = list((cfg.get("market", {}) or {}).get("tickers") or [])

    with _SIGNAL_LOCK:
        signal_rows = [dict(row) for row in BOT_STATE.get("signals", [])]

    try:
        history_rows = _dashboard_history_rows(newest_first=False)
    except Exception as exc:
        _log("WARN", f"[Dashboard] Rendimiento por ticker sin historial: {exc}")
        return jsonify({"status": "unavailable", "message": "Historial no verificable", "rows": []}), 503

    positions = _engine_positions_snapshot()
    execution_rows = _ticker_execution_rows(cfg, persist_missing=False)

    rows = build_ticker_performance(
        tickers,
        signal_rows=signal_rows,
        history_rows=history_rows,
        positions=positions,
        execution_rows=execution_rows,
        session_date=selected_date,
    )

    return jsonify({
        "status": "ok",
        "date": selected_date,
        "rows": rows,
        "summary": {
            "signals": sum(int(row.get("signals", 0)) for row in rows),
            "by_mode": {
                mode: {
                    "closed_trades": sum(row["mode_breakdown"][mode]["closed_trades"] for row in rows),
                    "excluded_closed_trades": sum(row["mode_breakdown"][mode]["excluded_closed_trades"] for row in rows),
                    **{
                        field: (
                            round(sum(row["mode_breakdown"][mode][field] for row in rows), 2)
                            if all(row["mode_breakdown"][mode][field] is not None for row in rows)
                            else None
                        ) for field in ("realized_pnl", "unrealized_pnl", "total_pnl")
                    },
                } for mode in ("PAPER", "REAL", "UNKNOWN")
            },
            "is_broker_reported": False,
        },
    })


@app.route('/api/ticker-analysis')
def get_ticker_analysis():
    """Automatic evidence ranking from recent executed/closed trades.

    The ranking is observational and read-only.  It never switches a ticker to
    REAL, changes its share quantity, promotes a model, or modifies x/50.
    Model BA and AI-veto outcomes are returned only as context; ranking uses
    actual closed-trade outcomes with a small-sample penalty.
    """
    from core.ticker_analysis import build_ticker_accuracy_analysis

    cfg = _read_config_runtime()
    analysis_cfg = cfg.get("performance_analysis", {}) or {}
    try:
        lookback_days = int(request.args.get(
            "days", analysis_cfg.get("lookback_days", 30)
        ) or 30)
    except (TypeError, ValueError):
        lookback_days = 30
    lookback_days = max(1, min(3650, lookback_days))
    try:
        min_closed = int(analysis_cfg.get("min_closed_trades", 10) or 10)
    except (TypeError, ValueError):
        min_closed = 10
    min_closed = max(1, min(10000, min_closed))

    evidence_mode = str(request.args.get("mode", "REAL")).strip().upper()
    if evidence_mode not in {"PAPER", "REAL"}:
        return jsonify({"status": "error", "message": "mode debe ser PAPER o REAL"}), 400

    as_of = str(
        request.args.get("date")
        or _ahora_ny_datetime().strftime("%Y-%m-%d")
    )[:10]
    tickers = list(BOT_STATE.get("tickers") or [])
    if not tickers:
        tickers = list((cfg.get("market", {}) or {}).get("tickers") or [])

    try:
        history_rows = _dashboard_history_rows(newest_first=False)
    except Exception as exc:
        _log("WARN", f"[Dashboard] Análisis automático sin historial: {exc}")
        return jsonify({"status": "unavailable", "message": "Historial no verificable", "rows": []}), 503

    execution_rows = _ticker_execution_rows(cfg, persist_missing=False)
    ai_rows = []
    try:
        trainer = _ai_trainer_for_status()
        ai_rows = trainer.retraining_status(tickers)
    except Exception as exc:
        _log("WARN", f"[Dashboard] Análisis automático sin métricas IA: {exc}")
    try:
        veto_rows = _veto_summary_rows(tickers)
    except Exception:
        veto_rows = []

    rows = build_ticker_accuracy_analysis(
        tickers,
        history_rows=history_rows,
        execution_rows=execution_rows,
        ai_rows=ai_rows,
        veto_rows=veto_rows,
        as_of_date=as_of,
        lookback_days=lookback_days,
        min_closed_trades=min_closed,
        evidence_mode=evidence_mode,
    )
    eligible = [row for row in rows if row.get("sample_eligible")]
    review = [row for row in eligible if row.get("review_candidate")]
    return jsonify({
        "status": "ok",
        "as_of_date": as_of,
        "lookback_days": lookback_days,
        "min_closed_trades": min_closed,
        "evidence_mode": evidence_mode,
        "is_broker_reported": False,
        "ranking_basis": (
            "resultados separados por modo: Wilson win-rate + retorno normalizado; "
            "BA del modelo solo contexto"
        ),
        "auto_switch_real": False,
        "rows": rows,
        "summary": {
            "eligible": len(eligible),
            "low_sample": len(rows) - len(eligible),
            "review_candidates": len(review),
            "top_ticker": (review[0].get("ticker") if review else (eligible[0].get("ticker") if eligible else "")),
        },
    })


# =============================================================
# LOGS
# =============================================================
@app.route('/api/logs')
def get_logs():
    return jsonify({"status":"ok","logs":BOT_STATE["logs"][-300:]})

@app.route('/api/logs', methods=['POST'])
def add_log_api():
    data = request.get_json() or {}
    _log(data.get("nivel","INFO"), data.get("mensaje",""))
    return jsonify({"status":"ok"})

# =============================================================
# HISTORIAL DE OPERACIONES (registro persistente, estilo Excel)
# =============================================================
@app.route('/api/historial')
def get_historial():
    """Persistent position lifecycle, newest first; stats count closes only."""
    try:
        filas = _dashboard_history_rows(newest_first=True)
    except Exception as e:
        return jsonify({"status": "error", "mensaje": str(e), "operaciones": []})
    closed = _closed_history_rows(filas)
    return jsonify({
        "status": "ok",
        "total": len(closed),
        "excluded_closed_count": sum(not r.get("performance_verified") for r in closed),
        "total_eventos": len(filas),
        "operaciones": filas,
    })

@app.route('/api/historial/descargar')
def descargar_historial():
    """Download the configured persistent closed-trade history."""
    path = _history_path()
    if not os.path.exists(path):
        return jsonify({"status": "error", "mensaje": "Aún no hay operaciones registradas"}), 404
    return send_from_directory(
        os.path.dirname(path), os.path.basename(path),
        as_attachment=True, download_name="historial_operaciones.csv")


@app.route('/api/auditoria-ordenes')
def get_auditoria_ordenes():
    """Append-only lifecycle events, newest first."""
    from core.order_audit import read_order_audit

    try:
        limit = max(1, min(int(request.args.get("limit", 500)), 2000))
    except (TypeError, ValueError):
        limit = 500
    try:
        events = read_order_audit(
            _order_audit_path(), limit=limit, newest_first=True)
        return jsonify({
            "status": "ok",
            "total_mostrado": len(events),
            "eventos": events,
        })
    except Exception as exc:
        return jsonify({
            "status": "error",
            "mensaje": str(exc),
            "eventos": [],
        }), 500


@app.route('/api/auditoria-ordenes/descargar')
def descargar_auditoria_ordenes():
    """Downloads the complete order lifecycle ledger for external analysis."""
    path = _order_audit_path()
    if not os.path.exists(path):
        return jsonify({
            "status": "error",
            "mensaje": "Aún no hay eventos de órdenes registrados",
        }), 404
    return send_from_directory(
        os.path.dirname(path),
        os.path.basename(path),
        as_attachment=True,
        download_name="auditoria_ordenes.csv",
    )


@app.route('/api/slippage-entradas')
def get_slippage_entradas():
    """Signal, submitted limit, broker fill, and signed adverse slippage."""

    from core.execution_quality import read_execution_quality

    try:
        limit = max(1, min(int(request.args.get("limit", 500)), 2000))
    except (TypeError, ValueError):
        limit = 500
    try:
        rows = read_execution_quality(
            _execution_quality_path(), limit=limit, newest_first=True
        )
        return jsonify({
            "status": "ok", "total_mostrado": len(rows), "entradas": rows,
        })
    except Exception as exc:
        return jsonify({
            "status": "error", "mensaje": str(exc), "entradas": [],
        }), 500


@app.route('/api/slippage-entradas/descargar')
def descargar_slippage_entradas():
    path = _execution_quality_path()
    if not os.path.exists(path):
        return jsonify({
            "status": "error",
            "mensaje": "Aún no hay entradas con slippage registrado",
        }), 404
    return send_from_directory(
        os.path.dirname(path),
        os.path.basename(path),
        as_attachment=True,
        download_name="slippage_entradas.csv",
    )

# =============================================================
# TICKERS
# =============================================================
@app.route('/api/tickers')
def get_tickers():
    return jsonify({"status":"ok","tickers":BOT_STATE["tickers"]})

@app.route('/api/tickers', methods=['POST'])
def update_tickers():
    data = request.get_json() or {}
    t = list(dict.fromkeys(
        str(x).strip().upper() for x in data.get("tickers", [])
        if str(x).strip()
    ))
    if t:
        # v2.9.2 safety: a REAL-routed ticker cannot silently disappear from the
        # monitored universe.  Change it to PAPER (with its broker-flat check) first.
        current_tickers = set(BOT_STATE.get("tickers") or [])
        removed = current_tickers - set(t)
        if removed:
            try:
                cfg = _read_config_runtime()
                live = cfg.get("live_trading", {}) or {}
                if bool(live.get("per_ticker_execution", False)):
                    reg = _ticker_execution_registry(cfg)
                    removed_real = [
                        ticker for ticker in sorted(removed)
                        if reg.mode(ticker) == "REAL"
                    ]
                    if removed_real:
                        return jsonify({
                            "status": "error",
                            "message": (
                                "No se puede eliminar un ticker en REAL. "
                                "Cámbielo primero a PAPER: "
                                + ", ".join(removed_real)
                            ),
                        }), 409
            except Exception as exc:
                return jsonify({
                    "status": "error",
                    "message": f"No se pudo validar el modo del ticker: {exc}",
                }), 500

        BOT_STATE["tickers"] = t
        try:
            _ticker_execution_registry().ensure_tickers(t, persist=True)
        except Exception as e:
            _log("WARN", f"No se pudo inicializar modo por ticker: {e}")
        # PERSISTIR en disco para que la lista NO se borre al reiniciar el bot.
        try:
            os.makedirs(os.path.dirname(TICKERS_PATH), exist_ok=True)
            with open(TICKERS_PATH, "w", encoding="utf-8") as f:
                json.dump(t, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _log("WARN", f"No se pudo guardar la lista de tickers: {e}")
        # Si el motor YA está corriendo, actualizar su lista EN VIVO para que empiece
        # a rastrear los tickers nuevos en el PRÓXIMO ciclo, sin reiniciar (antes el
        # motor seguía con la lista del arranque y no rastreaba lo recién agregado).
        try:
            if _ENGINE is not None:
                _ENGINE.tickers = t
        except Exception as e:
            _log("WARN", f"No se pudo actualizar los tickers del motor en vivo: {e}")
    _log("INFO", f"Tickers: {', '.join(BOT_STATE['tickers'])}")
    return jsonify({"status":"ok","tickers":BOT_STATE["tickers"]})

# =============================================================
# v2.9.2 — EXECUTION MODE / SHARES BY TICKER
# =============================================================
@app.route('/api/ticker-execution')
def get_ticker_execution():
    cfg = _read_config_runtime()
    live = cfg.get("live_trading", {}) or {}
    rows = _ticker_execution_rows(cfg, persist_missing=True)
    allowed = set(str(t).upper() for t in (live.get("allowed_tickers") or []))
    max_real_shares = max(1, int(live.get("max_shares_per_order", 1) or 1))
    max_paper_shares = max(1, int(live.get("max_paper_shares_per_order", 1000) or 1000))
    max_real_tickers = real_ticker_limit(live)
    max_paper_positions = max(1, int(live.get("max_paper_positions", 30) or 30))
    max_real_positions = max(1, int(live.get("max_real_positions", 30) or 30))
    max_total_positions = max_paper_positions + max_real_positions
    partial_cfg = cfg.get("salida_parcial", {}) or {}
    partial_enabled = bool(partial_cfg.get("activo", True))
    partial_pct = float(partial_cfg.get("pct_primer_objetivo", 0.70) or 0.70)
    from core.ticker_analysis import partial_share_plan
    for row in rows:
        row["real_allowed"] = (not allowed) or row["ticker"] in allowed
        row["real_block_reason"] = (
            "" if row["real_allowed"] else
            "Fuera de live_trading.allowed_tickers. "
            "Requiere aprobación explícita en configuración y Live Guard."
        )
        row["max_real_shares"] = max_real_shares
        row["partial_plan"] = partial_share_plan(
            row.get("shares", 1), partial_pct, partial_enabled
        )
    return jsonify({
        "status": "ok",
        "per_ticker_execution": bool(live.get("per_ticker_execution", False)),
        "max_real_tickers": max_real_tickers,
        "capacity": getattr(getattr(_ENGINE, "real_capacity", None), "last", {}),
        "quarantine": getattr(_ENGINE, "_ticker_quarantine", {}),
        "max_real_shares": max_real_shares,
        "max_paper_shares": max_paper_shares,
        "max_paper_positions": max_paper_positions,
        "max_real_positions": max_real_positions,
        "max_total_positions": max_total_positions,
        "partial_exit_enabled": partial_enabled,
        "partial_exit_pct": round(partial_pct, 4),
        "bot_running": bool(BOT_STATE.get("running")),
        "tickers": rows,
    })


def _broker_symbol_flat_for_mode_change(cfg: dict, ticker: str):
    """Read-only fail-closed check used before REAL -> PAPER."""
    from api.broker_check import ejecutar_broker_check
    account = str(
        BOT_STATE.get("account_number", "")
        or get_env("SCHWAB_ACCOUNT_NUMBER", "")
        or ""
    ).strip()
    if not account:
        return False, "SCHWAB_ACCOUNT_NUMBER está vacío"
    try:
        report = ejecutar_broker_check(CONFIG_PATH, account, ticker=ticker)
    except Exception as exc:
        return False, f"no se pudo verificar Schwab: {exc}"
    steps = report.get("steps") or {}
    positions = steps.get("positions") or {}
    orders = steps.get("open_orders") or {}
    if not positions.get("ok") or not orders.get("ok"):
        return False, "no se pudo confirmar posiciones/órdenes del broker"
    symbols_pos = {str(x).upper() for x in (positions.get("symbols") or [])}
    symbols_ord = {str(x).upper() for x in (orders.get("symbols") or [])}
    if ticker in symbols_pos:
        return False, f"{ticker} todavía tiene una posición REAL abierta"
    if ticker in symbols_ord:
        return False, f"{ticker} todavía tiene órdenes REAL abiertas"
    return True, ""


def _serialized_execution_change(func):
    from functools import wraps
    @wraps(func)
    def wrapped(*args, **kwargs):
        with _ENGINE_CONTROL_LOCK:
            return func(*args, **kwargs)
    return wrapped


@app.route('/api/ticker-execution/<ticker>', methods=['POST'])
@_serialized_execution_change
def update_ticker_execution(ticker):
    ticker = str(ticker or "").strip().upper()
    if not ticker or ticker not in set(BOT_STATE.get("tickers") or []):
        return jsonify({"status": "error", "message": "Ticker no monitorizado"}), 404
    # Deliberate safety rule: mode/share changes are configuration changes, never
    # mid-position mutations. Stop the engine first; the normal kill switch must
    # leave the ticker flat before a REAL -> PAPER transition is accepted.
    if BOT_STATE.get("running"):
        return jsonify({
            "status": "error",
            "message": "Detenga el bot antes de cambiar PAPER/REAL o shares",
        }), 409

    data = request.get_json(force=True, silent=True) or {}
    confirm_shadow_promotion = bool(data.get("confirm_shadow_promotion", False))
    force_real_override = bool(data.get("force_real_override", False))
    force_real_confirmation = str(data.get("force_real_confirmation", "") or "").strip()
    force_real_reason = str(
        data.get("force_real_reason", "client_explicit_dashboard_override_failed_gate")
        or "client_explicit_dashboard_override_failed_gate"
    ).strip()
    cfg = _read_config_runtime()
    live = cfg.get("live_trading", {}) or {}
    if not bool(live.get("per_ticker_execution", False)):
        return jsonify({
            "status": "error",
            "message": "live_trading.per_ticker_execution no está habilitado",
        }), 409

    registry = _ticker_execution_registry(cfg)
    current = registry.get(ticker)
    if ('expected_updated_utc' in data
            and data['expected_updated_utc'] != current.get('updated_utc', '')):
        return jsonify(status='error', code='EXECUTION_CHANGED',
            message='La configuración cambió en otra sesión. Descarte el borrador y revise los valores guardados.',
            current=current), 409
    try:
        requested_mode = str(data.get("mode", current["mode"])).strip().upper()
        if requested_mode not in ("PAPER", "REAL"):
            raise ValueError("mode debe ser PAPER o REAL")
        from core.ticker_execution import normalize_share_count
        requested_shares = normalize_share_count(data.get("shares", current["shares"]))
    except (TypeError, ValueError) as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    max_real = real_ticker_limit(live)
    max_real_shares = max(1, int(live.get("max_shares_per_order", 1) or 1))
    max_paper_shares = max(1, int(live.get("max_paper_shares_per_order", 100000) or 100000))
    allowed = {str(t).strip().upper() for t in (live.get("allowed_tickers") or []) if str(t).strip()}

    if requested_mode == "REAL":
        scheduler_mode = str((cfg.get("scheduler", {}) or {}).get("modo", "manual")).strip().lower()
        if scheduler_mode in ("automatico", "automatic"):
            return jsonify({
                "status": "error",
                "message": "Cambie el scheduler a MANUAL antes de seleccionar un ticker REAL",
            }), 409
        if allowed and ticker not in allowed:
            return jsonify({
                "status": "error",
                "message": f"{ticker} no está en live_trading.allowed_tickers",
            }), 409
        if requested_shares > max_real_shares:
            return jsonify({
                "status": "error",
                "message": f"REAL permite máximo {max_real_shares} acción(es) por orden",
            }), 409
        rows = registry.snapshot(BOT_STATE.get("tickers") or [])
        would_be_real = {
            row["ticker"] for row in rows
            if row["mode"] == "REAL" and row["ticker"] != ticker
        }
        would_be_real.add(ticker)
        if len(would_be_real) > max_real:
            return jsonify({
                "status": "error",
                "message": f"Máximo {max_real} ticker(s) pueden estar en REAL",
            }), 409

        arm_name = str(live.get("arm_env", "GALA10_LIVE_ARMED") or "").strip()
        arm_value = str(live.get("arm_value", "YES") or "YES").strip()
        if arm_name and str(get_env(arm_name, "") or "").strip().upper() != arm_value.upper():
            return jsonify({
                "status": "error",
                "message": f"Falta armado explícito: {arm_name}={arm_value}",
            }), 409
        pending_model_promotion = None
        if bool(live.get("require_approved_models", True)):
            try:
                from agents.training import (
                    EntrenadorAgentes, FEATURE_VERSION, FEATURES,
                    TRAINING_POLICY_VERSION,
                )
                from core.model_promotion import (
                    inspect_shadow_candidate,
                    inspect_shadow_candidate_for_override,
                    production_artifact_state,
                )

                trainer = EntrenadorAgentes(cfg)
                if not trainer.modelo_disponible(ticker):
                    promotion_cfg = live.get("model_promotion", {}) or {}
                    if not bool(promotion_cfg.get("enabled", False)):
                        return jsonify({
                            "status": "error",
                            "code": "NO_PRODUCTION_MODEL",
                            "message": (
                                f"{ticker} no tiene modelo PRODUCTION aprobado/cargable "
                                "y la promoción guiada está deshabilitada"
                            ),
                        }), 409

                    production_state = production_artifact_state(trainer, ticker)
                    # A partially present or incompatible PRODUCTION pair is not
                    # silently overwritten. It needs explicit administrator review.
                    if production_state.get("production_artifacts_present"):
                        return jsonify({
                            "status": "error",
                            "code": "PRODUCTION_ARTIFACT_INVALID",
                            "message": (
                                f"{ticker} tiene artefactos PRODUCTION presentes pero "
                                "no cargables. Revise data/modelos antes de continuar."
                            ),
                            "production": production_state,
                        }), 409

                    preview = inspect_shadow_candidate(
                        trainer, ticker,
                        expected_features=FEATURES,
                        feature_version=FEATURE_VERSION,
                        training_policy_version=TRAINING_POLICY_VERSION,
                        load_model=True,
                    )
                    public_preview = {
                        key: value for key, value in preview.items()
                        if not str(key).startswith("_")
                    }
                    if not preview.get("eligible"):
                        override_cfg = promotion_cfg.get("manual_override", {}) or {}
                        override_enabled = bool(override_cfg.get("enabled", False))
                        override_preview = inspect_shadow_candidate_for_override(
                            trainer, ticker,
                            expected_features=FEATURES,
                            feature_version=FEATURE_VERSION,
                            training_policy_version=TRAINING_POLICY_VERSION,
                            load_model=True,
                        )
                        public_override = {
                            key: value for key, value in override_preview.items()
                            if not str(key).startswith("_")
                        }
                        if not override_preview.get("override_eligible"):
                            return jsonify({
                                "status": "error",
                                "code": "NO_ELIGIBLE_SHADOW_MODEL",
                                "message": (
                                    f"{ticker} tiene SHADOW incompatible; el override manual "
                                    "no puede ignorar errores de features/esquema/modelo"
                                ),
                                "promotion": public_override,
                            }), 409
                        if not override_enabled:
                            return jsonify({
                                "status": "error",
                                "code": "FAILED_GATE_OVERRIDE_DISABLED",
                                "message": (
                                    f"{ticker} no pasó el gate de producción y el override "
                                    "manual está deshabilitado"
                                ),
                                "promotion": public_override,
                            }), 409

                        required_phrase = f"FORCE {ticker} REAL"
                        require_exact = bool(
                            override_cfg.get("require_exact_phrase", True)
                        )
                        if not force_real_override:
                            return jsonify({
                                "status": "override_required",
                                "code": "FORCE_REAL_OVERRIDE_REQUIRED",
                                "message": (
                                    f"{ticker} no pasó la validación de producción. "
                                    "Puede activarse únicamente mediante override manual explícito."
                                ),
                                "required_confirmation": required_phrase,
                                "promotion": public_override,
                                "warning": (
                                    "El modelo será marcado como PRODUCTION por decisión manual, "
                                    "pero production_gate_passed conservará su valor original."
                                ),
                            }), 409
                        if require_exact and force_real_confirmation != required_phrase:
                            return jsonify({
                                "status": "error",
                                "code": "OVERRIDE_CONFIRMATION_MISMATCH",
                                "message": (
                                    f"Confirmación inválida. Escriba exactamente: {required_phrase}"
                                ),
                            }), 409
                        pending_model_promotion = {
                            "trainer": trainer,
                            "preview": public_override,
                            "force_override": True,
                            "override_reason": force_real_reason,
                            "acknowledged_by": str(session.get("username", "") or "dashboard_user"),
                        }
                    else:
                        require_confirmation = bool(
                            promotion_cfg.get("require_explicit_confirmation", True)
                        )
                        if require_confirmation and not confirm_shadow_promotion:
                            return jsonify({
                                "status": "promotion_required",
                                "code": "PROMOTION_REQUIRED",
                                "message": (
                                    f"{ticker} no tiene PRODUCTION. Hay un SHADOW aceptado "
                                    "que puede promoverse después de confirmación explícita."
                                ),
                                "promotion": public_preview,
                            }), 409
                        pending_model_promotion = {
                            "trainer": trainer,
                            "preview": public_preview,
                            "force_override": False,
                        }
            except Exception as exc:
                return jsonify({
                    "status": "error",
                    "message": f"No se pudo validar modelo PRODUCTION/SHADOW: {exc}",
                }), 500
    elif requested_shares > max_paper_shares:
        return jsonify({
            "status": "error",
            "message": f"PAPER permite máximo {max_paper_shares} acciones",
        }), 409

    # Any edit involving a REAL-routed ticker must begin from a broker-flat
    # symbol.  This prevents mode/share changes from adopting or conflicting with
    # positions/orders opened outside GALA10 or left by a failed shutdown.
    if "REAL" in {current["mode"], requested_mode}:
        flat, reason = _broker_symbol_flat_for_mode_change(cfg, ticker)
        if not flat:
            return jsonify({"status": "error", "message": reason}), 409

    promotion_result = None
    if requested_mode == "REAL" and pending_model_promotion is not None:
        try:
            from agents.training import (
                EntrenadorAgentes, FEATURE_VERSION, FEATURES,
                TRAINING_POLICY_VERSION,
            )
            from core.model_promotion import (
                promote_shadow_candidate,
                promote_shadow_candidate_override,
            )

            promotion_cfg = live.get("model_promotion", {}) or {}
            audit_rel = str(
                promotion_cfg.get(
                    "audit_path", "data/model_promotion_audit.jsonl"
                ) or "data/model_promotion_audit.jsonl"
            )
            audit_path = (
                audit_rel if os.path.isabs(audit_rel)
                else os.path.join(BASE_DIR, audit_rel)
            )
            if pending_model_promotion.get("force_override"):
                promotion_result = promote_shadow_candidate_override(
                    pending_model_promotion["trainer"],
                    ticker,
                    expected_features=FEATURES,
                    feature_version=FEATURE_VERSION,
                    training_policy_version=TRAINING_POLICY_VERSION,
                    audit_path=audit_path,
                    reason=pending_model_promotion.get(
                        "override_reason", "client_explicit_dashboard_override_failed_gate"
                    ),
                    app_version="3.3.2",
                    acknowledged_by=pending_model_promotion.get(
                        "acknowledged_by", "dashboard_user"
                    ),
                )
            else:
                promotion_result = promote_shadow_candidate(
                    pending_model_promotion["trainer"],
                    ticker,
                    expected_features=FEATURES,
                    feature_version=FEATURE_VERSION,
                    training_policy_version=TRAINING_POLICY_VERSION,
                    audit_path=audit_path,
                    reason="explicit_paper_to_real",
                    app_version="3.3.2",
                )
            # Fresh loader = same compatibility check used by runtime startup.
            verify_trainer = EntrenadorAgentes(cfg)
            if not verify_trainer.modelo_disponible(ticker):
                return jsonify({
                    "status": "error",
                    "code": "PROMOTION_VERIFY_FAILED",
                    "message": (
                        f"{ticker}: el SHADOW se escribió en PRODUCTION pero el "
                        "runtime no pudo cargarlo. El ticker permanece en PAPER."
                    ),
                    "promotion": promotion_result,
                }), 500
            # Invalidate the read-only AI status trainer so the dashboard sees
            # the newly promoted PRODUCTION pair immediately without restart.
            with _AI_STATUS_LOCK:
                globals()["_AI_STATUS_TRAINER"] = None
                globals()["_AI_STATUS_SIGNATURE"] = None
        except Exception as exc:
            return jsonify({
                "status": "error",
                "code": "PROMOTION_FAILED",
                "message": (
                    f"{ticker}: promoción SHADOW -> PRODUCTION falló; "
                    f"el ticker permanece en PAPER: {exc}"
                ),
            }), 500

    try:
        from core.runtime_store import EventHistory
        EventHistory(live.get('authorization_audit_path', 'data/real_mode_audit.jsonl')).append({
            'event': 'EXECUTION_MODE_CHANGE_REQUESTED', 'ticker': ticker,
            'actor': session.get('username'), 'previous': current,
            'mode': requested_mode, 'shares': requested_shares,
            'model_promotion': bool(promotion_result)})
        updated = registry.update(
            ticker, mode=requested_mode, shares=requested_shares
        )
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    override_tag = (
        " | MODEL_GATE_OVERRIDE=CLIENT_MANUAL"
        if promotion_result and promotion_result.get("manual_override") else ""
    )
    _log(
        "WARN" if requested_mode == "REAL" else "INFO",
        f"[{ticker}] modo manual -> {requested_mode} | shares={requested_shares}; "
        f"historial IA/retraining NO modificado{override_tag}",
    )
    partial_cfg = cfg.get("salida_parcial", {}) or {}
    from core.ticker_analysis import partial_share_plan
    split = partial_share_plan(
        requested_shares,
        float(partial_cfg.get("pct_primer_objetivo", 0.70) or 0.70),
        bool(partial_cfg.get("activo", True)),
    )
    split_text = (
        f" Plan parcial: TP1 {split['partial_shares']} + resto {split['runner_shares']}."
        if split.get("enabled") else
        " Salida parcial desactivada para esta cantidad (requiere >= 3)."
    )
    promotion_text = ""
    if promotion_result:
        if promotion_result.get("manual_override"):
            promotion_text = (
                " ⚠ Modelo SHADOW promovido a PRODUCTION mediante OVERRIDE MANUAL; "
                "el gate fallido queda registrado."
            )
        else:
            promotion_text = " Modelo SHADOW elegible promovido y verificado como PRODUCTION."
    return jsonify({
        "status": "ok",
        "ticker": updated,
        "partial_plan": split,
        "model_promotion": promotion_result,
        "message": (
            f"{ticker}: {requested_mode}, {requested_shares} acción(es)."
            f"{promotion_text}{split_text} El progreso IA x/50 se conserva."
        ),
    })


# =============================================================
# MARKET DATA PROXY
# =============================================================
@app.route('/api/market/quote')
def market_quote():
    symbol = request.args.get("symbol","BA")
    hdrs   = schwab_headers()
    if not hdrs:
        return jsonify({"status":"error","message":"Sin token Schwab"}), 400
    try:
        r = guarded_market_get(f"{SCHWAB_MKT_URL}/quotes", params={"symbols":symbol},
                         headers=hdrs, timeout=10)
        return jsonify({"status":"ok","data":r.json()}) if r.ok else \
               jsonify({"status":"error","message":f"HTTP {r.status_code}"}), r.status_code
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500

# =============================================================
# AI TRAINING
# =============================================================
@app.route('/api/ai/train', methods=['POST'])
def trigger_training():
    """Manual fallback: retrain only tickers already eligible for x/50.

    Heavy XGBoost work is never started while the trading engine is active.
    The manual path consumes the same lifecycle/state as the automatic
    post-session worker; it never resets counters or model history.
    """
    if BOT_STATE.get("running"):
        return jsonify({
            "status": "error",
            "message": (
                "Detenga el motor antes de reentrenar manualmente. "
                "El contador x/50 no se reinicia."
            ),
        }), 409

    trainer = None
    if _ENGINE is not None:
        try:
            trainer = _ENGINE.orquestador.entrenador
        except Exception:
            trainer = None
    if trainer is None:
        trainer = _ai_trainer_for_status()

    tickers = list(BOT_STATE.get("tickers") or [])
    if not tickers:
        cfg = _read_config_runtime()
        tickers = list(
            (cfg.get("market", {}) or {}).get("tickers")
            or (cfg.get("agents", {}) or {}).get("tickers")
            or []
        )

    eligible = trainer.retraining.eligible_tickers(tickers)
    if not eligible:
        return jsonify({
            "status": "error",
            "message": "No hay tickers pendientes/elegibles con 50 observaciones nuevas.",
            "eligible": [],
        }), 400

    _log(
        "INFO",
        "[IA] Reentrenamiento manual pending-only: " + ", ".join(eligible),
    )
    programado = trainer.solicitar_reentreno_todos(
        eligible, solo_elegibles=True, manual_request=True
    )
    return jsonify({
        "status": "ok" if programado else "error",
        "message": (
            "Evaluación SHADOW manual encolada: " + ", ".join(eligible)
            if programado else
            "No se pudo iniciar el worker de reentrenamiento"
        ),
        "eligible": eligible,
        "preserves_x50": True,
    }), (200 if programado else 500)



def _ai_trainer_for_status():
    """Use the running model, or refresh the stopped dashboard on artifact changes."""
    # Concurrent dashboard polling must share one initialized trainer. Its
    # constructor reads model/lifecycle files and can be relatively expensive.
    with _AI_STATUS_LOCK:
        return _ai_trainer_for_status_locked()


def _ai_trainer_for_status_locked():
    global _AI_STATUS_TRAINER
    if _ENGINE is not None and BOT_STATE.get('running'):
        return _ENGINE.orquestador.entrenador
    from pathlib import Path
    from core.project_paths import resolve_runtime_path
    cfg = _read_config_runtime()
    ent = cfg.get('agents', {}).get('entrenamiento', {})
    shadow = cfg.get('agents', {}).get('shadow_mode', {})
    signature = []
    for raw_root in [ent.get('modelos_dir', 'data/modelos'), shadow.get('modelos_dir', 'data/modelos_shadow')]:
        root = Path(resolve_runtime_path(raw_root))
        for path in sorted(root.glob('modelo_*')):
            if path.is_file() and path.suffix in ('.pkl', '.json'):
                stat = path.stat()
                signature.append((str(path), stat.st_mtime_ns, stat.st_size))
    signature = (str(CONFIG_PATH), tuple(signature))
    if _AI_STATUS_TRAINER is None or globals().get('_AI_STATUS_SIGNATURE') != signature:
        from agents.training import EntrenadorAgentes
        _AI_STATUS_TRAINER = EntrenadorAgentes(cfg)
        globals()['_AI_STATUS_SIGNATURE'] = signature
    return _AI_STATUS_TRAINER


def _veto_summary_rows(tickers):
    global _VETO_STATUS_STORE
    cfg = _read_config_runtime()
    veto_cfg = ((cfg.get("agents", {}) or {}).get("veto_observation_audit", {}) or {})
    bot = globals().get("_ENGINE")
    store = getattr(bot, "veto_observations", None) if bot is not None else None
    if store is None:
        if _VETO_STATUS_STORE is None:
            _VETO_STATUS_STORE = VetoObservationStore(
                str(veto_cfg.get("path", "data/ai_veto_observations.csv")),
                enabled=bool(veto_cfg.get("enabled", True)),
                max_bars=max(1, int(veto_cfg.get("max_bars", 120) or 120)),
            )
        store = _VETO_STATUS_STORE
    return store.summary_by_ticker(tickers)


def _learning_summary_rows(tickers):
    global _LEARNING_STATUS_STORE
    cfg = _read_config_runtime()
    learning_cfg = ((cfg.get("agents", {}) or {}).get("signal_learning", {}) or {})
    bot = globals().get("_ENGINE")
    store = getattr(bot, "signal_learning", None) if bot is not None else None
    if store is None:
        if _LEARNING_STATUS_STORE is None:
            _LEARNING_STATUS_STORE = SignalLearningStore(
                str(learning_cfg.get("path", "data/signal_learning_observations.csv")),
                enabled=bool(learning_cfg.get("enabled", True)),
                max_bars=max(1, int(learning_cfg.get("max_bars", 120) or 120)),
            )
        store = _LEARNING_STATUS_STORE
    return store.summary_by_ticker(tickers)


@app.route('/api/ai/status')
def ai_retraining_status():
    """Per-ticker model availability and 30/50 retraining progress."""
    try:
        trainer = _ai_trainer_for_status()
        tickers = list(BOT_STATE.get("tickers") or [])
        rows = trainer.retraining_status(tickers)
        veto_rows = _veto_summary_rows(tickers)
        veto_by_ticker = {str(r.get("ticker", "")).upper(): r for r in veto_rows}
        learning_rows = _learning_summary_rows(tickers)
        learning_by_ticker = {
            str(r.get("ticker", "")).upper(): r for r in learning_rows
        }
        try:
            with trainer._lock:
                production_thresholds = dict(trainer._umbrales)
        except Exception:
            production_thresholds = {}
        for row in rows:
            ticker = str(row.get("ticker", "")).upper()
            row.update(veto_by_ticker.get(ticker, {}))
            row.update(learning_by_ticker.get(ticker, {}))
            threshold = production_thresholds.get(ticker)
            row["production_threshold"] = (
                round(float(threshold), 4) if threshold is not None else None
            )
            prod_meta = {}
            try:
                meta_path = trainer._ruta_meta(ticker)
                if meta_path and os.path.exists(meta_path):
                    with open(meta_path, "r", encoding="utf-8-sig") as handle:
                        loaded_meta = json.load(handle)
                    if isinstance(loaded_meta, dict):
                        prod_meta = loaded_meta
            except Exception:
                prod_meta = {}
            row["production_model"] = trainer.production_snapshot(ticker) if hasattr(trainer, "production_snapshot") else {}
            # Explicit, read-only deployment diagnostics. Never mark missing or
            # incompatible artifacts as approved; REAL remains fail-closed.
            loaded = bool(row["production_model"].get("loaded"))
            issues = getattr(trainer, "model_load_errors", {}) or {}
            row["production_model_issue"] = (
                "" if loaded else str(issues.get(ticker) or "modelo de producción no encontrado")
            )
            model_version = row["production_model"].get("model_version")
            override = trainer.manual_threshold_overrides.active(ticker, model_version)
            row["manual_threshold"] = override["threshold"] if override else None
            row["effective_threshold"] = (
                row["manual_threshold"] if override else row["production_threshold"]
            )
            row["threshold_source"] = "MANUAL" if override else "MODEL"
            shadow_model = trainer.shadow_snapshot(ticker) if hasattr(trainer, "shadow_snapshot") else {}
            row["shadow_model"] = shadow_model
            # v3.3.4: historical score of the SHADOW artifact that is currently
            # installed/accepted.  This is deliberately NOT the same metric as
            # last_shadow_incumbent_balanced_accuracy, which is the incumbent
            # re-evaluated on the newest candidate's TEST window.
            row["accepted_shadow_balanced_accuracy"] = (
                shadow_model.get("balanced_accuracy") if shadow_model.get("loaded") else None
            )
            row["accepted_shadow_validation_date"] = (
                shadow_model.get("validation_date") if shadow_model.get("loaded") else None
            )
            row["accepted_shadow_training_date"] = (
                shadow_model.get("training_date") if shadow_model.get("loaded") else None
            )
            row["accepted_shadow_threshold"] = (
                shadow_model.get("threshold") if shadow_model.get("loaded") else None
            )
            row["accepted_shadow_model_version"] = (
                shadow_model.get("model_version") if shadow_model.get("loaded") else None
            )
            row["production_manual_override"] = bool(
                prod_meta.get("manual_override")
                or prod_meta.get("production_gate_override")
            )
            row["production_gate_passed"] = (
                prod_meta.get("production_gate_passed") is True
                if prod_meta else None
            )
            row["production_approval_source"] = str(
                prod_meta.get("production_approval_source", "") or ""
            )
            row["production_override_gate_errors"] = list(
                prod_meta.get("manual_override_gate_errors") or []
            )
        summary = {
            "eligible_closed_real": sum(int(r.get("eligible_closed_real", 0) or 0) for r in rows),
            "eligible_closed_paper": sum(int(r.get("eligible_closed_paper", 0) or 0) for r in rows),
            "quarantined_closed_real": sum(int(r.get("quarantined_closed_real", 0) or 0) for r in rows),
            "quarantined_closed_paper": sum(int(r.get("quarantined_closed_paper", 0) or 0) for r in rows),
            "eligible_closed_legacy": sum(int(r.get("eligible_closed_legacy", 0) or 0) for r in rows),
            "training_ready": sum(bool((r.get("training_readiness") or {}).get("ready")) for r in rows),
            "collecting": sum(r.get("status") == "COLLECTING" for r in rows),
            "ready_review": sum(r.get("status") == "READY_REVIEW" for r in rows),
            "pending": sum(r.get("status") == "PENDING_RETRAIN" for r in rows),
            "training": sum(r.get("status") == "TRAINING" for r in rows),
            "shadow_models": sum(bool(r.get("shadow_model_loaded")) for r in rows),
            "production_models": sum(bool(r.get("production_model_loaded")) for r in rows),
            "ready_models": sum(
                bool(r.get("shadow_model_loaded") or r.get("production_model_loaded"))
                for r in rows
            ),
            "eligible_observations": sum(
                int(r.get("eligible_observations", 0) or 0) for r in rows
            ),
            "quarantined_observations": sum(
                int(r.get("quarantined_observations", 0) or 0) for r in rows
            ),
            "lifetime_observations": sum(
                int(r.get("lifetime_observations", 0) or 0) for r in rows
            ),
            "veto_total": sum(int(r.get("veto_total", 0) or 0) for r in rows),
            "veto_open": sum(int(r.get("veto_open", 0) or 0) for r in rows),
            "veto_resolved": sum(int(r.get("veto_resolved", 0) or 0) for r in rows),
            "veto_good": sum(int(r.get("veto_good", 0) or 0) for r in rows),
            "veto_bad": sum(int(r.get("veto_bad", 0) or 0) for r in rows),
            "learning_total": sum(int(r.get("learning_total", 0) or 0) for r in rows),
            "learning_open": sum(int(r.get("learning_open", 0) or 0) for r in rows),
            "learning_resolved": sum(int(r.get("learning_resolved", 0) or 0) for r in rows),
            "learning_trainable": sum(int(r.get("learning_trainable", 0) or 0) for r in rows),
        }
        return jsonify({
            "status": "ok",
            "automatic_retraining": bool(trainer.auto_retrain_enabled),
            "only_after_session": bool(trainer.auto_retrain_only_after_session),
            "auto_activate_production": bool(trainer.auto_activate_production),
            "warning_samples": int(trainer.retrain_warning_samples),
            "retrain_samples": int(trainer.retrain_trigger_samples),
            "learning_policy": {
                "closed_trade_sources": ["REAL", "PAPER"],
                "resolved_signals_fit_only": True,
                "signal_weight": float(trainer.signal_learning_weight),
                "signal_max_fit_ratio": float(trainer.signal_learning_max_ratio),
                "validation_test_actual_only": True,
                "round_trip_cost_bps": float(trainer.round_trip_cost_bps),
                "min_accepted_test_trades": int(trainer.economic_min_trades),
            },
            "summary": summary,
            "tickers": rows,
        })
    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": str(exc),
            "tickers": [],
        }), 500

# =============================================================
# STARTUP
# =============================================================
from api.v330_routes import register as register_v330_routes
register_v330_routes(app, globals())

if __name__ == '__main__':
    # ── Auto-auth Schwab on startup
    saved_token = get_env("SCHWAB_ACCESS_TOKEN")
    if saved_token:
        # _load_schwab_auth_from_env() restored the real persisted expiry.
        # A process restart never extends the lifetime Schwab granted.
        _log("INFO", "[Schwab] Token cargado desde .env")
    else:
        schwab_get_token_cc()

    BOT_STATE["account_number"] = get_env("SCHWAB_ACCOUNT_NUMBER","")

    # Manual is the safe default. If the operator explicitly selected
    # "automatico", this background service starts/stops PAPER at the configured
    # New York times. Automatic REAL is blocked inside _start_engine_controlled.
    _configure_scheduler_service()

    user = get_dashboard_user()
    pwd  = get_dashboard_password()

    dashboard_host = get_env("DASHBOARD_HOST", "127.0.0.1")
    try:
        dashboard_port = int(get_env("DASHBOARD_PORT", "5000"))
    except ValueError:
        dashboard_port = 5000

    _log("INFO", "GALA10 Chocoletra Bot Server iniciado")
    _log("INFO", f"Dashboard: http://{dashboard_host}:{dashboard_port}")
    _log("INFO", f"Login: usuario='{user}'")

    print("\n" + "="*55)
    print("  GALA10 CHOCOLETRA BOT")
    print("="*55)
    print(f"  Dashboard:  http://{dashboard_host}:{dashboard_port}")
    print(f"  Usuario:    {user}")
    print("  Password:   [configurada en .env]")
    print("="*55 + "\n")

    app.run(host=dashboard_host, port=dashboard_port, debug=False, use_reloader=False,
            threaded=True)

