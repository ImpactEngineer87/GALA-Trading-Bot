"""
=============================================================
MÓDULO: engine/bot_engine.py — MOTOR REAL (con Agentes SMC)
PROYECTO: GALA6 Chocoletra Bot
=============================================================
DESCRIPCIÓN:
    Motor principal multihilo. Este es el motor REAL que arranca el
    Dashboard (reemplaza el bucle de prueba de server.py).

    Flujo por ticker en cada ciclo:
      1. Descargar historial multi-temporal (Schwab, con caché).
      2. Calcular la estrategia matemática GALA6 (core/strategy.py).
      3. Consultar el sistema de 3 Agentes SMC (orquestador):
           Agente 1 (Macro/Liquidez) → Agente 2 (Micro/FVG) →
           Agente 3 (Auditor/Riesgo) + confluencia con la estrategia.
      4. Gestionar la posición existente (salida parcial 70/30,
         breakeven, reversión reactiva, cierre).
      5. Abrir nueva operación si el sistema da vía libre, con:
           - guardián de entradas duplicadas
           - ejecución híbrida + persecución de límites (chase)
           - protección 70/30

    Seguridad: en modo paper trading NO se envía nada al broker; el
    ciclo de vida de la operación se SIMULA contra las velas reales,
    lo que además genera datos de entrenamiento para los agentes.

    Kill switch: al apagar el motor (manual o por cierre de mercado)
    se liquidan todas las posiciones abiertas.
=============================================================
"""

import sys
import time
import os
import json
import yaml
import threading
import uuid
from contextlib import contextmanager, nullcontext
import pandas as pd
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Dict, Callable

from core.strategy import GALA10Strategy
from core.signals import MotorDeSeñales, Señal
from core.risk_manager import GestorDeRiesgo, OrdenTradingCalculada
from core.time_logic import MotorTemporal, CruceMACD
from core.order_audit import OrderAuditLedger
from core.execution_quality import ExecutionQualityLedger
from core.daily_loss_guard import DailyLossGuard
from core.ticker_execution import TickerExecutionRegistry
from core.live_capacity import RealCapacityManager, max_real_tickers
from core.runtime_reliability import effective_ai_threshold
from core.position_state import (
    InvalidPositionTransition,
    PositionState,
    PositionStateMachine,
    entry_action_for_state,
    exit_action_for_state,
    position_side_for_signal,
    state_for_signal,
)
from core.trade_history import TradeHistoryLedger

from api.schwab_client import SchwabClient
from api.hybrid_order_executor import EjecutorHibridoDeOrdenes, ContextoMercado

from gsco.sheets_client import SheetsClient
from gsco.tos_reader import TOSReader
from gsco.correlator import MotorDeCorrelacion

from notifications.telegram_bot import TelegramBot

from agents.orchestrator import OrquestadorAgentes
from agents.veto_observation_store import VetoObservationStore
from agents.signal_learning_store import SignalLearningStore
from agents.contracts import DatosMultiTemporal, Direccion
from agents.gala10_features import extraer_features_gala10

from engine.logger import log_señal, log_orden, log_error, log_info, log_inicio_bot


class _DailyLossCycleBlocked(Exception):
    """Internal control-flow marker: skip workers but keep the engine alive."""


class ChocoletraBot:
    """
    Bot de trading algorítmico GALA6 Chocoletra — Motor Real con
    sistema de 3 Agentes de IA (Smart Money Concepts).
    """

    TICKERS_DEFAULT = [
        "AAPL", "AMD", "AMZN", "BA", "META", "NVDA", "RCL", "UAL",
        "TSLA", "UNH", "PINS", "NCLH", "MSFT", "NKE", "PYPL", "NFLX",
        "ADBE",
    ]

    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

        audit_path = self.config.get("logging", {}).get(
            "auditoria_ordenes", "data/auditoria_ordenes.csv")
        self.order_audit = OrderAuditLedger(audit_path)
        execution_quality_path = self.config.get("logging", {}).get(
            "slippage_entradas", "data/slippage_entradas.csv"
        )
        self.execution_quality = ExecutionQualityLedger(execution_quality_path)
        from core.execution_diagnostics import ExecutionDiagnostics
        self.execution_diagnostics = ExecutionDiagnostics(self.config.get("logging", {}).get("execution_diagnostics", "data/execution_diagnostics.jsonl"))
        history_path = self.config.get("logging", {}).get(
            "historial_operaciones", "data/historial_operaciones.csv"
        )
        self.trade_history = TradeHistoryLedger(history_path)
        self.position_states = PositionStateMachine()
        self._reversal_contexts: Dict[str, dict] = {}
        self._audit_context_local = threading.local()

        # --- Módulos Core ---
        self.estrategia = GALA10Strategy(self.config)
        self.motor_señales = MotorDeSeñales()
        self.gestor_riesgo = GestorDeRiesgo(self.config)
        self.motor_temporal = MotorTemporal()

        # --- API Schwab ---
        self.schwab = SchwabClient(self.config)
        paper = self.config.get("risk", {}).get("paper_trading", True)
        self.ejecutor_hibrido = EjecutorHibridoDeOrdenes(
            paper_trading=paper, config=self.config,
        )
        # v2.9.2 uses a dedicated PAPER executor so PAPER and REAL tickers can
        # coexist safely in the same multithreaded engine.  We never toggle an
        # executor's mode per worker/ticker (that would be a race).
        self.ejecutor_paper = EjecutorHibridoDeOrdenes(
            paper_trading=True, config=self.config,
        )
        # El ejecutor usa sesiones HTTP por hilo. Este proveedor garantiza que
        # cada llamada real tome el access token vigente del mismo cliente OAuth,
        # incluso si Schwab lo renovó durante un ciclo de datos de mercado.
        self.ejecutor_hibrido.set_token_provider(
            lambda: (
                self.schwab.access_token
                if self.schwab._asegurar_token()
                and getattr(self.schwab, "token_es_usuario", False)
                else ""
            )
        )
        # v3.0.4: liquidation/flat checks must use the exact same authoritative
        # Schwab position reader as startup/dashboard reconciliation. This avoids
        # a split-brain state where reconciliation sees META BUY 1 but the order
        # executor's separate raw GET returns BROKER_POSITION_UNKNOWN.
        self.ejecutor_hibrido.set_position_provider(
            lambda account_hash: self.schwab.obtener_posiciones(account_hash)
        )
        # v3.3.5: use the same authoritative SchwabClient WORKING-order reader
        # used by startup/dashboard reconciliation. This prevents SmartExit from
        # seeing UNKNOWN while reconciliation already knows the order set.
        self.ejecutor_hibrido.set_working_orders_provider(
            lambda account_hash: self.schwab.obtener_ordenes_abiertas(account_hash)
        )
        self.ejecutor_hibrido.set_quote_provider(self.schwab.obtener_cotizacion_ejecucion)
        self.ejecutor_hibrido.set_audit_hook(self._audit_event)
        self.ejecutor_paper.set_audit_hook(self._audit_event)

        # --- Google Sheets + GSCO ---
        self.sheets = SheetsClient()
        self.tos_reader = TOSReader(self.sheets)
        self.correlador = MotorDeCorrelacion(self.sheets)

        # --- Telegram ---
        self.telegram = TelegramBot()

        # --- Sistema de 3 Agentes SMC ---
        self.orquestador = OrquestadorAgentes(self.config)
        self.agentes_activos = self.config.get("agents", {}).get("activo", True)
        entrenamiento_cfg = self.config.get("agents", {}).get("entrenamiento", {})
        self.requerir_modelo_aprobado_en_real = bool(
            entrenamiento_cfg.get("requerir_modelo_aprobado_en_real", True)
        )
        live_cfg = self.config.get("live_trading", {}) or {}
        self.live_allowed_tickers = [
            str(t).strip().upper()
            for t in live_cfg.get("allowed_tickers", [])
            if str(t).strip()
        ]
        self.live_max_tickers = max_real_tickers(live_cfg)
        self.real_capacity = RealCapacityManager({**(live_cfg.get('capacity', {}) or {}),
            'max_positions': min(self.live_max_tickers, int(live_cfg.get('max_real_positions', 30)))})
        self._ticker_quarantine = {}
        # v3.3.6: quarantine is temporary runtime state only. It must never
        # rewrite the operator's persisted REAL/PAPER selection.
        self._ticker_quarantine_health = {}
        # v3.3.11: per-ticker runtime observability.  This is intentionally
        # in-memory and read-only from the dashboard; it never influences a
        # trading decision.  It answers whether a ticker was processed, vetoed,
        # quarantined, or blocked by execution/broker safety.
        self._ticker_processing = {}
        self.live_max_shares_per_order = max(1, int(
            live_cfg.get("max_shares_per_order", 1)
        ))
        self.live_allow_short = bool(live_cfg.get("allow_short", False))
        self.per_ticker_execution = bool(
            live_cfg.get("per_ticker_execution", False)
        )
        self.ticker_execution = TickerExecutionRegistry(
            str(live_cfg.get(
                "ticker_execution_path", "data/ticker_execution.json"
            )),
            default_mode="PAPER",
            initial_real_tickers=live_cfg.get('real_tickers', []),
            default_shares=max(1, int(
                self.config.get("risk", {}).get("fixed_shares", 1) or 1
            )),
        )
        self.max_paper_shares_per_order = max(1, int(
            live_cfg.get("max_paper_shares_per_order", 100000) or 100000
        ))
        # v2.9.5: PAPER and REAL books have independent capacity ceilings.
        # The REAL ticker allowlist/max_tickers remains a separate safety gate:
        # capacity=30 does NOT make 30 tickers REAL by itself.
        self.max_paper_positions = max(1, int(
            live_cfg.get("max_paper_positions", 30) or 30
        ))
        self.max_real_positions = max(1, int(
            live_cfg.get("max_real_positions", 30) or 30
        ))
        self.max_total_positions = (
            self.max_paper_positions + self.max_real_positions
        )

        veto_cfg = self.config.get("agents", {}).get(
            "veto_observation_audit", {}
        ) or {}
        self.veto_observations = VetoObservationStore(
            str(veto_cfg.get(
                "path", "data/ai_veto_observations.csv"
            )),
            enabled=bool(veto_cfg.get("enabled", True)),
            max_bars=max(1, int(veto_cfg.get("max_bars", 120) or 120)),
        )
        # v3.3.7: complete signal familiarization.  This separate ledger follows
        # non-executed GALA10 signals (vetoed/missed) to a hypothetical TP/SL
        # result.  It NEVER advances x/50; actual execution-valid closed trades
        # remain the trigger and validation authority for retraining.
        learning_cfg = self.config.get("agents", {}).get("signal_learning", {}) or {}
        self.signal_learning = SignalLearningStore(
            str(learning_cfg.get(
                "path", "data/signal_learning_observations.csv"
            )),
            enabled=bool(learning_cfg.get("enabled", True)),
            max_bars=max(1, int(learning_cfg.get("max_bars", 120) or 120)),
        )
        if self.per_ticker_execution:
            # The main executor is permanently REAL-capable; PAPER orders use the
            # separate executor above.  Never toggle this flag from worker threads.
            self.ejecutor_hibrido.paper_trading = False
            # The risk manager receives an exact per-ticker override.  Let the
            # dedicated executors enforce mode-specific hard caps afterwards.
            self.gestor_riesgo.max_acciones_por_orden = 0
            self.ejecutor_hibrido.max_acciones = self.live_max_shares_per_order
            self.ejecutor_paper.max_acciones = self.max_paper_shares_per_order

        # --- Configuración de reversión / kill switch ---
        rev = self.config.get("reversion", {})
        self.reversion_activa = rev.get("activo", True)
        self.breakeven_offset_pct = self.config.get("salida_parcial", {}).get("breakeven_offset_pct", 0.0)
        self.entrada_timeout_seg = self.config.get("ejecucion", {}).get("entrada_timeout_seg", 180)
        ks = self.config.get("kill_switch", {})
        self.ks_liquidar_apagar = ks.get("liquidar_al_apagar", True)
        self.ks_liquidar_cierre = ks.get("liquidar_al_cierre_mercado", True)
        self.ks_max_liquidation_attempts = max(1, int(
            ks.get("max_liquidation_attempts", 5) or 5
        ))
        self.ks_retry_delay_sec = max(0.0, float(
            ks.get("retry_delay_sec", 1.0) or 0.0
        ))
        sched = self.config.get("scheduler", {})
        self._hora_fin_mercado = self._parse_hhmm(sched.get("hora_fin", "16:00"))

        # --- Estado del Bot ---
        self._activo = False
        self._fin_sesion_alcanzado = False
        self._post_session_retrain_scheduled = False
        self._lock = threading.RLock()   # reentrante: permite anidar secciones críticas con seguridad
        # v3.3.1: stopping can be requested by the dashboard while the worker
        # thread is also leaving run() and entering its finally block.  Serialize
        # shutdown so liquidation/reconciliation runs exactly once.
        self._stop_lock = threading.RLock()
        self._stop_completed = False
        self._stop_result = {
            "stopped": False, "broker_flat": True, "ok": True, "message": ""
        }
        # Broker reconciliation can be persisted by multiple worker/stop paths.
        # Protect atomic replacement and use unique temporary files on Windows.
        self._broker_reconciliation_write_lock = threading.RLock()
        # Rate-limit a broker/OAuth outage to one useful operator message instead
        # of one ERROR per ticker/cycle.
        self._trading_auth_state_lock = threading.RLock()
        self._trading_auth_failure_active = False
        self._last_trading_auth_error_log_monotonic = 0.0
        self._trading_auth_error_log_interval_sec = max(
            10.0, float(live_cfg.get("oauth_error_log_interval_sec", 60) or 60)
        )
        self._resumen = {"buy": 0, "sell": 0, "omr": 0, "omt": 0,
                         "alertas": 0, "gsco_match": 0, "reversiones": 0,
                         "parciales": 0}
        # Tickers que ACABAMOS de liquidar con MARKET (ticker -> datetime). La red
        # de seguridad NO re-adopta uno de estos mientras la orden de cierre se
        # asienta en el broker (evita que la red pelee con una liquidación en curso).
        self._liquidados_recientes = {}
        self.COOLDOWN_LIQUIDACION_SEG = 180
        # Órdenes candidatas a huérfanas (order_id -> ciclos consecutivos vistas sin
        # posición). Se exige confirmación en 2 ciclos antes de cancelar, para no
        # actuar sobre una foto de posiciones momentáneamente incompleta.
        self._orden_huerfana_ciclos = {}
        # True cuando el hash de cuenta está resuelto (real) — sin él NO se envían
        # órdenes reales. En paper siempre listo.
        self._trading_listo = False
        self._account_plain = ""
        # v2.9.6: REAL entries are fail-closed until startup broker
        # reconciliation has succeeded.  The status is persisted so the
        # dashboard can still show an unresolved exposure after the engine stops.
        self._broker_reconciliation_ok = True
        self._broker_reconciliation = {
            "ok": True, "flat": True, "context": "INIT",
            "message": "Pendiente de reconciliación al iniciar",
            "positions": [], "open_order_symbols": [],
            "updated_at": "", "last_order_failures": {},
        }
        self._broker_reconciliation_state_path = str(
            live_cfg.get(
                "broker_reconciliation_state_path",
                "data/broker_reconciliation_state.json",
            )
        )
        self._last_order_failures: Dict[str, dict] = {}
        # Tickers con un cierre EN CURSO (guarda anti-doble-cierre entre hilos:
        # un worker y el kill switch podrían cerrar el mismo ticker a la vez).
        self._cerrando = set()
        self._order_monitor_stop = threading.Event()
        self._order_monitor_thread = None

        # --- Protección persistente por pérdida diaria neta ---
        self.daily_loss_guard = DailyLossGuard(
            self.config, history_path=history_path, now_provider=self._ahora_et
        )
        self._daily_kill_lock = threading.RLock()
        self._daily_kill_in_progress = False
        self._daily_liquidation_attempted = False
        self._last_prices: Dict[str, float] = {}
        self._last_price_times: Dict[str, float] = {}
        self._max_risk_price_age_sec = max(
            15.0, float(live_cfg.get("max_risk_price_age_sec", 120) or 120)
        )

        self.tickers: List[str] = list(self.TICKERS_DEFAULT)
        self.account_number: str = ""
        self.capital: float = 10000.0
        self.paper_trading = paper

        # Posiciones abiertas: ticker -> dict con el ciclo de vida completo
        self._posiciones: Dict[str, dict] = {}
        # Caché multi-temporal: ticker -> (epoch, DatosMultiTemporal)
        self._mtf_cache: Dict[str, tuple] = {}
        self._ultima_vela_procesada: Dict[str, object] = {}
        self._gala10_contexto: Dict[str, dict] = {}
        # Exact closed 1m bar that originated the latest GALA10 signal.
        # The decision itself is emitted seconds later; keeping both timestamps
        # prevents the chart from placing/dropping a signal on the wrong minute.
        self._gala10_signal_bar: Dict[str, object] = {}
        # Refresco MICRO (1m/5m): en CADA ciclo se re-baja solo el 1m (barato) para que
        # el precio y la señal estén al día como la pantalla del bróker (ThinkOrSwim).
        # Debe ser menor que loop_interval_sec.
        self._mtf_ttl = self.config.get("engine", {}).get("mtf_refresh_sec", 0)
        # Refresco MACRO (30m/1H/4H/1D/1W): se re-bajan cada varios minutos (cambian
        # lento). Así el precio va fresco SIN saturar la API cuando hay muchos tickers.
        self._mtf_ttl_macro = self.config.get("engine", {}).get("mtf_refresh_macro_sec", 300)

        # Hooks del dashboard (los inyecta server.py)
        self._hooks: Dict[str, Callable] = {}

        log_info("ChocoletraBot (Motor Real + Agentes SMC) inicializado correctamente")

    def configurar_modo_ejecucion(self, paper_trading: bool):
        """Fija el modo antes del arranque y mantiene motor/ejecutor coherentes."""
        if self._activo:
            raise RuntimeError("No se puede cambiar el modo con el motor activo")
        paper = bool(paper_trading)
        self.paper_trading = paper
        self.ejecutor_hibrido.paper_trading = paper
        self.config.setdefault("risk", {})["paper_trading"] = paper
        if paper:
            self._trading_listo = False
            self.ejecutor_hibrido.set_token(None)

    # ==========================================================
    # v2.9.2 — PAPER / REAL POR TICKER
    # ==========================================================

    def execution_mode_for(self, ticker: str) -> str:
        """Return the effective execution mode for one ticker.

        With v2.9.2 per-ticker routing enabled, the persistent registry is the
        execution authority. ``risk.paper_trading`` remains only the legacy
        fallback when per-ticker routing is disabled.
        """
        ticker = str(ticker or "").strip().upper()
        # getattr keeps legacy tests/tools that construct the engine with __new__
        # compatible with the pre-v2.9.2 all-PAPER/all-REAL semantics.
        if not bool(getattr(self, "per_ticker_execution", False)):
            return "PAPER" if bool(getattr(self, "paper_trading", True)) else "REAL"
        try:
            return self.ticker_execution.mode(ticker)
        except Exception:
            return "PAPER"  # fail closed

    def _is_paper_ticker(self, ticker: str, pos: Optional[dict] = None) -> bool:
        if isinstance(pos, dict):
            stored = str(pos.get("execution_mode", "") or "").upper()
            if stored in ("PAPER", "REAL"):
                return stored == "PAPER"
        return self.execution_mode_for(ticker) == "PAPER"

    def _real_tickers(self, tickers=None) -> List[str]:
        if tickers is not None:
            universe = list(tickers)
        else:
            known = getattr(self, "tickers", None)
            if known is None:
                known = list((getattr(self, "_posiciones", {}) or {}).keys())
            universe = list(known or [])
        if not bool(getattr(self, "per_ticker_execution", False)):
            return [] if bool(getattr(self, "paper_trading", True)) else universe
        return [t for t in universe if not self._is_paper_ticker(t)]

    def _has_real_tickers(self) -> bool:
        return bool(self._real_tickers())

    def shares_for_ticker(self, ticker: str) -> int:
        if not bool(getattr(self, "per_ticker_execution", False)):
            return max(1, int(getattr(self.gestor_riesgo, "fixed_shares", 1)))
        try:
            shares = max(1, int(self.ticker_execution.shares(ticker)))
        except Exception:
            shares = 1
        if not self._is_paper_ticker(ticker):
            shares = min(shares, self.live_max_shares_per_order)
        return shares

    def ticker_execution_snapshot(self) -> List[dict]:
        rows = self.ticker_execution.snapshot(self.tickers)
        for row in rows:
            row["effective_mode"] = self.execution_mode_for(row["ticker"])
        return rows

    # ==========================================================
    # HOOKS DEL DASHBOARD
    # ==========================================================

    def set_hooks(self, on_signal: Callable = None, on_trade: Callable = None,
                  on_log: Callable = None, on_candles: Callable = None):
        """server.py inyecta callbacks para reflejar el estado en el Dashboard."""
        if on_signal:
            self._hooks["signal"] = on_signal
        if on_trade:
            self._hooks["trade"] = on_trade
        if on_log:
            self._hooks["log"] = on_log
        if on_candles:
            self._hooks["candles"] = on_candles

    def _emit_log(self, nivel: str, mensaje: str):
        if nivel in ("ERROR",):
            log_error(mensaje)
        else:
            log_info(mensaje)
        cb = self._hooks.get("log")
        if cb:
            try:
                cb(nivel, mensaje)
            except Exception:
                pass

    def _emit_signal(
        self, tipo: str, ticker: str, precio: float, atr: float,
        prob: float = None, motivo: str = "", ai_threshold: float = None,
        execution_mode: str = "", ai_decision: str = "", ai_context: dict = None,
    ):
        """Emit a dashboard signal together with the decision context.

        v2.9.7 keeps the raw strategy side separate from the reason it did or
        did not reach execution.  This prevents an OM-R marker from being
        interpreted ambiguously as either an AI veto, a REAL policy block, or
        another omission.
        """
        cb = self._hooks.get("signal")
        if cb:
            try:
                result = cb(
                    tipo, ticker, precio, atr, prob, motivo,
                    ai_threshold, execution_mode, ai_decision, **({"ai_context": ai_context} if ai_context is not None else {}),
                )
                if isinstance(result, dict):
                    return result
            except Exception:
                pass
        return {
            "signal_id": uuid.uuid4().hex,
            "tipo": str(tipo or "").upper(),
            "ticker": str(ticker or "").upper(),
            "motivo": str(motivo or ""),
            "ai_threshold": ai_threshold,
            "execution_mode": str(execution_mode or "").upper(),
            "ai_decision": str(ai_decision or "").upper(),
            "ai_context": ai_context or {},
        }

    def _emit_candles(self, ticker: str, frame):
        """Best-effort dashboard archive hook; it can never interrupt trading."""
        cb = self._hooks.get("candles")
        if cb:
            try:
                cb(ticker, frame)
            except Exception:
                pass

    def _emit_trade(self, tipo: str, ticker: str, precio: float, cantidad: int,
                    estado: str = "OPEN", pnl: float = 0.0):
        cb = self._hooks.get("trade")
        if cb:
            try:
                cb(tipo, ticker, precio, cantidad, estado, pnl)
            except Exception:
                pass

    def _audit_event(self, evento: str, **fields):
        """Best-effort persistent audit; it can never interrupt trading."""
        try:
            context = dict(
                getattr(getattr(self, "_audit_context_local", None), "fields", None)
                or {}
            )
            for key, value in context.items():
                fields.setdefault(key, value)
            fields.pop('exit_signal_price', None)
            ticker_for_mode = str(fields.get("ticker", "") or "").upper()
            fields.setdefault("modo", "PAPER" if self._is_paper_ticker(ticker_for_mode) else "REAL")
            event_upper = str(evento or "").upper()
            if ticker_for_mode and (
                "REJECTED" in event_upper
                or "FAILED" in event_upper
                or event_upper in {
                    "ENTRY_BLOCKED", "EXIT_PENDING_CONFIRMATION",
                    "REVERSAL_ABORTED", "REVERSAL_OPPOSITE_ENTRY_FAILED",
                }
            ):
                failures = getattr(self, "_last_order_failures", None)
                if failures is None:
                    failures = {}
                    self._last_order_failures = failures
                failures[ticker_for_mode] = {
                    "event": event_upper,
                    "state": str(fields.get("estado", "") or ""),
                    "reason": str(fields.get("motivo", "") or ""),
                    "order_id": str(fields.get("order_id", "") or ""),
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                }
            diagnostics = getattr(self, "execution_diagnostics", None)
            if diagnostics:
                diagnostics.record(evento, fields)
            return self.order_audit.record(evento, **fields)
        except Exception as exc:
            # Avoid recursion through the dashboard audit hook. A disk problem is
            # still visible in the normal operational log.
            self._emit_log("WARN", f"Auditoría de órdenes no disponible: {exc}")
            return None

    def _state_machine(self) -> PositionStateMachine:
        machine = getattr(self, "position_states", None)
        if machine is None:
            machine = PositionStateMachine()
            self.position_states = machine
        return machine

    def _history_ledger(self) -> TradeHistoryLedger:
        ledger = getattr(self, "trade_history", None)
        if ledger is None:
            path = getattr(self, "config", {}).get("logging", {}).get(
                "historial_operaciones", "data/historial_operaciones.csv"
            )
            ledger = TradeHistoryLedger(path)
            self.trade_history = ledger
        return ledger

    def _history_event(self, evento: str, **fields) -> Optional[dict]:
        if not hasattr(self, "trade_history") and not hasattr(self, "config"):
            return None
        try:
            if evento == "POSITION_OPEN":
                return self._history_ledger().record_open(**fields)
            return self._history_ledger().record_close(**fields)
        except Exception as exc:
            ticker = str(fields.get("ticker", "") or "")
            self._emit_log(
                "WARN", f"[{ticker}] No se pudo guardar en el historial v2.9: {exc}"
            )
            return None

    def _state_snapshot(self, ticker: str):
        """Return explicit state, adopting only a pre-existing tracked position.

        The adoption fallback keeps old unit fixtures and restart-recovered v2.8
        positions compatible. Pending/reserved orders remain OUT until filled.
        """

        machine = self._state_machine()
        snapshot = machine.snapshot(ticker)
        with getattr(self, "_lock", threading.RLock()):
            pos = getattr(self, "_posiciones", {}).get(ticker)
            active = isinstance(pos, dict) and not pos.get("pendiente") \
                and not pos.get("reservado")
            if snapshot.state == PositionState.OUT and active:
                position_id = str(pos.get("position_id") or uuid.uuid4().hex)
                pos["position_id"] = position_id
                try:
                    snapshot = machine.adopt(
                        ticker, pos.get("tipo", ""), position_id
                    )
                    snapshot = machine.snapshot(ticker)
                    pos["position_state"] = snapshot.state.value
                except Exception:
                    pass
        return snapshot

    @staticmethod
    def _decision_lifecycle(decision) -> dict:
        return {
            "signal_id": str(getattr(decision, "signal_id", "") or ""),
            "execution_id": str(getattr(decision, "execution_id", "") or ""),
            "position_id": str(getattr(decision, "position_id", "") or ""),
            "reversal_id": str(getattr(decision, "reversal_id", "") or ""),
            "correlation_id": str(getattr(decision, "correlation_id", "") or ""),
        }

    def _assign_entry_lifecycle(self, decision, reversal_id: str = "") -> dict:
        signal_id = str(getattr(decision, "signal_id", "") or uuid.uuid4().hex)
        execution_id = str(getattr(decision, "execution_id", "") or uuid.uuid4().hex)
        position_id = str(getattr(decision, "position_id", "") or uuid.uuid4().hex)
        correlation_id = str(
            getattr(decision, "correlation_id", "")
            or reversal_id
            or execution_id
        )
        values = {
            "signal_id": signal_id,
            "execution_id": execution_id,
            "position_id": position_id,
            "reversal_id": str(reversal_id or getattr(decision, "reversal_id", "") or ""),
            "correlation_id": correlation_id,
        }
        for key, value in values.items():
            setattr(decision, key, value)
        return self._decision_lifecycle(decision)

    def _executor_lifecycle_scope(self, fields: dict):
        executor = getattr(self, "ejecutor_hibrido", None)
        factory = getattr(executor, "lifecycle_context", None)
        if callable(factory):
            return factory(**fields)
        return nullcontext()

    @contextmanager
    def _lifecycle_scope(self, fields: dict):
        local = getattr(self, "_audit_context_local", None)
        if local is None:
            local = threading.local()
            self._audit_context_local = local
        previous = getattr(local, "fields", None)
        merged = dict(previous or {})
        merged.update({key: value for key, value in fields.items() if value not in (None, "")})
        local.fields = merged
        try:
            with self._executor_lifecycle_scope(merged):
                yield
        finally:
            local.fields = previous

    def _position_lifecycle_context(self, ticker: str, pos: dict) -> dict:
        reversal = dict(
            getattr(self, "_reversal_contexts", {}).get(ticker) or {}
        )
        signal_side = str(reversal.get("signal_side") or "").upper()
        state = PositionState.LONG if str(pos.get("tipo", "")).upper() == "BUY" \
            else PositionState.SHORT
        return {
            "ticker": str(ticker or "").upper(),
            "correlation_id": reversal.get("correlation_id")
            or pos.get("correlation_id", ""),
            "reversal_id": reversal.get("reversal_id", ""),
            "signal_id": reversal.get("signal_id", ""),
            "execution_id": pos.get("execution_id", ""),
            "position_id": pos.get("position_id", ""),
            "state_before": state.value,
            "state_after": PositionState.OUT.value,
            "position_side": state.value,
            "signal_side": signal_side,
            "order_action": exit_action_for_state(state),
        }

    def _record_entry_quality(
        self, ticker: str, order, entry_order_id: str, context: Optional[dict]
    ) -> Optional[dict]:
        """Persist signal/limit/fill separately; never block a protected entry."""

        context = context or {}
        signal_price = float(
            context.get("signal_price") or order.precio_limite or 0.0
        )
        submitted_limit = float(
            context.get("submitted_limit_price")
            or context.get("fill_price")
            or order.precio_limite
            or 0.0
        )
        fill_price = float(
            context.get("fill_price") or order.precio_limite or 0.0
        )
        try:
            lifecycle = self._decision_lifecycle(
                context.get("decision") or getattr(order, "decision", None)
            ) if context.get("decision") is not None else dict(
                context.get("lifecycle") or {}
            )
            state_after = position_side_for_signal(order.tipo)
            return self.execution_quality.record_entry(
                modo="PAPER" if self._is_paper_ticker(ticker) else "REAL",
                ticker=ticker,
                lado=order.tipo,
                cantidad=order.cantidad,
                signal_price=signal_price,
                submitted_limit_price=submitted_limit,
                fill_price=fill_price,
                order_id=entry_order_id,
                order_type=context.get("submitted_order_type", "LIMIT"),
                execution_id=lifecycle.get("execution_id", ""),
                correlation_id=lifecycle.get("correlation_id", ""),
                reversal_id=lifecycle.get("reversal_id", ""),
                signal_id=lifecycle.get("signal_id", ""),
                position_id=lifecycle.get("position_id", ""),
                state_before=PositionState.OUT.value,
                state_after=state_after,
                position_side=state_after,
                signal_side=order.tipo,
                order_action=entry_action_for_state(state_after),
            )
        except Exception as exc:
            self._emit_log(
                "WARN", f"[{ticker}] No se pudo guardar el slippage: {exc}"
            )
            return None

    # ==========================================================
    # KILL SWITCH DE PÉRDIDA DIARIA NETA
    # ==========================================================

    def _daily_guard(self):
        """Devuelve el guard diario cuando el objeto fue inicializado completo."""
        return getattr(self, "daily_loss_guard", None)

    def _daily_blocked(self) -> bool:
        guard = self._daily_guard()
        if guard is None or not guard.enabled:
            return False
        try:
            return bool(guard.is_blocked())
        except Exception as exc:
            # Fail closed only for new entries when the persistent guard itself is
            # unavailable. Existing positions continue under their normal stops.
            self._emit_log("ERROR", f"Estado del límite diario no disponible: {exc}")
            return True

    def _registrar_pnl_realizado_diario(
        self, amount: float, ticker: str = "", motivo: str = ""
    ) -> None:
        guard = self._daily_guard()
        if guard is None or not guard.enabled:
            return
        try:
            paper_mode = self._is_paper_ticker(ticker)
            total = guard.add_realized(paper_mode, float(amount or 0.0))
            self._audit_event(
                "DAILY_REALIZED_PNL_UPDATED",
                ticker=ticker,
                estado="UPDATED",
                pnl_usd=amount,
                motivo=motivo,
                detalle={"realized_session_total": round(total, 2)},
            )
        except Exception as exc:
            self._emit_log("ERROR", f"No se pudo persistir el PnL diario: {exc}")

    def daily_risk_snapshot(self) -> dict:
        """Estado JSON-safe del límite diario para el dashboard y las pruebas."""
        guard = self._daily_guard()
        if guard is None:
            return {
                "enabled": False, "blocked": False, "mode": "PAPER",
                "limit_usd": 0.0, "realized_pnl": 0.0,
                "unrealized_pnl": 0.0, "net_pnl": 0.0,
                "remaining_to_limit_usd": 0.0,
            }

        # In a mixed session the REAL loss limit governs the live capital.
        # PAPER PnL keeps accumulating separately but cannot trip the REAL guard.
        guard_paper = not self._has_real_tickers()
        unrealized = 0.0
        priced = 0
        unpriced = 0
        with getattr(self, "_lock", threading.RLock()):
            items = list(getattr(self, "_posiciones", {}).items())
            prices = dict(getattr(self, "_last_prices", {}))
            price_times = dict(getattr(self, "_last_price_times", {}))

        for ticker, pos in items:
            if not isinstance(pos, dict) or pos.get("reservado") or pos.get("pendiente"):
                continue
            if self._is_paper_ticker(ticker, pos=pos) != guard_paper:
                continue
            try:
                entry = float(pos.get("entrada", 0.0) or 0.0)
                order = pos.get("orden")
                initial = int(pos.get("cantidad") or getattr(order, "cantidad", 0) or 0)
                qty = int(pos.get("cantidad_resto", initial) if pos.get("tp1_hit") else initial)
                if entry <= 0 or qty <= 0:
                    continue
                price = prices.get(ticker)
                if price is None:
                    unpriced += 1
                    continue
                # Objects constructed by legacy tests have no provenance map;
                # production bots always do. A stale quote is not a valuation.
                if price_times:
                    age = time.time() - float(price_times.get(ticker, 0.0) or 0.0)
                    if age > float(getattr(self, "_max_risk_price_age_sec", 120.0)):
                        unpriced += 1
                        continue
                price = float(price)
                if str(pos.get("tipo", "")).upper() == "BUY":
                    unrealized += (price - entry) * qty
                else:
                    unrealized += (entry - price) * qty
                priced += 1
            except (TypeError, ValueError):
                unpriced += 1

        snapshot = guard.snapshot(
            guard_paper,
            unrealized_pnl=unrealized,
            priced_positions=priced,
            unpriced_positions=unpriced,
        )
        snapshot["pricing_complete"] = unpriced == 0
        snapshot["valuation_source"] = "trusted_market_data"
        return snapshot

    def _evaluar_limite_perdida_diaria(self) -> bool:
        """Activa el kill switch cuando PnL realizado + no realizado toca el límite."""
        guard = self._daily_guard()
        if guard is None or not guard.enabled:
            return False
        if guard.is_blocked():
            return True
        status = self.daily_risk_snapshot()
        if float(status.get("net_pnl", 0.0)) > float(status.get("limit_usd", 0.0)):
            return False
        self._activar_kill_switch_diario(status)
        return True

    def _activar_kill_switch_diario(self, status: dict) -> None:
        """Bloquea primero, luego cancela y liquida. Idempotente entre hilos."""
        guard = self._daily_guard()
        if guard is None or not guard.enabled:
            return
        lock = getattr(self, "_daily_kill_lock", None) or threading.RLock()
        with lock:
            if guard.is_blocked():
                return
            guard_paper = str(status.get("mode", "PAPER")).upper() == "PAPER"
            limit = float(status.get("limit_usd", guard.limit_for(guard_paper)))
            net = float(status.get("net_pnl", 0.0))
            unreal = float(status.get("unrealized_pnl", 0.0))
            reason = f"límite diario alcanzado ({net:.2f} <= {limit:.2f} USD)"
            if not guard.mark_triggered(
                guard_paper, net_pnl=net, unrealized_pnl=unreal, reason=reason
            ):
                return
            self._daily_kill_in_progress = True

        self._emit_log(
            "ERROR",
            f"KILL SWITCH DIARIO: PnL neto ${net:.2f} alcanzó el límite "
            f"${limit:.2f}. Cancelando órdenes, liquidando posiciones y "
            "bloqueando nuevas entradas hasta la próxima sesión.",
        )
        self._audit_event(
            "DAILY_LOSS_KILL_TRIGGERED",
            estado="BLOCKED",
            pnl_usd=net,
            motivo=reason,
            detalle={
                "limit_usd": limit,
                "realized_pnl": status.get("realized_pnl", 0.0),
                "unrealized_pnl": unreal,
            },
        )
        try:
            self._cancelar_todas_ordenes_pendientes(
                "kill switch — pérdida diaria"
            )
            self._liquidar_todo("kill switch — pérdida diaria")
            self._daily_liquidation_attempted = True
        finally:
            with lock:
                self._daily_kill_in_progress = False

    def _cancelar_todas_ordenes_pendientes(self, motivo: str) -> None:
        """Cancela entradas/protecciones pendientes de todos los tickers gestionados."""
        with self._lock:
            pendientes = [
                ticker for ticker, pos in self._posiciones.items()
                if isinstance(pos, dict) and pos.get("pendiente")
            ]
        for ticker in pendientes:
            try:
                self._cancelar_pendiente(ticker)
            except Exception as exc:
                self._emit_log("ERROR", f"[{ticker}] No se pudo cancelar entrada pendiente: {exc}")

        if not self._has_real_tickers() or not self.account_number:
            self._set_broker_reconciliation(
                ok=True, flat=True, context="STARTUP",
                message="Sin tickers REAL o sin cuenta configurada",
                positions=[], open_order_symbols=[],
            )
            return True
        if not self._asegurar_token_trading():
            self._emit_log("ERROR", "Kill switch diario: token de trading no disponible para cancelar órdenes")
            return
        try:
            orders = self.schwab.obtener_ordenes_abiertas(self.account_number) or []
        except Exception as exc:
            self._emit_log("ERROR", f"Kill switch diario: no se pudieron leer órdenes abiertas: {exc}")
            return

        managed = {str(t).upper() for t in self._real_tickers(self.tickers)}
        for order in orders:
            try:
                oid = str(order.get("orderId", "") or "")
                legs = order.get("orderLegCollection") or []
                symbols = {
                    str((leg.get("instrument") or {}).get("symbol", "")).upper()
                    for leg in legs
                }
                symbols.discard("")
            except Exception:
                continue
            if not oid or not (symbols & managed):
                continue
            cancel_ticker = next(iter(symbols & managed), "")
            result = self.ejecutor_hibrido.cancelar_orden_confirmada(
                self.account_number, oid, ticker=cancel_ticker
            )
            self._audit_event(
                "DAILY_KILL_ORDER_CANCEL",
                ticker=next(iter(symbols & managed), ""),
                estado=result.get("broker_status", "DESCONOCIDO"),
                order_id=oid,
                latency_ms=result.get("latency_ms"),
                motivo=motivo,
                detalle={"confirmado": result.get("confirmado", False)},
            )

    # ==========================================================
    # INICIO / DETENCIÓN
    # ==========================================================

    @staticmethod
    def _segundos_hasta_cierre_confirmado(
        ahora_epoch: Optional[float] = None,
        delay_ms: int = 100,
    ) -> float:
        """Seconds until the next ``:00 + delay`` evaluation instant.

        A 1-minute candle whose timestamp is ``10:29:00`` is evaluated at
        ``10:30:00.100`` using the previous, fully closed row. If the process
        starts a few milliseconds before the configured delay, it joins that
        same boundary; if it starts after it, it waits for the next minute.
        """
        epoch = time.time() if ahora_epoch is None else float(ahora_epoch)
        delay_s = max(0.0, float(delay_ms) / 1000.0)
        phase = epoch % 60.0
        if phase <= delay_s:
            return max(0.0, delay_s - phase)
        return max(0.0, 60.0 - phase + delay_s)

    def _esperar_cierre_confirmado(self, delay_ms: int) -> None:
        """Interruptible wait used by the one-minute signal loop."""
        espera = self._segundos_hasta_cierre_confirmado(
            delay_ms=delay_ms
        )
        fin_espera = time.monotonic() + espera
        while self._activo:
            restante = fin_espera - time.monotonic()
            if restante <= 0:
                break
            time.sleep(min(0.05, restante))

    def iniciar(self, tickers: Optional[List[str]] = None,
                account_number: str = "", capital: float = 10000.0):
        """Inicia el motor real con todos los módulos y agentes activos."""
        if tickers:
            self.tickers = [str(t).strip().upper() for t in tickers if str(t).strip()]

        if self.per_ticker_execution:
            # Ensure every monitored ticker has a persistent routing record.
            self.ticker_execution.ensure_tickers(self.tickers, persist=True)

        if self._has_real_tickers() or (
            not self.per_ticker_execution and not self.paper_trading
        ):
            real_tickers = self._real_tickers(self.tickers)
            if self.per_ticker_execution:
                # v2.9.2 keeps the complete monitored universe.  Only the tickers
                # explicitly marked REAL can reach Schwab; all others continue
                # simulated PAPER operation and keep feeding the same AI history.
                if len(real_tickers) > self.live_max_tickers:
                    raise RuntimeError(
                        "Modo REAL bloqueado: tickers REAL exceden "
                        f"max_tickers={self.live_max_tickers}"
                    )
                allowed = set(self.live_allowed_tickers or self.tickers)
                blocked = [t for t in real_tickers if t not in allowed]
                if blocked:
                    raise RuntimeError(
                        "Modo REAL bloqueado: ticker(s) fuera del allowlist: "
                        + ", ".join(blocked)
                    )
            else:
                # Legacy all-real behavior retained for installations that do not
                # enable per-ticker execution.
                if not self.live_allowed_tickers:
                    raise RuntimeError(
                        "Modo REAL bloqueado: live_trading.allowed_tickers está vacío"
                    )
                if len(self.live_allowed_tickers) > self.live_max_tickers:
                    raise RuntimeError(
                        "Modo REAL bloqueado: allowed_tickers excede "
                        f"max_tickers={self.live_max_tickers}"
                    )
                requested = list(dict.fromkeys(self.tickers))
                allowed = set(self.live_allowed_tickers)
                blocked = [ticker for ticker in requested if ticker not in allowed]
                selected = [ticker for ticker in requested if ticker in allowed]
                if not selected:
                    selected = list(self.live_allowed_tickers)
                self.tickers = selected[:self.live_max_tickers]
                if blocked:
                    self._emit_log(
                        "WARN",
                        "Modo REAL: tickers fuera del allowlist ignorados: "
                        + ", ".join(blocked),
                    )

                current_cap = int(getattr(
                    self.gestor_riesgo, "max_acciones_por_orden", 0
                ) or 0)
                if current_cap <= 0 or current_cap > self.live_max_shares_per_order:
                    self.gestor_riesgo.max_acciones_por_orden = (
                        self.live_max_shares_per_order
                    )
                    self._emit_log(
                        "WARN",
                        "Modo REAL: tope de acciones ajustado a "
                        f"{self.live_max_shares_per_order} por seguridad",
                    )

        if account_number:
            self.account_number = account_number
        self.capital = capital

        log_inicio_bot(self.tickers)
        try:
            self.telegram.enviar_inicio_sesion(self.tickers)
        except Exception:
            pass
        self.tos_reader.iniciar_monitoreo(self.tickers)

        self._activo = True
        self._fin_sesion_alcanzado = False
        self._post_session_retrain_scheduled = False

        # Modo real: preparar credenciales de TRADING (token de usuario fresco +
        # hash de cuenta) ANTES de reconciliar o enviar cualquier orden.
        self._preparar_trading_real()

        # Reconciliación tras reinicio: readoptar posiciones reales abiertas en
        # Schwab (si el VPS se reinició en mitad de una operación) para no quedar
        # ciego. Las protecciones (SL/TP) siguen vivas en el broker.
        self._reconciliar_al_iniciar()
        self._iniciar_monitor_ordenes()

        guard = self._daily_guard()
        if guard is not None:
            guard.ensure_session()
            if guard.is_blocked():
                status = self.daily_risk_snapshot()
                self._emit_log(
                    "WARN",
                    "Bloqueo diario persistente activo para esta sesión: "
                    f"nuevas entradas deshabilitadas (PnL de disparo "
                    f"${float(status.get('trigger_net_pnl', 0.0)):.2f}).",
                )
                # Un reinicio no puede saltarse el kill switch. Se vuelve a
                # cancelar/liquidar por idempotencia antes de continuar en modo
                # solo-supervisión.
                self._cancelar_todas_ordenes_pendientes(
                    "kill switch — pérdida diaria persistente"
                )
                self._liquidar_todo(
                    "kill switch — pérdida diaria persistente"
                )

        intervalo = self.config.get("engine", {}).get("loop_interval_sec", 60)
        bar_delay_ms = int(self.config.get("engine", {}).get("bar_close_delay_ms", 100))
        max_workers = self.config.get("engine", {}).get("max_workers", 20)
        timeframe_1m = self.config.get("market", {}).get("timeframe", "1m") == "1m"
        real_tickers = self._real_tickers(self.tickers)
        if real_tickers and len(real_tickers) < len(self.tickers):
            modo = "MIXED"
        elif real_tickers:
            modo = "REAL"
        else:
            modo = "PAPER"
        self._emit_log("INFO", f"Motor iniciado en modo {modo} | {len(self.tickers)} activos | "
                               f"agentes={'ON' if self.agentes_activos else 'OFF'}")
        if real_tickers:
            self._emit_log(
                "WARN", "Tickers REAL: " + ", ".join(real_tickers)
                + " | resto PAPER"
            )
            if self.requerir_modelo_aprobado_en_real:
                faltantes = self.orquestador.entrenador.modelos_faltantes(real_tickers)
                if faltantes:
                    self._emit_log(
                        "ERROR",
                        "Puerta IA de modo real: nuevas entradas bloqueadas para "
                        f"tickers sin modelo aprobado/cargado: {', '.join(faltantes)}",
                    )
            self._emit_log(
                "INFO",
                f"Tope REAL por ticker: {self.live_max_shares_per_order} acción(es)."
            )
        if self.per_ticker_execution:
            self._emit_log(
                "INFO",
                "Capacidad simultánea v2.9.5: "
                f"PAPER {self.max_paper_positions} + REAL {self.max_real_positions} "
                f"= total máximo {self.max_total_positions}",
            )

        try:
            while self._activo:
                # Strict bar-close cadence: no strategy evaluation occurs in the
                # first seconds of a forming candle. Every 1m cycle starts at
                # HH:MM:00.100 (configurable) and reads the previous closed bar.
                if timeframe_1m:
                    self._esperar_cierre_confirmado(bar_delay_ms)
                    if not self._activo:
                        break
                try:
                    # Kill switch por cierre de mercado
                    if self.ks_liquidar_cierre and self._es_hora_cierre():
                        self._emit_log("WARN", "Cierre de mercado alcanzado — ejecutando kill switch")
                        self._liquidar_todo("kill switch — cierre de mercado")
                        self._fin_sesion_alcanzado = True
                        self._activo = False
                        break

                    self._emit_log("INFO", f"Ciclo | {len(self.tickers)} activos | "
                                           f"{self._ahora_et().strftime('%H:%M:%S')} NY")

                    # La pérdida diaria se evalúa ANTES de lanzar workers. Si ya
                    # quedó bloqueado (incluido tras reinicio), el motor permanece
                    # vivo para supervisión pero no procesa nuevas señales.
                    if self._evaluar_limite_perdida_diaria():
                        # The trigger path already performs one bounded emergency
                        # liquidation.  Re-sending it every minute after an OAuth
                        # failure can cancel broker protection and floods Schwab.
                        self._emit_log(
                            "WARN",
                            "Entradas bloqueadas por límite diario hasta la próxima sesión NY. "
                            "No se repetirán órdenes de liquidación automáticamente.",
                        )
                        # No reconciliar/adoptar ni ejecutar señales mientras el
                        # bloqueo diario esté activo.
                        raise _DailyLossCycleBlocked()

                    # RED DE SEGURIDAD (una vez por ciclo): adoptar con protección
                    # cualquier posición real HUÉRFANA del broker que el motor no siga.
                    # Garantiza que ninguna acción real quede sin stop ni seguimiento,
                    # pase lo que pase en las rutas individuales (invariante: el broker
                    # es la fuente de verdad y se reconcilia cada ciclo).
                    self._reconciliar_huerfanas()
                    n_workers = max(1, min(len(self.tickers), max_workers))
                    with ThreadPoolExecutor(max_workers=n_workers) as pool:
                        futuros = {pool.submit(self._procesar_ticker, t): t for t in self.tickers}
                        for futuro in as_completed(futuros):
                            ticker = futuros[futuro]
                            try:
                                futuro.result()
                            except Exception as e:
                                self._emit_log("ERROR", f"Error inesperado en {ticker}: {e}")
                except _DailyLossCycleBlocked:
                    pass
                except Exception as e:
                    # Un error a NIVEL DE CICLO (timing, reconciliación con el broker,
                    # pool de hilos) NO debe apagar el bot: se registra y se continúa en
                    # el siguiente ciclo. Antes cualquier fallo transitorio de la API se
                    # escapaba del while y el motor se apagaba solo (había que reencender).
                    self._emit_log("ERROR", f"Error en el ciclo (se continúa, no se apaga): {e}")

                # Non-1m modes retain their configured fixed interval. The 1m
                # mode waits at the top of the next iteration so its first cycle
                # is also aligned instead of firing immediately on startup.
                if not timeframe_1m:
                    fin_espera = time.monotonic() + max(0.0, float(intervalo))
                    while self._activo:
                        restante = fin_espera - time.monotonic()
                        if restante <= 0:
                            break
                        time.sleep(min(0.25, restante))

        except KeyboardInterrupt:
            self._emit_log("INFO", "Bot detenido por el usuario (Ctrl-C)")
        finally:
            self.detener()

    def detener(self, fin_sesion: Optional[bool] = None):
        """Serialized stop with recoverable emergency-flatten retries.

        v3.3.1 made shutdown idempotent, but that also meant a first unresolved
        broker liquidation could never be retried through the same engine.
        v3.3.2 keeps normal shutdown idempotent while allowing a later stop call
        to retry *only* unresolved REAL exposure.
        """
        # Some recovery/test integrations instantiate the engine through
        # ``__new__``.  Lazily initialize the shutdown guard for compatibility.
        if not hasattr(self, "_stop_lock"):
            self._stop_lock = threading.RLock()
        if not hasattr(self, "_stop_completed"):
            self._stop_completed = False
        if not hasattr(self, "_stop_result"):
            self._stop_result = {
                "stopped": False, "broker_flat": True, "ok": True, "message": ""
            }
        with self._stop_lock:
            if self._stop_completed:
                if fin_sesion is not None:
                    self._fin_sesion_alcanzado = (
                        self._fin_sesion_alcanzado or bool(fin_sesion)
                    )
                previous = dict(getattr(self, "_stop_result", {}) or {})
                if (
                    bool(getattr(self, "ks_liquidar_apagar", False))
                    and callable(getattr(self, "_has_real_tickers", None))
                    and self._has_real_tickers()
                    and not bool(previous.get("broker_flat", True))
                ):
                    self._emit_log(
                        "WARN",
                        "Parada ya ejecutada, pero queda exposición REAL; "
                        "reintentando SOLO liquidación/reconciliación",
                    )
                    broker_state = self._liquidar_todo(
                        "kill switch — reintento de exposición no resuelta"
                    ) or {}
                    flat = bool(broker_state.get("ok") and broker_state.get("flat"))
                    self._stop_result = {
                        "stopped": True,
                        "broker_flat": flat,
                        "ok": flat,
                        "message": str(broker_state.get("message", "") or ""),
                        "broker_state": dict(broker_state),
                    }
                return dict(getattr(self, "_stop_result", {}) or {})
            try:
                result = self._detener_impl(fin_sesion=fin_sesion)
                self._stop_result = dict(result or {})
                return dict(self._stop_result)
            finally:
                self._stop_completed = True

    def _detener_impl(self, fin_sesion: Optional[bool] = None):
        """Stop the engine and optionally run eligible post-session retraining.

        Manual or emergency stops never start training.  ``fin_sesion`` is set
        by the scheduler, while the engine also detects its configured close.
        """
        if fin_sesion is not None:
            self._fin_sesion_alcanzado = bool(fin_sesion)
        already_stopped = not self._activo and not self._posiciones
        self._activo = False
        self._detener_monitor_ordenes()
        broker_state = dict(getattr(self, "_broker_reconciliation", {}) or {})
        if not already_stopped:
            try:
                if self.ks_liquidar_apagar:
                    broker_state = self._liquidar_todo(
                        "kill switch — apagado del motor"
                    ) or broker_state
            except Exception as e:
                self._emit_log("ERROR", f"Error en kill switch: {e}")
                broker_state = {
                    "ok": False, "flat": False,
                    "message": f"Error en kill switch: {e}",
                }
        if (
            self._fin_sesion_alcanzado
            and not self._post_session_retrain_scheduled
            and self.config.get("agents", {}).get("entrenamiento", {}).get(
                "reentrenar_fin_sesion", True
            )
        ):
            # v2.9.5: model training and broker liquidation are separate concerns.
            # Eligible CLOSED/VERIFIED rows may retrain even if a stuck REAL
            # position requires manual attention.  The still-open trade is absent
            # from the training batch, and automatic production promotion remains
            # disabled.  This avoids losing the whole day's eligible x/50 work
            # merely because one broker close could not be confirmed.
            broker_safe = self._post_session_training_safe()
            auto_promote = bool(
                self.config.get("agents", {}).get("entrenamiento", {})
                .get("automatic_retraining", {}).get("auto_activate_production", False)
            )
            if not broker_safe:
                level = "ERROR" if auto_promote else "WARN"
                self._emit_log(
                    level,
                    "Fin de sesión con exposición/órdenes REAL no planas. "
                    + (
                        "Reentrenamiento bloqueado porque auto_activate_production=true."
                        if auto_promote else
                        "El reentrenamiento continuará SOLO con cierres verificados; "
                        "ningún modelo se promociona automáticamente."
                    ),
                )
            if broker_safe or not auto_promote:
                self._post_session_retrain_scheduled = True
                if self.orquestador.al_fin_sesion(self.tickers):
                    self._emit_log(
                        "INFO",
                        "Reentrenamiento post-sesión programado para tickers elegibles",
                    )
                else:
                    self._emit_log(
                        "INFO",
                        "Fin de sesión: ningún ticker alcanzó el umbral de reentrenamiento",
                    )
        try:
            self.tos_reader.detener()
        except Exception:
            pass
        try:
            self.telegram.enviar_fin_sesion(self._resumen)
        except Exception:
            pass
        broker_flat = True
        if self._has_real_tickers() and self.ks_liquidar_apagar:
            broker_flat = bool(broker_state.get("ok") and broker_state.get("flat"))
        if broker_flat:
            self._emit_log("INFO", "Motor detenido correctamente — broker plano confirmado")
        else:
            self._emit_log(
                "ERROR",
                "MOTOR DETENIDO — EXPOSICIÓN REAL NO RESUELTA. "
                "No reiniciar nuevas entradas hasta confirmar Schwab plano.",
            )
        return {
            "stopped": True,
            "broker_flat": broker_flat,
            "ok": broker_flat,
            "message": str(broker_state.get("message", "") or ""),
            "broker_state": dict(broker_state),
        }

    def _post_session_training_safe(self) -> bool:
        """Fail closed unless managed positions and orders are confirmed flat."""
        with self._lock:
            if any(not p.get("reservado") for p in self._posiciones.values()):
                return False
        if not self._has_real_tickers():
            return True
        if not self.account_number or not self._asegurar_token_trading():
            return False
        try:
            positions = self.schwab.obtener_posiciones(self.account_number)
            orders = self.schwab.obtener_ordenes_abiertas(self.account_number)
        except Exception:
            return False
        if positions is None or orders is None:
            return False
        managed = {str(t).upper() for t in self.tickers}
        for position in positions:
            symbol = str(position.get("symbol", "") or "").upper()
            quantity = int(position.get("cantidad", 0) or 0)
            if symbol in managed and quantity > 0:
                return False
        for order in orders:
            legs = order.get("orderLegCollection") or []
            symbols = {
                str((leg.get("instrument") or {}).get("symbol", "")).upper()
                for leg in legs
            }
            if symbols & managed:
                return False
        return True

    # ==========================================================
    # PIPELINE POR TICKER
    # ==========================================================

    def _set_ticker_processing(self, ticker: str, status: str, stage: str,
                               reason: str = "", **extra):
        """Record the latest processing stage without affecting execution."""
        ticker = str(ticker or "").upper()
        if not ticker:
            return
        now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            row = dict(self._ticker_processing.get(ticker) or {})
            if str(stage).upper() == "START":
                row["cycles_seen"] = int(row.get("cycles_seen", 0) or 0) + 1
                row["cycle_started_utc"] = now_utc
                row["gala_signal"] = ""
                row["ai_decision"] = ""
                row["execution"] = ""
            row.update({
                "ticker": ticker,
                "mode": self.execution_mode_for(ticker),
                "status": str(status or "").upper(),
                "stage": str(stage or "").upper(),
                "reason": str(reason or ""),
                "updated_utc": now_utc,
                "quarantine_reason": str(
                    getattr(self, "_ticker_quarantine", {}).get(ticker, "") or ""
                ),
            })
            for key, value in extra.items():
                if value is not None:
                    row[key] = value
            if row["status"] in {
                "PROCESSED_NO_SIGNAL", "VETOED", "APPROVED", "EXECUTED",
                "ORDER_SUBMITTED", "BLOCKED", "QUARANTINED", "ERROR",
            }:
                row["last_completed_utc"] = now_utc
            self._ticker_processing[ticker] = row

    def ticker_processing_snapshot(self):
        """JSON-safe processing health for every monitored ticker."""
        with self._lock:
            current = {
                str(k).upper(): dict(v)
                for k, v in getattr(self, "_ticker_processing", {}).items()
                if isinstance(v, dict)
            }
            tickers = list(getattr(self, "tickers", []) or self.TICKERS_DEFAULT)
            quarantine = dict(getattr(self, "_ticker_quarantine", {}) or {})
        rows = []
        for ticker in tickers:
            ticker = str(ticker).upper()
            row = current.get(ticker, {
                "ticker": ticker,
                "status": "WAITING",
                "stage": "IDLE",
                "reason": "No cycle recorded yet",
                "updated_utc": "",
                "cycles_seen": 0,
            })
            row["mode"] = self.execution_mode_for(ticker)
            row["quarantine_reason"] = str(quarantine.get(ticker, "") or "")
            row["quarantined"] = bool(row["quarantine_reason"])
            rows.append(row)
        return sorted(rows, key=lambda x: x.get("ticker", ""))

    def _procesar_ticker(self, ticker: str):
        """Pipeline completo para un ticker (se ejecuta en un hilo)."""
        paper_ticker = self._is_paper_ticker(ticker)
        self._set_ticker_processing(
            ticker, "PROCESSING", "START",
            market_data="PENDING", configured_mode=("PAPER" if paper_ticker else "REAL"),
        )
        # 1) Datos multi-temporal (con caché)
        try:
            datos = self._obtener_mtf(ticker)
        except Exception as exc:
            self._quarantine_real_ticker(ticker, 'Market data request failed')
            self._set_ticker_processing(
                ticker, "QUARANTINED", "MARKET_DATA",
                "MARKET_DATA_REQUEST_FAILED", market_data="ERROR",
                detail=type(exc).__name__,
            )
            return
        datos_confirmados = self._datos_micro_confirmados(datos)
        # GALA10 y el dimensionamiento inmediato trabajan sobre 1m. Las TF mayores
        # siguen disponibles para los agentes como contexto/veto.
        df_1m = datos_confirmados.tf("1m")
        df_micro = df_1m
        if df_micro is None:
            df_micro = datos_confirmados.tf("5m")
        if df_micro is None:
            df_micro = datos_confirmados.tf("15m")
        if df_micro is None or len(df_micro) < 30:
            self._emit_log("WARN", f"[{ticker}] Datos micro insuficientes")
            self._quarantine_real_ticker(ticker, 'Insufficient market data')
            self._set_ticker_processing(
                ticker, "QUARANTINED", "MARKET_DATA",
                "INSUFFICIENT_MARKET_DATA", market_data="INSUFFICIENT",
            )
            return
        # A fallback frame is useful for keeping diagnostics alive, but it is not
        # a price source.  Never publish it into risk/PnL, manage positions, close
        # PAPER trades, or route REAL orders.  This check must precede
        # ``_last_prices`` and the daily-loss evaluation.
        if datos.sintetico:
            if not paper_ticker:
                self._quarantine_real_ticker(ticker, "Synthetic market data")
            self._emit_log(
                "WARN",
                f"[{ticker}] Datos sintéticos descartados: sin valoración ni ejecución",
            )
            self._set_ticker_processing(
                ticker, "QUARANTINED", "MARKET_DATA",
                "SYNTHETIC_MARKET_DATA", market_data="SYNTHETIC",
            )
            return
        if df_1m is not None and len(df_1m) and not datos.sintetico:
            self._emit_candles(ticker, df_1m)
        precio_actual = float(df_micro["close"].iloc[-1])
        self._set_ticker_processing(
            ticker, "PROCESSING", "DATA_READY",
            market_data="REAL", last_price=round(precio_actual, 4),
        )
        with self._lock:
            self._last_prices[ticker] = precio_actual
            self._last_price_times[ticker] = time.time()
        self._recover_ticker_quarantine(ticker, 'market_data')
        paper_ticker = self._is_paper_ticker(ticker)

        # v2.9.4: advance audit-only hypothetical outcomes for signals that the
        # production AI vetoed in prior cycles.  This ledger is intentionally
        # separate from agentes_feedback.csv and never increments x/50.
        try:
            last_bar = df_micro.iloc[-1]
            bar_ts = df_micro.index[-1] if len(df_micro.index) else None
            resolved_vetoes = self.veto_observations.update_bar(
                ticker,
                high=float(last_bar["high"]),
                low=float(last_bar["low"]),
                close=float(last_bar["close"]),
                bar_timestamp=bar_ts,
            )
            for audit_row in resolved_vetoes:
                self._emit_log(
                    "INFO",
                    f"[AI-VETO-AUDIT] {ticker} {audit_row.get('tipo')} "
                    f"→ {audit_row.get('resolution')} "
                    f"R={audit_row.get('r_multiple', '')} "
                    "(no suma x/50)",
                )
        except Exception as exc:
            self._emit_log(
                "WARN",
                f"[AI-VETO-AUDIT] {ticker}: no se pudo actualizar: {exc}",
            )

        # v3.3.7: advance the broader signal-learning ledger with the same
        # confirmed bar. Only unambiguous TP/SL counterfactuals may later be
        # used as low-weight FIT examples; they never count toward x/50.
        try:
            last_bar = df_micro.iloc[-1]
            bar_ts = df_micro.index[-1] if len(df_micro.index) else None
            resolved_learning = self.signal_learning.update_bar(
                ticker,
                high=float(last_bar["high"]),
                low=float(last_bar["low"]),
                close=float(last_bar["close"]),
                bar_timestamp=bar_ts,
            )
            for learning_row in resolved_learning:
                self._emit_log(
                    "INFO",
                    f"[AI-LEARNING] {ticker} {learning_row.get('tipo')} "
                    f"{learning_row.get('source_kind')} → "
                    f"{learning_row.get('resolution')} "
                    f"R={learning_row.get('r_multiple', '')} "
                    f"trainable={learning_row.get('training_eligible', 0)} "
                    "(suplementaria, no suma x/50)",
                )
        except Exception as exc:
            self._emit_log(
                "WARN",
                f"[AI-LEARNING] {ticker}: no se pudo actualizar: {exc}",
            )

        # Evaluación intra-ciclo: con 17 workers el límite puede tocarse después
        # de obtener un precio y antes del siguiente ciclo global. Bloquear primero
        # impide que otra hebra reserve/mande una entrada nueva.
        if self._evaluar_limite_perdida_diaria():
            self._set_ticker_processing(
                ticker, "BLOCKED", "RISK", "DAILY_LOSS_BLOCK"
            )
            return

        # 2) Gestionar posición ya abierta (parciales / breakeven / cierre)
        with self.ejecutor_hibrido.secuencia_simbolo(ticker):
            self._gestionar_posiciones(ticker, precio_actual, df_micro)

        # 3) GALA 10 = DISPARADOR ÚNICO. Sin señal de Gala 10 no se opera.
        senal_gala10 = self._senal_estrategia(
            datos_confirmados, ticker, precio_actual
        )
        if senal_gala10 not in ("BUY", "SELL"):
            self._set_ticker_processing(
                ticker, "PROCESSED_NO_SIGNAL", "STRATEGY", "NO_GALA10_SIGNAL",
                gala_signal=str(senal_gala10 or ""),
            )
            return
        self._set_ticker_processing(
            ticker, "PROCESSING", "GALA10_SIGNAL", "",
            gala_signal=senal_gala10,
        )
        if not self.agentes_activos:
            self._set_ticker_processing(
                ticker, "BLOCKED", "AGENTS", "AGENTS_DISABLED",
                gala_signal=senal_gala10,
            )
            return

        # v2.9.3: una señal opuesta a una posición ya abierta tiene DOS efectos
        # distintos: (1) cerrar la posición actual y (2) eventualmente abrir la
        # opuesta. La IA de producción puede vetar (2), pero nunca debe dejar una
        # posición antigua abierta solo porque el nuevo lado fue vetado.
        with self._lock:
            _pos_signal = self._posiciones.get(ticker)
            _opposite_existing = bool(
                _pos_signal
                and not _pos_signal.get("reservado")
                and not _pos_signal.get("pendiente")
                and (
                    (_pos_signal.get("tipo") == "BUY" and senal_gala10 == "SELL")
                    or (_pos_signal.get("tipo") == "SELL" and senal_gala10 == "BUY")
                )
            )
            _existing_long = bool(
                _pos_signal
                and not _pos_signal.get("reservado")
                and not _pos_signal.get("pendiente")
                and _pos_signal.get("tipo") == "BUY"
            )

        # En long-only, SELL sigue siendo válida para CERRAR un LONG existente.
        # Solo se bloquea SELL cuando implicaría ABRIR un SHORT desde OUT.
        if (
            not paper_ticker
            and senal_gala10 == "SELL"
            and not self.live_allow_short
            and not _existing_long
        ):
            self._emit_log(
                "WARN",
                f"[{ticker}] SELL de entrada bloqueada: REAL long-only. "
                "SELL para cerrar un LONG sí está permitida.",
            )
            self._emit_signal(
                "OM-R", ticker, precio_actual, self._atr_micro(df_micro), None,
                motivo="REAL_LONG_ONLY",
                ai_threshold=self._ai_threshold_for_ticker(ticker),
                execution_mode="REAL",
                ai_decision="BLOCKED_POLICY",
            )
            self._set_ticker_processing(
                ticker, "BLOCKED", "POLICY", "REAL_LONG_ONLY",
                gala_signal=senal_gala10, ai_decision="BLOCKED_POLICY",
            )
            return
        requerir_modelo = bool(
            not paper_ticker and self.requerir_modelo_aprobado_en_real
        )
        if requerir_modelo and self.orquestador.entrenador.modelo_disponible(ticker):
            self._recover_ticker_quarantine(ticker, 'model')
        if (
            requerir_modelo
            and not self.orquestador.entrenador.modelo_disponible(ticker)
        ):
            self._quarantine_real_ticker(ticker, 'Production model unavailable')
            self._emit_log(
                "ERROR",
                f"[{ticker}] Señal {senal_gala10} bloqueada: modo real exige "
                "modelo IA aprobado y cargado.",
            )
            self._emit_signal(
                "OM-R", ticker, precio_actual, self._atr_micro(df_micro), None,
                motivo="REAL_MODEL_REQUIRED",
                ai_threshold=self._ai_threshold_for_ticker(ticker),
                execution_mode="REAL",
                ai_decision="BLOCKED_MODEL",
            )
            self._set_ticker_processing(
                ticker, "BLOCKED", "AI_MODEL", "REAL_MODEL_REQUIRED",
                gala_signal=senal_gala10, ai_decision="BLOCKED_MODEL",
            )
            return

        # 4) ATR micro + features GSCO (opcional) para la auditoría del ticker.
        atr_micro = self._atr_micro(df_micro)
        gsco = self._gsco_features(ticker, senal_gala10, precio_actual, self._ahora_et())

        # 5) El XGBoost INDIVIDUAL del ticker AUDITA la señal de Gala 10 (veto).
        try:
            # Hora ET (naive) para la decisión: la feature 'hora' del modelo debe
            # tener la MISMA convención horaria que el pre-entrenamiento (que deriva
            # 'hora' del índice de velas convertido a ET). Usar datetime.now() aquí
            # metería la hora LOCAL del servidor y sesgaría el veto por franja horaria
            # si el VPS no está en ET → skew train/serve. Ver backtest_trainer._as_dt.
            decision = self.orquestador.auditar_gala10(
                datos_confirmados, precio_actual, senal_gala10, atr_micro,
                ahora=self._ahora_et().replace(tzinfo=None),
                actual_en_formacion=False,
                macro_en_formacion=True,
                micro_en_formacion=False,
                gsco=gsco,
                gala10_contexto=self._gala10_contexto.get(ticker),
                requerir_modelo=requerir_modelo,
                execution_mode=("PAPER" if paper_ticker else "REAL"),
            )
        except Exception as e:
            self._emit_log("ERROR", f"[{ticker}] Error en auditoría de agentes: {e}")
            self._quarantine_real_ticker(ticker, 'AI inference failed')
            self._set_ticker_processing(
                ticker, "ERROR", "AI_INFERENCE", "AI_INFERENCE_FAILED",
                gala_signal=senal_gala10, detail=type(e).__name__,
            )
            return

        if getattr(decision, 'ai_inference_error', False) and not paper_ticker:
            self._quarantine_real_ticker(ticker, 'Production inference unavailable')
        elif not paper_ticker:
            self._recover_ticker_quarantine(ticker, 'inference')

        # v3.1: one explainable line per GALA10 decision.  This is deliberately
        # emitted after the full AI/regime audit and before execution so the
        # dashboard/server log preserves the exact decision context.
        try:
            self._emit_log("INFO", f"[AI-AUDIT] [{ticker}] {decision.resumen}")
        except Exception:
            pass

        # PANEL COHERENTE. Se muestra el DISPARADOR de Gala 10 (BUY/SELL) con la
        # probabilidad de la IA. Si la IA APRUEBA → BUY/SELL (se ejecuta). Si la IA
        # VETA → OM-R (omitida, VISIBLE en el panel). La dirección macro del Agente 1
        # (ALCISTA/BAJISTA/NEUTRAL) es solo un feature de CONTEXTO y ya NO se muestra
        # como si fuera la decisión: antes se veía 'NEUTRAL 93%' al lado de un BUY y
        # parecía una contradicción, cuando ese 93% es la IA APROBANDO el BUY de Gala 10.
        ai_context = dict(getattr(decision, 'ai_model_snapshot', {}) or {})
        ai_context.update(market_regime=getattr(decision, 'market_regime', 'UNKNOWN'),
                          raw_probability=getattr(decision, 'ai_raw_probability', None),
                          manual_authorization=getattr(decision, 'ai_manual_authorization', None))
        # v3.3.1: persist both decision wall-clock time (server hook) and the
        # exact closed bar that generated GALA10.  The 1m loop can emit at
        # 13:34:04 for the bar that opened at 13:33:00.
        try:
            with self._lock:
                signal_bar = self._gala10_signal_bar.get(ticker)
            if signal_bar is not None:
                bar_ts = pd.Timestamp(signal_bar)
                if bar_ts.tzinfo is None:
                    bar_ts = bar_ts.tz_localize("UTC")
                else:
                    bar_ts = bar_ts.tz_convert("UTC")
                ai_context["signal_bar_epoch_ms"] = int(bar_ts.timestamp() * 1000)
                ai_context["signal_bar_timestamp_iso"] = bar_ts.isoformat()
        except Exception:
            pass
        if decision.operar:
            self._set_ticker_processing(
                ticker, "APPROVED", "AI_DECISION", "AI_APPROVED",
                gala_signal=senal_gala10, ai_decision="APPROVED",
                ai_probability=getattr(decision, "prob_meta", None),
                ai_threshold=self._effective_ai_threshold(decision, ticker),
            )
            signal_row = self._emit_signal(
                senal_gala10, ticker, precio_actual,
                atr_micro, decision.prob_meta,
                motivo=self._ai_decision_reason(decision, approved=True),
                ai_threshold=self._effective_ai_threshold(decision, ticker),
                execution_mode=("PAPER" if paper_ticker else "REAL"),
                ai_decision="APPROVED", ai_context=ai_context,
            )
            decision.signal_id = str(
                (signal_row or {}).get("signal_id") or uuid.uuid4().hex
            )
        else:
            # v2.9.3: la IA es un filtro de ENTRADA, no un permiso para mantener
            # una posición contra una nueva señal maestra. Si GALA10 gira contra
            # la posición actual, se cierra a OUT aunque la IA vete abrir el lado
            # opuesto. El panel muestra la dirección real de salida, no una falsa
            # ausencia de SELL/BUY.
            if _opposite_existing:
                signal_row = self._emit_signal(
                    senal_gala10, ticker, precio_actual, atr_micro, decision.prob_meta,
                    motivo=(
                        "AI_VETO_CLOSE_ONLY:"
                        + self._ai_decision_reason(decision, approved=False)
                    ),
                    ai_threshold=self._effective_ai_threshold(decision, ticker),
                    execution_mode=("PAPER" if paper_ticker else "REAL"),
                    ai_decision="VETO_CLOSE_ONLY", ai_context=ai_context,
                )
                decision.signal_id = str(
                    (signal_row or {}).get("signal_id") or uuid.uuid4().hex
                )
                self._registrar_veto_ia(
                    decision,
                    execution_mode=("PAPER" if paper_ticker else "REAL"),
                )
                self._emit_log(
                    "INFO",
                    f"[{ticker}] {senal_gala10} opuesta: IA vetó la nueva entrada, "
                    "pero se cerrará la posición vigente y quedará OUT.",
                )
                with self.ejecutor_hibrido.secuencia_simbolo(ticker):
                    self._cerrar_posicion(
                        ticker, precio_actual,
                        motivo=f"señal opuesta {senal_gala10} (entrada IA vetada)",
                    )
                self._registrar_no_op(decision)
                return
            signal_row = self._emit_signal(
                "OM-R", ticker, precio_actual, atr_micro, decision.prob_meta,
                motivo=(
                    "AI_VETO:"
                    + self._ai_decision_reason(decision, approved=False)
                ),
                ai_threshold=self._effective_ai_threshold(decision, ticker),
                execution_mode=("PAPER" if paper_ticker else "REAL"),
                ai_decision="VETO", ai_context=ai_context,
            )
            decision.signal_id = str(
                (signal_row or {}).get("signal_id") or uuid.uuid4().hex
            )
            self._registrar_veto_ia(
                decision,
                execution_mode=("PAPER" if paper_ticker else "REAL"),
            )
            self._registrar_no_op(decision)
            self._set_ticker_processing(
                ticker, "VETOED", "AI_DECISION", "AI_VETO",
                gala_signal=senal_gala10, ai_decision="VETO",
                ai_probability=getattr(decision, "prob_meta", None),
                ai_threshold=self._effective_ai_threshold(decision, ticker),
            )
            return

        # 6) Reversión o apertura
        with self.ejecutor_hibrido.secuencia_simbolo(ticker):
            self._abrir_o_revertir(decision, precio_actual, df_micro)

    def _atr_micro(self, df_micro) -> float:
        """ATR de la temporalidad micro (para dimensionar SL/TP de la entrada)."""
        try:
            from agents import smc_primitives as smc
            serie = smc.atr_wilder(df_micro, self.config.get("atr", {}).get("period", 14))
            return float(serie.iloc[-1]) if len(serie) else 0.0
        except Exception:
            return 0.0

    def _gsco_features(self, ticker: str, tipo: str, precio: float, ahora_et) -> dict:
        """Features GSCO (institucional) para la señal de Gala 10, OPCIONALES:
        - Los posteos salen 15:15-15:40 ET; se toman los primeros 4.
        - Antes de las 15:15 o si la hoja está vacía → NaN (no bloquea; el modelo
          decide con el resto de features).
        Devuelve {'match': 1/0/NaN, 'dist_pct': %/NaN}."""
        nan = float("nan")
        vacio = {"match": nan, "dist_pct": nan}
        try:
            minutos = ahora_et.hour * 60 + ahora_et.minute
            if minutos < 15 * 60 + 15:      # antes de 15:15 ET aún no hay posteos
                return vacio
            posteos = self.sheets.obtener_posteos_gsco(ticker, solo_hoy=True)
            posteos = self._posteos_en_ventana(posteos)[:4]   # primeros 4 de 15:15-15:40
            if not posteos:
                return vacio                # hoja vacía → opcional, sigue operando
            direccion_macd = "BULL" if tipo == "BUY" else "BEAR"
            match = self.correlador._buscar_coincidencia(posteos, direccion_macd, precio)
            if match is None:
                return {"match": 0.0, "dist_pct": nan}   # hay datos pero no coincide
            dist = abs(match.precio - precio) / precio * 100.0 if precio > 0 else nan
            return {"match": 1.0, "dist_pct": round(dist, 4)}
        except Exception:
            return vacio

    @staticmethod
    def _posteos_en_ventana(posteos):
        """Filtra los posteos a la ventana 15:15-15:40 ET (donde salen)."""
        out = []
        for p in (posteos or []):
            try:
                hm = str(p.hora).split(" ")[-1]     # "HH:MM:SS"
                hh, mm = int(hm.split(":")[0]), int(hm.split(":")[1])
                minutos = hh * 60 + mm
                if 15 * 60 + 15 <= minutos <= 15 * 60 + 40:
                    out.append(p)
            except Exception:
                out.append(p)   # si no se puede parsear la hora, no descartar
        return out

    def _ai_threshold_for_ticker(self, ticker: str):
        """Return the currently loaded production decision threshold, if any."""
        trainer = getattr(self.orquestador, "entrenador", None)
        if trainer is None:
            return None
        ticker = str(ticker or "").upper()
        try:
            with trainer._lock:
                value = trainer._umbrales.get(ticker, trainer.umbral_meta)
        except Exception:
            value = getattr(trainer, "umbral_meta", None)
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    def _effective_ai_threshold(self, decision, ticker: str):
        """Threshold used by this exact AI decision (SHADOW or PRODUCTION)."""
        snapshot = getattr(decision, "ai_model_snapshot", {}) or {}
        return effective_ai_threshold(
            snapshot, self._ai_threshold_for_ticker(ticker)
        )

    def _registrar_veto_ia(self, decision, execution_mode: str):
        """Persist a non-executed audited signal in both audit layers.

        ``ai_veto_observations.csv`` remains the historical veto-usefulness
        report.  v3.3.7 additionally writes the full feature snapshot into
        ``signal_learning_observations.csv`` so resolved vetoes/misses can be
        used as *low-weight FIT-only* examples. Neither ledger advances x/50.
        """
        learning_row = None
        try:
            reason = str(getattr(decision, "execution_veto_reason", "") or "")
            hard_reason = str(getattr(decision, "hard_veto_reason", "") or "")
            if not reason:
                reason = f"HARD_VETO:{hard_reason}" if hard_reason else "AI_THRESHOLD"
            source_kind = (
                "MODEL_UNAVAILABLE"
                if getattr(decision, "prob_meta", None) is None
                else "AI_VETO"
            )
            learning_row = self.signal_learning.record_nonexecuted(
                decision,
                execution_mode=str(execution_mode or "").upper(),
                source_kind=source_kind,
                disposition="BLOCKED",
                reason=reason,
                training_candidate=True,
            )
        except Exception as exc:
            self._emit_log(
                "WARN", f"[AI-LEARNING] No se pudo registrar señal vetada: {exc}"
            )

        try:
            prob = getattr(decision, "prob_meta", None)
            propuesta = getattr(decision, "propuesta", None)
            # The legacy veto-usefulness ledger requires an actual model score.
            # MODEL_UNAVAILABLE signals are still captured by signal_learning.
            if prob is None or propuesta is None:
                return learning_row
            ticker = str(getattr(decision, "ticker", "") or "").upper()
            tipo = str(getattr(decision, "tipo", "") or "").upper()
            if not ticker or tipo not in ("BUY", "SELL"):
                return learning_row

            threshold = None
            trainer = getattr(self.orquestador, "entrenador", None)
            if trainer is not None:
                try:
                    with trainer._lock:
                        threshold = trainer._umbrales.get(
                            ticker, trainer.umbral_meta
                        )
                except Exception:
                    threshold = getattr(trainer, "umbral_meta", None)

            hard_reason = str(getattr(decision, "hard_veto_reason", "") or "")
            reason = (
                f"HARD_VETO:{hard_reason}"
                if hard_reason else str(
                    getattr(decision, "execution_veto_reason", "") or "AI_THRESHOLD"
                )
            )
            row = self.veto_observations.record_veto(
                ticker=ticker,
                tipo=tipo,
                probability=float(prob),
                threshold=threshold,
                entry_price=float(propuesta.precio_entrada),
                stop_loss=float(propuesta.stop_loss),
                take_profit=float(propuesta.take_profit),
                execution_mode=str(execution_mode or "").upper(),
                veto_reason=reason,
                signal_id=str(getattr(decision, "signal_id", "") or ""),
                timestamp=getattr(decision, "timestamp", None),
            )
            if row:
                threshold_text = (
                    "N/A" if threshold is None else f"{float(threshold):.3f}"
                )
                self._emit_log(
                    "INFO",
                    f"[AI-VETO-AUDIT] {ticker} {tipo} registrada "
                    f"prob={float(prob):.3f} umbral={threshold_text}; "
                    "audit-only, no suma x/50",
                )
            return row or learning_row
        except Exception as exc:
            self._emit_log(
                "WARN",
                f"[AI-VETO-AUDIT] No se pudo registrar veto: {exc}",
            )
            return learning_row

    def _registrar_senal_no_ejecutada(
        self, decision, *, reason: str, source_kind: str = "EXECUTION_MISSED",
        training_candidate: bool = True,
    ):
        """Record an approved GALA10 signal that never became a position.

        This is signal-quality evidence, not execution PnL. The hypothetical
        outcome is followed on confirmed bars. Account-policy-only blocks can
        set ``training_candidate=False`` so they remain visible without becoming
        model labels.
        """
        try:
            mode = str(getattr(decision, "execution_mode", "") or "").upper()
            if mode not in {"PAPER", "REAL"}:
                mode = "PAPER" if self._is_paper_ticker(getattr(decision, "ticker", "")) else "REAL"
            row = self.signal_learning.record_nonexecuted(
                decision,
                execution_mode=mode,
                source_kind=source_kind,
                disposition="MISSED",
                reason=reason,
                training_candidate=training_candidate,
            )
            if row:
                self._emit_log(
                    "INFO",
                    f"[AI-LEARNING] {getattr(decision, 'ticker', '')} "
                    f"{getattr(decision, 'tipo', '')} no ejecutada: {reason}; "
                    f"trainable={int(bool(training_candidate))}, no suma x/50",
                )
            return row
        except Exception as exc:
            self._emit_log(
                "WARN", f"[AI-LEARNING] No se pudo registrar señal perdida: {exc}"
            )
            return None

    def _registrar_no_op(self, decision):
        """Contabiliza una decisión de no operar según su causa estructurada."""
        with self._lock:
            if decision.direccion_dia is not None and decision.direccion_dia.direccion == Direccion.NEUTRAL:
                self._resumen["omt"] += 1        # macro sin dirección
            elif decision.veredicto is not None and not decision.veredicto.aprobada:
                self._resumen["omr"] += 1        # vetado por el Auditor (riesgo/rango)

    # ----------------------------------------------------------

    def _solo_velas_cerradas(
        self,
        df,
        timeframe_minutes: int = 1,
        ahora_utc=None,
    ):
        """Return only bars whose full duration has elapsed.

        Schwab minute candles are timestamped at their opening instant. A bar
        marked ``10:29:00`` is therefore confirmed only at/after ``10:30:00``.
        The comparison is performed in UTC and supports both naive and
        timezone-aware indices.

        MOTIVO (pedido del cliente y correcto): calcular la señal sobre la vela en
        curso genera señales FANTASMA — un pico intra-minuto cruza el MACD y al
        cerrar la vela el cruce se deshace, pero el bot ya entró. ThinkOrSwim evalúa
        el CIERRE de la vela anterior. Además el pre-entrenamiento se hizo con velas
        cerradas, así que operar en formación rompía la coherencia con la IA."""
        try:
            if df is None or len(df) == 0:
                return df
            now = (
                pd.Timestamp.now(tz="UTC")
                if ahora_utc is None
                else pd.Timestamp(ahora_utc)
            )
            if now.tzinfo is None:
                now = now.tz_localize("UTC")
            else:
                now = now.tz_convert("UTC")

            idx_utc = pd.to_datetime(df.index, utc=True, errors="coerce")
            duration = pd.Timedelta(minutes=max(1, int(timeframe_minutes)))
            mask = idx_utc.notna() & ((idx_utc + duration) <= now)

            # Some providers expose an explicit forming-bar flag. It is an
            # additional fail-closed guard, never a replacement for timing.
            if "actual_in_formation" in df.columns:
                forming = df["actual_in_formation"].fillna(True).astype(bool)
                mask = mask & (~forming.to_numpy())

            return df.iloc[mask]
        except Exception:
            # Conservador ante cualquier duda: usar la vela ANTERIOR (como TOS).
            try:
                return df.iloc[:-1] if len(df) >= 2 else df.iloc[0:0]
            except Exception:
                return df

    def _datos_micro_confirmados(
        self,
        datos: DatosMultiTemporal,
        ahora_utc=None,
    ) -> DatosMultiTemporal:
        """Clone MTF data with native minute frames trimmed to closed bars.

        GALA10, Agent 2, Agent 3 and volume features must inspect the same
        confirmed micro context. Macro frames are kept unchanged and continue
        to use their explicit in-formation handling.
        """
        velas = dict(datos.velas)
        for tf, minutes in (("1m", 1), ("5m", 5), ("15m", 15), ("30m", 30)):
            frame = velas.get(tf)
            if frame is None:
                continue
            velas[tf] = self._solo_velas_cerradas(
                frame,
                timeframe_minutes=minutes,
                ahora_utc=ahora_utc,
            )
        return DatosMultiTemporal(
            ticker=datos.ticker,
            velas=velas,
            sintetico=datos.sintetico,
        )

    def _senal_estrategia(self, datos: DatosMultiTemporal, ticker: str,
                          precio_actual: float) -> Optional[str]:
        """Calcula la señal de core/strategy.py sobre 1m para la confluencia."""
        df_1m = datos.tf("1m")
        if df_1m is None:
            df_1m = datos.tf("5m")
        if df_1m is None or len(df_1m) < 35:
            return None
        # SOLO VELAS CERRADAS: la señal se evalúa sobre la última vela COMPLETA,
        # igual que TOS. Descartar la vela en formación evita señales fantasma y
        # alinea el vivo con el pre-entrenamiento (que usó velas cerradas).
        df_1m = self._solo_velas_cerradas(df_1m)
        if df_1m is None or len(df_1m) < 35:
            return None
        vela_id = df_1m.index[-1]
        with self._lock:
            if self._ultima_vela_procesada.get(ticker) == vela_id:
                return None
        try:
            df_calc = self.estrategia.calcular(df_1m)
            señal = df_calc["señal"].iloc[-1]
            if señal in ("BUY", "SELL"):
                contexto = extraer_features_gala10(
                    df_calc, posicion=-1, senal=señal, timestamp=vela_id)
                with self._lock:
                    self._gala10_contexto[ticker] = contexto
                    self._gala10_signal_bar[ticker] = vela_id
            # Marcar incluso una vela sin señal: una vela cerrada se evalúa una sola
            # vez, exactamente como el evento de cierre de barra en ThinkOrSwim.
            with self._lock:
                self._ultima_vela_procesada[ticker] = vela_id
            # NO se emite la señal cruda aquí. Se emite DESPUÉS de la auditoría de la
            # IA (con su probabilidad y si fue APROBADA o VETADA), para que el panel
            # sea coherente y no muestre la señal separada de la decisión.
            return señal if señal in ("BUY", "SELL") else None
        except Exception as e:
            self._emit_log("WARN", f"[{ticker}] Estrategia no disponible: {e}")
            return None

    def _obtener_mtf(self, ticker: str) -> DatosMultiTemporal:
        """Datos multi-temporal con DOBLE refresco: el 1m/5m se re-baja en cada ciclo
        (barato) para que el precio y la señal estén al día como el bróker; las
        temporalidades mayores se cachean más tiempo (cambian lento). Así el precio no
        se atrasa frente a la pantalla real y no se satura la API con muchos tickers."""
        ahora = time.time()
        if not self.schwab._asegurar_token():
            # Demo: genera una nueva ventana con timestamps actuales en cada cierre
            # de minuto, sin disparar solicitudes 401. Real: el marcador sintetico
            # bloquea cualquier entrada y el monitor de órdenes queda fail-closed.
            datos = self.schwab._multitemporal_fallback(ticker)
            self._mtf_cache[ticker] = (ahora, datos, ahora)
            return datos
        ent = self._mtf_cache.get(ticker)     # (t_macro, datos, t_micro)
        if ent and (ahora - ent[0]) < self._mtf_ttl_macro:
            datos = ent[1]
            if (ahora - ent[2]) >= self._mtf_ttl:
                m1 = None
                try:
                    m1 = self.schwab.obtener_1m_reciente(ticker)
                except Exception as e:
                    self._emit_log("WARN", f"[{ticker}] Error refrescando el precio 1m: {e}")
                if m1 is not None and len(m1) > 40:
                    from api.schwab_client import SchwabClient
                    datos.velas["1m"] = m1
                    m5 = SchwabClient._resample(m1, "5min")
                    if m5 is not None and len(m5) > 0:
                        datos.velas["5m"] = m5
                    # Solo marcar el micro como FRESCO si el 1m se actualizó de verdad.
                    self._mtf_cache[ticker] = (ent[0], datos, ahora)
                else:
                    # Fetch falló o vino corto: NO se toca t_micro, así el próximo ciclo
                    # REINTENTA (no se congela el precio en silencio) y se avisa. La
                    # antigüedad máxima queda acotada por el refresco macro (mtf_refresh_macro_sec).
                    self._emit_log("WARN", f"[{ticker}] Precio 1m no se actualizó "
                                           f"(API sin datos) — se usa el último y se reintenta")
            return datos
        datos = self.schwab.obtener_multitemporal(ticker, self.config)
        self._mtf_cache[ticker] = (ahora, datos, ahora)
        return datos

    # ==========================================================
    # APERTURA / REVERSIÓN
    # ==========================================================

    def _quarantine_real_ticker(self, ticker, reason):
        """Temporarily block new REAL entries without changing saved routing.

        v3.3.6 fixes a destructive fallback: a transient market-data/preflight
        problem could persist REAL -> PAPER into ticker_execution.json.
        Quarantine is now runtime-only; the operator's saved REAL selection
        remains the execution authority.
        """
        if not (getattr(self, 'config', {}).get('live_trading', {}).get('runtime_isolate_ticker_failures', False)):
            return
        ticker = str(ticker or '').strip().upper()
        if not ticker or self.execution_mode_for(ticker) != 'REAL':
            return
        reason = str(reason or 'Temporary REAL safety block')
        previous = self._ticker_quarantine.get(ticker)
        self._ticker_quarantine[ticker] = reason
        self._ticker_quarantine_health[ticker] = 0
        if previous != reason:
            self._audit_event(
                'LIVEGUARD_QUARANTINE', ticker=ticker,
                estado='REAL_BLOCKED_TEMPORARY', motivo=reason,
            )
            self._emit_log(
                'WARN',
                f'[{ticker}] REAL temporalmente bloqueado: {reason}. '
                'La selección REAL guardada se conserva.',
            )

    @staticmethod
    def _quarantine_reason_category(reason):
        text = str(reason or '').lower()
        if any(token in text for token in (
            'market data', 'synthetic market data', 'insufficient market data',
            'sin velas reales', 'velas reales de 1m', 'api sin datos',
        )):
            return 'market_data'
        if any(token in text for token in (
            'production model unavailable', 'modelo production',
            'falta modelo production',
        )):
            return 'model'
        if any(token in text for token in (
            'ai inference failed', 'production inference unavailable',
            'inferencia',
        )):
            return 'inference'
        return 'other'

    def _recover_ticker_quarantine(self, ticker, category):
        """Clear a temporary REAL block only after the failed subsystem recovers."""
        ticker = str(ticker or '').strip().upper()
        reason = self._ticker_quarantine.get(ticker)
        if not reason:
            return False
        if self._quarantine_reason_category(reason) != str(category or ''):
            return False

        required = 2 if category == 'market_data' else 1
        healthy = int(self._ticker_quarantine_health.get(ticker, 0) or 0) + 1
        self._ticker_quarantine_health[ticker] = healthy
        if healthy < required:
            return False

        self._ticker_quarantine.pop(ticker, None)
        self._ticker_quarantine_health.pop(ticker, None)
        self._audit_event(
            'LIVEGUARD_QUARANTINE_CLEARED', ticker=ticker,
            estado='REAL_READY', motivo=reason,
            detalle={'healthy_cycles': healthy, 'category': category},
        )
        self._emit_log(
            'INFO',
            f'[{ticker}] REAL rehabilitado tras recuperación de {category}; '
            'la selección REAL guardada nunca cambió.',
        )
        return True

    def _apply_ticker_quarantine(self, ticker):
        """Compatibility no-op: quarantine no longer persists REAL -> PAPER."""
        return ticker in getattr(self, '_ticker_quarantine', {})

    def _reserve_real_capacity(self, ticker, order):
        live = getattr(self, 'config', {}).get('live_trading', {}) or {}
        if not (live.get('capacity', {}) or {}).get('enabled', False):
            return True
        # Lock covers broker balance read + reservations, including concurrent workers.
        with self._lock:
            balance = self.schwab.obtener_balance(self.account_number)
            daily = self.daily_risk_snapshot()
            if daily.get('blocked') or not daily.get('enabled') or daily.get('unpriced_positions', 0):
                result = {'allowed': False, 'reason': 'Daily risk unavailable, blocked, or exposure unpriced'}
            else:
                result = self.real_capacity.reserve(ticker, order.cantidad, order.precio_limite,
                    order.stop_loss, balance, daily.get('remaining_to_limit_usd', 0), self._posiciones)
            self._audit_event('REAL_CAPACITY_ALLOWED' if result['allowed'] else 'ENTRY_BLOCKED',
                ticker=ticker, estado='CAPACITY', motivo=result['reason'], detalle=result)
            return result['allowed']

    def _reservar_slot(self, ticker: str, tipo: str) -> bool:
        """Atomically reserve an execution-mode-specific position slot.

        v2.9.5 keeps PAPER and REAL position books independent.  A full
        REAL book can no longer consume PAPER capacity, and vice versa.
        Placeholders carry ``execution_mode`` so concurrent workers are counted
        correctly before an order is actually submitted.
        """
        with self._lock:
            if not self._activo:
                self._set_ticker_processing(
                    ticker, "BLOCKED", "ENGINE", "ENGINE_STOPPING",
                    gala_signal=tipo,
                )
                return False
            if ticker in getattr(self, '_ticker_quarantine', {}) and self.execution_mode_for(ticker) == 'REAL':
                return False
            if self._daily_blocked():
                self._emit_log(
                    "WARN",
                    f"[{ticker}] Entrada bloqueada por límite diario hasta la próxima sesión",
                )
                return False
            if ticker in self._posiciones:
                return False

            target_mode = self.execution_mode_for(ticker)
            paper_count = 0
            real_count = 0
            for existing_ticker, pos in self._posiciones.items():
                mode = str((pos or {}).get("execution_mode", "") or "").upper()
                if mode not in ("PAPER", "REAL"):
                    mode = (
                        "PAPER"
                        if self._is_paper_ticker(existing_ticker, pos=pos)
                        else "REAL"
                    )
                if mode == "PAPER":
                    paper_count += 1
                else:
                    real_count += 1

            total_count = paper_count + real_count
            # Safe defaults keep older tests/integrations that construct the bot
            # with __new__ compatible; normal runtime instances always set these
            # values from config in __init__.
            max_paper = int(getattr(
                self, "max_paper_positions",
                getattr(getattr(self, "gestor_riesgo", None), "max_posiciones", 15),
            ))
            max_real = int(getattr(
                self, "max_real_positions",
                max(1, int(getattr(self, "live_max_tickers", 2) or 2)),
            ))
            max_total = int(getattr(
                self, "max_total_positions", max_paper + max_real
            ))
            if total_count >= max_total:
                self._emit_log(
                    "INFO",
                    f"[{ticker}] Límite total alcanzado "
                    f"({total_count}/{max_total})",
                )
                return False
            if target_mode == "PAPER" and paper_count >= max_paper:
                self._emit_log(
                    "INFO",
                    f"[{ticker}] Límite PAPER alcanzado "
                    f"({paper_count}/{max_paper}); "
                    f"REAL={real_count}/{max_real}",
                )
                return False
            if target_mode == "REAL" and real_count >= max_real:
                self._emit_log(
                    "INFO",
                    f"[{ticker}] Límite REAL alcanzado "
                    f"({real_count}/{max_real}); "
                    f"PAPER={paper_count}/{max_paper}",
                )
                return False

            self._posiciones[ticker] = {
                "reservado": True,
                "tipo": tipo,
                "execution_mode": target_mode,
            }
            return True

    def _liberar_reserva(self, ticker: str):
        """Libera el placeholder si la apertura no llegó a concretarse."""
        with self._lock:
            p = self._posiciones.get(ticker)
            if p is not None and p.get("reservado"):
                self._posiciones.pop(ticker, None)
                manager = getattr(self, 'real_capacity', None)
                if manager:
                    manager.release(ticker)

    def _abrir_o_revertir(self, decision, precio_actual: float, df_micro):
        ticker = decision.ticker
        tipo = decision.tipo

        if self._daily_blocked():
            self._set_ticker_processing(
                ticker, "BLOCKED", "RISK", "DAILY_LOSS_BLOCK",
                gala_signal=tipo, ai_decision="APPROVED",
            )
            self._emit_log(
                "WARN",
                f"[{ticker}] Señal {tipo} ignorada: límite diario activo",
            )
            self._registrar_senal_no_ejecutada(
                decision, reason="DAILY_LOSS_BLOCK",
                source_kind="RISK_POLICY_BLOCK", training_candidate=False,
            )
            return

        # Decisión bajo lock: duplicado / reversión / apertura nueva. La máquina
        # explícita conserva solo OUT/LONG/SHORT; una orden pendiente sigue OUT.
        with self._lock:
            if not self._activo:
                return
            pos = self._posiciones.get(ticker)
            reverse = False
            cancelar_pend = False
            if pos is not None:
                if pos.get("reservado"):
                    # Otra hebra ya está abriendo este ticker → no duplicar
                    self._set_ticker_processing(
                        ticker, "BLOCKED", "POSITION", "ENTRY_ALREADY_RESERVED",
                        gala_signal=tipo,
                    )
                    return
                if pos["tipo"] == tipo:
                    self._emit_log("INFO", f"[{ticker}] Señal {tipo} repetida — se mantiene (sin adicionar)")
                    self._set_ticker_processing(
                        ticker, "BLOCKED", "POSITION", "SAME_SIDE_POSITION_EXISTS",
                        gala_signal=tipo,
                    )
                    return
                if not self.reversion_activa:
                    self._emit_log("INFO", f"[{ticker}] Reversión desactivada — se ignora giro de señal")
                    self._registrar_senal_no_ejecutada(
                        decision, reason="REVERSAL_DISABLED",
                        source_kind="POLICY_BLOCK", training_candidate=False,
                    )
                    self._set_ticker_processing(
                        ticker, "BLOCKED", "REVERSAL", "REVERSAL_DISABLED",
                        gala_signal=tipo,
                    )
                    return
                if pos.get("pendiente"):
                    cancelar_pend = True   # giro con entrada sin llenar → cancelar la pendiente
                else:
                    reverse = True

        state_before = self._state_snapshot(ticker)
        if pos is None and state_before.state != PositionState.OUT:
            self._emit_log(
                "ERROR",
                f"[{ticker}] Invariante v2.9: estado {state_before.state.value} "
                "sin posición local; entrada bloqueada",
            )
            self._audit_event(
                "POSITION_STATE_MISMATCH", ticker=ticker, lado=tipo,
                estado="BLOCKED", state_before=state_before.state.value,
                state_after=state_before.state.value, signal_side=tipo,
                motivo="Estado activo sin posición local",
            )
            return

        if cancelar_pend:
            if not self._cancelar_pendiente(ticker):
                # La entrada pendiente ya estaba llenada: quedó adoptada como
                # posición real. NO abrir la opuesta encima; el próximo ciclo la
                # verá como posición contraria y la cerrará+volteará por la vía
                # normal (con verificación de cierre).
                self._emit_log("INFO", f"[{ticker}] Giro pospuesto: la entrada pendiente ya estaba "
                                       f"llenada; se cerrará y volteará el próximo ciclo")
                return
        elif reverse:
            prior_state = state_before.state
            target_state = state_for_signal(tipo)
            if prior_state == PositionState.OUT:
                self._emit_log(
                    "ERROR",
                    f"[{ticker}] Reversión bloqueada: posición local sin estado "
                    "LONG/SHORT confirmado",
                )
                return
            reversal_id = uuid.uuid4().hex
            signal_id = str(getattr(decision, "signal_id", "") or uuid.uuid4().hex)
            reversal_context = {
                "reversal_id": reversal_id,
                "correlation_id": reversal_id,
                "signal_id": signal_id,
                "signal_side": tipo,
                "state_before": prior_state.value,
                "state_after": PositionState.OUT.value,
                "position_side": prior_state.value,
                "order_action": exit_action_for_state(prior_state),
            }
            if not hasattr(self, "_reversal_contexts"):
                self._reversal_contexts = {}
            self._reversal_contexts[ticker] = reversal_context
            decision.signal_id = signal_id
            decision.reversal_id = reversal_id
            decision.correlation_id = reversal_id
            # Cerrar la posición contraria (cancela protecciones y liquida en real)
            self._emit_log("INFO", f"[{ticker}] REVERSIÓN → {tipo}: cerrando y volteando")
            self._audit_event(
                "REVERSAL_REQUESTED", ticker=ticker, lado=tipo,
                estado="INICIADA", precio=precio_actual,
                motivo="Señal maestra opuesta a la posición actual",
                **reversal_context,
            )
            if not self._cerrar_posicion(ticker, precio_actual, motivo="reversión"):
                self._emit_log("WARN", f"[{ticker}] Reversión abortada: no se pudo cerrar la posición previa")
                self._registrar_senal_no_ejecutada(
                    decision, reason="REVERSAL_PRIOR_CLOSE_FAILED",
                    source_kind="EXECUTION_MISSED", training_candidate=True,
                )
                self._audit_event(
                    "REVERSAL_ABORTED", ticker=ticker, lado=tipo,
                    estado="CIERRE_PREVIO_FALLIDO", precio=precio_actual,
                    motivo="No se confirmó el cierre de la posición anterior",
                    **reversal_context,
                )
                self._reversal_contexts.pop(ticker, None)
                return
            with self._lock:
                self._resumen["reversiones"] += 1
            self._audit_event(
                "REVERSAL_PRIOR_POSITION_CLOSED", ticker=ticker, lado=tipo,
                estado="LISTA_PARA_APERTURA", precio=precio_actual,
                correlation_id=reversal_id, reversal_id=reversal_id,
                signal_id=signal_id, signal_side=tipo,
                state_before=prior_state.value,
                state_after=PositionState.OUT.value,
                position_side=prior_state.value,
                order_action=exit_action_for_state(prior_state),
                detalle={"target_state": target_state.value},
            )
            # REAL long-only: el giro SELL SIEMPRE puede cerrar LONG, pero
            # no se convierte en una apertura SELL_SHORT. Se termina en OUT.
            if (
                not self._is_paper_ticker(ticker)
                and tipo == "SELL"
                and not bool(getattr(self, "live_allow_short", True))
            ):
                self._emit_log(
                    "INFO",
                    f"[{ticker}] LONG cerrado por SELL; REAL long-only mantiene OUT "
                    "(SHORT no abierto).",
                )
                self._audit_event(
                    "REVERSAL_CLOSE_ONLY", ticker=ticker, lado=tipo,
                    estado="OUT", precio=precio_actual,
                    correlation_id=reversal_id, reversal_id=reversal_id,
                    signal_id=signal_id, signal_side=tipo,
                    state_before=prior_state.value,
                    state_after=PositionState.OUT.value,
                    position_side=prior_state.value,
                    order_action=exit_action_for_state(prior_state),
                    motivo="live_trading.allow_short=false",
                )
                self._registrar_senal_no_ejecutada(
                    decision, reason="REAL_LONG_ONLY_CLOSE_ONLY",
                    source_kind="POLICY_BLOCK", training_candidate=False,
                )
                self._reversal_contexts.pop(ticker, None)
                return
        else:
            reversal_id = ""

        lifecycle = self._assign_entry_lifecycle(decision, reversal_id)
        target_state = state_for_signal(tipo)
        entry_audit_context = {
            **lifecycle,
            "state_before": PositionState.OUT.value,
            "state_after": target_state.value,
            "position_side": target_state.value,
            "signal_side": tipo,
            "order_action": entry_action_for_state(target_state),
        }

        # Reservar hueco de forma atómica antes de ejecutar la apertura
        if not self._reservar_slot(ticker, tipo):
            # Same-side repeats are intentionally not new learning samples. If
            # there was no active position before the attempt, this was a real
            # missed opportunity (capacity/quarantine/risk gate).
            if pos is None:
                self._registrar_senal_no_ejecutada(
                    decision, reason="ENTRY_SLOT_NOT_AVAILABLE",
                    source_kind="CAPACITY_OR_GUARD_MISSED", training_candidate=True,
                )
            if reverse:
                self._reversal_contexts.pop(ticker, None)
            self._set_ticker_processing(
                ticker, "BLOCKED", "EXECUTION", "ENTRY_SLOT_NOT_AVAILABLE",
                gala_signal=tipo, ai_decision="APPROVED",
            )
            return
        # try/finally: si _abrir_operacion LANZA, la reserva DEBE liberarse igual
        # (si no, el hueco 'reservado' bloquea el ticker para siempre).
        try:
            with self._lifecycle_scope(entry_audit_context):
                abierta = self._abrir_operacion(decision, precio_actual, df_micro)
        except Exception:
            self._liberar_reserva(ticker)
            if reverse:
                self._reversal_contexts.pop(ticker, None)
            raise
        if not abierta:
            self._liberar_reserva(ticker)
            miss_reason = str(
                getattr(decision, "execution_skip_reason", "")
                or "ENTRY_NOT_EXECUTED"
            )
            policy_only = miss_reason in {
                "DAILY_LOSS_BLOCK", "ATR_INVALID", "RISK_ORDER_REJECTED"
            }
            self._registrar_senal_no_ejecutada(
                decision,
                reason=miss_reason,
                source_kind=(
                    "RISK_POLICY_BLOCK" if policy_only else "EXECUTION_MISSED"
                ),
                training_candidate=not policy_only,
            )
            self._set_ticker_processing(
                ticker, "BLOCKED", "EXECUTION", miss_reason,
                gala_signal=tipo, ai_decision="APPROVED",
                execution="NOT_EXECUTED",
            )
            if reverse:
                self._audit_event(
                    "REVERSAL_OPPOSITE_ENTRY_FAILED", ticker=ticker, lado=tipo,
                    estado="OUT", precio=precio_actual,
                    motivo="La posición previa cerró, pero la entrada opuesta no fue aceptada",
                    **entry_audit_context,
                )
        if reverse:
            self._reversal_contexts.pop(ticker, None)

    def _abrir_operacion(self, decision, precio_actual: float, df_micro) -> bool:
        """Ejecuta la apertura. Devuelve True si la posición quedó abierta."""
        prop = decision.propuesta
        ticker = prop.ticker
        paper_ticker = self._is_paper_ticker(ticker)
        execution_mode = "PAPER" if paper_ticker else "REAL"
        if self._daily_blocked():
            decision.execution_skip_reason = "DAILY_LOSS_BLOCK"
            self._emit_log("WARN", f"[{ticker}] Apertura abortada: límite diario activo")
            self._set_ticker_processing(
                ticker, "BLOCKED", "RISK", "DAILY_LOSS_BLOCK",
                execution="NOT_EXECUTED",
            )
            return False
        # ATR de la temporalidad micro (sin mutar el DataFrame de la caché)
        from agents import smc_primitives as smc
        serie_atr = smc.atr_wilder(df_micro, self.config.get("atr", {}).get("period", 14))
        atr_micro = float(serie_atr.iloc[-1]) if len(serie_atr) else 0.0
        if atr_micro <= 0:
            decision.execution_skip_reason = "ATR_INVALID"
            self._set_ticker_processing(
                ticker, "BLOCKED", "RISK", "ATR_INVALID",
                execution="NOT_EXECUTED",
            )
            return False

        orden = self.gestor_riesgo.calcular_orden(
            ticker=ticker, tipo=prop.tipo, precio_actual=precio_actual, atr=atr_micro,
            capital_disponible=self.capital,
            precio_entrada_override=prop.precio_entrada,
            stop_loss_override=prop.stop_loss,
            take_profit_override=prop.take_profit,
            cantidad_override=self.shares_for_ticker(ticker),
        )
        if orden is None:
            decision.execution_skip_reason = "RISK_ORDER_REJECTED"
            self._audit_event(
                "ENTRY_BLOCKED", ticker=ticker, lado=prop.tipo,
                estado="RISK_REJECTED", precio=precio_actual,
                motivo="El gestor de riesgo no generó una orden válida",
            )
            self._set_ticker_processing(
                ticker, "BLOCKED", "RISK", "RISK_ORDER_REJECTED",
                execution="NOT_EXECUTED",
            )
            return False

        self._audit_event(
            "ENTRY_REQUESTED", ticker=ticker, lado=prop.tipo,
            estado="PREPARADA", cantidad=orden.cantidad,
            precio=orden.precio_limite,
            detalle={
                "stop_loss": orden.stop_loss,
                "tp1": orden.take_profit_parcial,
                "tp_final": orden.take_profit,
            },
        )

        contexto = ContextoMercado(
            atr_relativo=decision.veredicto.atr_relativo if decision.veredicto else 0.3,
            es_rango_lateral=False,
            prob_ia=decision.prob_meta,
            es_senal_maestra=(decision.confluencia_estrategia and
                              decision.direccion_dia is not None and
                              decision.direccion_dia.confianza >= 0.7),
            # Sin FVG del Agente 2 → es la entrada A MERCADO reactiva a Gala 10:
            # debe llenarse ya (marketable limit), no descansar como LIMIT y expirar.
            es_entrada_mercado=(prop.fvg is None),
        )

        # -----------------------------------------------------------------
        # PAPER: llenado inmediato simulado (comportamiento demostrado)
        # -----------------------------------------------------------------
        if paper_ticker:
            resultado = self.ejecutor_paper.ejecutar_con_persecucion(
                orden=orden, contexto=contexto, account_number=self.account_number,
                cotizar_fn=None,
            )
            if resultado.get("status") not in ("OK", "SIMULADA"):
                decision.execution_skip_reason = f"PAPER_ENTRY_{resultado.get('status', 'REJECTED')}"
                self._emit_log("WARN", f"[{ticker}] Entrada no simulada: {resultado.get('status')}")
                self._audit_event(
                    "ENTRY_REJECTED", ticker=ticker, lado=prop.tipo,
                    estado=resultado.get("status", "ERROR"),
                    cantidad=orden.cantidad, precio=orden.precio_limite,
                    motivo=resultado.get("mensaje", ""),
                )
                self._set_ticker_processing(
                    ticker, "BLOCKED", "PAPER_EXECUTION",
                    decision.execution_skip_reason, execution="REJECTED",
                )
                return False
            # Usar la orden AJUSTADA por volatilidad (cantidad real), no la base
            self._registrar_posicion_abierta(
                ticker,
                resultado.get("orden_ajustada", orden),
                decision,
                entry_order_id=resultado.get("order_id", ""),
                execution_context=resultado,
            )
            self._set_ticker_processing(
                ticker, "EXECUTED", "PAPER_EXECUTION", "PAPER_FILLED",
                execution="FILLED", quantity=int(orden.cantidad or 0),
            )
            return True

        # -----------------------------------------------------------------
        # REAL: colocar la ENTRADA y esperar su LLENADO. La protección (TP1/SL)
        # se coloca SOLO cuando el llenado se confirma (ver _gestionar_entrada_
        # pendiente). Así nunca hay protecciones para una posición inexistente.
        # -----------------------------------------------------------------
        # Sin hash de cuenta resuelto NO se puede operar en real (los endpoints de
        # órdenes usan el hash cifrado, no el número plano). Reintentar resolver; si
        # no se logra, NO abrir (mejor no operar que mandar una orden que rebota).
        if not self._trading_listo:
            self._preparar_trading_real()
            if not self._trading_listo:
                decision.execution_skip_reason = "BROKER_ACCOUNT_NOT_READY"
                self._emit_log("ERROR", f"[{ticker}] Cuenta no lista (hash sin resolver) — "
                                        f"entrada NO colocada")
                self._audit_event(
                    "ENTRY_BLOCKED", ticker=ticker, lado=prop.tipo,
                    estado="BROKER_NOT_READY", cantidad=orden.cantidad,
                    precio=orden.precio_limite,
                    motivo="Cuenta/hash de Schwab no disponible",
                )
                self._set_ticker_processing(
                    ticker, "BLOCKED", "BROKER", "BROKER_ACCOUNT_NOT_READY",
                    execution="NOT_EXECUTED",
                )
                return False
        if not self._asegurar_token_trading():
            decision.execution_skip_reason = "BROKER_OAUTH_UNAVAILABLE"
            self._emit_log("ERROR", f"[{ticker}] OAuth de Schwab no disponible — entrada bloqueada")
            self._audit_event(
                "ENTRY_BLOCKED", ticker=ticker, lado=prop.tipo,
                estado="OAUTH_UNAVAILABLE", cantidad=orden.cantidad,
                precio=orden.precio_limite,
            )
            self._set_ticker_processing(
                ticker, "BLOCKED", "BROKER", "BROKER_OAUTH_UNAVAILABLE",
                execution="NOT_EXECUTED",
            )
            return False
        if not self._confirm_real_ticker_flat_before_entry(ticker):
            decision.execution_skip_reason = "BROKER_RECONCILIATION_REQUIRED"
            self._audit_event(
                "ENTRY_BLOCKED", ticker=ticker, lado=prop.tipo,
                estado="BROKER_RECONCILIATION_REQUIRED",
                cantidad=orden.cantidad, precio=orden.precio_limite,
                motivo=(getattr(self, "_broker_reconciliation", {}) or {}).get(
                    "message", "Broker no reconciliado"
                ),
            )
            self._set_ticker_processing(
                ticker, "BLOCKED", "BROKER", "BROKER_RECONCILIATION_REQUIRED",
                execution="NOT_EXECUTED",
            )
            return False
        # Antes de ABRIR (sobre todo tras una reversión): garantizar que no queda
        # ningún stop/orden de cierre viejo de este símbolo vivo en el broker.
        # Schwab RECHAZA una apertura si una orden de cierre previa sigue activa
        # (la interpreta como 'sobrevendido/sobrecomprado'). Se cancelan primero.
        if not self._asegurar_ticker_limpio(ticker):
            decision.execution_skip_reason = "BROKER_SYMBOL_NOT_CLEAN"
            self._audit_event(
                "ENTRY_BLOCKED", ticker=ticker, lado=prop.tipo,
                estado="SYMBOL_NOT_CLEAN", cantidad=orden.cantidad,
                precio=orden.precio_limite,
                motivo="Persisten órdenes activas del símbolo",
            )
            self._set_ticker_processing(
                ticker, "BLOCKED", "BROKER", "BROKER_SYMBOL_NOT_CLEAN",
                execution="NOT_EXECUTED",
            )
            return False
        # Última barrera justo antes del envío. Otro worker puede haber activado
        # el kill switch mientras se calculaban niveles o se limpiaban órdenes.
        if self._daily_blocked():
            decision.execution_skip_reason = "DAILY_LOSS_BLOCK"
            self._emit_log("WARN", f"[{ticker}] Entrada no enviada: límite diario activo")
            return False
        if not self._reserve_real_capacity(ticker, orden):
            decision.execution_skip_reason = "REAL_CAPACITY_BLOCK"
            self._set_ticker_processing(
                ticker, "BLOCKED", "CAPACITY", "REAL_CAPACITY_BLOCK",
                execution="NOT_EXECUTED",
            )
            return False
        res = self.ejecutor_hibrido.enviar_entrada(orden, contexto, self.account_number)
        if res.get("status") not in ("OK", "RECONCILIAR"):
            decision.execution_skip_reason = f"BROKER_ENTRY_{res.get('status', 'REJECTED')}"
            self._emit_log("WARN", f"[{ticker}] Entrada no colocada: {res.get('status')} "
                                   f"— {res.get('mensaje', '')}")
            self._audit_event(
                "ENTRY_REJECTED", ticker=ticker, lado=prop.tipo,
                estado=res.get("status", "ERROR"),
                order_id=res.get("order_id", ""),
                cantidad=orden.cantidad, precio=orden.precio_limite,
                latency_ms=res.get("latency_ms"),
                motivo=res.get("mensaje", ""),
            )
            self._set_ticker_processing(
                ticker, "BLOCKED", "BROKER_EXECUTION",
                decision.execution_skip_reason, execution="REJECTED",
            )
            return False
        if res.get("status") == "RECONCILIAR":
            # La orden pudo enviarse pero no tenemos id: se reconciliará por posiciones
            self._emit_log("WARN", f"[{ticker}] Entrada enviada SIN id de orden — se verificará por posiciones")
        orden_aj = res.get("orden_ajustada", orden)
        with self._lock:
            # Reemplaza la reserva por una ENTRADA PENDIENTE (aún no es posición)
            self._posiciones[ticker] = {
                "pendiente": True,
                "execution_mode": execution_mode,
                "tipo": orden_aj.tipo,
                "order_id": res.get("order_id", ""),
                "orden": orden_aj,
                "decision": decision,
                "ts_pendiente": datetime.now(),
                "execution_context": {
                    "signal_price": res.get(
                        "signal_price", prop.precio_entrada
                    ),
                    "submitted_limit_price": res.get(
                        "submitted_limit_price", orden_aj.precio_limite
                    ),
                    "submitted_order_type": res.get(
                        "submitted_order_type", res.get("tipo_orden", "LIMIT")
                    ),
                },
            }
            pendiente_ref = self._posiciones[ticker]
        filled_now = int(res.get("filled", 0) or 0)
        if filled_now > 0:
            # Llenado observado durante la confirmación inicial: protegerlo en este
            # mismo flujo, sin esperar al próximo ciclo de velas.
            return self._abrir_desde_llenado(
                ticker,
                pendiente_ref,
                filled_now,
                res.get("avg_price") or orden_aj.precio_limite,
                parcial=(filled_now < orden_aj.cantidad),
                oid=res.get("order_id", ""),
            )
        self._emit_log("INFO", f"[{ticker}] Entrada REAL colocada, esperando llenado | {orden_aj}")
        self._set_ticker_processing(
            ticker, "ORDER_SUBMITTED", "BROKER_EXECUTION",
            "REAL_ORDER_SUBMITTED", execution="PENDING_FILL",
            quantity=int(orden_aj.cantidad or 0),
            order_id=str(res.get("order_id", "") or ""),
        )
        return True

    @staticmethod
    def _submitted_exit_order_id(result: Optional[dict]) -> str:
        """Return an id for any broker-accepted exit/protection submission.

        ``RECONCILIAR`` means Schwab accepted the HTTP request and returned an
        order id, but the immediate acknowledgement read did not become visible
        inside the short poll window.  It is *not* permission to submit a second
        order.  v3.0.2 preserves that id and reconciles it later.
        """
        result = result or {}
        status = str(result.get("status", "") or "").upper()
        order_id = str(result.get("order_id", "") or "").strip()
        if order_id and status in {"OK", "RECONCILIAR", "SIMULADA"}:
            return order_id
        return ""

    def _registrar_posicion_abierta(
        self, ticker: str, orden, decision, entry_order_id: str = "",
        execution_context: Optional[dict] = None,
    ):
        lifecycle = self._assign_entry_lifecycle(
            decision, str(getattr(decision, "reversal_id", "") or "")
        )
        target = state_for_signal(orden.tipo)
        context = {
            **lifecycle,
            "state_before": PositionState.OUT.value,
            "state_after": target.value,
            "position_side": target.value,
            "signal_side": orden.tipo,
            "order_action": entry_action_for_state(target),
        }
        with self._lifecycle_scope(context):
            return self._registrar_posicion_abierta_impl(
                ticker, orden, decision, entry_order_id, execution_context
            )

    def _registrar_posicion_abierta_impl(
        self, ticker: str, orden, decision, entry_order_id: str = "",
        execution_context: Optional[dict] = None,
    ):
        paper_ticker = self._is_paper_ticker(ticker)
        execution_mode = "PAPER" if paper_ticker else "REAL"
        """
        Crea la posición ABIERTA en el estado + coloca la protección (TP1/SL en
        modo real) + registra el aprendizaje y el trade. Se invoca en paper
        (llenado inmediato) y en real (SOLO tras confirmarse el llenado).
        """
        prot = {}
        sl_id = ""
        tp1_id = ""
        sl_reconcile_pending = False
        tp1_reconcile_pending = False
        if not paper_ticker:
            if orden.salida_parcial_activa:
                prot = self.ejecutor_hibrido.montar_proteccion_parcial(
                    orden, self.account_number
                )
            else:
                prot = {"sl": self.ejecutor_hibrido.colocar_salida(
                    ticker, orden.tipo, orden.cantidad, orden.stop_loss,
                    "STOP", self.account_number
                )}

            sl_res = prot.get("sl") or {}
            sl_status = str(sl_res.get("status", "") or "").upper()
            sl_id = self._submitted_exit_order_id(sl_res)
            sl_reconcile_pending = bool(
                sl_id and sl_status == "RECONCILIAR"
            )

            # v3.0.2 — CRITICAL: RECONCILIAR + order_id means the broker accepted
            # the STOP.  It is NOT a failed order and must never trigger an
            # immediate second STOP.  The Aug-24 META/NVDA incident created 3
            # protective STOP submissions for one 1-share position because this
            # distinction was lost here.
            if sl_reconcile_pending:
                self._emit_log(
                    "WARN",
                    f"[{ticker}] STOP aceptado por Schwab, pendiente de "
                    f"visibilidad ({sl_id}); se conserva el ID y NO se duplica.",
                )
                self._audit_event(
                    "STOP_ID_PRESERVED_RECONCILE",
                    ticker=ticker, lado=orden.tipo, estado="RECONCILIAR",
                    order_id=sl_id, cantidad=orden.cantidad,
                    precio=orden.stop_loss,
                    motivo="HTTP aceptado con order_id; no se reenvía STOP",
                )

            # Only a definite send failure (ERROR/no accepted order) permits one
            # retry.  An accepted-but-unresolved request, even without Location,
            # is first reconciled by symbol before any replacement can be sent.
            if not sl_id and sl_status not in {"RECONCILIAR"}:
                sl2 = self.ejecutor_hibrido.colocar_salida(
                    ticker, orden.tipo, orden.cantidad, orden.stop_loss,
                    "STOP", self.account_number
                )
                prot["sl"] = sl2
                sl_res = sl2 or {}
                sl_status = str(sl_res.get("status", "") or "").upper()
                sl_id = self._submitted_exit_order_id(sl_res)
                sl_reconcile_pending = bool(
                    sl_id and sl_status == "RECONCILIAR"
                )

            tp1_res = prot.get("tp1") or {}
            tp1_status = str(tp1_res.get("status", "") or "").upper()
            tp1_id = self._submitted_exit_order_id(tp1_res)
            tp1_reconcile_pending = bool(
                tp1_id and tp1_status == "RECONCILIAR"
            )

            # No identified STOP after the bounded send/reconciliation attempt:
            # clean every current broker order for the symbol before an emergency
            # market close.  This avoids racing an accepted request whose Location
            # header was missing.
            if not sl_id:
                if tp1_id:
                    self.ejecutor_hibrido.cancelar_orden_confirmada(
                        self.account_number, tp1_id, ticker=ticker
                    )
                symbol_clean = self._asegurar_ticker_limpio(ticker)
                cierre = {}
                if symbol_clean:
                    cierre = self.ejecutor_hibrido.liquidar_posicion(
                        ticker, orden.tipo, orden.cantidad, self.account_number,
                        "STOP no identificado"
                    )
                if cierre.get("status") in ("OK", "SIMULADA"):
                    self._marcar_liquidado(ticker)
                    self._emit_log(
                        "ERROR",
                        f"[{ticker}] STOP no identificado — posición CERRADA por "
                        "seguridad después de limpiar órdenes del símbolo",
                    )
                    with self._lock:
                        self._posiciones.pop(ticker, None)
                    self._audit_event(
                        "POSITION_EMERGENCY_CLOSED", ticker=ticker,
                        lado=orden.tipo, estado=cierre.get("status", ""),
                        order_id=cierre.get("order_id", ""),
                        parent_order_id=entry_order_id, cantidad=orden.cantidad,
                        precio=orden.precio_limite,
                        motivo="No se pudo identificar el stop de protección",
                    )
                    return False

                # Preserve the filled broker position in local tracking.  The next
                # real-position monitor will reconcile/repair protection; it must
                # never send another blind duplicate while a working-order
                # snapshot is unavailable.
                self._emit_log(
                    "ERROR",
                    f"[{ticker}] CRÍTICO: STOP sin ID verificable y cierre de "
                    "emergencia no confirmado — posición retenida para "
                    "reconciliación; nuevas órdenes del ticker quedan bloqueadas.",
                )

        entrada = orden.precio_limite
        lifecycle = self._assign_entry_lifecycle(
            decision, str(getattr(decision, "reversal_id", "") or "")
        )
        target_state = state_for_signal(orden.tipo)
        transition_ok = True
        try:
            transition = self._state_machine().open(
                ticker, orden.tipo, lifecycle["position_id"]
            )
        except InvalidPositionTransition as exc:
            transition_ok = False
            snapshot = self._state_machine().snapshot(ticker)
            transition = type("TransitionFallback", (), {
                "state_before": snapshot.state,
                "state_after": snapshot.state,
                "position_id": lifecycle["position_id"],
            })()
            self._emit_log(
                "ERROR",
                f"[{ticker}] Invariante v2.9 tras fill: {exc}. La posición se "
                "mantiene bajo seguimiento, pero no alimentará la IA.",
            )
            self._audit_event(
                "POSITION_STATE_MISMATCH", ticker=ticker, lado=orden.tipo,
                estado="TRACKED_FAIL_CLOSED", order_id=entry_order_id,
                state_before=snapshot.state.value,
                state_after=snapshot.state.value,
                position_side=target_state.value,
                signal_side=orden.tipo,
                order_action=entry_action_for_state(target_state),
                motivo=str(exc), **lifecycle,
            )
        entry_context = {
            **lifecycle,
            "modo": execution_mode,
            "entry_order_id": entry_order_id,
            "entry_confirmed": bool(transition_ok),
            "state_before": PositionState.OUT.value,
            "state_after": target_state.value,
            "position_side": target_state.value,
            "signal_side": orden.tipo,
            "entry_action": entry_action_for_state(target_state),
            "transition_ok": transition_ok,
        }
        sl_id_value = sl_id if prot else ""
        with self._lock:
            self._posiciones[ticker] = {
                "execution_mode": execution_mode,
                "tipo": orden.tipo,
                "position_state": target_state.value,
                "position_id": lifecycle["position_id"],
                "execution_id": lifecycle["execution_id"],
                "signal_id": lifecycle["signal_id"],
                "reversal_id": lifecycle["reversal_id"],
                "correlation_id": lifecycle["correlation_id"],
                "signal_side": orden.tipo,
                "entry_order_id": entry_order_id,
                "entry_integrity": entry_context,
                "cantidad": orden.cantidad,
                "cantidad_resto": orden.cantidad_resto if orden.salida_parcial_activa else orden.cantidad,
                "entrada": entrada,
                "sl": orden.stop_loss,
                "sl_actual": orden.stop_loss,
                "tp1": orden.take_profit_parcial,
                "tp_final": orden.take_profit,
                "tp1_hit": False,
                "modo_resto": orden.modo_resto,
                "salida_parcial": orden.salida_parcial_activa,
                "pnl_realizado": 0.0,
                "orden": orden,
                "decision": decision,
                "sl_order_id": sl_id_value,
                "sl_reconcile_pending": bool(sl_reconcile_pending),
                "tp1_order_id": tp1_id if prot else "",
                "tp1_reconcile_pending": bool(tp1_reconcile_pending),
                "abierta_ts": self._ahora_et(),
            }
            if orden.tipo == "BUY":
                self._resumen["buy"] += 1
            else:
                self._resumen["sell"] += 1

        self.motor_señales.registrar_entrada(
            ticker=ticker, precio=entrada,
            direccion="LONG" if orden.tipo == "BUY" else "SHORT",
            stop_loss=orden.stop_loss, trailing_stop=orden.trailing_stop_pct,
        )
        quality_context = dict(execution_context or {})
        quality_context.setdefault("fill_price", entrada)
        quality_context["lifecycle"] = lifecycle
        quality_row = self._record_entry_quality(
            ticker, orden, entry_order_id, quality_context
        )
        audit_row = self._audit_event(
            "ENTRY_FILLED", ticker=ticker, lado=orden.tipo,
            estado="SIMULATED_FILLED" if paper_ticker else "FILLED",
            order_id=entry_order_id, cantidad=orden.cantidad, precio=entrada,
            state_before=PositionState.OUT.value,
            state_after=target_state.value,
            position_side=target_state.value,
            signal_side=orden.tipo,
            order_action=entry_action_for_state(target_state),
            **lifecycle,
            detalle={
                "stop_loss": orden.stop_loss,
                "tp1": orden.take_profit_parcial,
                "tp_final": orden.take_profit,
                "sl_order_id": sl_id if prot else "",
                "sl_reconcile_pending": bool(sl_reconcile_pending),
                "tp1_order_id": tp1_id if prot else "",
                "tp1_reconcile_pending": bool(tp1_reconcile_pending),
                "signal_price": quality_context.get("signal_price", entrada),
                "submitted_limit_price": quality_context.get(
                    "submitted_limit_price", entrada
                ),
                "fill_price": entrada,
            },
        )
        history_row = self._history_event(
            "POSITION_OPEN",
            modo=execution_mode,
            ticker=ticker,
            state_before=PositionState.OUT.value,
            state_after=target_state.value,
            position_side=target_state.value,
            signal_side=orden.tipo,
            order_action=entry_action_for_state(target_state),
            order_id=entry_order_id,
            entrada=entrada,
            cantidad=orden.cantidad,
            motivo=(
                "REVERSAL_OPEN" if lifecycle.get("reversal_id")
                else "ENTRY_FILLED"
            ),
            **lifecycle,
        )
        entry_context.update({
            "slippage_linked": bool(quality_row),
            "audit_linked": bool(audit_row),
            "history_linked": bool(history_row),
        })
        with self._lock:
            active = self._posiciones.get(ticker)
            if active is not None:
                active["entry_integrity"] = dict(entry_context)
        self.orquestador.al_abrir(
            decision, entrada, orden.stop_loss, lifecycle=entry_context
        )
        if lifecycle.get("reversal_id"):
            self._audit_event(
                "REVERSAL_COMPLETED", ticker=ticker, lado=orden.tipo,
                estado=target_state.value, order_id=entry_order_id,
                cantidad=orden.cantidad, precio=entrada,
                state_before=PositionState.OUT.value,
                state_after=target_state.value,
                position_side=target_state.value,
                signal_side=orden.tipo,
                order_action=entry_action_for_state(target_state),
                **lifecycle,
            )
        self._emit_trade(orden.tipo, ticker, entrada, orden.cantidad, "OPEN", 0.0)
        self._emit_log("INFO", f"[ENTRADA] {orden}")
        try:
            self.telegram.enviar_alerta_temporal(
                f"{'🟢' if orden.tipo == 'BUY' else '🔴'} [{orden.tipo} {ticker}] "
                f"@ ${entrada:.2f} | SL ${orden.stop_loss:.2f} | TP1 ${orden.take_profit_parcial:.2f} "
                f"| {decision.resumen}", prioridad="alta")
        except Exception:
            pass
        return True

    def _gestionar_entrada_pendiente(self, ticker: str, pos: dict, precio_actual: float):
        """
        Modo real: revisa si la ENTRADA pendiente se llenó.
          - FILLED   → abre la posición y coloca la protección (TP1/SL).
          - CANCELED/REJECTED/EXPIRED → libera el hueco.
          - Sin llenar tras 'entrada_timeout_seg' → cancela y libera.
        """
        # Si el motor se está deteniendo, deja que el kill switch gestione todo
        # (evita colocar/abrir órdenes en carrera con la liquidación de apagado).
        if not self._activo:
            return
        if self._daily_blocked():
            # Una orden que quedó pendiente justo cuando se alcanzó el límite no
            # puede convertirse en una posición nueva. Cancelar; si ya tuvo fill,
            # _cancelar_pendiente la adopta y el kill switch la liquidará.
            self._cancelar_pendiente(ticker)
            return
        with self._lock:
            if self._posiciones.get(ticker) is not pos or not pos.get("pendiente"):
                return
        if not self._asegurar_token_trading():
            return
        oid = pos.get("order_id", "")
        det = self.ejecutor_hibrido.detalle_orden(self.account_number, oid) if oid \
            else {"status": "DESCONOCIDO", "filled": 0.0, "avg_price": 0.0}
        status = det.get("status", "DESCONOCIDO")
        filled = int(det.get("filled", 0) or 0)

        # Llenado (total o PARCIAL) → abrir con la cantidad y el precio REALES.
        # Schwab marca los parciales como WORKING con filledQuantity>0, así que se
        # detecta por ACCIONES ejecutadas, NO por el estado: protege de inmediato
        # las acciones ya adquiridas en vez de esperar al timeout.
        if filled > 0:
            self._abrir_desde_llenado(
                ticker, pos, filled,
                det.get("avg_price") or pos["orden"].precio_limite,
                parcial=(filled < pos["orden"].cantidad), oid=oid)
            return

        if status in ("CANCELED", "REJECTED", "EXPIRED", "REPLACED"):
            with self._lock:
                self._posiciones.pop(ticker, None)
            self._emit_log("INFO", f"[{ticker}] Entrada {status} — hueco liberado")
            self._audit_event(
                "ENTRY_TERMINATED", ticker=ticker, lado=pos.get("tipo", ""),
                estado=status, order_id=oid,
                cantidad=pos.get("orden").cantidad if pos.get("orden") else "",
                motivo="Entrada finalizada sin posición",
            )
            return

        # Estado desconocido / sin id → verificar por POSICIONES reales (pudo llenarse)
        if status == "DESCONOCIDO" or not oid:
            if self._reconciliar_una_posicion(ticker, pos):
                return

        # Timeout: cancelar y decidir según el estado FINAL real.
        edad = (datetime.now() - pos["ts_pendiente"]).total_seconds()
        if edad > self.entrada_timeout_seg:
            # Cancelar PRIMERO congela la orden (tras esto ya no puede seguir
            # llenándose); solo DESPUÉS verificamos qué pasó. Una cancelación
            # "exitosa" NO prueba que la entrada no se llenó (el remanente de un
            # parcial se cancela con éxito dejando acciones reales).
            cancelacion = None
            if oid:
                cancelacion = self.ejecutor_hibrido.cancelar_orden_confirmada(
                    self.account_number, oid, ticker=ticker)
            # (1) ¿Posición real? (fuente autoritativa) → adoptar con protección.
            if self._reconciliar_una_posicion(ticker, pos):
                return
            if oid:
                # El llenado ya se comprobó por POSICIONES (autoritativo) arriba;
                # aquí solo se lee el ESTADO de la orden para decidir si liberar el
                # hueco. NO se re-adopta por 'filled' del detalle (evita actuar sobre
                # acciones que pudieron cerrarse por seguridad); cualquier llenado que
                # 'posiciones' aún no refleje lo recoge la reconciliación periódica de
                # huérfanas el próximo ciclo.
                est2 = (cancelacion or {}).get("broker_status") \
                    or self.ejecutor_hibrido._estado_orden(self.account_number, oid)
                # Sin llenado (por posiciones). Decidir según el estado de la orden.
                if est2 not in ("CANCELED", "REJECTED", "EXPIRED", "REPLACED", "NOT_WORKING"):
                    if est2 == "DESCONOCIDO":
                        # No se puede confirmar el estado (API caída o la orden
                        # cayó fuera de la ventana de consulta). Mantener pendiente
                        # y reintentar, con un TOPE para no bloquear el ticker de
                        # forma indefinida: pasado el tope, posiciones ya tuvo
                        # minutos para reflejar cualquier llenado y no mostró nada.
                        tope = self.entrada_timeout_seg + max(120, self.entrada_timeout_seg)
                        if edad < tope:
                            self._emit_log("WARN", f"[{ticker}] Entrada incierta (estado {est2}); "
                                                   f"se reintenta el próximo ciclo")
                            return
                        # Última verificación autoritativa por posiciones antes de
                        # soltar: si el llenado apareció por fin, adoptarlo con
                        # protección en vez de dejarlo huérfano.
                        if self._reconciliar_una_posicion(ticker, pos):
                            return
                        self._emit_log("ERROR", f"[{ticker}] Entrada NO confirmable en {int(edad)}s "
                                                f"y sin posición real — se libera el hueco. "
                                                f"Revisa órdenes abiertas en el broker.")
                    else:
                        # Confirmada VIVA (WORKING/PENDING_*): reintentar cancelar
                        # el próximo ciclo. NO se libera (sería huérfana); la
                        # expiración DAY o el kill switch la limpian si el broker
                        # no responde a la cancelación.
                        self._emit_log("WARN", f"[{ticker}] Entrada aún viva (estado {est2}); "
                                               f"se reintenta cancelar el próximo ciclo")
                        return
            with self._lock:
                self._posiciones.pop(ticker, None)
            self._emit_log("INFO", f"[{ticker}] Entrada no llenada en {int(edad)}s — cancelada")
            self._audit_event(
                "ENTRY_TIMEOUT_CANCELED", ticker=ticker, lado=pos.get("tipo", ""),
                estado=(cancelacion or {}).get("broker_status", "SIN_ORDER_ID"),
                order_id=oid,
                cantidad=pos.get("orden").cantidad if pos.get("orden") else "",
                latency_ms=(cancelacion or {}).get("latency_ms"),
                motivo=f"Entrada no llenada en {int(edad)}s",
            )

    def _abrir_desde_llenado(self, ticker: str, pos: dict, filled: int, avg_price: float,
                             parcial: bool, oid: str) -> bool:
        """Abre la posición con la cantidad y el precio REALES del llenado. Si el
        llenado fue parcial, cancela el remanente y RE-LEE la cantidad definitiva
        (pudieron ejecutarse más acciones entre la lectura y la cancelación) para
        dimensionar la posición y el STOP a lo REALMENTE ejecutado, no a un conteo
        obsoleto. Devuelve True si la posición quedó abierta y PROTEGIDA; False si
        se cerró por seguridad (p. ej. no se pudo colocar el STOP)."""
        orden = pos["orden"]
        if parcial and filled < orden.cantidad and oid:
            # Cancelar el remanente PRIMERO congela la orden; luego re-leer la
            # cantidad REAL final para no dejar acciones sin proteger.
            cancelacion = self.ejecutor_hibrido.cancelar_orden_confirmada(
                self.account_number, oid, ticker=ticker)
            if not cancelacion.get("confirmado"):
                self._emit_log(
                    "ERROR",
                    f"[{ticker}] CRÍTICO: llenado parcial de {filled} acción(es), pero "
                    f"el remanente no confirmó cancelación "
                    f"({cancelacion.get('broker_status')}). Se mantiene PENDIENTE y "
                    "no se envían órdenes que puedan rebotar.",
                )
                return False
            det = self.ejecutor_hibrido.detalle_orden(self.account_number, oid)
            filled_final = int(det.get("filled", 0) or 0)
            if filled_final > filled:
                filled = filled_final
                avg_price = det.get("avg_price") or avg_price
            elif cancelacion.get("terminal_by_absence"):
                # NOT_WORKING may mean the remainder filled while cancellation
                # raced.  Re-read the authoritative broker position before sizing
                # the STOP; never protect only the stale partial quantity.
                try:
                    posiciones = self.schwab.obtener_posiciones(self.account_number)
                except Exception:
                    posiciones = None
                if posiciones is None:
                    self._emit_log(
                        "ERROR",
                        f"[{ticker}] Cancelación del remanente quedó NOT_WORKING, "
                        "pero Schwab no confirmó la cantidad final; se mantiene "
                        "PENDIENTE para no subproteger la posición.",
                    )
                    return False
                for broker_pos in posiciones or []:
                    if (
                        str(broker_pos.get("symbol", "") or "").upper() == ticker
                        and broker_pos.get("tipo") == pos.get("tipo")
                    ):
                        broker_qty = int(broker_pos.get("cantidad", 0) or 0)
                        if broker_qty > filled:
                            filled = broker_qty
                            avg_price = broker_pos.get("precio") or avg_price
                        break
        orden_fill = self._ajustar_orden_llenada(orden, filled, avg_price)
        execution_context = dict(pos.get("execution_context") or {})
        execution_context["fill_price"] = float(avg_price)
        ok = self._registrar_posicion_abierta(
            ticker, orden_fill, pos["decision"], entry_order_id=oid,
            execution_context=execution_context,
        )
        if ok:
            self._emit_log("INFO", f"[{ticker}] Entrada REAL LLENADA {filled}@${avg_price:.2f}"
                                   f"{' (parcial, resto cancelado)' if filled < orden.cantidad else ''}")
        else:
            self._emit_log("ERROR", f"[{ticker}] Entrada llenada {filled}@${avg_price:.2f} pero la "
                                    f"posición se CERRÓ por seguridad (no se pudo proteger)")
        return ok

    def _reconciliar_una_posicion(self, ticker: str, pos: dict) -> bool:
        """Verifica en Schwab si el ticker ya tiene posición real (llenado que no
        vimos por el estado de la orden) y, de ser así, la adopta con protección."""
        try:
            posiciones = self.schwab.obtener_posiciones(self.account_number)
        except Exception:
            return False
        for p in (posiciones or []):
            if p.get("symbol") == ticker and p.get("tipo") == pos["tipo"] and p.get("cantidad", 0) > 0:
                orden_fill = self._ajustar_orden_llenada(
                    pos["orden"], int(p["cantidad"]), p.get("precio") or pos["orden"].precio_limite)
                ok = self._registrar_posicion_abierta(
                    ticker, orden_fill, pos["decision"],
                    entry_order_id=pos.get("order_id", ""),
                    execution_context={
                        **dict(pos.get("execution_context") or {}),
                        "fill_price": p.get("precio")
                        or pos["orden"].precio_limite,
                    },
                )
                if ok:
                    self._emit_log("WARN", f"[{ticker}] Entrada reconciliada por posiciones: "
                                           f"{p['cantidad']}@${p.get('precio', 0):.2f}")
                # ok=False → la posición se cerró por seguridad (STOP imposible) y ya
                # no está en seguimiento: se informa para no darla por adoptada.
                return ok
        return False

    def _marcar_liquidado(self, ticker: str):
        """Marca que acabamos de enviar una liquidación MARKET de 'ticker', para que
        la red de seguridad no lo re-adopte mientras el cierre se asienta. Se toma el
        lock: lo llaman tanto los hilos de trabajo como el hilo del kill switch."""
        with self._lock:
            self._liquidados_recientes[ticker] = datetime.now()

    def _cancelar_ordenes_simbolo(self, ticker: str):
        """Cancela TODAS las órdenes de trabajo del broker para 'ticker' (best-effort).
        Se usa antes de colocar una protección nueva para no dejar dos órdenes sobre
        las mismas acciones."""
        if self._is_paper_ticker(ticker):
            return
        ok_orders, ordenes, err_orders = self._broker_read_with_retry(
            f"órdenes abiertas {ticker}",
            lambda: self.schwab.obtener_ordenes_abiertas(self.account_number),
        )
        if not ok_orders:
            self._emit_log(
                "WARN",
                f"[{ticker}] No se pudieron consultar órdenes para cancelación: {err_orders}",
            )
            return False
        seen = set()
        for o in self._iter_broker_orders(ordenes):
            try:
                sym = self._symbol_from_broker_order(o)
                oid = str(o.get("orderId", "") or "")
            except Exception:
                continue
            if sym == ticker and oid and oid not in seen:
                seen.add(oid)
                self.ejecutor_hibrido.cancelar_orden(self.account_number, oid)
        return True

    def _cantidad_real_broker(self, ticker: str, tipo: str):
        """Cantidad REAL de acciones que el broker reporta para (ticker, tipo).
        0 si el broker está plano; None si no se pudo consultar (para no bloquear
        el cierre por un fallo transitorio de la API)."""
        try:
            posiciones = self.schwab.obtener_posiciones(self.account_number)
            if posiciones is None:
                return None
            for p in posiciones:
                if p.get("symbol") != ticker:
                    continue
                if p.get("tipo") != tipo:
                    self._emit_log(
                        "ERROR",
                        f"[{ticker}] El broker reporta dirección opuesta "
                        f"({p.get('tipo')}); cierre bloqueado para no aumentar "
                        "una posición inesperada",
                    )
                    return None
                return int(p.get("cantidad", 0) or 0)
            return 0
        except Exception:
            return None

    @staticmethod
    def _ai_decision_reason(decision, approved: bool = False) -> str:
        """Human/audit-friendly explanation for the current AI gate.

        v3.1 keeps deterministic hard-veto, regime-veto and learned probability
        threshold outcomes distinct.  Previously a regime/policy veto without a
        ``hard_veto_reason`` would be displayed as ``AI_THRESHOLD``, which hid
        the real cause from the dashboard and client review.
        """
        hard = str(getattr(decision, "hard_veto_reason", "") or "").strip()
        exact_veto = str(
            getattr(decision, "execution_veto_reason", "") or ""
        ).strip()
        regime = str(getattr(decision, "market_regime_reason", "") or "").strip()
        regime_block = bool(getattr(decision, "market_regime_would_block", False))
        policy = str(
            getattr(decision, "market_regime_policy", "") or ""
        ).lower()
        if policy not in {"off", "observe", "enforce"}:
            # v3.1.0 compatibility for older DecisionFinal objects.
            enforcement = str(
                getattr(decision, "market_regime_enforcement", "off") or "off"
            ).lower()
            policy = "enforce" if enforcement == "real" else (
                "observe" if enforcement == "shadow" else "off"
            )
        regime_name = str(getattr(decision, "market_regime", "") or "").strip()
        execution_mode = str(getattr(decision, "execution_mode", "") or "").upper()

        if approved:
            if regime_block and policy == "observe" and regime:
                return (
                    f"AI_APPROVED|REGIME_OBSERVE_VETO={regime}|"
                    f"REGIME={regime_name}|EXEC={execution_mode or 'UNKNOWN'}"
                )
            if regime_name:
                return f"AI_APPROVED|REGIME={regime_name}|EXEC={execution_mode or 'UNKNOWN'}"
            return "AI_APPROVED"
        if exact_veto:
            return exact_veto
        if hard:
            return hard
        if regime_block and policy == "enforce" and regime:
            return regime
        return "AI_THRESHOLD"

    def _asegurar_ticker_limpio(self, ticker: str) -> bool:
        """Secuencia 'liberar acciones' EXACTA antes de ABRIR una entrada real:
        CANCELA de forma explícita cualquier orden de trabajo del símbolo (un stop u
        orden de cierre previo) → ESPERA y CONFIRMA que ya no está viva → recién
        entonces autoriza la orden nueva. Schwab RECHAZA abrir/cerrar/modificar si un
        stop (u OCO) activo tiene esas acciones 'comprometidas', sobre todo al
        REVERTIR. Devuelve True = símbolo confirmado limpio (abrir ya); False = no se
        pudo confirmar (posponer al próximo ciclo, nunca abrir con un stop vivo)."""
        if self._is_paper_ticker(ticker):
            return True
        for _intento in range(4):
            ok_orders, ordenes, err_orders = self._broker_read_with_retry(
                f"órdenes abiertas {ticker}",
                lambda: self.schwab.obtener_ordenes_abiertas(self.account_number),
            )
            if not ok_orders:
                # No abrir/cerrar a ciegas si Schwab no confirmó la foto de órdenes.
                self._emit_log(
                    "WARN",
                    f"[{ticker}] No se pudo confirmar las órdenes del broker "
                    f"({err_orders}); se pospone la operación",
                )
                return False
            propias = []
            seen = set()
            flat_ordenes = list(self._iter_broker_orders(ordenes))
            for o in flat_ordenes:
                try:
                    sym = self._symbol_from_broker_order(o)
                    oid = str(o.get("orderId", "") or "")
                except Exception:
                    continue
                if sym == ticker and oid and oid not in seen:
                    seen.add(oid)
                    propias.append(oid)
            if not propias:
                return True  # CONFIRMADO: sin órdenes vivas del símbolo → abrir
            # Aún hay órdenes del símbolo: cancelarlas explícitamente y re-confirmar.
            for oid in propias:
                self.ejecutor_hibrido.cancelar_orden_confirmada(
                    self.account_number, oid, ticker=ticker)
            self._emit_log("INFO", f"[{ticker}] Cancelando {len(propias)} orden(es) previa(s) del "
                                   f"símbolo antes de abrir; esperando confirmación de cancelación…")
            # No se usa una espera fija. Cada cancelar_orden_confirmada ya hizo
            # polling por estado; la siguiente lectura verifica la foto completa.
        self._emit_log("WARN", f"[{ticker}] No se confirmó la cancelación de órdenes previas; se "
                               f"pospone la apertura para no chocar con un stop activo en Schwab")
        return False

    def _reconciliar_huerfanas(self):
        """RED DE SEGURIDAD por ciclo (modo real): adopta con protección cualquier
        posición REAL del broker que el motor NO esté siguiendo. Es el invariante
        que hace segura toda la gestión: aunque una ruta suelte un hueco con
        acciones vivas, aquí se recuperan y protegen en el siguiente ciclo, así que
        nunca quedan acciones huérfanas ni sin stop."""
        if not self._has_real_tickers() or not self.account_number or not self._activo:
            return
        # Órdenes reales (STOP/cierre) requieren un token de trading vigente.
        if not self._asegurar_token_trading():
            return
        try:
            posiciones = self.schwab.obtener_posiciones(self.account_number)
        except Exception:
            posiciones = None
        # None = no se pudo consultar. NO adoptar ni limpiar órdenes en ese caso:
        # tratar 'no sé' como 'cuenta plana' cancelaría la protección de una
        # posición real ante un fallo transitorio de la API. Solo actuar con datos.
        if posiciones is None:
            return
        ahora = datetime.now()
        for p in posiciones:
            ticker = str(p.get("symbol") or "").upper()
            cant = int(p.get("cantidad", 0) or 0)
            if not ticker or cant <= 0:
                continue
            # Una cuenta puede contener posiciones manuales u otras estrategias.
            # La red de seguridad solo administra el universo solicitado al bot.
            if ticker not in {str(t).upper() for t in self.tickers}:
                continue
            with self._lock:
                ya = self._posiciones.get(ticker)
            # Si YA hay cualquier seguimiento (posición, reserva o pendiente) para
            # ese ticker, lo gestiona el flujo normal — no tocar (evita duplicar).
            if ya is not None:
                continue
            # No re-adoptar un ticker que ACABAMOS de liquidar: su cierre MARKET
            # puede seguir asentándose y la posición aún figurar viva un ciclo.
            ts_liq = self._liquidados_recientes.get(ticker)
            if ts_liq and (ahora - ts_liq).total_seconds() < self.COOLDOWN_LIQUIDACION_SEG:
                continue
            self._emit_log("WARN", f"[{ticker}] Posición HUÉRFANA en el broker "
                                   f"({p.get('tipo')} {cant}@${float(p.get('precio', 0) or 0):.2f}) — "
                                   f"adoptando con protección (red de seguridad)")
            self._adoptar_huerfana(ticker, p)

        # LIMPIEZA de órdenes HUÉRFANAS: cancelar cualquier orden de trabajo del
        # broker que ya NO corresponda a una posición real ni a nada que el motor
        # siga (p. ej. un take-profit/stop que quedó descansando tras cerrarse su
        # posición en una sesión anterior). El broker RECHAZA esas órdenes al madurar
        # (SELL/BUY [TO CLOSE] sobre una posición inexistente); aquí se limpian solas.
        tickers_con_posicion = {p.get("symbol") for p in posiciones
                                if int(p.get("cantidad", 0) or 0) > 0}
        try:
            ordenes = self.schwab.obtener_ordenes_abiertas(self.account_number)
        except Exception:
            ordenes = None
        # None = no se pudo leer las órdenes: NO tocar los contadores de huérfanas ni
        # cancelar nada este ciclo (un fallo transitorio no debe reiniciar la
        # confirmación de 2 ciclos ni ocultar una orden realmente huérfana).
        if ordenes is None:
            return
        huerfanas_ahora = set()
        for o in ordenes:
            try:
                leg = (o.get("orderLegCollection") or [{}])[0]
                sym = leg.get("instrument", {}).get("symbol", "")
                oid = str(o.get("orderId", ""))
            except Exception:
                continue
            if not sym or not oid:
                continue
            # SOLO el universo de tickers del bot: jamás tocar órdenes ajenas de la
            # cuenta (otra estrategia o del titular), igual que el barrido del kill switch.
            if sym not in self.tickers:
                continue
            # Conservar si hay CUALQUIER seguimiento del ticker (posición, reserva,
            # pendiente) o si el broker reporta una posición real de ese símbolo.
            with self._lock:
                seguido = self._posiciones.get(sym) is not None
            if seguido or sym in tickers_con_posicion:
                continue
            # Candidata a huérfana. Se exige verla huérfana en 2 ciclos consecutivos
            # antes de cancelar: un payload de posiciones parcial (200 incompleto por
            # consistencia eventual) podría ocultar una posición real un ciclo, y no
            # se debe cancelar la protección de una posición real por una foto momentánea.
            huerfanas_ahora.add(oid)
            n = self._orden_huerfana_ciclos.get(oid, 0) + 1
            self._orden_huerfana_ciclos[oid] = n
            if n < 2:
                continue
            self._emit_log("WARN", f"[{sym}] Orden HUÉRFANA confirmada (id {oid}) sin posición "
                                   f"asociada — cancelando (limpieza de la red de seguridad)")
            cancel_ok = self.ejecutor_hibrido.cancelar_orden_confirmada(
                self.account_number, oid, ticker=sym)
            self._audit_event(
                "ORPHAN_ORDER_CLEANUP", ticker=sym,
                estado=cancel_ok.get("broker_status", "DESCONOCIDO"),
                order_id=oid, latency_ms=cancel_ok.get("latency_ms"),
                motivo="Orden abierta sin posición ni seguimiento local",
                detalle={"confirmado": cancel_ok.get("confirmado", False)},
            )
            self._orden_huerfana_ciclos.pop(oid, None)
        # Olvidar candidatas que dejaron de figurar como huérfanas (posición
        # reaparecida en el payload, o la orden ya no existe): reinicia su conteo.
        for oid in list(self._orden_huerfana_ciclos.keys()):
            if oid not in huerfanas_ahora:
                self._orden_huerfana_ciclos.pop(oid, None)

    def _adoptar_huerfana(self, ticker: str, p: dict):
        """Coloca un STOP de protección a una posición real huérfana y la registra
        en el seguimiento. Si no se puede proteger, la cierra (mejor plano que
        desnudo)."""
        tipo = p.get("tipo")
        cant = int(p.get("cantidad", 0) or 0)
        entrada = float(p.get("precio", 0) or 0)
        if tipo not in ("BUY", "SELL") or cant <= 0 or entrada <= 0:
            return
        pct = float(self.config.get("risk", {}).get("stop_emergencia_pct", 1.0)) / 100.0
        sl_price = round(entrada * (1 - pct), 2) if tipo == "BUY" else round(entrada * (1 + pct), 2)
        # Cancelar CUALQUIER orden previa del símbolo antes de colocar el STOP nuevo:
        # una huérfana puede traer ya un stop viejo, y dos stops sobre las mismas
        # acciones sobre-venden (doble cierre → posición opuesta desnuda).
        self._cancelar_ordenes_simbolo(ticker)
        sl = self.ejecutor_hibrido.colocar_salida(
            ticker, tipo, cant, sl_price, "STOP", self.account_number)
        sl_id = (sl or {}).get("order_id", "")
        if not sl_id:
            cierre = self.ejecutor_hibrido.liquidar_posicion(
                ticker, tipo, cant, self.account_number, "huérfana sin poder proteger")
            if cierre.get("status") in ("OK", "SIMULADA"):
                self._marcar_liquidado(ticker)
                self._emit_log("ERROR", f"[{ticker}] Huérfana sin STOP posible — LIQUIDADA por seguridad")
            else:
                self._emit_log("ERROR", f"[{ticker}] CRÍTICO: huérfana sin STOP y sin poder cerrar — "
                                        f"se reintentará el próximo ciclo. Revisa el broker URGENTE.")
            return
        orden_stub = OrdenTradingCalculada(
            ticker=ticker, tipo=tipo, precio_limite=round(entrada, 2), stop_loss=sl_price,
            trailing_stop_pct=0.0, cantidad=cant, riesgo_usd=0.0, atr_actual=0.0,
            cantidad_resto=cant, modo_resto=self.gestor_riesgo.modo_resto,
            salida_parcial_activa=False)
        position_id = uuid.uuid4().hex
        execution_id = uuid.uuid4().hex
        state = state_for_signal(tipo)
        self._state_machine().adopt(ticker, tipo, position_id)
        with self._lock:
            self._posiciones[ticker] = {
                "position_state": state.value, "position_id": position_id,
                "execution_id": execution_id, "signal_id": "",
                "reversal_id": "", "correlation_id": execution_id,
                "signal_side": "", "entry_order_id": "",
                "entry_integrity": {"entry_confirmed": False},
                "tipo": tipo, "cantidad": cant, "cantidad_resto": cant, "entrada": entrada,
                "sl": sl_price, "sl_actual": sl_price, "tp1": 0.0, "tp_final": 0.0,
                "tp1_hit": False, "modo_resto": self.gestor_riesgo.modo_resto,
                "salida_parcial": False, "pnl_realizado": 0.0, "orden": orden_stub,
                "decision": None, "sl_order_id": sl_id, "tp1_order_id": "",
                "abierta_ts": datetime.now(), "readoptada": True,
            }
        self._emit_log("WARN", f"[{ticker}] Huérfana adoptada y PROTEGIDA: {tipo} {cant}"
                               f"@${entrada:.2f} | SL ${sl_price:.2f}")
        self._audit_event(
            "ORPHAN_POSITION_ADOPTED", ticker=ticker, lado=tipo,
            estado="PROTECTED", order_id=sl_id,
            cantidad=cant, precio=entrada,
            correlation_id=execution_id, execution_id=execution_id,
            position_id=position_id, state_before=PositionState.OUT.value,
            state_after=state.value, position_side=state.value,
            order_action=entry_action_for_state(state),
            detalle={"stop_loss": sl_price, "training_eligible": False},
        )

    def _ajustar_orden_llenada(self, orden, filled: int, avg_price: float):
        """Copia la orden con la cantidad y el precio REALES del llenado,
        RE-ANCLANDO SL/TP al precio de llenado y recalculando el reparto 70/30.

        Si la entrada se llena lejos del límite (marketable limit / hueco), mantener
        el SL/TP en los precios ORIGINALES los dejaría del lado equivocado: p. ej. un
        BUY que llena por debajo del límite dejaría el STOP por ENCIMA de la entrada
        real → Schwab lo dispara al instante y liquida con pérdida. Se conservan las
        DISTANCIAS del plan (riesgo y objetivos en R) re-ancladas al llenado real."""
        from dataclasses import replace
        activa = orden.salida_parcial_activa and filled >= 3
        if activa:
            parcial = max(1, min(round(filled * self.gestor_riesgo.pct_primer_objetivo), filled - 1))
            resto = filled - parcial
        else:
            parcial, resto = 0, filled
        avg = round(float(avg_price), 2)
        ent0 = float(orden.precio_limite)
        d_sl = abs(ent0 - float(orden.stop_loss))
        d_tp = abs(float(orden.take_profit) - ent0)
        d_tp1 = abs(float(orden.take_profit_parcial) - ent0)
        if orden.tipo == "BUY":
            sl = round(avg - d_sl, 2); tp = round(avg + d_tp, 2); tp1 = round(avg + d_tp1, 2)
        else:  # SELL / SELL_SHORT
            sl = round(avg + d_sl, 2); tp = round(avg - d_tp, 2); tp1 = round(avg - d_tp1, 2)
        # Separación mínima de 1 centavo tras redondear (evita SL==entrada en baratos);
        # se compara en CENTAVOS enteros para no depender del ruido de coma flotante.
        if abs(round(avg * 100) - round(sl * 100)) < 1:
            sl = round(avg - 0.01, 2) if orden.tipo == "BUY" else round(avg + 0.01, 2)
        return replace(orden, precio_limite=avg, stop_loss=sl, take_profit=tp,
                       take_profit_parcial=tp1, cantidad=int(filled),
                       cantidad_parcial=parcial, cantidad_resto=resto, salida_parcial_activa=activa)

    def _cancelar_pendiente(self, ticker: str) -> bool:
        """Cancela una entrada PENDIENTE (giro de señal o kill switch). Si la orden
        YA había ejecutado acciones reales, las ADOPTA como posición real CON
        PROTECCIÓN y devuelve False: el hueco NO quedó libre, ahora es una posición
        real que el kill switch / la reversión cerrarán por la vía normal (con
        verificación de resultado y reintento) — nunca se dejan acciones huérfanas.
        Devuelve True si el hueco quedó libre (no hubo llenado)."""
        with self._lock:
            pos = self._posiciones.get(ticker)
        if pos is None or not pos.get("pendiente"):
            return True
        oid = pos.get("order_id", "")
        paper_ticker = self._is_paper_ticker(ticker, pos=pos)
        cancelacion = None
        if not paper_ticker:
            # Cancelar PRIMERO congela la orden (si hay id que cancelar).
            cancelacion = None
            if oid:
                cancelacion = self.ejecutor_hibrido.cancelar_orden_confirmada(
                    self.account_number, oid, ticker=ticker)
            # Comprobar SIEMPRE (incluso SIN oid: entrada RECONCILIAR ya enviada al
            # broker) si la entrada dejó acciones reales, por POSICIONES (fuente
            # autoritativa). Si las hay, adoptarlas con protección. Un llenado que
            # 'posiciones' aún no refleje por retraso lo recoge la reconciliación
            # periódica de huérfanas el próximo ciclo — nunca se pierden acciones.
            if self._reconciliar_una_posicion(ticker, pos):
                self._emit_log("WARN", f"[{ticker}] La entrada pendiente ya estaba llenada: "
                                       f"adoptada con protección (se cerrará por la vía normal)")
                return False
            if oid and not (cancelacion or {}).get("confirmado"):
                self._emit_log(
                    "WARN",
                    f"[{ticker}] Cancelación pendiente NO confirmada "
                    f"(estado {(cancelacion or {}).get('broker_status', 'DESCONOCIDO')}); "
                    "se conserva el estado local y no se abre la orden opuesta",
                )
                self._audit_event(
                    "PENDING_ENTRY_CANCEL_FAILED", ticker=ticker,
                    lado=pos.get("tipo", ""),
                    estado=(cancelacion or {}).get("broker_status", "DESCONOCIDO"),
                    order_id=oid,
                    latency_ms=(cancelacion or {}).get("latency_ms"),
                    motivo=(cancelacion or {}).get("error", ""),
                )
                return False
            if not oid:
                if not self._asegurar_ticker_limpio(ticker):
                    self._emit_log(
                        "WARN",
                        f"[{ticker}] Entrada sin order_id: no es seguro liberar el símbolo "
                        "hasta reconciliar con órdenes/posiciones del broker",
                    )
                    return False
        with self._lock:
            self._posiciones.pop(ticker, None)
        self._emit_log("INFO", f"[{ticker}] Entrada pendiente cancelada")
        self._audit_event(
            "PENDING_ENTRY_CANCELED", ticker=ticker, lado=pos.get("tipo", ""),
            estado=(cancelacion or {}).get("broker_status", "CANCELED")
            if not paper_ticker else "SIMULADA",
            order_id=oid,
            latency_ms=(cancelacion or {}).get("latency_ms")
            if not paper_ticker else 0,
        )
        return True

    # ==========================================================
    # MONITOR DE ÓRDENES (independiente del ciclo de velas)
    # ==========================================================

    def _iniciar_monitor_ordenes(self):
        """
        Vigila llenados a la cadencia del broker, no a la cadencia de la estrategia.

        Las señales se evalúan una vez por vela cerrada de 1m, pero una entrada,
        TP1 o stop puede cambiar de estado entre velas. Separar ambos relojes evita
        dejar una posición llenada sin protección durante hasta 60 segundos.
        """
        if not self._has_real_tickers():
            return
        if self._order_monitor_thread and self._order_monitor_thread.is_alive():
            return
        self._order_monitor_stop.clear()
        self._order_monitor_thread = threading.Thread(
            target=self._order_monitor_loop,
            name="schwab-order-monitor",
            daemon=True,
        )
        self._order_monitor_thread.start()
        self._emit_log(
            "INFO",
            f"Monitor de órdenes activo cada "
            f"{self.ejecutor_hibrido.poll_interval_ms}ms",
        )

    def _detener_monitor_ordenes(self):
        self._order_monitor_stop.set()
        hilo = self._order_monitor_thread
        if hilo and hilo.is_alive() and hilo is not threading.current_thread():
            hilo.join(timeout=2.0)
        self._order_monitor_thread = None

    def _order_monitor_loop(self):
        max_workers = max(
            1,
            int(self.config.get("risk", {}).get("max_simultaneous_positions", 17)),
        )
        poll_s = max(0.025, self.ejecutor_hibrido.poll_interval_ms / 1000.0)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            while self._activo and not self._order_monitor_stop.is_set():
                with self._lock:
                    tickers = [
                        ticker for ticker, pos in self._posiciones.items()
                        if pos and not pos.get("reservado")
                    ]
                futures = [
                    pool.submit(self._monitor_orden_ticker, ticker)
                    for ticker in tickers
                ]
                for future in futures:
                    try:
                        future.result()
                    except Exception as exc:
                        self._emit_log("ERROR", f"Monitor de órdenes: {exc}")
                self._order_monitor_stop.wait(poll_s)

    def _monitor_orden_ticker(self, ticker: str):
        with self.ejecutor_hibrido.secuencia_simbolo(ticker):
            with self._lock:
                pos = self._posiciones.get(ticker)
            if pos is None or pos.get("reservado") or not self._activo:
                return
            if self._is_paper_ticker(ticker, pos=pos):
                return
            if pos.get("pendiente"):
                self._gestionar_entrada_pendiente(
                    ticker, pos, pos["orden"].precio_limite)
            else:
                self._gestionar_real(ticker, pos, pos.get("entrada", 0.0))

    # ==========================================================
    # GESTIÓN DE POSICIONES (parcial / breakeven / cierre)
    # ==========================================================

    def _gestionar_posiciones(self, ticker: str, precio_actual: float, df_micro):
        with self._lock:
            pos = self._posiciones.get(ticker)
        if pos is None or pos.get("reservado"):
            return

        # Entrada real aún sin llenar: gestionar su llenado/timeout aparte
        if pos.get("pendiente"):
            self._gestionar_entrada_pendiente(ticker, pos, precio_actual)
            return

        if self._is_paper_ticker(ticker, pos=pos):
            self._gestionar_paper(ticker, pos, df_micro)
        else:
            self._gestionar_real(ticker, pos, precio_actual)

    def _gestionar_paper(self, ticker: str, pos: dict, df_micro):
        """
        Simula el ciclo de vida contra la última vela real: salida parcial
        del 70% en TP1, movimiento a breakeven, y cierre por SL o TP final.
        Genera datos de entrenamiento reales para los agentes.
        """
        vela = df_micro.iloc[-1]
        hi = float(vela["high"])
        lo = float(vela["low"])
        tipo = pos["tipo"]
        entrada = pos["entrada"]

        if tipo == "BUY":
            # Conservador: si la vela toca SL y TP a la vez, prevalece el SL
            if lo <= pos["sl_actual"]:
                self._cerrar_posicion(ticker, pos["sl_actual"],
                                      motivo="SL" if not pos["tp1_hit"] else "SL(breakeven)")
                return
            if not pos["tp1_hit"] and pos["salida_parcial"] and hi >= pos["tp1"]:
                self._ejecutar_parcial(ticker, pos)
                return
            if pos["tp1_hit"] and hi >= pos["tp_final"]:
                self._cerrar_posicion(ticker, pos["tp_final"], motivo="TP final")
                return
            if not pos["salida_parcial"] and hi >= pos["tp_final"]:
                self._cerrar_posicion(ticker, pos["tp_final"], motivo="TP")
                return
        else:  # SELL
            if hi >= pos["sl_actual"]:
                self._cerrar_posicion(ticker, pos["sl_actual"],
                                      motivo="SL" if not pos["tp1_hit"] else "SL(breakeven)")
                return
            if not pos["tp1_hit"] and pos["salida_parcial"] and lo <= pos["tp1"]:
                self._ejecutar_parcial(ticker, pos)
                return
            if pos["tp1_hit"] and lo <= pos["tp_final"]:
                self._cerrar_posicion(ticker, pos["tp_final"], motivo="TP final")
                return
            if not pos["salida_parcial"] and lo <= pos["tp_final"]:
                self._cerrar_posicion(ticker, pos["tp_final"], motivo="TP")
                return

    def _ejecutar_parcial(self, ticker: str, pos: dict):
        state = PositionState.LONG if pos.get("tipo") == "BUY" else PositionState.SHORT
        context = {
            "correlation_id": pos.get("correlation_id", ""),
            "reversal_id": pos.get("reversal_id", ""),
            "signal_id": pos.get("signal_id", ""),
            "execution_id": pos.get("execution_id", ""),
            "position_id": pos.get("position_id", ""),
            "state_before": state.value,
            "state_after": state.value,
            "position_side": state.value,
            "signal_side": "",
            "order_action": exit_action_for_state(state),
        }
        with self._lifecycle_scope(context):
            return self._ejecutar_parcial_impl(ticker, pos)

    def _ejecutar_parcial_impl(self, ticker: str, pos: dict):
        """
        Cierra el 70% en TP1 y RE-PROTEGE el 30% restante.

        CRÍTICO (real): tras llenarse el TP1, el STOP inicial es del 100% de las
        acciones. SIEMPRE se cancela y se coloca un STOP fresco por SOLO el 30%
        restante — en breakeven (precio de entrada) o en el SL fijo inicial según
        modo_resto. Nunca se deja vivo un STOP del 100% (evita vender de más y
        abrir una posición opuesta no deseada).
        """
        with self._lock:
            if self._posiciones.get(ticker) is not pos or pos.get("tp1_hit"):
                return
        paper_ticker = self._is_paper_ticker(ticker, pos=pos)
        orden = pos["orden"]
        tp1 = pos["tp1"]
        tp1_target = tp1
        actual_tp1_price = None
        entrada = pos["entrada"]
        cant_parcial = orden.cantidad_parcial

        if pos["modo_resto"] == "breakeven":
            # Breakeven con margen de respiro: coloca el stop un pequeño % por
            # debajo (BUY) / encima (SELL) de la entrada, para que el ruido de
            # 1m no saque el 30% al retestear la zona de entrada.
            offset = entrada * (self.breakeven_offset_pct / 100.0)
            nuevo_sl_price = (entrada - offset) if pos["tipo"] == "BUY" else (entrada + offset)
        else:
            nuevo_sl_price = pos["sl"]
        nuevo_sl_id = pos["sl_order_id"]
        reproteccion_ok = True

        if not paper_ticker:
            if not self._asegurar_token_trading():
                return
            detail_reader = getattr(self.ejecutor_hibrido, 'detalle_orden', None)
            if callable(detail_reader):
                fill = detail_reader(self.account_number, pos['tp1_order_id'])
                if str(fill.get('status', '')).upper() != 'FILLED':
                    return
                actual_tp1_price = float(fill.get('avg_price', 0) or 0)
                if actual_tp1_price > 0:
                    tp1 = actual_tp1_price
            # El TP1 (LIMIT) ya cerró el 70% en el broker. Reemplazar el STOP del
            # 100% por uno del 30% (cancela el viejo y coloca el nuevo).
            res = self.ejecutor_hibrido.mover_stop_breakeven(
                ticker, pos["tipo"], pos["cantidad_resto"], nuevo_sl_price,
                pos["sl_order_id"], self.account_number,
            )
            if res and res.get("status") in ("OK", "SIMULADA"):
                nuevo_sl_id = res.get("order_id", pos["sl_order_id"])
            else:
                reproteccion_ok = False
                cancel_ok = bool((res or {}).get("cancelacion", {}).get("confirmado"))
                self._emit_log(
                    "ERROR",
                    f"[{ticker}] No se pudo re-proteger el 30% tras TP1 "
                    f"(estado {(res or {}).get('status', 'ERROR')}, "
                    f"cancelación_stop={cancel_ok})",
                )
                if not cancel_ok:
                    # El stop de 100% todavía puede estar vivo o haberse llenado.
                    # Reintentar en el siguiente poll; no enviar otra orden encima.
                    return

        if not paper_ticker and actual_tp1_price is not None and actual_tp1_price <= 0:
            # Protect the remainder immediately, but do not fabricate realized PnL
            # from the requested limit while execution prices are unavailable.
            with self._lock:
                pos['sl_order_id'] = nuevo_sl_id if reproteccion_ok else ''
                pos['sl_actual'] = nuevo_sl_price
                pos['partial_price_pending'] = True
            self._audit_event('TP1_PRICE_RECONCILIATION_REQUIRED', ticker=ticker,
                estado='ACTUAL_FILL_PRICE_UNKNOWN', order_id=pos.get('tp1_order_id', ''),
                motivo='Remainder protection updated; TP1 accounting awaits actual execution price')
            if not reproteccion_ok:
                self._cerrar_posicion(ticker, entrada, motivo='sin proteccion tras TP1')
            return

        pnl_parcial = (tp1 - entrada) * cant_parcial if pos["tipo"] == "BUY" \
            else (entrada - tp1) * cant_parcial

        with self._lock:
            pos["tp1_hit"] = True
            pos["pnl_realizado"] += pnl_parcial
            pos["sl_actual"] = nuevo_sl_price
            pos["sl_order_id"] = nuevo_sl_id if reproteccion_ok else ""
            pos["partial_exit_at"] = self._ahora_et()
            pos["partial_exit_price"] = tp1
            self._resumen["parciales"] += 1

        self._registrar_pnl_realizado_diario(
            pnl_parcial, ticker=ticker, motivo="TP1 70%"
        )
        self._emit_trade(pos["tipo"], ticker, tp1, cant_parcial, "PARTIAL", pnl_parcial)
        self._audit_event(
            "TP1_FILLED_70_PERCENT",
            ticker=ticker, lado=pos["tipo"],
            estado="SIMULATED_FILLED" if paper_ticker else "FILLED",
            order_id=pos.get("tp1_order_id", ""),
            cantidad=cant_parcial, precio=tp1, pnl_usd=pnl_parcial,
            detalle={
                "cantidad_resto": pos["cantidad_resto"],
                "target_price": tp1_target,
                "fill_price": tp1,
                "nuevo_stop": nuevo_sl_price,
                "nuevo_sl_order_id": nuevo_sl_id if reproteccion_ok else "",
                "reprotegido": reproteccion_ok,
            },
        )
        if self._evaluar_limite_perdida_diaria():
            return

        if reproteccion_ok:
            self._emit_log("INFO", f"[PARCIAL 70%] {ticker} vendió {cant_parcial} @ ${tp1:.2f} "
                                   f"| PnL ${pnl_parcial:.2f} | resto {pos['cantidad_resto']} protegido en "
                                   f"${nuevo_sl_price:.2f} ({pos['modo_resto']})")
        else:
            # La cancelación del stop viejo sí se confirmó pero el stop nuevo no.
            # Cerrar el remanente es la única salida fail-safe que no lo deja desnudo.
            self._emit_log(
                "ERROR",
                f"[{ticker}] STOP anterior cancelado pero STOP de 30% no confirmado; "
                "liquidando el remanente por seguridad",
            )
            precio = self._precio_rapido(ticker, entrada)
            self._cerrar_posicion(ticker, precio, motivo="sin proteccion tras TP1")

    def _recuperar_cierre_stop_llenado(
        self,
        ticker: str,
        pos: dict,
        cantidad_esperada: int,
        cancel_result: Optional[dict] = None,
    ) -> dict:
        """Recover broker evidence when the tracked STOP won the cancel race.

        v3.0.1: a REAL close can arrive here after Schwab has already flattened the
        position with the protective STOP.  v2.9.8 correctly treated Schwab as the
        source of truth, but it discarded the STOP order id returned by the cancel
        reconciliation path.  The feedback row then had ``exit_confirmed=1`` with
        an empty ``exit_order_id`` and was quarantined from x/50.

        Only the tracked SL order is eligible for this recovery.  TP1 is
        intentionally ignored because it can be a partial fill and therefore is
        not sufficient evidence of the final position close.
        """
        oid = str((pos or {}).get("sl_order_id", "") or "").strip()
        if not oid:
            return {}
        try:
            expected = max(1, int(cantidad_esperada or 1))
        except (TypeError, ValueError):
            expected = 1

        evidence = dict(cancel_result or {})
        status = str(evidence.get("broker_status", "") or "").upper()
        try:
            filled = float(evidence.get("filled", 0) or 0)
        except (TypeError, ValueError):
            filled = 0.0
        avg_price = 0.0

        # Re-read the exact known order when cancellation evidence is incomplete,
        # or simply to recover the broker fill price for correct PnL/labels.
        if status != "FILLED" or filled < expected or avg_price <= 0:
            try:
                detail = self.ejecutor_hibrido.detalle_orden(
                    self.account_number, oid
                ) or {}
            except Exception:
                detail = {}
            detail_status = str(detail.get("status", "") or "").upper()
            try:
                detail_filled = float(detail.get("filled", 0) or 0)
            except (TypeError, ValueError):
                detail_filled = 0.0
            try:
                detail_avg = float(detail.get("avg_price", 0) or 0)
            except (TypeError, ValueError):
                detail_avg = 0.0
            if detail_status:
                status = detail_status
            filled = max(filled, detail_filled)
            if detail_avg > 0:
                avg_price = detail_avg

        if status != "FILLED" or filled < expected or avg_price <= 0:
            # Do not make the row trainable unless both broker identity and the
            # actual fill price are available. Otherwise PnL/labels could be wrong.
            return {}

        recovered = {
            "order_id": oid,
            "filled": filled,
            "avg_price": avg_price,
            "broker_status": "FILLED",
        }
        self._audit_event(
            "EXIT_ORDER_ID_RECOVERED",
            ticker=ticker,
            estado="FILLED",
            order_id=oid,
            cantidad=filled,
            precio=avg_price if avg_price > 0 else "",
            motivo="tracked protective STOP filled before local close reconciliation",
            detalle={
                "expected_quantity": expected,
                "source": "tracked_sl_order",
            },
        )
        return recovered

    def _cerrar_posicion(self, ticker: str, precio_salida: float, motivo: str = "") -> bool:
        """Cierra una posición SERIALIZANDO por ticker: si otro hilo ya está
        cerrando este ticker (worker vs kill switch), no se ejecuta un segundo
        cierre (evita doble MARKET / sobreventa)."""
        with self._lock:
            pos = self._posiciones.get(ticker)
        lifecycle = self._position_lifecycle_context(ticker, pos or {})
        lifecycle['exit_signal_price'] = precio_salida
        with self._lifecycle_scope(lifecycle):
            with self.ejecutor_hibrido.secuencia_simbolo(ticker):
                with self._lock:
                    if ticker in self._cerrando:
                        return False
                    self._cerrando.add(ticker)
                try:
                    return self._cerrar_posicion_impl(ticker, precio_salida, motivo)
                finally:
                    with self._lock:
                        self._cerrando.discard(ticker)

    def _cerrar_posicion_impl(self, ticker: str, precio_salida: float, motivo: str = "") -> bool:
        """
        Cierra el remanente de la posición, registra PnL y alimenta el aprendizaje.
        Devuelve True si la posición quedó cerrada; False si la liquidación real
        falló (en cuyo caso la posición se MANTIENE para no perder el rastro).
        """
        with self._lock:
            pos = self._posiciones.get(ticker)
        if pos is None:
            return False
        paper_ticker = self._is_paper_ticker(ticker, pos=pos)
        execution_mode = "PAPER" if paper_ticker else "REAL"
        if pos.get("reservado"):
            self._liberar_reserva(ticker)
            return True
        if pos.get("pendiente"):
            # Entrada aún sin llenar (o llenada y adoptada): _cancelar_pendiente
            # devuelve False si dejó una posición real adoptada, de modo que el
            # llamador (kill switch) la re-vea y la cierre por la vía normal.
            return self._cancelar_pendiente(ticker)

        tipo = pos["tipo"]
        entrada = pos["entrada"]
        cantidad_total = pos["orden"].cantidad
        # Acciones que restaban por cerrar
        cant_rest = pos["cantidad_resto"] if pos["tp1_hit"] else cantidad_total
        position_state = (
            PositionState.LONG if tipo == "BUY" else PositionState.SHORT
        )
        lifecycle = self._position_lifecycle_context(ticker, pos)
        # v3.0.3 hotfix candidate: ``lifecycle`` intentionally carries ticker
        # for thread-local/executor audit context. Never expand that same ticker
        # again into calls that already pass ``ticker=ticker``.
        lifecycle_fields = {
            key: value for key, value in lifecycle.items() if key != "ticker"
        }
        close_order_id = ""
        actual_exit_price = float(precio_salida)
        exit_confirmed = bool(paper_ticker)
        # Broker-flat and price-verified are separate facts. Emergency recovery
        # may safely flatten an account even when historical fill price evidence
        # is unavailable; such a close must not enter the verified x/50 dataset.
        exit_price_verified = bool(paper_ticker)
        # v3.0.1: preserve cancellation evidence for the tracked protective STOP.
        # If the STOP fills before/while we cancel it, Schwab may already be flat
        # by the time the local close path checks the position.
        sl_cancel_result = None
        # v2.9.8: cualquier cierre REAL que NO sea la confirmación de un STOP
        # ya llenado debe cerrar la posición REAL en Schwab.  Antes se dependía de
        # textos concretos de ``motivo`` (reversión/kill switch/TP); una salida
        # ``señal opuesta ... (entrada IA vetada)`` podía cancelar las protecciones,
        # soltar el estado local y dejar la posición viva en el broker.
        motivo_normalizado = str(motivo or "").lower()
        stop_real_ya_llenado = motivo_normalizado.startswith("sl(real)")
        self._audit_event(
            "POSITION_CLOSE_REQUESTED", ticker=ticker, lado=tipo,
            estado="INICIADA", cantidad=cant_rest, precio=precio_salida,
            motivo=motivo,
            **lifecycle_fields,
        )

        if not paper_ticker:
            if not self._asegurar_token_trading():
                return False
            # 1) Cancelar SIEMPRE las protecciones que sigan vivas (TP1 y/o SL).
            #    El DELETE no basta: cada orden debe quedar terminal/no visible como
            #    WORKING antes de enviar una liquidación o soltar el estado local.
            for oid_key in ("tp1_order_id", "sl_order_id"):
                oid = pos.get(oid_key)
                if oid:
                    cancel_result = self.ejecutor_hibrido.cancelar_orden_confirmada(
                        self.account_number, oid, ticker=ticker)
                    if oid_key == "sl_order_id":
                        sl_cancel_result = dict(cancel_result or {})
                    self._audit_event(
                        "PROTECTION_CANCEL_RESULT", ticker=ticker, lado=tipo,
                        estado=cancel_result.get("broker_status", "DESCONOCIDO"),
                        order_id=oid,
                        cantidad=cancel_result.get("filled"),
                        latency_ms=cancel_result.get("latency_ms"),
                        motivo=oid_key,
                        detalle={"confirmado": cancel_result.get("confirmado", False)},
                    )
            if not self._asegurar_ticker_limpio(ticker):
                self._emit_log(
                    "WARN",
                    f"[{ticker}] Cierre pospuesto: todavía existen órdenes del símbolo "
                    "sin cancelación confirmada en Schwab",
                )
                return False
            # 2) Fuente de verdad para TODOS los cierres REAL.
            #    Única excepción: SL(real), porque ese STOP ya fue llenado por Schwab.
            if not stop_real_ya_llenado:
                real = self._cantidad_real_broker(ticker, tipo)
                has_pending = getattr(self.ejecutor_hibrido, 'has_pending_exit', lambda *a: False)(self.account_number, ticker)
                if real == 0 and not has_pending:
                    # El broker ya está plano.  No enviar un segundo cierre.  Antes
                    # de soltar el estado local, recuperar la evidencia del STOP
                    # conocido si fue él quien ganó la carrera de cancelación.
                    expected_close_qty = max(1, int(cant_rest or 1))
                    recovered = self._recuperar_cierre_stop_llenado(
                        ticker, pos, expected_close_qty, sl_cancel_result
                    )
                    if recovered:
                        close_order_id = str(recovered.get("order_id", "") or "")
                        recovered_price = float(recovered.get("avg_price", 0) or 0)
                        if recovered_price > 0:
                            actual_exit_price = recovered_price
                            exit_price_verified = True
                        # Keep cant_rest intact: those shares DID close at the STOP,
                        # and they must contribute to realized PnL and feedback.
                        self._emit_log(
                            "INFO",
                            f"[{ticker}] Broker ya plano: cierre vinculado al STOP "
                            f"FILLED {close_order_id}",
                        )
                    else:
                        # Without a broker-linked exit order we preserve the old
                        # fail-closed behavior: the close can release local exposure,
                        # but the observation remains quarantined from x/50.
                        cant_rest = 0
                        self._emit_log(
                            "WARN",
                            f"[{ticker}] Broker ya plano al procesar cierre ({motivo}); "
                            "sin order-id de salida verificable, el aprendizaje se "
                            "mantendrá en cuarentena.",
                        )
                    exit_confirmed = True
                else:
                    # ``liquidar_posicion`` vuelve a leer Schwab y usa SU lado/cantidad
                    # aunque el estado local esté desfasado.
                    requested_qty = cant_rest if real is None else min(cant_rest, real)
                    res = self.ejecutor_hibrido.liquidar_posicion(
                        ticker, tipo, max(1, int(requested_qty or cant_rest or 1)),
                        self.account_number, motivo,
                    )
                    if res.get("status") not in ("OK", "SIMULADA"):
                        self._emit_log(
                            "ERROR",
                            f"[{ticker}] Liquidación FALLÓ ({res.get('status')}) — "
                            "se mantiene la posición para reintentar",
                        )
                        self._audit_event(
                            "POSITION_CLOSE_FAILED", ticker=ticker, lado=tipo,
                            estado=res.get("status", "ERROR"),
                            order_id=res.get("order_id", ""),
                            cantidad=cant_rest, precio=precio_salida,
                            latency_ms=res.get("latency_ms"),
                            motivo=res.get("mensaje", motivo),
                        )
                        return False
                    close_order_id = str(res.get("order_id", "") or "")
                    broker_qty = int(res.get("cantidad", 0) or 0)
                    if broker_qty > 0:
                        cant_rest = min(cant_rest, broker_qty)
                    if float(res.get("avg_price", 0.0) or 0.0) > 0:
                        actual_exit_price = float(res["avg_price"])
                        exit_price_verified = True
                    elif res.get("fill_price_verified") is False:
                        exit_price_verified = False
                    # By contract liquidar_posicion returns OK only after Schwab
                    # confirmed flat.  Keep compatibility with older test doubles
                    # that return OK without the explicit ``posicion_plana`` field.
                    exit_confirmed = bool(
                        res.get(
                            "posicion_plana",
                            res.get("status") in ("OK", "SIMULADA"),
                        )
                    )
                    if exit_confirmed:
                        self._marcar_liquidado(ticker)
                        # Race: _cantidad_real_broker saw shares, but
                        # liquidar_posicion re-read Schwab after the protective STOP
                        # had already flattened the account and therefore returned
                        # OK/FLAT with no order id. Recover the tracked STOP evidence.
                        if not close_order_id:
                            recovered = self._recuperar_cierre_stop_llenado(
                                ticker, pos, max(1, int(cant_rest or 1)),
                                sl_cancel_result,
                            )
                            if recovered:
                                close_order_id = str(
                                    recovered.get("order_id", "") or ""
                                )
                                recovered_price = float(
                                    recovered.get("avg_price", 0) or 0
                                )
                                if recovered_price > 0:
                                    actual_exit_price = recovered_price
                                    exit_price_verified = True

            # Un STOP real ya FILLED no necesita otra orden de liquidación. Aun así
            # se exige confirmar que Schwab quedó plano antes de liberar el estado.
            if stop_real_ya_llenado:
                close_order_id = str(pos.get("sl_order_id", "") or "")
                if close_order_id:
                    detail = self.ejecutor_hibrido.detalle_orden(
                        self.account_number, close_order_id
                    )
                    if float(detail.get("avg_price", 0.0) or 0.0) > 0:
                        actual_exit_price = float(detail["avg_price"])
                        exit_price_verified = True
                    filled = str(detail.get("status", "")).upper() == "FILLED"
                    broker_remaining = self._cantidad_real_broker(ticker, tipo) if filled else None
                    exit_confirmed = bool(filled and broker_remaining == 0)
                else:
                    exit_confirmed = False

            # Nunca convertir un cierre REAL no confirmado en un cierre local.
            if not exit_confirmed:
                self._emit_log(
                    "ERROR",
                    f"[{ticker}] Cierre REAL NO confirmado en Schwab ({motivo}); "
                    "la posición permanece activa en el motor.",
                )
                self._audit_event(
                    "POSITION_CLOSE_FAILED", ticker=ticker, lado=tipo,
                    estado="BROKER_NOT_FLAT", order_id=close_order_id,
                    cantidad=cant_rest, precio=precio_salida,
                    motivo="Schwab no confirmó posición plana",
                )
                return False

        if paper_ticker:
            close_order_id = f"SIM-CLOSE-{pos.get('position_id', ticker)[:16]}"
            exit_confirmed = True
        elif exit_confirmed and not close_order_id:
            # Keep integrity strict: confirmed-flat is necessary but not sufficient
            # for REAL model training.  Missing broker order identity remains an
            # explicit quarantine reason instead of being silently invented.
            self._audit_event(
                "EXIT_ORDER_ID_UNRESOLVED",
                ticker=ticker,
                lado=tipo,
                estado="BROKER_FLAT_NO_ORDER_ID",
                cantidad=cant_rest,
                precio=actual_exit_price,
                motivo=motivo,
            )

        # PnL del remanente. El parcial 70% ya fue agregado al acumulado diario
        # cuando se ejecutó TP1; aquí se suma SOLO el remanente para no duplicarlo.
        pnl_resto = (actual_exit_price - entrada) * cant_rest if tipo == "BUY" \
            else (entrada - actual_exit_price) * cant_rest
        pnl_total = pos["pnl_realizado"] + pnl_resto
        self._registrar_pnl_realizado_diario(
            pnl_resto, ticker=ticker, motivo=motivo or "cierre"
        )

        # Confirmado (o cierre natural por SL/TP, o modo paper): transición
        # explícita LONG/SHORT -> OUT. Un desfase nunca se oculta a la IA.
        transition_ok = True
        try:
            transition = self._state_machine().close(
                ticker,
                expected_state=position_state,
                position_id=str(pos.get("position_id", "") or ""),
            )
        except InvalidPositionTransition as exc:
            transition_ok = False
            snapshot = self._state_machine().snapshot(ticker)
            if snapshot.state != PositionState.OUT:
                try:
                    self._state_machine().close(ticker)
                except InvalidPositionTransition:
                    pass
            self._audit_event(
                "POSITION_STATE_MISMATCH", ticker=ticker, lado=tipo,
                estado="BROKER_FLAT_LOCAL_RECONCILED", order_id=close_order_id,
                state_before=snapshot.state.value,
                state_after=PositionState.OUT.value,
                position_side=position_state.value,
                order_action=exit_action_for_state(position_state),
                motivo=str(exc), **{
                    key: value for key, value in lifecycle_fields.items()
                    if key not in {"state_before", "state_after", "position_side", "order_action"}
                },
            )

        # The broker is already confirmed flat; release the tracked position.
        with self._lock:
            self._posiciones.pop(ticker, None)

        # Salida efectiva (para el R-múltiple del aprendizaje, refleja el 70/30)
        if cantidad_total > 0:
            eff_exit = entrada + (pnl_total / cantidad_total) if tipo == "BUY" \
                else entrada - (pnl_total / cantidad_total)
        else:
            eff_exit = actual_exit_price

        self.motor_señales.registrar_salida(ticker)
        close_fields = {
            **lifecycle_fields,
            "state_before": position_state.value,
            "state_after": PositionState.OUT.value,
            "position_side": position_state.value,
            "order_action": exit_action_for_state(position_state),
        }
        history_row = self._history_event(
            "POSITION_CLOSE",
            modo=execution_mode,
            ticker=ticker,
            signal_side=close_fields.get("signal_side", ""),
            order_id=close_order_id,
            parent_order_id=str(pos.get("entry_order_id", "") or ""),
            entrada=entrada,
            salida=eff_exit,
            cantidad=cantidad_total,
            pnl_usd=pnl_total,
            motivo=(
                "REVERSAL_CLOSE" if close_fields.get("reversal_id")
                else motivo
            ),
            **{
                key: value for key, value in close_fields.items()
                if key != "signal_side"
            },
        )
        audit_row = self._audit_event(
            "POSITION_CLOSED", ticker=ticker, lado=tipo,
            estado="CLOSED", cantidad=cantidad_total,
            precio=eff_exit, pnl_usd=pnl_total, motivo=motivo,
            order_id=close_order_id,
            parent_order_id=str(pos.get("entry_order_id", "") or ""),
            **close_fields,
            detalle={
                "entrada": entrada,
                "salida_solicitada": precio_salida,
                "salida_real": actual_exit_price,
                "tp1_ejecutado": bool(pos.get("tp1_hit")),
                "broker_confirmed": bool(exit_confirmed),
                "fill_price_verified": bool(exit_price_verified),
            },
        )
        close_integrity = {
            "ticker": ticker,
            **close_fields,
            "modo": execution_mode,
            "exit_action": exit_action_for_state(position_state),
            "exit_order_id": close_order_id,
            "exit_confirmed": bool(
                exit_confirmed and transition_ok
                and (paper_ticker or exit_price_verified)
            ),
            "fill_price_verified": bool(exit_price_verified),
            "audit_linked": bool(audit_row),
            "history_linked": bool(history_row),
        }
        try:
            try:
                self.orquestador.al_cerrar(
                    ticker, eff_exit, lifecycle=close_integrity
                )
            except TypeError:
                # Compatibility with lightweight test doubles from earlier builds.
                self.orquestador.al_cerrar(ticker, eff_exit)
        except Exception as e:
            self._emit_log("WARN", f"[{ticker}] Aprendizaje: {e}")

        self._emit_trade(tipo, ticker, actual_exit_price, cant_rest, "CLOSED", pnl_total)
        self._emit_log(
            "INFO",
            f"[CIERRE {motivo}] {ticker} @ ${actual_exit_price:.2f} | "
            f"PnL total ${pnl_total:.2f} | estado {position_state.value}->OUT",
        )
        # Una pérdida realizada puede tocar el límite aunque ya no haya posición
        # en este ticker. La activación liquidará de inmediato los demás símbolos.
        self._evaluar_limite_perdida_diaria()
        return True

    def _registrar_operacion_historial(self, ticker, tipo, entrada, salida,
                                       cantidad, pnl, motivo):
        """Backward-compatible close writer used by older integrations/tests."""
        state = PositionState.LONG if tipo == "BUY" else PositionState.SHORT
        return self._history_event(
            "POSITION_CLOSE",
            modo="PAPER" if self._is_paper_ticker(ticker) else "REAL",
            ticker=ticker,
            state_before=state.value,
            state_after=PositionState.OUT.value,
            position_side=state.value,
            order_action=exit_action_for_state(state),
            entrada=entrada,
            salida=salida,
            cantidad=cantidad,
            pnl_usd=pnl,
            motivo=motivo,
        )

    def _gestionar_real(self, ticker: str, pos: dict, precio_actual: float):
        """
        Monitor en modo real: detecta el llenado del TP1 (70%) para mover el
        30% a breakeven, y el cierre por SL/TP final para finalizar el registro.
        """
        if not self._activo:
            return
        with self._lock:
            if self._posiciones.get(ticker) is not pos or pos.get("pendiente"):
                return
        if not self._asegurar_token_trading():
            return

        # v3.0.2 — reconcile an HTTP-accepted STOP before any replacement.
        # Never convert "ack not visible yet" into a second protective order.
        if pos.get("sl_order_id") and pos.get("sl_reconcile_pending"):
            sl_id = str(pos.get("sl_order_id") or "")
            working = self.ejecutor_hibrido.orden_trabajando(
                self.account_number, sl_id
            )
            if working is True:
                with self._lock:
                    pos["sl_reconcile_pending"] = False
                self._audit_event(
                    "STOP_RECONCILED_WORKING", ticker=ticker,
                    lado=pos.get("tipo", ""), estado="WORKING",
                    order_id=sl_id,
                    motivo="order_id confirmado en snapshot WORKING",
                )
            elif working is False:
                detail = self.ejecutor_hibrido.detalle_orden(
                    self.account_number, sl_id
                ) or {}
                detail_status = str(detail.get("status", "") or "").upper()
                if detail_status == "FILLED":
                    self._cerrar_posicion(
                        ticker,
                        float(detail.get("avg_price", 0) or pos["sl_actual"]),
                        motivo="SL(real)",
                    )
                    return
                broker_qty = self._cantidad_real_broker(ticker, pos["tipo"])
                if broker_qty is None:
                    return
                if broker_qty == 0:
                    self._cerrar_posicion(
                        ticker, precio_actual,
                        motivo="reconciliación STOP: broker plano",
                    )
                    return
                # The order is not working and the position still exists. It is now
                # safe to forget this stale id; the guarded repair below will first
                # confirm the symbol has no working orders before placing ONE STOP.
                with self._lock:
                    pos["sl_order_id"] = ""
                    pos["sl_reconcile_pending"] = False
                self._audit_event(
                    "STOP_RECONCILE_STALE_ID", ticker=ticker,
                    lado=pos.get("tipo", ""), estado=detail_status or "NOT_WORKING",
                    order_id=sl_id, cantidad=broker_qty,
                    motivo="stop no working; broker position remains",
                )
            else:
                # No authoritative WORKING snapshot: preserve the id and do nothing.
                # A blind retry here is exactly what created the Aug-24 duplicates.
                return

        # Position without a tracked STOP id: first prove the symbol is free of
        # every current broker order, then re-read the real position and place a
        # single replacement. This also repairs legacy/readopted positions safely.
        if not pos.get("sl_order_id"):
            if not self._asegurar_ticker_limpio(ticker):
                self._emit_log(
                    "ERROR",
                    f"[{ticker}] Sin STOP identificado y no se pudo confirmar "
                    "limpieza de órdenes; NO se envía un STOP duplicado.",
                )
                return
            broker_qty = self._cantidad_real_broker(ticker, pos["tipo"])
            if broker_qty is None:
                return
            if broker_qty == 0:
                self._cerrar_posicion(
                    ticker, precio_actual,
                    motivo="reconciliación: broker plano sin STOP local",
                )
                return
            cant_stop = int(broker_qty)
            sl = self.ejecutor_hibrido.colocar_salida(
                ticker, pos["tipo"], cant_stop, pos["sl_actual"],
                "STOP", self.account_number
            )
            new_id = self._submitted_exit_order_id(sl)
            if new_id:
                with self._lock:
                    pos["sl_order_id"] = new_id
                    pos["sl_reconcile_pending"] = (
                        str((sl or {}).get("status", "") or "").upper()
                        == "RECONCILIAR"
                    )
                self._emit_log(
                    "WARN",
                    f"[{ticker}] STOP recolocado una sola vez ({new_id})"
                )
            else:
                self._emit_log(
                    "ERROR",
                    f"[{ticker}] Sin STOP y no se pudo colocar uno identificado — "
                    "cerrando por seguridad",
                )
                self._cerrar_posicion(
                    ticker, precio_actual, motivo="sin proteccion"
                )
                return

        # If a 3-share partial plan was deferred because the STOP had not become
        # visible yet, place TP1 only after the STOP is confirmed WORKING.
        if (
            pos.get("salida_parcial")
            and not pos.get("tp1_hit")
            and not pos.get("tp1_order_id")
            and pos.get("sl_order_id")
            and not pos.get("sl_reconcile_pending")
        ):
            orden = pos.get("orden")
            cantidad_parcial = int(getattr(orden, "cantidad_parcial", 0) or 0)
            if cantidad_parcial > 0:
                tp1 = self.ejecutor_hibrido.colocar_salida(
                    ticker, pos["tipo"], cantidad_parcial, pos["tp1"],
                    "LIMIT", self.account_number
                )
                tp1_id = self._submitted_exit_order_id(tp1)
                if tp1_id:
                    with self._lock:
                        pos["tp1_order_id"] = tp1_id
                        pos["tp1_reconcile_pending"] = (
                            str((tp1 or {}).get("status", "") or "").upper()
                            == "RECONCILIAR"
                        )

        # TP1 llenado → mover a breakeven una vez
        if pos["salida_parcial"] and not pos["tp1_hit"] and pos["tp1_order_id"]:
            if self.ejecutor_hibrido._estado_orden(self.account_number, pos["tp1_order_id"]) == "FILLED":
                self._ejecutar_parcial(ticker, pos)
                return
        # SL/remanente llenado → finalizar
        if pos["sl_order_id"]:
            if self.ejecutor_hibrido._estado_orden(self.account_number, pos["sl_order_id"]) == "FILLED":
                self._cerrar_posicion(ticker, pos["sl_actual"], motivo="SL(real)")
                return

        # Tras TP1 el 30% restante conserva el STOP de breakeven, pero el objetivo
        # final se ejecuta desde el motor al observar el precio. El cierre usa la
        # misma secuencia estricta: cancelar stop -> confirmar -> MARKET -> plano.
        objetivo = float(pos.get("tp_final", 0) or 0)
        if pos.get("tp1_hit") and objetivo > 0 and precio_actual > 0:
            alcanzado = (
                pos["tipo"] == "BUY" and precio_actual >= objetivo
            ) or (
                pos["tipo"] == "SELL" and precio_actual <= objetivo
            )
            if alcanzado:
                self._cerrar_posicion(ticker, objetivo, motivo="TP final")

    # ==========================================================
    # KILL SWITCH
    # ==========================================================

    def _asegurar_token_trading(self, forzar: bool = False) -> bool:
        """Keep the authenticated USER OAuth token ready for REAL orders.

        v3.3.1 also rate-limits outage messages. During an OAuth interruption the
        same condition can be observed by every ticker and every engine cycle;
        logging each observation obscures the first actionable failure.
        """
        if not self._has_real_tickers():
            return True

        def _auth_failure(message: str) -> bool:
            now_mono = time.monotonic()
            should_log = False
            with self._trading_auth_state_lock:
                if (
                    not self._trading_auth_failure_active
                    or now_mono - self._last_trading_auth_error_log_monotonic
                    >= self._trading_auth_error_log_interval_sec
                ):
                    should_log = True
                    self._last_trading_auth_error_log_monotonic = now_mono
                self._trading_auth_failure_active = True
            self._trading_listo = False
            self.ejecutor_hibrido.set_token(None)
            if should_log:
                self._emit_log("ERROR", message)
            return False

        try:
            if forzar and self.schwab.refresh_token:
                token_ok = self.schwab._asegurar_token(forzar_refresh=True)
            else:
                token_ok = self.schwab._asegurar_token()
            token_ok = bool(
                token_ok
                and getattr(self.schwab, "token_es_usuario", False)
                and self.schwab.access_token
            )
            if not token_ok:
                return _auth_failure(
                    "Token de TRADING no disponible (el token de usuario caducó). "
                    "REAL queda bloqueado y PAPER queda en pausa de datos reales "
                    "hasta reconectar Schwab."
                )

            recovered = False
            with self._trading_auth_state_lock:
                recovered = self._trading_auth_failure_active
                self._trading_auth_failure_active = False
                self._last_trading_auth_error_log_monotonic = 0.0
            self.ejecutor_hibrido.set_token(self.schwab.access_token)
            if recovered:
                self._emit_log(
                    "INFO",
                    "OAuth Schwab recuperado: datos reales y ejecución REAL vuelven "
                    "a estar disponibles según los demás controles de seguridad.",
                )
            return True
        except Exception as e:
            return _auth_failure(
                f"Token de trading: {e} — puede requerir reconectar Schwab"
            )

    def _preparar_trading_real(self):
        """
        Prepara las credenciales de trading en modo real: token de usuario fresco
        y HASH cifrado de la cuenta (ambos requeridos por la API de órdenes de
        Schwab). El número de cuenta plano solo sirve para datos de mercado.
        """
        if not self._has_real_tickers() or not self.account_number:
            return
        # Guardar el número de cuenta PLANO original (para poder reintentar la
        # resolución del hash aunque self.account_number ya haya cambiado).
        if not getattr(self, "_account_plain", None):
            self._account_plain = self.account_number
        # 1) Token de usuario fresco
        if not self._asegurar_token_trading(forzar=True):
            return
        # 2) Hash cifrado de la cuenta para la API de trading
        try:
            h = self.schwab.obtener_account_hash(self._account_plain)
        except Exception as e:
            h = None
            self._emit_log("ERROR", f"No se pudo resolver el hash de cuenta: {e}")
        if h:
            self.account_number = h   # a partir de aquí se usa el hash en la API
            # Listo para trading SOLO si hay hash Y un token de USUARIO válido (un
            # token de datos de mercado no sirve para órdenes). El hash resuelto NO
            # debe habilitar el trading si el token de usuario caducó.
            token_ok = bool(getattr(self.schwab, "token_es_usuario", False))
            self._trading_listo = token_ok
            if token_ok:
                self._emit_log("INFO", f"Cuenta lista para trading (hash …{h[-6:]})")
            else:
                self._emit_log("ERROR", "Hash OK pero SIN token de USUARIO válido — trading "
                                        "bloqueado hasta reconectar Schwab.")
        else:
            # Sin hash NO se puede operar en real: los endpoints de órdenes usan el
            # hash cifrado, no el número plano. Se bloquean las órdenes reales hasta
            # resolverlo (se reintenta antes de cada apertura), en vez de mandar
            # órdenes que el broker rechazaría con el número plano.
            self._trading_listo = False
            self._emit_log("ERROR", "No se resolvió el hash de la cuenta — NO se enviarán "
                                    "órdenes reales hasta resolverlo (se reintenta en cada apertura)")

    def _persist_broker_reconciliation(self) -> None:
        path = None
        tmp = None
        try:
            path = self._broker_reconciliation_state_path
            if not os.path.isabs(path):
                path = os.path.join(os.getcwd(), path)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            payload = dict(getattr(self, "_broker_reconciliation", {}) or {})
            payload["last_order_failures"] = dict(
                getattr(self, "_last_order_failures", {}) or {}
            )
            # v3.3.1: never let concurrent worker/kill-switch writes share the
            # same '.tmp' filename.  Windows refuses os.replace when the other
            # writer still has that file open, which left reconciliation stale.
            with self._broker_reconciliation_write_lock:
                tmp = (
                    f"{path}.{os.getpid()}.{threading.get_ident()}."
                    f"{uuid.uuid4().hex}.tmp"
                )
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(
                        payload, fh, ensure_ascii=False, indent=2, sort_keys=True
                    )
                    fh.flush()
                    try:
                        os.fsync(fh.fileno())
                    except OSError:
                        pass
                os.replace(tmp, path)
                tmp = None
        except Exception as exc:
            self._emit_log(
                "WARN", f"No se pudo persistir reconciliación broker: {exc}"
            )
        finally:
            if tmp:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass

    def _set_broker_reconciliation(
        self, *, ok: bool, flat: bool, context: str, message: str,
        positions=None, open_order_symbols=None,
    ) -> dict:
        self._broker_reconciliation_ok = bool(ok)
        self._broker_reconciliation = {
            "ok": bool(ok),
            "flat": bool(flat),
            "context": str(context or ""),
            "message": str(message or ""),
            "positions": list(positions or []),
            "open_order_symbols": sorted(set(open_order_symbols or [])),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "last_order_failures": dict(
                getattr(self, "_last_order_failures", {}) or {}
            ),
        }
        self._persist_broker_reconciliation()
        return dict(self._broker_reconciliation)

    def broker_reconciliation_snapshot(self) -> dict:
        snap = dict(getattr(self, "_broker_reconciliation", {}) or {})
        snap["last_order_failures"] = dict(
            getattr(self, "_last_order_failures", {}) or {}
        )
        return snap

    def synchronize_stopped_real_positions_from_broker(self, broker_state: dict) -> dict:
        """Retire stale local REAL rows after a successful read-only broker proof.

        v3.3.3: Schwab is authoritative for REAL exposure.  When the engine is
        stopped and a successful reconciliation proves a ticker has neither a
        broker position nor a WORKING order, a leftover in-memory REAL position
        is stale and may be retired locally.  This method never sends, cancels,
        or replaces an order and never touches PAPER positions.
        """
        result = {
            "applied": False,
            "retired": [],
            "kept": [],
            "reason": "",
        }
        state = dict(broker_state or {})
        if bool(getattr(self, "_activo", False)):
            result["reason"] = "ENGINE_ACTIVE"
            return result
        if state.get("ok") is False:
            result["reason"] = "BROKER_NOT_CONFIRMED"
            return result

        broker_positions = {
            str(row.get("ticker", "") or "").upper()
            for row in (state.get("positions") or [])
            if isinstance(row, dict) and str(row.get("ticker", "") or "").strip()
        }
        working_symbols = {
            str(symbol or "").upper()
            for symbol in (state.get("open_order_symbols") or [])
            if str(symbol or "").strip()
        }

        retired_rows = []
        with getattr(self, "_lock", threading.RLock()):
            for ticker, pos in list(getattr(self, "_posiciones", {}).items()):
                if not isinstance(pos, dict):
                    continue
                symbol = str(ticker or "").upper()
                # Deliberately conservative: only explicitly REAL positions are
                # eligible.  Legacy/unknown and PAPER rows are left untouched.
                if str(pos.get("execution_mode", "") or "").upper() != "REAL":
                    continue
                if symbol in broker_positions or symbol in working_symbols:
                    result["kept"].append(symbol)
                    continue
                retired_rows.append((symbol, dict(pos)))
                self._posiciones.pop(ticker, None)

        for symbol, pos in retired_rows:
            try:
                snap = self._state_machine().snapshot(symbol)
                if snap.state != PositionState.OUT:
                    self._state_machine().close(
                        symbol,
                        position_id=str(pos.get("position_id", "") or ""),
                    )
            except Exception:
                # The broker-flat proof is stronger than an old local lifecycle
                # registry.  A stale state-machine row must not re-create a trade.
                try:
                    self._state_machine()._states.pop(symbol, None)
                except Exception:
                    pass

            manager = getattr(self, "real_capacity", None)
            if manager is not None:
                try:
                    manager.release(symbol)
                except Exception:
                    pass

            pending_retired = False
            executor = getattr(self, "ejecutor_hibrido", None)
            retire_pending = getattr(
                executor, "retire_pending_exit_after_authoritative_flat", None
            )
            if callable(retire_pending) and getattr(self, "account_number", ""):
                try:
                    pending_retired = bool(retire_pending(
                        self.account_number, symbol,
                        "Schwab read-only reconciliation confirmed no position and no WORKING order",
                    ))
                except Exception as exc:
                    self._emit_log(
                        "WARN",
                        f"[{symbol}] No se pudo retirar journal de salida stale: {exc}",
                    )

            failures = getattr(self, "_last_order_failures", None)
            if isinstance(failures, dict):
                failures.pop(symbol, None)

            try:
                self._audit_event(
                    "LOCAL_REAL_POSITION_RETIRED_BROKER_FLAT",
                    ticker=symbol,
                    estado="BROKER_FLAT",
                    cantidad=int(pos.get("cantidad_resto", pos.get("cantidad", 0)) or 0),
                    motivo="Schwab confirmó sin posición ni orden WORKING; estado REAL local retirado",
                    detalle={
                        "read_only": True,
                        "pending_exit_retired": pending_retired,
                        "previous_local_status": (
                            "PENDING" if pos.get("pendiente") else "OPEN"
                        ),
                    },
                )
            except Exception:
                pass
            self._emit_log(
                "INFO",
                f"[{symbol}] Reconciliación manual: Schwab sin exposición/orden WORKING; "
                "posición REAL local stale retirada",
            )
            result["retired"].append(symbol)

        if result["retired"]:
            result["applied"] = True
            result["reason"] = "BROKER_AUTHORITATIVE_SYNC"
            current = dict(getattr(self, "_broker_reconciliation", {}) or state)
            current["last_order_failures"] = dict(
                getattr(self, "_last_order_failures", {}) or {}
            )
            suffix = "Estado local REAL sincronizado: " + ", ".join(result["retired"])
            base_message = str(current.get("message", "") or "").strip()
            if suffix not in base_message:
                current["message"] = (base_message + "; " + suffix).strip("; ")
            self._broker_reconciliation = current
            try:
                self._persist_broker_reconciliation()
            except Exception:
                pass
        else:
            result["reason"] = "NO_STALE_REAL_POSITIONS"
        return result

    @staticmethod
    def _iter_broker_orders(orders):
        for order in orders or []:
            if not isinstance(order, dict):
                continue
            yield order
            children = order.get("childOrderStrategies", []) or []
            yield from ChocoletraBot._iter_broker_orders(children)

    @staticmethod
    def _symbol_from_broker_order(order: dict) -> str:
        try:
            for candidate in ChocoletraBot._iter_broker_orders([order]):
                for leg in candidate.get("orderLegCollection", []) or []:
                    symbol = str((leg.get("instrument") or {}).get("symbol", "") or "").upper()
                    if symbol:
                        return symbol
        except Exception:
            pass
        return ""

    def _broker_retry_settings(self):
        cfg = getattr(self, "config", {}) or {}
        live = (cfg.get("live_trading", {}) or {})
        attempts = max(1, int(live.get("reconciliation_retry_attempts", 3) or 3))
        delay = max(0.0, float(live.get("reconciliation_retry_delay_sec", 0.75) or 0.75))
        return attempts, delay

    def _broker_read_with_retry(self, label: str, callback):
        """Read broker state with bounded retry; an empty list is a valid success."""
        attempts, delay = self._broker_retry_settings()
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                value = callback()
                if value is None:
                    raise RuntimeError("respuesta vacía")
                if attempt > 1:
                    self._emit_log(
                        "INFO",
                        f"[Schwab] {label} recuperado en intento {attempt}/{attempts}",
                    )
                return True, value, ""
            except Exception as exc:
                last_error = str(exc)
                if attempt < attempts:
                    self._emit_log(
                        "WARN",
                        f"[Schwab] {label} falló intento {attempt}/{attempts}: {exc}; reintentando",
                    )
                    time.sleep(delay)
        return False, None, last_error or "sin respuesta"

    def _refresh_broker_reconciliation(
        self, *, context: str, require_flat: bool = False
    ) -> dict:
        """Refresh authoritative REAL account state without mutating positions.

        Query failures are fail-closed.  ``require_flat`` is used by the kill
        switch/end-of-session path; startup can be synchronized even when an
        already-open position is safely readopted.
        """
        real_tickers = set(self._real_tickers(self.tickers))
        if not real_tickers or not self.account_number:
            return self._set_broker_reconciliation(
                ok=True, flat=True, context=context,
                message="Sin tickers REAL activos", positions=[],
                open_order_symbols=[],
            )
        ok_pos, positions_raw, err_pos = self._broker_read_with_retry(
            "posiciones",
            lambda: self.schwab.obtener_posiciones(self.account_number),
        )
        if not ok_pos:
            return self._set_broker_reconciliation(
                ok=False, flat=False, context=context,
                message=f"No se pudo confirmar Schwab posiciones: {err_pos}",
                positions=[], open_order_symbols=[],
            )
        ok_ord, orders_raw, err_ord = self._broker_read_with_retry(
            "órdenes abiertas",
            lambda: self.schwab.obtener_ordenes_abiertas(self.account_number),
        )
        if not ok_ord:
            return self._set_broker_reconciliation(
                ok=False, flat=False, context=context,
                message=f"No se pudo confirmar Schwab órdenes: {err_ord}",
                positions=[], open_order_symbols=[],
            )

        positions = []
        for pos in positions_raw or []:
            ticker = str(pos.get("symbol", "") or "").upper()
            qty = int(pos.get("cantidad", 0) or 0)
            side = str(pos.get("tipo", "") or "").upper()
            if ticker in real_tickers and qty > 0 and side in ("BUY", "SELL"):
                positions.append({"ticker": ticker, "side": side, "quantity": qty})
        order_symbols = []
        for order in self._iter_broker_orders(orders_raw):
            symbol = self._symbol_from_broker_order(order)
            if symbol in real_tickers:
                order_symbols.append(symbol)

        flat = not positions and not order_symbols
        ok = bool(flat) if require_flat else True
        message = (
            "Broker confirmado plano" if flat else
            ("Exposición/órdenes REAL siguen abiertas" if require_flat else
             "Broker sincronizado; exposición REAL conocida")
        )
        return self._set_broker_reconciliation(
            ok=ok, flat=flat, context=context, message=message,
            positions=positions, open_order_symbols=order_symbols,
        )

    def _confirm_real_ticker_flat_before_entry(self, ticker: str) -> bool:
        """Fail closed if Schwab still has exposure in this OUT ticker."""
        ticker = str(ticker or "").upper()
        if not getattr(self, "_broker_reconciliation_ok", True):
            # v2.9.7: a transient startup/API read must not create a permanent
            # stale lock. Retry a fresh authoritative reconciliation immediately
            # before the entry. Persistent failure still blocks REAL fail-closed.
            recovered = self._refresh_broker_reconciliation(
                context="PRE_ENTRY_RECOVERY", require_flat=False
            )
            if not bool(recovered.get("ok")):
                self._emit_log(
                    "ERROR",
                    f"[{ticker}] Entrada REAL bloqueada: Schwab sigue sin reconciliar "
                    f"({recovered.get('message', 'estado desconocido')})",
                )
                return False
            self._emit_log(
                "INFO",
                f"[{ticker}] Reconciliación Schwab recuperada antes de la entrada REAL",
            )

        ok_pos, positions, err_pos = self._broker_read_with_retry(
            f"posición {ticker}",
            lambda: self.schwab.obtener_posiciones(self.account_number),
        )
        if not ok_pos:
            self._set_broker_reconciliation(
                ok=False, flat=False, context="PRE_ENTRY",
                message=f"No se pudo confirmar posición de {ticker}: {err_pos}",
                positions=[], open_order_symbols=[],
            )
            return False
        for pos in positions or []:
            if str(pos.get("symbol", "") or "").upper() != ticker:
                continue
            qty = int(pos.get("cantidad", 0) or 0)
            if qty > 0:
                side = str(pos.get("tipo", "") or "").upper()
                self._set_broker_reconciliation(
                    ok=False, flat=False, context="PRE_ENTRY",
                    message=f"{ticker} no está plano en Schwab ({side} {qty})",
                    positions=[{"ticker": ticker, "side": side, "quantity": qty}],
                    open_order_symbols=[],
                )
                return False
        return True

    def _reconciliar_al_iniciar(self):
        """
        Tras un reinicio, consulta las posiciones REALES en Schwab y readopta
        las que el bot no conoce, para no quedar ciego. Las protecciones (SL/TP)
        siguen vivas en el broker aunque el bot se haya reiniciado; aquí se
        recupera el control (kill switch, seguimiento del stop).
        """
        if not self._has_real_tickers() or not self.account_number:
            self._set_broker_reconciliation(
                ok=True, flat=True, context="STARTUP",
                message="Sin tickers REAL o sin cuenta configurada",
                positions=[], open_order_symbols=[],
            )
            return True
        ok_pos, posiciones, err_pos = self._broker_read_with_retry(
            "posiciones al iniciar",
            lambda: self.schwab.obtener_posiciones(self.account_number),
        )
        if not ok_pos:
            self._emit_log(
                "ERROR",
                f"Reconciliación inicial sin respuesta fiable de posiciones: {err_pos}",
            )
            self._set_broker_reconciliation(
                ok=False, flat=False, context="STARTUP",
                message=f"Schwab no confirmó posiciones tras reintentos: {err_pos}",
                positions=[], open_order_symbols=[],
            )
            return False
        gestionados = {str(t).upper() for t in self._real_tickers(self.tickers)}
        posiciones = [
            p for p in posiciones
            if str(p.get("symbol") or "").upper() in gestionados
        ]
        ok_ord, ordenes, err_ord = self._broker_read_with_retry(
            "órdenes abiertas al iniciar",
            lambda: self.schwab.obtener_ordenes_abiertas(self.account_number),
        )
        if not ok_ord:
            self._emit_log("ERROR", f"Reconciliación de órdenes falló: {err_ord}")
            self._set_broker_reconciliation(
                ok=False, flat=False, context="STARTUP",
                message=f"Consulta de órdenes abiertas falló tras reintentos: {err_ord}",
                positions=[{
                    "ticker": str(p.get("symbol", "") or "").upper(),
                    "side": str(p.get("tipo", "") or "").upper(),
                    "quantity": int(p.get("cantidad", 0) or 0),
                } for p in posiciones],
                open_order_symbols=[],
            )
            return False

        flat_ordenes = list(self._iter_broker_orders(ordenes))
        managed_order_symbols = [
            self._symbol_from_broker_order(o) for o in flat_ordenes
        ]
        managed_order_symbols = [
            s for s in managed_order_symbols if s in gestionados
        ]
        if not posiciones:
            if managed_order_symbols:
                self._emit_log(
                    "ERROR",
                    "RECONCILIACIÓN: órdenes REAL abiertas sin posición conocida: "
                    + ", ".join(sorted(set(managed_order_symbols))),
                )
                self._set_broker_reconciliation(
                    ok=False, flat=False, context="STARTUP",
                    message="Órdenes REAL abiertas sin posición; requiere revisión",
                    positions=[], open_order_symbols=managed_order_symbols,
                )
                return False
            self._set_broker_reconciliation(
                ok=True, flat=True, context="STARTUP",
                message="Schwab confirmado plano al iniciar", positions=[],
                open_order_symbols=[],
            )
            return True

        self._emit_log("WARN", f"RECONCILIACIÓN: {len(posiciones)} posición(es) abierta(s) "
                               f"en Schwab al arrancar — readoptando para no quedar ciego")

        for p in posiciones:
            ticker, tipo, cant, entrada = p["symbol"], p["tipo"], p["cantidad"], p["precio"]
            sl_id, tp1_id, sl_price = "", "", entrada
            for o in ordenes:
                try:
                    leg = o.get("orderLegCollection", [{}])[0]
                    if leg.get("instrument", {}).get("symbol") != ticker:
                        continue
                    ot = str(o.get("orderType", "")).upper()
                    oid = str(o.get("orderId", ""))
                    if ot == "STOP":
                        sl_id = oid
                        sl_price = float(o.get("stopPrice", entrada) or entrada)
                    elif ot == "LIMIT":
                        tp1_id = oid
                except Exception:
                    continue

            orden_stub = OrdenTradingCalculada(
                ticker=ticker, tipo=tipo, precio_limite=round(entrada, 2),
                stop_loss=round(sl_price, 2), trailing_stop_pct=0.0, cantidad=cant,
                riesgo_usd=0.0, atr_actual=0.0, cantidad_resto=cant,
                modo_resto=self.gestor_riesgo.modo_resto, salida_parcial_activa=bool(tp1_id),
            )
            position_id = uuid.uuid4().hex
            execution_id = uuid.uuid4().hex
            state = state_for_signal(tipo)
            self._state_machine().adopt(ticker, tipo, position_id)
            with self._lock:
                self._posiciones[ticker] = {
                    "execution_mode": "REAL",
                    "position_state": state.value, "position_id": position_id,
                    "execution_id": execution_id, "signal_id": "",
                    "reversal_id": "", "correlation_id": execution_id,
                    "signal_side": "", "entry_order_id": "",
                    "entry_integrity": {"entry_confirmed": False},
                    "tipo": tipo, "cantidad": cant, "cantidad_resto": cant,
                    "entrada": entrada, "sl": sl_price, "sl_actual": sl_price,
                    "tp1": 0.0, "tp_final": 0.0, "tp1_hit": False,
                    "modo_resto": self.gestor_riesgo.modo_resto,
                    "salida_parcial": bool(tp1_id), "pnl_realizado": 0.0,
                    "orden": orden_stub, "decision": None,
                    "sl_order_id": sl_id, "tp1_order_id": tp1_id,
                    "abierta_ts": datetime.now(), "readoptada": True,
                }
            self._emit_log("WARN", f"[{ticker}] Readoptada: {tipo} {cant} @ ${entrada:.2f} "
                                   f"| SL {'recuperado' if sl_id else 'NO ENCONTRADO'} "
                                   f"| TP1 {'recuperado' if tp1_id else '-'}")

        missing_protection = [
            str(t).upper() for t, pos in self._posiciones.items()
            if str(pos.get("execution_mode", "")).upper() == "REAL"
            and pos.get("readoptada") and not pos.get("sl_order_id")
        ]
        broker_positions = [{
            "ticker": str(p.get("symbol", "") or "").upper(),
            "side": str(p.get("tipo", "") or "").upper(),
            "quantity": int(p.get("cantidad", 0) or 0),
        } for p in posiciones]
        if missing_protection:
            self._set_broker_reconciliation(
                ok=False, flat=False, context="STARTUP",
                message=(
                    "Posición REAL readoptada sin STOP confirmado: "
                    + ", ".join(sorted(missing_protection))
                ),
                positions=broker_positions,
                open_order_symbols=managed_order_symbols,
            )
            self._emit_log(
                "ERROR",
                "Nuevas entradas REAL bloqueadas hasta resolver protección/reconciliación",
            )
            return False
        self._set_broker_reconciliation(
            ok=True, flat=False, context="STARTUP",
            message="Posiciones REAL readoptadas y sincronizadas",
            positions=broker_positions,
            open_order_symbols=managed_order_symbols,
        )
        return True

    def _liquidar_todo(self, motivo: str):
        """
        Cierra TODAS las posiciones abiertas (kill switch). Reintenta hasta
        vaciar el estado (o agotar los intentos), porque una liquidación real
        puede fallar y dejar la posición viva.
        """
        max_attempts = max(1, int(getattr(self, "ks_max_liquidation_attempts", 5) or 5))
        retry_delay = max(0.0, float(getattr(self, "ks_retry_delay_sec", 1.0) or 0.0))
        for intento in range(1, max_attempts + 1):
            with self._lock:
                items = [(t, p) for t, p in self._posiciones.items() if not p.get("reservado")]
            if not items:
                break
            self._emit_log("WARN", f"KILL SWITCH ({motivo}) intento {intento}: "
                                   f"liquidando {len(items)} posiciones")
            for ticker, pos in items:
                if pos.get("pendiente"):
                    # Entrada sin llenar: cancelar la orden, no hay nada que liquidar
                    self._cancelar_pendiente(ticker)
                    continue
                fallback = pos.get("entrada")
                if fallback is None and pos.get("orden") is not None:
                    fallback = pos["orden"].precio_limite
                precio = self._precio_rapido(ticker, fallback or 0.0)
                self._cerrar_posicion(ticker, precio, motivo=motivo)
            if intento < max_attempts and retry_delay > 0:
                time.sleep(retry_delay)
        # Barrido FINAL contra el broker: liquidar cualquier posición real que siga
        # viva en un ticker del bot (p. ej. una entrada RECONCILIAR sin id que se
        # llenó TARDE y que 'posiciones' no reflejaba durante los reintentos, o
        # cualquier hueco que se soltó por error). El kill switch debe terminar
        # PLANO en el broker, no solo en el estado interno. Se limita al universo
        # de tickers del bot para no tocar posiciones ajenas de la cuenta.
        if self._has_real_tickers() and self.account_number:
            try:
                reales = self.schwab.obtener_posiciones(self.account_number)
            except Exception:
                reales = []
            ahora = datetime.now()
            for p in (reales or []):
                tk = p.get("symbol"); cant = int(p.get("cantidad", 0) or 0); tp = p.get("tipo")
                if (
                    not tk or cant <= 0 or tp not in ("BUY", "SELL")
                    or tk not in self._real_tickers(self.tickers)
                ):
                    continue
                # NO re-liquidar un ticker que el bucle de reintentos ACABA de cerrar
                # con MARKET: su orden se está asentando y 'posiciones' aún lo muestra
                # vivo un instante. Sin esta guarda se enviaría un SEGUNDO cierre y se
                # sobre-vendería (posición opuesta desnuda).
                ts_liq = self._liquidados_recientes.get(tk)
                if ts_liq and (ahora - ts_liq).total_seconds() < self.COOLDOWN_LIQUIDACION_SEG:
                    continue
                self._emit_log("WARN", f"[{tk}] Kill switch — barrido final: posición real viva "
                                       f"{tp} {cant}, liquidando para quedar plano")
                # Cancel every working order for the symbol before the emergency
                # close.  The close itself re-reads broker side/quantity.
                if not self._asegurar_ticker_limpio(tk):
                    self._emit_log(
                        "ERROR",
                        f"[{tk}] Kill switch: no se confirmó limpieza de órdenes; "
                        "no se envía un cierre potencialmente conflictivo",
                    )
                    continue
                res = self.ejecutor_hibrido.liquidar_posicion(
                    tk, tp, cant, self.account_number, motivo + " (barrido final)")
                # Solo darlo por cerrado si la liquidación fue ACEPTADA: un cierre que
                # falla NO debe reportarse como plano (dejaría acciones reales vivas).
                if res.get("status") in ("OK", "SIMULADA"):
                    self._marcar_liquidado(tk)
                    with self._lock:
                        self._posiciones.pop(tk, None)
                else:
                    self._emit_log("ERROR", f"[{tk}] Kill switch — barrido final FALLÓ "
                                            f"({res.get('status')}): posición real SIGUE viva. "
                                            f"Revisa el broker URGENTE.")
        # Aviso si algo quedó abierto tras los reintentos
        with self._lock:
            restantes = [t for t, p in self._posiciones.items() if not p.get("reservado")]
        if restantes:
            self._emit_log("ERROR", f"KILL SWITCH: quedaron posiciones sin liquidar: {restantes}")

        # v2.9.6 final authoritative confirmation.  Never report a successful
        # REAL kill switch from local state alone.  Persist the unresolved broker
        # exposure so it remains visible on the dashboard after the engine stops.
        broker_state = self._refresh_broker_reconciliation(
            context="KILL_SWITCH", require_flat=True
        )
        if not broker_state.get("ok"):
            self._emit_log(
                "ERROR",
                "CRITICAL — UNRESOLVED REAL EXPOSURE: "
                + str(broker_state.get("message", "Schwab no confirmado plano")),
            )
        return broker_state

    def dashboard_snapshot(self):
        """Return a JSON-safe, thread-safe snapshot for the dashboard.

        The trading engine remains the source of truth for active positions.  The
        dashboard previously inferred open positions from event rows (``OPEN``,
        ``PARTIAL`` and ``CLOSED``), which made historical entry events look like
        current positions and could display the wrong quantity after a 70/30 exit.
        This snapshot exposes the actual lifecycle state instead.
        """
        rows = []
        with self._lock:
            items = list(self._posiciones.items())
            last_prices = dict(getattr(self, "_last_prices", {}))

        for ticker, pos in items:
            if not isinstance(pos, dict) or pos.get("reservado"):
                continue

            order = pos.get("orden")
            initial_qty = int(
                pos.get("cantidad")
                or getattr(order, "cantidad", 0)
                or 0
            )
            planned_partial = int(getattr(order, "cantidad_parcial", 0) or 0)
            planned_remaining = int(
                getattr(order, "cantidad_resto", 0)
                or pos.get("cantidad_resto")
                or initial_qty
            )
            pending = bool(pos.get("pendiente"))
            tp1_hit = bool(pos.get("tp1_hit"))

            if pending:
                remaining_qty = initial_qty
                executed_partial = 0
                lifecycle = "PENDING"
                entry = float(getattr(order, "precio_limite", 0.0) or 0.0)
                stop_initial = float(getattr(order, "stop_loss", 0.0) or 0.0)
                stop_current = stop_initial
                tp1 = float(getattr(order, "take_profit_parcial", 0.0) or 0.0)
                final_target = float(getattr(order, "take_profit", 0.0) or 0.0)
                opened = pos.get("ts_pendiente")
            else:
                remaining_qty = int(
                    pos.get("cantidad_resto", initial_qty)
                    if tp1_hit else initial_qty
                )
                executed_partial = max(0, initial_qty - remaining_qty)
                lifecycle = "PARTIAL" if tp1_hit else "OPEN"
                entry = float(pos.get("entrada", 0.0) or 0.0)
                stop_initial = float(pos.get("sl", 0.0) or 0.0)
                stop_current = float(pos.get("sl_actual", stop_initial) or 0.0)
                tp1 = float(pos.get("tp1", 0.0) or 0.0)
                final_target = float(pos.get("tp_final", 0.0) or 0.0)
                opened = pos.get("abierta_ts")

            if not planned_partial and initial_qty > 0 and planned_remaining < initial_qty:
                planned_partial = initial_qty - planned_remaining

            opened_at = ""
            if opened is not None:
                try:
                    opened_at = opened.isoformat(timespec="seconds")
                except Exception:
                    opened_at = str(opened)

            partial_at = pos.get("partial_exit_at")
            partial_exit_at = ""
            if partial_at is not None:
                try:
                    partial_exit_at = partial_at.isoformat(timespec="seconds")
                except Exception:
                    partial_exit_at = str(partial_at)

            last_price = float(last_prices.get(ticker, 0.0) or 0.0)
            if pending or entry <= 0 or last_price <= 0:
                floating_pnl = 0.0
            elif str(pos.get("tipo") or getattr(order, "tipo", "")) == "BUY":
                floating_pnl = (last_price - entry) * remaining_qty
            else:
                floating_pnl = (entry - last_price) * remaining_qty

            rows.append({
                "ticker": str(ticker),
                "side": str(pos.get("tipo") or getattr(order, "tipo", "")),
                "position_state": (
                    "OUT" if pending else str(
                        pos.get("position_state")
                        or ("LONG" if str(pos.get("tipo") or getattr(order, "tipo", "")) == "BUY" else "SHORT")
                    )
                ),
                "position_id": str(pos.get("position_id", "")),
                "execution_id": str(pos.get("execution_id", "")),
                "signal_id": str(pos.get("signal_id", "")),
                "reversal_id": str(pos.get("reversal_id", "")),
                "correlation_id": str(pos.get("correlation_id", "")),
                "status": lifecycle,
                "initial_quantity": initial_qty,
                "remaining_quantity": remaining_qty,
                "partial_quantity_executed": executed_partial,
                "planned_partial_quantity": planned_partial,
                "planned_remaining_quantity": planned_remaining,
                "entry_price": round(entry, 4),
                "stop_initial": round(stop_initial, 4),
                "stop_current": round(stop_current, 4),
                "tp1_price": round(tp1, 4),
                "final_target": round(final_target, 4),
                "tp1_hit": tp1_hit,
                "breakeven_active": bool(
                    tp1_hit and str(pos.get("modo_resto", "")).lower() == "breakeven"
                ),
                "protection_mode": str(pos.get("modo_resto", "")),
                "realized_pnl": round(float(pos.get("pnl_realizado", 0.0) or 0.0), 2),
                "floating_pnl": round(floating_pnl, 2),
                "last_price": round(last_price, 4),
                "last_price_at": datetime.fromtimestamp(
                    float(getattr(self, "_last_price_times", {}).get(ticker, 0.0) or 0.0)
                ).isoformat(timespec="seconds") if float(
                    getattr(self, "_last_price_times", {}).get(ticker, 0.0) or 0.0
                ) > 0 else "",
                "planned_risk_usd": round(
                    float(getattr(order, "riesgo_usd", 0.0) or 0.0), 2
                ),
                "atr_at_entry": round(
                    float(getattr(order, "atr_actual", 0.0) or 0.0), 4
                ),
                "stop_loss_atr_factor": round(
                    float(getattr(order, "stop_loss_atr_factor", 0.0) or 0.0), 4
                ),
                "take_profit_atr_factor": round(
                    float(getattr(order, "take_profit_atr_factor", 0.0) or 0.0), 4
                ),
                "partial_exit_at": partial_exit_at,
                "partial_exit_price": round(
                    float(pos.get("partial_exit_price", 0.0) or 0.0), 4
                ),
                "opened_at": opened_at,
                "mode": str(pos.get("execution_mode") or (
                    "PAPER" if self._is_paper_ticker(ticker, pos=pos) else "REAL"
                )),
            })

        rows.sort(key=lambda row: (row.get("opened_at", ""), row.get("ticker", "")))
        return rows

    @staticmethod
    def _parse_hhmm(valor: str):
        from datetime import time as _t
        try:
            h, m = map(int, str(valor).split(":"))
            return _t(h, m)
        except Exception:
            return _t(16, 0)

    def _ahora_et(self) -> datetime:
        """Hora actual en la zona del mercado (America/New_York)."""
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            return datetime.now()

    def _es_hora_cierre(self) -> bool:
        """True si ya pasó la hora de cierre del mercado en un día laborable."""
        ahora = self._ahora_et()
        if ahora.weekday() >= 5:
            return False
        return ahora.time() >= self._hora_fin_mercado

    def _precio_rapido(self, ticker: str, fallback: float) -> float:
        """Trusted reference price for liquidation/accounting.

        REAL requests prefer Schwab. Cached synthetic fallback frames are never
        accepted because they can manufacture PnL even though the market order
        itself would execute independently at the broker.
        """
        try:
            is_paper = self._is_paper_ticker(ticker)
            if not is_paper:
                p = self.schwab.obtener_precio_actual(ticker)
                if p:
                    return float(p)
            ent = self._mtf_cache.get(ticker)
            if ent and not bool(getattr(ent[1], "sintetico", False)):
                df = ent[1].tf("5m")
                if df is None:
                    df = ent[1].tf("1m")
                if df is not None:
                    return float(df["close"].iloc[-1])
        except Exception:
            pass
        return fallback

    # ==========================================================
    # BACKTEST CON CSV (compatibilidad)
    # ==========================================================

    def probar_con_csv(self, ruta_csv: str, ticker: str = "TEST"):
        """Prueba la estrategia matemática con datos históricos de CSV."""
        import pandas as pd
        log_info(f"Cargando datos: {ruta_csv}")
        df = pd.read_csv(ruta_csv, index_col='datetime', parse_dates=True)
        df.columns = [c.lower() for c in df.columns]
        df_resultado = self.estrategia.calcular(df)
        señales = df_resultado[df_resultado['señal'].notna()]
        conteo = señales['señal'].value_counts()
        log_info(f"RESUMEN BACKTEST — {ticker}")
        for tipo, cantidad in conteo.items():
            log_info(f"  {tipo}: {cantidad}")
        return df_resultado
