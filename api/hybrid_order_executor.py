"""
=============================================================
MÓDULO: api/hybrid_order_executor.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Ejecutor Híbrido de Órdenes — Versión Inteligente.

    LÓGICA COMPLETA (según lo acordado con el cliente):

    1. TIPO DE ORDEN (LIMIT vs MARKET):
       - Si volatilidad ATR es BAJA  → LIMIT (evita slippage)
       - Si volatilidad ATR es ALTA  → MARKET (entra rápido antes de que el precio se escape)
       - En rango lateral detectado   → LIMIT con precio dentro del rango
       - En mercado lento             → LIMIT ajustado al bid más cercano

    2. TAMAÑO DE LA POSICIÓN (cantidad de acciones):
       - Señal NORMAL (probabilidad IA 60-79%)  → cantidad estándar
       - Señal EN RANGO (OM-R detectado)         → cantidad REDUCIDA (50%)
       - Señal ALTA PROBABILIDAD (IA >= 80%)     → cantidad DUPLICADA (200%)
       - Señal MAESTRA (4 factores confluentes)  → cantidad MÁXIMA (250%)

    3. PRECIO LÍMITE ADAPTATIVO:
       - Mercado trending:  precio actual + buffer (0.01%)
       - Mercado en rango:  precio dentro del rango (más conservador)
       - Alta volatilidad:  orden MARKET directa

    RESUMEN:
      "En rango o mercado lento → menos acciones + LIMIT dentro del rango"
      "Señal de alta probabilidad → más acciones + tipo de orden según volatilidad"
=============================================================
"""

import math
import os
import re
import time
import requests
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from dataclasses import dataclass
from dotenv import load_dotenv
from core.risk_manager import OrdenTradingCalculada
from api.order_sync import AsyncOrderSynchronizer, normalize_status, valid_order_snapshot

load_dotenv()


# Estados documentados/observados en el ciclo de órdenes de Schwab. Un 2xx al
# POST/DELETE solo confirma que la solicitud fue aceptada; la transición se da
# por completada exclusivamente cuando el GET posterior muestra el estado.
ESTADOS_VISIBLES = {
    "AWAITING_PARENT_ORDER", "AWAITING_CONDITION", "AWAITING_MANUAL_REVIEW",
    "ACCEPTED", "AWAITING_UR_OUT", "PENDING_ACTIVATION",
    "PENDING_ACKNOWLEDGEMENT", "PENDING_RECALL", "PENDING_REPLACE",
    "PENDING_CANCEL", "PENDING_REVIEW", "AWAITING_STOP_CONDITION",
    "AWAITING_RELEASE_TIME", "QUEUED", "WORKING", "PARTIALLY_FILLED", "FILLED",
}
ESTADOS_CANCELACION_OK = {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
ESTADOS_FALLO_ENVIO = {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
ESTADOS_FALLO_LLENADO = {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "REPLACED"}
TERMINAL_ORDER_STATES = {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}


def _ps(msg):
    """
    Print seguro: nunca lanza excepción por codificación de consola.
    CRÍTICO — se usa tras enviar órdenes reales; un fallo de 'print'
    (p. ej. consola cp1252 con caracteres UTF-8) JAMÁS debe enmascarar
    una orden ya enviada al broker ni interrumpir el flujo.
    """
    try:
        print(msg)
    except Exception:
        try:
            print(str(msg).encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass


def _err_detalle(e) -> str:
    """Extrae el motivo REAL de un error de requests: código HTTP + cuerpo de
    la respuesta de Schwab (que explica el porqué de un 400/401/etc.). Sin esto
    solo vemos 'Bad Request' sin la causa. Nunca lanza."""
    try:
        resp = getattr(e, "response", None)
        if resp is not None:
            cuerpo = ""
            try:
                cuerpo = (resp.text or "").strip().replace("\n", " ")[:400]
            except Exception:
                cuerpo = ""
            return f"HTTP {resp.status_code} | {cuerpo}" if cuerpo else f"HTTP {resp.status_code}"
    except Exception:
        pass
    return str(e)


# =============================================================
# INSTRUCCIONES SCHWAB (mapeo estricto largo/corto)
# =============================================================
# Schwab exige la instrucción CORRECTA según el sentido, o rebota la orden:
#   LARGO  → abre con BUY,          cierra con SELL          (positionEffect CLOSING)
#   CORTO  → abre con SELL_SHORT,   cierra con BUY_TO_COVER  (positionEffect CLOSING)
# 'tipo'/'tipo_entrada' del bot: "BUY" = largo, "SELL" = corto.

def _instruccion_apertura(tipo: str) -> str:
    """Never confuse a short entry with a long close (SELL)."""
    side = str(tipo or "").upper().strip()
    if side not in {"BUY", "SELL"}:
        raise ValueError("Opening signal must be BUY or SELL")
    return "BUY" if side == "BUY" else "SELL_SHORT"


def _instruccion_cierre(tipo_entrada: str) -> str:
    """Instrucción Schwab para CERRAR una posición (opuesta a la entrada)."""
    return "SELL" if tipo_entrada == "BUY" else "BUY_TO_COVER"


# =============================================================
# CLASIFICACIÓN DE MERCADO
# =============================================================

@dataclass
class ContextoMercado:
    """
    Contexto del mercado en el momento de la señal.
    Determina el tipo de orden y el tamaño de posición.
    """
    atr_relativo: float      # ATR / Precio × 100 (volatilidad %)
    es_rango_lateral: bool   # True si OM-R fue detectado
    prob_ia: Optional[float] # None = modelo no cargado (dashboard: N/A)
    es_senal_maestra: bool   # True si confluyen los 4 factores
    spread_pct: float = 0.01 # Spread bid-ask estimado (%)
    es_entrada_mercado: bool = False  # True = entrada REACTIVA a Gala 10 (sin FVG) → MARKET


def _clasificar_volatilidad(atr_relativo: float) -> str:
    """
    Clasifica la volatilidad del mercado en tres niveles.

    Umbrales calibrados para acciones de US equity 1-minuto:
      BAJA   → mercado lateral o lento (< 0.15% ATR relativo)
      NORMAL → condiciones normales    (0.15% - 0.50%)
      ALTA   → mercado muy volátil     (> 0.50%)
    """
    if atr_relativo < 0.15:
        return "BAJA"
    elif atr_relativo <= 0.50:
        return "NORMAL"
    else:
        return "ALTA"


# =============================================================
# CALCULADORA DE ORDEN HÍBRIDA
# =============================================================

def calcular_orden_hibrida(
    orden_base: OrdenTradingCalculada,
    contexto: ContextoMercado
) -> Tuple[OrdenTradingCalculada, str, str]:
    """
    Ajusta la orden base según el contexto del mercado.

    Parámetros:
        orden_base → Orden calculada por el GestorDeRiesgo (cantidad y precio base)
        contexto   → ContextoMercado con volatilidad, rango y probabilidad IA

    Retorna:
        (orden_ajustada, tipo_orden, motivo)
          - orden_ajustada: OrdenTradingCalculada con cantidad y precio modificados
          - tipo_orden:     "LIMIT" o "MARKET"
          - motivo:         Descripción del ajuste aplicado
    """
    volatilidad = _clasificar_volatilidad(contexto.atr_relativo)
    cantidad_original = orden_base.cantidad
    precio_original = orden_base.precio_limite
    tipo_orden = "LIMIT"
    motivo = ""

    # -------------------------------------------------------
    # PASO 1: Determinar tipo de orden (LIMIT vs MARKET)
    # -------------------------------------------------------
    if getattr(contexto, "es_entrada_mercado", False):
        # Entrada REACTIVA a la señal de Gala 10 (sin FVG preciso): debe llenarse
        # YA. Se envía como MARKET → el ejecutor la manda como marketable limit con
        # tope de slippage (no descansa como LIMIT hasta que el precio vuelva).
        tipo_orden = "MARKET"
        motivo = "Entrada reactiva Gala 10 → MARKET (marketable limit)"

    elif contexto.es_rango_lateral:
        # En rango → LIMIT siempre (entrar dentro del rango, no en el extremo)
        tipo_orden = "LIMIT"
        motivo = "Rango lateral → LIMIT conservador"

    elif volatilidad == "ALTA":
        # Mercado muy volátil → MARKET para no perder la entrada
        tipo_orden = "MARKET"
        motivo = "Alta volatilidad → MARKET (evitar que el precio escape)"

    elif volatilidad == "BAJA":
        # Mercado lento → LIMIT ajustado al bid más cercano
        tipo_orden = "LIMIT"
        motivo = "Mercado lento → LIMIT ajustado al bid"
        # Precio más conservador: un poco por debajo del precio actual
        if orden_base.tipo == "BUY":
            precio_original = precio_original * (1 - contexto.spread_pct / 100)
        else:
            precio_original = precio_original * (1 + contexto.spread_pct / 100)

    else:
        # Normal → LIMIT estándar
        tipo_orden = "LIMIT"
        motivo = "Volatilidad normal → LIMIT estándar"

    # -------------------------------------------------------
    # PASO 2: Ajustar cantidad de acciones
    # -------------------------------------------------------
    factor_cantidad = 1.0

    if contexto.es_senal_maestra:
        # Señal Maestra (4 factores confluentes) → máxima posición
        factor_cantidad = 2.5
        motivo += " | SEÑAL MAESTRA → 250% acciones"

    elif contexto.prob_ia is not None and contexto.prob_ia >= 0.80:
        # Alta probabilidad IA (>= 80%) → duplicar posición
        factor_cantidad = 2.0
        motivo += f" | IA {contexto.prob_ia:.0%} → 200% acciones"

    elif contexto.es_rango_lateral:
        # En rango lateral → reducir posición a la mitad (menos riesgo)
        factor_cantidad = 0.5
        motivo += " | Rango lateral → 50% acciones (gestión de riesgo)"

    elif contexto.prob_ia is not None and contexto.prob_ia < 0.70:
        # Probabilidad IA normal (60-70%) → cantidad estándar
        factor_cantidad = 1.0
        motivo += f" | IA {contexto.prob_ia:.0%} → 100% acciones"

    elif contexto.prob_ia is None:
        # Transparencia: sin modelo no se inventa 50%. La ausencia de IA no
        # modifica el tamaño ya validado de GALA10.
        factor_cantidad = 1.0
        motivo += " | IA N/A → 100% acciones"

    # Aplicar factor (mínimo 1 acción, máximo razonable)
    nueva_cantidad = max(1, round(cantidad_original * factor_cantidad))

    # -------------------------------------------------------
    # PASO 3: Construir orden ajustada
    # -------------------------------------------------------
    # Recalcular el reparto 70/30 con la cantidad ajustada, preservando el
    # plan de salida parcial y los objetivos (necesarios para el chase y la protección).
    if orden_base.salida_parcial_activa and nueva_cantidad >= 2:
        cantidad_parcial = max(1, min(round(nueva_cantidad * 0.70), nueva_cantidad - 1))
        cantidad_resto = nueva_cantidad - cantidad_parcial
    else:
        cantidad_parcial, cantidad_resto = orden_base.cantidad_parcial, orden_base.cantidad_resto

    orden_ajustada = OrdenTradingCalculada(
        ticker=orden_base.ticker,
        tipo=orden_base.tipo,
        precio_limite=round(precio_original, 4),
        stop_loss=orden_base.stop_loss,
        trailing_stop_pct=orden_base.trailing_stop_pct,
        cantidad=nueva_cantidad,
        riesgo_usd=round(
            abs(precio_original - orden_base.stop_loss) * nueva_cantidad, 2
        ),
        atr_actual=orden_base.atr_actual,
        take_profit=orden_base.take_profit,
        take_profit_parcial=orden_base.take_profit_parcial,
        cantidad_parcial=cantidad_parcial,
        cantidad_resto=cantidad_resto,
        modo_resto=orden_base.modo_resto,
        salida_parcial_activa=orden_base.salida_parcial_activa and nueva_cantidad >= 2,
        stop_loss_atr_factor=orden_base.stop_loss_atr_factor,
        take_profit_atr_factor=orden_base.take_profit_atr_factor,
    )

    return orden_ajustada, tipo_orden, motivo


# =============================================================
# EJECUTOR HÍBRIDO COMPLETO
# =============================================================

class EjecutorHibridoDeOrdenes:
    """
    Versión inteligente del ejecutor de órdenes.

    Decide automáticamente:
      1. Usar LIMIT o MARKET según la volatilidad del mercado
      2. Reducir cantidad en señales de rango (OM-R)
      3. Duplicar cantidad en señales de alta probabilidad IA
      4. Maximizar cantidad en Señales Maestras

    Uso:
        ejecutor = EjecutorHibridoDeOrdenes(paper_trading=True)
        resultado = ejecutor.ejecutar_hibrido(
            orden=orden_base,
            contexto=ContextoMercado(atr_relativo=0.30, es_rango_lateral=False,
                                     prob_ia=0.82, es_senal_maestra=False),
            account_number="12345678"
        )
    """

    TRADER_URL = "https://api.schwabapi.com/trader/v1"

    def __init__(self, paper_trading: bool = True, access_token: str = "",
                 config: dict = None):
        self.paper_trading = paper_trading
        self.access_token = access_token
        self._session = requests.Session()
        self._root_session = self._session
        self._session_local = threading.local()
        self._token_provider = None
        # Optional authoritative broker-position provider. In production this
        # is wired to SchwabClient.obtener_posiciones(), so liquidation, startup
        # reconciliation and flat-confirmation all use the same OAuth/401-refresh
        # path instead of a second raw GET implementation.
        self._position_provider = None
        # v3.3.5: current WORKING-order truth must use the same SchwabClient
        # OAuth/account path as dashboard/startup reconciliation.  The previous
        # direct requests.Session path could fail independently and return None
        # while SchwabClient had already proved there were no live orders.
        self._working_orders_provider = None
        self._audit_hook = None
        self._lifecycle_local = threading.local()
        self._order_observation_local = threading.local()
        self._order_observation_lock = threading.Lock()
        self._order_observation_generation = 0
        self._order_read_warning_lock = threading.Lock()
        self._order_read_warnings = {}
        if access_token:
            self._session.headers.update({
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json"
            })

        # --- Configuración de persecución activa (chase) ---
        cfg = config or {}
        self.execution_v2_config = cfg.get('execution_engine', {}) or {}
        self._quote_provider = None
        self._smart_exit = None
        chase = cfg.get("chase", {})
        self.chase_activo = chase.get("activo", True)
        self.chase_max_reintentos = chase.get("max_reintentos", 3)
        self.chase_espera_seg = chase.get("espera_seg", 2)
        self.chase_desplazamiento_pct = chase.get("desplazamiento_pct", 0.03)
        self.chase_cancelar_si_rb_menor = chase.get("cancelar_si_rb_menor", 1.2)
        # Tope de slippage para órdenes MARKET (marketable limit)
        self.max_slippage_pct = cfg.get("ejecucion", {}).get("max_slippage_pct", 0.0)
        # Tope DURO de acciones por orden (aplicado DESPUÉS de los multiplicadores
        # de confianza, para que el modo de validación de 1 acción sea inviolable)
        self.max_acciones = int(cfg.get("risk", {}).get("max_acciones_por_orden", 0))
        self.sizing_mode = str(
            cfg.get("risk", {}).get("sizing_mode", "atr")
        ).strip().lower()

        # --- Sincronización estricta con el estado real del broker ---
        sync_cfg = cfg.get("order_sync", {})
        self.poll_interval_ms = int(sync_cfg.get("poll_interval_ms", 100))
        # Background position monitoring has its own cadence. Keep it separate
        # from the faster acknowledgement/fill polling, with the engine's 1s floor.
        self.monitor_interval_ms = max(
            1000, int(sync_cfg.get("monitor_interval_ms", 1000))
        )
        self.ack_timeout_s = float(sync_cfg.get("ack_timeout_ms", 750)) / 1000.0
        self.cancel_timeout_s = float(sync_cfg.get("cancel_timeout_ms", 2500)) / 1000.0
        self.fill_timeout_s = float(sync_cfg.get("fill_timeout_ms", 5000)) / 1000.0
        self.flat_timeout_s = float(sync_cfg.get("flat_timeout_ms", 2500)) / 1000.0
        self.request_timeout_s = float(sync_cfg.get("request_timeout_sec", 5.0))
        self._sync = AsyncOrderSynchronizer(self.poll_interval_ms)

        # Una secuencia por símbolo es indivisible: cancelación, cierre, verificación
        # de posición plana y apertura contraria no pueden cruzarse con otro hilo.
        self._symbol_locks = {}
        self._symbol_locks_guard = threading.Lock()

        if self.paper_trading:
            _ps("[HIBRIDO] Modo PAPER TRADING — ordenes simuladas")

    def set_audit_hook(self, hook):
        """Injects a best-effort lifecycle audit callback from the engine."""
        self._audit_hook = hook

    def set_quote_provider(self, provider):
        self._quote_provider = provider

    @contextmanager
    def order_observation_scope(self, orders, *, account_number="", generation=None):
        """Reuse positive WORKING states only during one monitor observation.

        Fill details and cancellation checks continue to read the broker afresh.
        Absence from this snapshot never establishes a terminal order state.
        """
        previous = getattr(self._order_observation_local, "scope", None)
        # Capture before traversing: a mutation during construction must expire
        # this observation. The monitor can pass its version from before the GET.
        if generation is None:
            with self._order_observation_lock:
                generation = self._order_observation_generation
        working_ids = frozenset(
            str(order["orderId"])
            for order in self._iter_order_nodes(orders)
            if order.get("orderId")
            and normalize_status(order.get("status")) == "WORKING"
        ) if isinstance(orders, list) else frozenset()
        self._order_observation_local.scope = (
            str(account_number), working_ids, generation
        )
        try:
            yield
        finally:
            self._order_observation_local.scope = previous

    def _invalidate_order_observations(self):
        """Expire every worker's observation before any broker mutation."""
        with self._order_observation_lock:
            self._order_observation_generation += 1

    def _submit_order_request(self, method, url, **kwargs):
        """Invalidate observations around successful and failed POST/DELETE calls."""
        self._invalidate_order_observations()
        try:
            return getattr(self._http_session(), method)(url, **kwargs)
        finally:
            # A snapshot fetched while the request was in flight may also be old.
            self._invalidate_order_observations()

    def _report_order_read_failure(self, account_number, direct_error, list_error):
        """Keep endpoint failures actionable without printing each polling retry."""
        direct_response = getattr(direct_error, "response", None)
        direct_status = getattr(direct_response, "status_code", None)
        list_response = getattr(list_error, "response", None)
        list_status = getattr(list_response, "status_code", None)
        signature = (direct_status, list_status, type(list_error).__name__)
        now = time.monotonic()
        with self._order_read_warning_lock:
            previous = self._order_read_warnings.get(account_number)
            if previous and previous[0] == signature and now - previous[1] < 30:
                self._order_read_warnings[account_number] = (
                    signature, previous[1], previous[2] + 1
                )
                return
            repeats = previous[2] if previous else 0
            self._order_read_warnings[account_number] = (signature, now, 0)
        detail = (
            _err_detalle(list_error) if list_response is not None
            else type(list_error).__name__
        )
        _ps(
            "[WARN] Lectura de órdenes no disponible: "
            f"GET por id HTTP={direct_status or type(direct_error).__name__}; "
            f"listado {detail}; repeticiones={repeats}. "
            "Estado desconocido: se mantiene la reconciliación con el broker."
        )

    def _report_order_read_recovery(self, account_number):
        with self._order_read_warning_lock:
            previous = self._order_read_warnings.pop(account_number, None)
        if previous:
            _ps("[OK] Lectura de órdenes recuperada; "
                f"repeticiones agrupadas={previous[2]}")

    def retire_pending_exit_after_authoritative_flat(self, account, ticker, reason="broker confirmed flat"):
        """Clear a durable pending exit after a read-only broker flat proof.

        No broker request that can mutate the account is sent here.  The caller
        is responsible for proving there is no position and no WORKING order.
        """
        from core.execution_v2 import SmartExit
        with self.secuencia_simbolo(str(ticker).upper()):
            if self._smart_exit is None:
                self._smart_exit = SmartExit(self, self.execution_v2_config)
            return self._smart_exit.retire_pending_after_authoritative_flat(
                account, str(ticker).upper(), reason
            )

    def has_pending_exit(self, account, ticker):
        import hashlib
        from core.runtime_store import JsonState
        key = hashlib.sha256(str(account).encode()).hexdigest()[:16] + ':' + ticker
        return JsonState(self.execution_v2_config.get('pending_path', 'data/execution_pending.json')).get(key) is not None

    @contextmanager
    def lifecycle_context(self, **fields):
        """Attach v2.9 correlation fields to every nested broker audit event."""

        previous = getattr(self._lifecycle_local, "fields", None)
        merged = dict(previous or {})
        merged.update({key: value for key, value in fields.items() if value not in (None, "")})
        self._lifecycle_local.fields = merged
        try:
            yield
        finally:
            self._lifecycle_local.fields = previous

    def _audit(self, evento: str, **fields):
        """Audit failures must never interrupt or mask a broker operation."""
        hook = self._audit_hook
        if not hook:
            return
        try:
            context = dict(getattr(self._lifecycle_local, "fields", None) or {})
            for key, value in context.items():
                fields.setdefault(key, value)
            event_name = str(evento or "").upper()
            side = str(fields.get("lado", "") or "").upper()
            if side in ("BUY", "SELL"):
                position_side = "LONG" if side == "BUY" else "SHORT"
                fields.setdefault("position_side", position_side)
                if event_name.startswith("ENTRY"):
                    fields.setdefault(
                        "order_action", "BUY" if side == "BUY" else "SELL_SHORT"
                    )
                elif event_name.startswith(("EXIT", "STOP", "TP1")):
                    fields.setdefault(
                        "order_action", "SELL" if side == "BUY" else "BUY_TO_COVER"
                    )
            hook(evento, **fields)
        except Exception:
            pass

    def _aplicar_tope_cantidad(self, orden_aj):
        """Clampa la cantidad FINAL al tope duro (tras el multiplicador híbrido)
        y recalcula el reparto 70/30 para esa cantidad."""
        if self.max_acciones > 0 and orden_aj.cantidad > self.max_acciones:
            from dataclasses import replace
            cant = int(self.max_acciones)
            if orden_aj.salida_parcial_activa and cant >= 3:
                parcial = max(1, min(round(cant * 0.70), cant - 1)); resto = cant - parcial; sp = True
            else:
                parcial, resto, sp = 0, cant, False
            return replace(orden_aj, cantidad=cant, cantidad_parcial=parcial,
                           cantidad_resto=resto, salida_parcial_activa=sp)
        return orden_aj

    def _normalizar_cantidad_final(self, orden_base, orden_aj):
        """En modo fijo la cantidad manual es exacta; la IA solo aprueba o veta."""
        if self.execution_v2_config.get('enabled', False):
            # Capital and stop-risk were reserved against this exact order.
            return self._aplicar_tope_cantidad(orden_base)
        if (self.sizing_mode == "fixed" or self.execution_v2_config.get("enabled", False)) and orden_aj.cantidad != orden_base.cantidad:
            from dataclasses import replace
            orden_aj = replace(
                orden_aj,
                cantidad=orden_base.cantidad,
                cantidad_parcial=orden_base.cantidad_parcial,
                cantidad_resto=orden_base.cantidad_resto,
                salida_parcial_activa=orden_base.salida_parcial_activa,
            )
        return self._aplicar_tope_cantidad(orden_aj)

    @staticmethod
    def _aplicar_token_sesion(session, access_token: str):
        # GET order reads have no entity. Schwab rejects them with HTTP 400
        # when Content-Type is inherited from the session, hiding real fills
        # and aborting reversals. requests supplies JSON Content-Type only on
        # POSTs that actually pass json=, including entries and protective STOPs.
        session.headers.pop("Content-Type", None)
        if access_token:
            session.headers.update({
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            })
        else:
            session.headers.pop("Authorization", None)

    def set_token_provider(self, provider):
        """Proveedor callable del access token OAuth vigente."""
        self._token_provider = provider

    def set_position_provider(self, provider):
        """Proveedor autoritativo de posiciones del broker.

        ``provider(account_number)`` debe devolver la misma lista usada por
        SchwabClient.obtener_posiciones(): ``[{symbol,tipo,cantidad,precio}, ...]``.
        ``None`` significa consulta desconocida/fallida y mantiene fail-closed.
        """
        self._position_provider = provider

    def set_working_orders_provider(self, provider):
        """Proveedor autoritativo de órdenes WORKING del broker.

        Production wires this to ``SchwabClient.obtener_ordenes_abiertas`` so
        SmartExit, startup reconciliation and the dashboard all share the same
        OAuth refresh/account semantics. ``None`` remains unknown/fail-closed;
        an empty list is authoritative proof that there are no WORKING orders.
        """
        self._working_orders_provider = provider

    def set_token(self, access_token: str):
        """Actualiza el token OAuth2 (para el paso a cuenta real)."""
        self.access_token = access_token or ""
        self._aplicar_token_sesion(self._session, self.access_token)
        thread_session = getattr(self._session_local, "session", None)
        if thread_session is not None:
            self._aplicar_token_sesion(thread_session, self.access_token)

    def _http_session(self):
        """One requests.Session per worker thread (Session is not thread-safe)."""
        if self._token_provider is not None:
            try:
                token = self._token_provider() or ""
            except Exception:
                token = ""
            if token != self.access_token:
                self.set_token(token)

        # Tests/integrations may inject a custom session after construction.
        if self._session is not self._root_session:
            self._aplicar_token_sesion(self._session, self.access_token)
            return self._session
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = requests.Session()
            self._session_local.session = session
        self._aplicar_token_sesion(session, self.access_token)
        return session

    @contextmanager
    def secuencia_simbolo(self, ticker: str):
        """Serializa el ciclo de órdenes completo de un ticker entre hilos."""
        key = str(ticker or "").upper()
        with self._symbol_locks_guard:
            lock = self._symbol_locks.setdefault(key, threading.RLock())
        with lock:
            yield

    def ejecutar_hibrido(
        self,
        orden: OrdenTradingCalculada,
        contexto: ContextoMercado,
        account_number: str
    ) -> dict:
        """
        Ejecuta una orden con lógica híbrida adaptativa.

        Parámetros:
            orden          → OrdenTradingCalculada base del GestorDeRiesgo
            contexto       → ContextoMercado (volatilidad + probabilidad IA)
            account_number → Número de cuenta Schwab

        Retorna:
            Dict con resultado, tipo de orden usado y motivo del ajuste
        """
        # Calcular ajustes
        orden_ajustada, tipo_orden, motivo = calcular_orden_hibrida(orden, contexto)
        if self.execution_v2_config.get("enabled", False):
            tipo_orden = "LIMIT"
        orden_ajustada = self._normalizar_cantidad_final(orden, orden_ajustada)

        volatilidad_str = _clasificar_volatilidad(contexto.atr_relativo)
        log_prefix = (
            f"[HIBRIDO] {orden_ajustada.ticker} {orden_ajustada.tipo} | "
            f"Volatilidad: {volatilidad_str} | Tipo: {tipo_orden} | "
            f"Cant: {orden.cantidad}->{orden_ajustada.cantidad} | {motivo}"
        )
        _ps(log_prefix)

        if self.paper_trading:
            return self._simular_hibrido(orden_ajustada, tipo_orden, motivo)
        else:
            return self._enviar_orden_real(orden_ajustada, tipo_orden, account_number, motivo)

    def _simular_hibrido(self, orden: OrdenTradingCalculada,
                          tipo_orden: str, motivo: str) -> dict:
        """Simula la orden híbrida en paper trading."""
        submitted_limit = self._submitted_limit_price(orden, tipo_orden)
        submitted_type = (
            "LIMIT" if tipo_orden == "LIMIT"
            else "MARKETABLE_LIMIT" if submitted_limit is not None else "MARKET"
        )
        result = {
            "status": "SIMULADA",
            "order_id": f"HIBRIDO-{orden.ticker}-{orden.tipo}",
            "tipo_orden": tipo_orden,
            "precio_limite": orden.precio_limite,
            "stop_loss": orden.stop_loss,
            "trailing_stop_pct": orden.trailing_stop_pct,
            "cantidad": orden.cantidad,
            "motivo_ajuste": motivo,
            "signal_price": orden.precio_limite,
            "submitted_limit_price": submitted_limit,
            "fill_price": orden.precio_limite,
            "submitted_order_type": submitted_type,
        }
        self._audit(
            "ENTRY_SIMULATED",
            ticker=orden.ticker,
            lado=orden.tipo,
            estado="SIMULADA",
            order_id=result["order_id"],
            cantidad=orden.cantidad,
            precio=orden.precio_limite,
            motivo=motivo,
            detalle={
                "tipo_orden": tipo_orden,
                "submitted_order_type": submitted_type,
                "signal_price": orden.precio_limite,
                "submitted_limit_price": submitted_limit,
                "fill_price": orden.precio_limite,
            },
        )
        return result

    def _submitted_limit_price(
        self, orden: OrdenTradingCalculada, tipo_orden: str
    ) -> Optional[float]:
        """Return the actual LIMIT sent to Schwab, including the buffer cap."""

        if tipo_orden == "LIMIT":
            # Opt-in bounded tolerance for fast markets. A SELL_SHORT LIMIT
            # may improve fill likelihood by crossing *down* up to 10 cents;
            # BUY moves *up*. This is a cap, not a guaranteed execution price.
            # No unbounded MARKET fallback and no change to PAPER orders.
            price = float(orden.precio_limite)
            if self.paper_trading or not self.execution_v2_config.get("enabled", False):
                return round(price, 4)
            tolerance = float(self.execution_v2_config.get(
                "entry_limit_tolerance_usd", 0.0
            ) or 0.0)
            if not math.isfinite(tolerance) or not (0.0 <= tolerance <= 0.10):
                raise ValueError("entry_limit_tolerance_usd must be 0.00–0.10")
            if tolerance > 0:
                price = price + tolerance if orden.tipo == "BUY" else price - tolerance
            if not math.isfinite(price) or price <= 0:
                raise ValueError("Invalid broker entry LIMIT after tolerance")
            return round(price, 2) if tolerance else round(price, 4)
        if self.max_slippage_pct > 0:
            cap = orden.precio_limite * (self.max_slippage_pct / 100.0)
            price = (
                orden.precio_limite + cap
                if orden.tipo == "BUY"
                else orden.precio_limite - cap
            )
            return round(float(price), 2)
        return None

    def _enviar_orden_real(self, orden: OrdenTradingCalculada,
                            tipo_orden: str, account_number: str,
                            motivo: str) -> dict:
        """
        Envía SOLO la orden de ENTRADA a Schwab API (SINGLE, sin protección
        adjunta). La protección 70/30 (TP1 + SL) la coloca por separado
        montar_proteccion_parcial. Esto evita órdenes de protección DUPLICADAS
        (antes se adjuntaba un TRAILING_STOP del 100% además del bracket 70/30).
        """
        if self.execution_v2_config.get('enabled', False):
            tipo_orden = 'LIMIT'
        instruccion = _instruccion_apertura(orden.tipo)
        quote = {}
        if self.execution_v2_config.get('enabled', False):
            from core.execution_v2 import SmartExit
            quote = SmartExit(self, self.execution_v2_config)._quote(orden.ticker)

        cuerpo: dict = {
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "orderLegType": "EQUITY",
                    "legId": 1,
                    "instrument": {
                        "symbol": orden.ticker,
                        "assetType": "EQUITY",
                    },
                    "instruction": instruccion,
                    "positionEffect": "OPENING",
                    "quantity": orden.cantidad,
                }
            ],
        }

        # LIMIT includes its price. Reactive MARKET intent is converted to a
        # marketable LIMIT with a hard buffer whenever max_slippage_pct > 0.
        submitted_limit = self._submitted_limit_price(orden, tipo_orden)
        submitted_type = "MARKET"
        if submitted_limit is not None:
            cuerpo["orderType"] = "LIMIT"
            cuerpo["price"] = str(submitted_limit)
            submitted_type = (
                "LIMIT" if tipo_orden == "LIMIT" else "MARKETABLE_LIMIT"
            )
        else:
            cuerpo["orderType"] = "MARKET"

        url = f"{self.TRADER_URL}/accounts/{account_number}/orders"
        try:
            resp = self._submit_order_request(
                "post", url, json=cuerpo, timeout=self.request_timeout_s)
            resp.raise_for_status()
            order_id = resp.headers.get("Location", "").split("/")[-1]
            self._audit(
                "ENTRY_SUBMITTED",
                ticker=orden.ticker,
                lado=orden.tipo,
                estado="ACEPTADA_HTTP",
                order_id=order_id,
                cantidad=orden.cantidad,
                precio=cuerpo.get("price", orden.precio_limite),
                motivo=motivo,
                detalle={
                    "tipo_orden": tipo_orden,
                    "submitted_order_type": submitted_type,
                    "duration": cuerpo.get("duration"),
                    "signal_price": orden.precio_limite,
                    "submitted_limit_price": submitted_limit,
                    "requested_price": submitted_limit,
                    "bid": quote.get('bid'), "ask": quote.get('ask'),
                },
            )
            # ID vacío = la orden pudo enviarse pero no podemos rastrearla: estado a reconciliar
            if not order_id:
                _ps(f"[WARN] Orden {tipo_orden} enviada SIN id de localización | {motivo} — requiere reconciliación")
                self._audit(
                    "ENTRY_RECONCILE_REQUIRED",
                    ticker=orden.ticker,
                    lado=orden.tipo,
                    estado="SIN_ORDER_ID",
                    cantidad=orden.cantidad,
                    precio=cuerpo.get("price", orden.precio_limite),
                    motivo="Schwab aceptó la entrada sin devolver order_id",
                )
                return {
                    "status": "RECONCILIAR", "order_id": "",
                    "tipo_orden": tipo_orden, "motivo_ajuste": motivo,
                    "signal_price": orden.precio_limite,
                    "submitted_limit_price": submitted_limit,
                    "submitted_order_type": submitted_type,
                }
            confirmacion = self.esperar_estado_orden(
                account_number,
                order_id,
                deseados=ESTADOS_VISIBLES,
                fallos=ESTADOS_FALLO_ENVIO,
                timeout_s=self.ack_timeout_s,
            )
            if not confirmacion["confirmado"]:
                _ps(f"[WARN] Orden {tipo_orden} enviada, pero no visible/confirmada "
                    f"en {confirmacion['latency_ms']}ms | ID: {order_id}")
                self._audit(
                    "ENTRY_RECONCILE_REQUIRED",
                    ticker=orden.ticker,
                    lado=orden.tipo,
                    estado=confirmacion.get("broker_status", "DESCONOCIDO"),
                    order_id=order_id,
                    cantidad=orden.cantidad,
                    precio=cuerpo.get("price", orden.precio_limite),
                    latency_ms=confirmacion.get("latency_ms"),
                    motivo=confirmacion.get("error", "Confirmación no recibida"),
                )
                return {
                    "status": "RECONCILIAR",
                    "order_id": order_id,
                    "tipo_orden": tipo_orden,
                    "motivo_ajuste": motivo,
                    "signal_price": orden.precio_limite,
                    "submitted_limit_price": submitted_limit,
                    "submitted_order_type": submitted_type,
                    **confirmacion,
                }
            _ps(f"[OK] Orden {tipo_orden} confirmada {confirmacion['broker_status']} "
                f"en {confirmacion['latency_ms']}ms | ID: {order_id} | {motivo}")
            self._audit(
                "ENTRY_ACKNOWLEDGED",
                ticker=orden.ticker,
                lado=orden.tipo,
                estado=confirmacion.get("broker_status", ""),
                order_id=order_id,
                cantidad=orden.cantidad,
                precio=cuerpo.get("price", orden.precio_limite),
                latency_ms=confirmacion.get("latency_ms"),
                motivo=motivo,
            )
            return {
                "status": "OK",
                "order_id": order_id,
                "tipo_orden": tipo_orden,
                "motivo_ajuste": motivo,
                "signal_price": orden.precio_limite,
                "submitted_limit_price": submitted_limit,
                "submitted_order_type": submitted_type,
                **confirmacion,
            }
        except requests.exceptions.RequestException as e:
            _ps(f"[ERROR] Orden fallida: {e}")
            self._audit(
                "ENTRY_REJECTED",
                ticker=orden.ticker,
                lado=orden.tipo,
                estado="ERROR",
                cantidad=orden.cantidad,
                precio=orden.precio_limite,
                motivo=_err_detalle(e),
                detalle={"tipo_orden": tipo_orden},
            )
            return {"status": "ERROR", "mensaje": str(e)}

    # =========================================================
    # PERSECUCIÓN ACTIVA DE LÍMITES (SMART LIMIT CHASING)
    # =========================================================

    def ejecutar_con_persecucion(self, orden: OrdenTradingCalculada,
                                 contexto: ContextoMercado, account_number: str,
                                 cotizar_fn=None) -> dict:
        """
        Envía la orden con lógica híbrida y, si es LIMIT y no se llena
        mientras el mercado corre a favor, PERSIGUE el precio: cancela y
        vuelve a cotizar hasta 'max_reintentos' veces. Si el R/B tras
        perseguir cae por debajo del umbral, cancela y desiste.

        cotizar_fn(ticker) -> precio actual (obligatorio para perseguir en real).

        En paper trading la orden se simula como llenada de inmediato.
        """
        orden_ajustada, tipo_orden, motivo = calcular_orden_hibrida(orden, contexto)
        if self.execution_v2_config.get("enabled", False):
            tipo_orden = "LIMIT"
        orden_ajustada = self._normalizar_cantidad_final(orden, orden_ajustada)

        if self.paper_trading:
            res = self._simular_hibrido(orden_ajustada, tipo_orden, motivo)
            res["perseguido"] = False
            res["orden_ajustada"] = orden_ajustada
            res["signal_price"] = orden.precio_limite
            return res

        # MARKET no necesita persecución (llenado inmediato)
        if tipo_orden == "MARKET" or not self.chase_activo or cotizar_fn is None:
            res = self._enviar_orden_real(
                orden_ajustada, tipo_orden, account_number, motivo
            )
            res["signal_price"] = orden.precio_limite
            return res

        return self._perseguir_limit(orden_ajustada, account_number, motivo, cotizar_fn)

    def enviar_entrada(self, orden: OrdenTradingCalculada,
                       contexto: ContextoMercado, account_number: str) -> dict:
        """
        Coloca ÚNICAMENTE la orden de ENTRADA (sin persecución ni protección).
        Devuelve el id para hacer seguimiento del llenado. La protección (TP1/SL)
        la coloca el motor APARTE, solo tras confirmarse el llenado — así nunca
        hay órdenes de protección para una posición que aún no existe.

        Elige LIMIT o MARKET (marketable) según la volatilidad, igual que el
        flujo híbrido, pero sin cancelar por 'deriva' (una LIMIT de retroceso
        debe poder descansar hasta que el precio vuelva a su nivel).
        """
        orden_ajustada, tipo_orden, motivo = calcular_orden_hibrida(orden, contexto)
        if self.execution_v2_config.get("enabled", False):
            tipo_orden = "LIMIT"
        orden_ajustada = self._normalizar_cantidad_final(orden, orden_ajustada)
        if self.paper_trading:
            res = self._simular_hibrido(orden_ajustada, tipo_orden, motivo)
        else:
            res = self._enviar_orden_real(orden_ajustada, tipo_orden, account_number, motivo)
        res["orden_ajustada"] = orden_ajustada
        res["tipo_orden"] = tipo_orden
        res["signal_price"] = orden.precio_limite
        return res

    def _perseguir_limit(self, orden: OrdenTradingCalculada, account_number: str,
                         motivo: str, cotizar_fn) -> dict:
        """
        Coloca una LIMIT y la persigue si el precio se escapa a favor.

        Regla de persecución:
          - BUY: si el precio sube por encima de nuestra LIMIT sin llenarla,
                 subimos la LIMIT (perseguimos) para no quedar fuera.
          - SELL: si el precio baja por debajo de nuestra LIMIT sin llenarla,
                 bajamos la LIMIT.
          - Antes de cada re-cotización se recalcula el R/B contra el SL/TP;
            si ya no compensa (< cancelar_si_rb_menor), se cancela y desiste.
        """
        import time as _time

        entrada_inicial = orden.precio_limite
        entrada = orden.precio_limite
        # Deriva máxima absoluta permitida frente a la entrada original
        max_deriva = entrada_inicial * (self.chase_desplazamiento_pct / 100.0) * (self.chase_max_reintentos + 1)
        resultado = self._enviar_orden_real(orden, "LIMIT", account_number, motivo)
        if resultado.get("status") != "OK":
            return resultado
        order_id = resultado.get("order_id", "")

        for intento in range(1, self.chase_max_reintentos + 1):
            _time.sleep(self.chase_espera_seg)

            estado = self._estado_orden(account_number, order_id)
            if estado == "FILLED":
                resultado["perseguido"] = intento > 1
                resultado["intentos_chase"] = intento - 1
                return resultado
            if estado in ("CANCELED", "REJECTED", "EXPIRED"):
                resultado["status"] = "NO_LLENADA"
                return resultado

            precio = None
            try:
                precio = float(cotizar_fn(orden.ticker))
            except Exception:
                precio = None
            if not precio or precio <= 0:
                continue

            escapa = (orden.tipo == "BUY" and precio > entrada) or \
                     (orden.tipo == "SELL" and precio < entrada)
            if not escapa:
                # El precio sigue alcanzable; esperar otra ronda
                continue

            # Nueva cotización perseguida
            desp = precio * (self.chase_desplazamiento_pct / 100.0)
            nueva_entrada = precio + desp if orden.tipo == "BUY" else precio - desp

            # Tope de deriva acumulada respecto a la entrada original
            if abs(nueva_entrada - entrada_inicial) > max_deriva:
                self.cancelar_orden(account_number, order_id)
                _ps(f"[CHASE] {orden.ticker} cancelada: deriva > tope (${max_deriva:.2f})")
                return {"status": "CANCELADA_DERIVA", "order_id": order_id}

            # ¿Sigue compensando el R/B? (usa TP1 si existe, si no el TP final)
            objetivo = orden.take_profit_parcial or orden.take_profit
            riesgo = abs(nueva_entrada - orden.stop_loss)
            if not objetivo or riesgo <= 0:
                # Sin objetivo fiable → no perseguir a ciegas: cancelar
                self.cancelar_orden(account_number, order_id)
                return {"status": "CANCELADA_SIN_TP", "order_id": order_id}
            rb = abs(objetivo - nueva_entrada) / riesgo
            if rb < self.chase_cancelar_si_rb_menor:
                self.cancelar_orden(account_number, order_id)
                _ps(f"[CHASE] {orden.ticker} cancelada: R/B {rb:.2f} < {self.chase_cancelar_si_rb_menor}")
                return {"status": "CANCELADA_RB", "order_id": order_id, "rb": round(rb, 2)}

            # Cancelar la orden vieja y CONFIRMAR antes de re-cotizar (evita duplicados)
            if not self.cancelar_orden(account_number, order_id):
                estado = self._estado_orden(account_number, order_id)
                if estado == "FILLED":
                    # Se llenó mientras intentábamos cancelar: aceptar el llenado
                    resultado["perseguido"] = True
                    return resultado
                # No pudimos cancelar y no está llenada: abortar sin duplicar
                _ps(f"[CHASE] {orden.ticker}: cancelación falló (estado {estado}) — no se re-cotiza")
                return {"status": "CANCEL_FALLO", "order_id": order_id, "estado": estado}

            orden.precio_limite = round(nueva_entrada, 2)
            entrada = orden.precio_limite
            resultado = self._enviar_orden_real(orden, "LIMIT", account_number,
                                                motivo + f" | chase #{intento} @ ${entrada:.2f}")
            if resultado.get("status") != "OK":
                return resultado
            order_id = resultado.get("order_id", order_id)
            _ps(f"[CHASE] {orden.ticker} persiguiendo #{intento} → LIMIT ${entrada:.2f}")

        resultado["perseguido"] = True
        resultado["intentos_chase"] = self.chase_max_reintentos
        return resultado

    def _buscar_orden_en_listado(self, account_number: str, order_id: str) -> Optional[dict]:
        """Obtiene UNA orden, usando primero el endpoint por id y luego el listado.

        El GET por id reduce la latencia y el tamaño de respuesta. Como algunas
        cuentas han mostrado errores 5xx intermitentes en ese endpoint, el listado
        temporal se conserva como fallback de reconciliación.
        """
        if not order_id:
            return None
        direct_url = f"{self.TRADER_URL}/accounts/{account_number}/orders/{order_id}"
        direct_error = None
        try:
            direct = self._http_session().get(
                direct_url, timeout=self.request_timeout_s)
            direct.raise_for_status()
            data = direct.json()
            if (isinstance(data, dict)
                    and str(data.get("orderId", "")) == str(order_id)
                    and isinstance(data.get("status"), str) and data["status"].strip()):
                self._report_order_read_recovery(account_number)
                return data
            direct_error = ValueError("Invalid order detail response")
        except Exception as exc:
            # El fallback de listado es deliberado; no se registra aquí para evitar
            # ruido por cada poll. Si ambos caminos fallan sí se registra abajo.
            direct_error = exc

        ahora = datetime.now(timezone.utc)
        desde = ahora - timedelta(days=2)
        # El límite SUPERIOR se adelanta 1 día: si el reloj del VPS va ATRASADO,
        # la 'enteredTime' real (reloj de Schwab) de una orden recién puesta sería
        # POSTERIOR a 'ahora' y quedaría FUERA de la ventana. El colchón evita que
        # un reloj desfasado oculte órdenes recientes de la consulta.
        hasta = ahora + timedelta(days=1)
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        url = f"{self.TRADER_URL}/accounts/{account_number}/orders"
        try:
            r = self._http_session().get(url, params={
                "fromEnteredTime": desde.strftime(fmt),
                "toEnteredTime": hasta.strftime(fmt),
                "maxResults": 500,
            }, timeout=self.request_timeout_s)
            r.raise_for_status()
            data = r.json()
            if not valid_order_snapshot(data):
                raise ValueError("Invalid order list response")
            self._report_order_read_recovery(account_number)
            objetivo = str(order_id)
            for o in self._iter_order_nodes(data):
                if str(o.get("orderId", "")) == objetivo:
                    return o
            return None
        except Exception as e:
            self._report_order_read_failure(account_number, direct_error, e)
            return None


    def _working_orders_snapshot(self, account_number: str) -> Optional[list]:
        """Return the current Schwab WORKING-order snapshot, or ``None`` on read failure.

        v3.3.5 prefers the shared SchwabClient provider. This removes a
        split-brain failure where dashboard/startup reconciliation successfully
        read ``[]`` while SmartExit's separate raw HTTP session returned unknown
        and permanently blocked reversals/TP/kill-switch exits.
        """
        if self.paper_trading:
            return []
        if not account_number:
            return None
        if self._working_orders_provider is not None:
            try:
                data = self._working_orders_provider(account_number)
            except Exception as exc:
                _ps(f"[ERROR] provider órdenes WORKING: {_err_detalle(exc)}")
                return None
            return data if valid_order_snapshot(data) else None
        ahora = datetime.now(timezone.utc)
        desde = ahora - timedelta(days=7)
        hasta = ahora + timedelta(days=1)
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        url = f"{self.TRADER_URL}/accounts/{account_number}/orders"
        try:
            r = self._http_session().get(
                url,
                params={
                    "status": "WORKING",
                    "fromEnteredTime": desde.strftime(fmt),
                    "toEnteredTime": hasta.strftime(fmt),
                    "maxResults": 500,
                },
                timeout=self.request_timeout_s,
            )
            r.raise_for_status()
            data = r.json()
            return data if valid_order_snapshot(data) else None
        except Exception as exc:
            _ps(f"[ERROR] snapshot órdenes WORKING: {_err_detalle(exc)}")
            return None

    @staticmethod
    def _iter_order_nodes(orders):
        for order in orders or []:
            if not isinstance(order, dict):
                continue
            yield order
            children = order.get("childOrderStrategies", []) or []
            yield from EjecutorHibridoDeOrdenes._iter_order_nodes(children)

    def orden_trabajando(
        self, account_number: str, order_id: str
    ) -> Optional[bool]:
        """True/False only when the current WORKING snapshot was read successfully.

        ``False`` means the order is no longer working. It may have been canceled,
        rejected, expired, or filled; callers must still reconcile the broker
        position before deciding whether a replacement/close is safe.
        """
        if not order_id:
            return False
        snapshot = self._working_orders_snapshot(account_number)
        if snapshot is None:
            return None
        objetivo = str(order_id)
        return any(
            str(order.get("orderId", "") or "") == objetivo
            for order in self._iter_order_nodes(snapshot)
        )

    def ordenes_trabajando_ticker(
        self, account_number: str, ticker: str
    ) -> Optional[list]:
        """Return WORKING order ids for one symbol, or ``None`` if unreadable.

        v3.3.2 uses this as a safety barrier before retiring a stale local exit
        record.  An empty list is authoritative evidence that no broker order is
        currently reserving/closing shares for the symbol.
        """
        snapshot = self._working_orders_snapshot(account_number)
        if snapshot is None:
            return None
        wanted = str(ticker or "").upper()
        ids = []
        seen = set()
        for order in self._iter_order_nodes(snapshot):
            try:
                legs = order.get("orderLegCollection", []) or []
                symbol = ""
                for leg in legs:
                    instrument = (leg or {}).get("instrument", {}) or {}
                    symbol = str(instrument.get("symbol", "") or "").upper()
                    if symbol:
                        break
                if symbol != wanted:
                    continue
                oid = str(order.get("orderId", "") or "")
                if oid and oid not in seen:
                    seen.add(oid)
                    ids.append(oid)
            except Exception:
                continue
        return ids

    def detalle_orden(self, account_number: str, order_id: str) -> dict:
        """
        Devuelve el detalle de una orden: {status, filled, avg_price}.
        'filled' = acciones ejecutadas; 'avg_price' = precio medio real de
        ejecución (para no asumir el precio y calcular bien SL/TP/PnL).

        Consulta vía el LISTADO de órdenes (fiable), no el endpoint por-id de
        Schwab (que da 'Internal Server Error' intermitente). Si la orden no
        aparece, devuelve DESCONOCIDO y el motor reconcilia por POSICIONES reales.
        """
        if self.paper_trading:
            return {"status": "FILLED", "filled": 0.0, "avg_price": 0.0}
        if not order_id:
            return {"status": "DESCONOCIDO", "filled": 0.0, "avg_price": 0.0}
        d = self._buscar_orden_en_listado(account_number, order_id)
        if not d:
            # The historical/detail endpoint has shown intermittent 5xx/empty
            # responses while the current WORKING snapshot is healthy.  Treat a
            # successful live snapshot as sufficient acknowledgement that the
            # submitted order exists, without inventing any terminal/fill data.
            snapshot = self._working_orders_snapshot(account_number)
            for order in self._iter_order_nodes(snapshot):
                if (str(order.get("orderId", "")) == str(order_id)
                        and normalize_status(order.get("status")) == "WORKING"):
                    detail = self._detalle_desde_orden(order)
                    detail["status_source"] = "WORKING_SNAPSHOT"
                    return detail
            return {"status": "DESCONOCIDO", "filled": 0.0, "avg_price": 0.0}
        return self._detalle_desde_orden(d)

    @staticmethod
    def _detalle_desde_orden(d):
        """Preserve actual quantities and execution prices from either endpoint."""
        status = str(d.get("status", "DESCONOCIDO")).upper()
        # Acciones ejecutadas + precio medio desde las PATAS de ejecución. Schwab
        # a veces NO reporta filledQuantity en una orden CANCELED tras un llenado
        # parcial, pero las patas SÍ registran la ejecución: se toma el MÁXIMO
        # entre ambas fuentes para no ignorar acciones ya adquiridas.
        precios, cantidades = [], []
        valid_prices = True
        seen_executions = set()
        for act in (d.get("orderActivityCollection", []) or []):
            if not isinstance(act, dict):
                valid_prices = False
                continue
            execution_id = act.get("executionId")
            if execution_id is not None:
                if str(execution_id) in seen_executions:
                    valid_prices = False
                    continue
                seen_executions.add(str(execution_id))
            if (act.get("activityType") not in (None, "EXECUTION")
                    or act.get("executionType") not in (None, "FILL")):
                valid_prices = False
                continue
            for leg in (act.get("executionLegs", []) or []):
                try:
                    price, qty = float(leg.get("price", 0) or 0), float(leg.get("quantity", 0) or 0)
                    if not math.isfinite(qty) or qty <= 0:
                        raise ValueError("Invalid fill quantity")
                    cantidades.append(qty)
                    precios.append(price)
                    valid_prices = valid_prices and math.isfinite(price) and price > 0
                except (AttributeError, TypeError, ValueError, OverflowError):
                    valid_prices = False
        try:
            filled_field = float(d.get("filledQuantity", 0) or 0)
            if not math.isfinite(filled_field) or filled_field < 0:
                raise ValueError("Invalid broker quantity")
        except (TypeError, ValueError, OverflowError):
            filled_field = 0.0
            valid_prices = False
        filled_legs = sum(cantidades)
        filled = max(filled_field, filled_legs)
        valid_prices = bool(valid_prices and filled_legs > 0
                            and math.isfinite(filled)
                            and abs(filled - filled_legs) <= 1e-6
                            and len(d.get("orderLegCollection") or []) <= 1)
        if valid_prices:
            avg = sum(p * q for p, q in zip(precios, cantidades)) / filled_legs
            valid_prices = math.isfinite(avg) and avg > 0
            if not valid_prices:
                avg = 0.0
        else:
            # A limit is an instruction, never evidence of an execution price.
            avg = 0.0
        return {"status": status, "filled": filled, "avg_price": avg,
                "fill_price_verified": bool(valid_prices)}

    def esperar_estado_orden(self, account_number: str, order_id: str,
                             deseados, fallos=(), timeout_s: Optional[float] = None) -> dict:
        """Espera por polling asíncrono hasta observar una transición real."""
        if self.paper_trading:
            return {
                "confirmado": True,
                "broker_status": "FILLED",
                "filled": 0.0,
                "avg_price": 0.0,
                "latency_ms": 0,
                "timeout": False,
                "error": "",
            }
        result = self._sync.run_order(
            lambda: self.detalle_orden(account_number, order_id),
            desired_statuses=deseados,
            failure_statuses=fallos,
            timeout_s=self.ack_timeout_s if timeout_s is None else timeout_s,
        )
        return {
            "confirmado": result.confirmed,
            "broker_status": result.status,
            "filled": result.filled,
            "avg_price": result.avg_price,
            "latency_ms": result.elapsed_ms,
            "timeout": result.timed_out,
            "error": result.error,
        }

    def _estado_orden(self, account_number: str, order_id: str) -> str:
        """Consulta el estado de una orden (WORKING/FILLED/...) vía el LISTADO."""
        if not order_id:
            return "DESCONOCIDO"
        observation = getattr(self._order_observation_local, "scope", None)
        if observation:
            account, working_ids, generation = observation
            with self._order_observation_lock:
                if (account and account == str(account_number)
                        and generation == self._order_observation_generation
                        and str(order_id) in working_ids):
                    return "WORKING"
        d = self._buscar_orden_en_listado(account_number, order_id)
        if not d:
            return "DESCONOCIDO"
        return str(d.get("status", "DESCONOCIDO")).upper()

    # =========================================================
    # SALIDA PARCIAL 70/30 (BRACKET)
    # =========================================================

    def colocar_salida(self, ticker: str, tipo_entrada: str, cantidad: int,
                       precio: float, tipo_orden: str, account_number: str) -> dict:
        """
        Coloca una orden de salida (cierre) para 'cantidad' acciones.

        tipo_entrada → dirección de la ENTRADA ('BUY'/'SELL'); el cierre es
        la instrucción opuesta. tipo_orden → 'LIMIT' (con 'precio') o 'STOP'.
        """
        if cantidad <= 0:
            self._audit(
                "EXIT_ORDER_REJECTED",
                ticker=ticker, lado=tipo_entrada, estado="ERROR",
                cantidad=cantidad, precio=precio, motivo="cantidad inválida",
                detalle={"tipo_orden": tipo_orden},
            )
            return {"status": "ERROR", "mensaje": "cantidad inválida"}
        if self.paper_trading:
            result = {"status": "SIMULADA",
                      "order_id": f"SALIDA-{ticker}-{tipo_orden}-{cantidad}",
                      "tipo_orden": tipo_orden, "precio": precio, "cantidad": cantidad}
            self._audit(
                "STOP_SIMULATED" if tipo_orden == "STOP" else "TP1_SIMULATED",
                ticker=ticker, lado=tipo_entrada, estado="SIMULADA",
                order_id=result["order_id"], cantidad=cantidad, precio=precio,
                detalle={"tipo_orden": tipo_orden},
            )
            return result

        instruccion = _instruccion_cierre(tipo_entrada)
        cuerpo = {
            "session": "NORMAL", "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderType": tipo_orden,
            "orderLegCollection": [{
                "orderLegType": "EQUITY", "legId": 1,
                "instruction": instruccion,
                "positionEffect": "CLOSING",
                "quantity": cantidad,
                "instrument": {"symbol": ticker, "assetType": "EQUITY"},
            }],
        }
        if tipo_orden == "LIMIT":
            cuerpo["price"] = str(round(precio, 2))
        elif tipo_orden == "STOP":
            cuerpo["stopPrice"] = str(round(precio, 2))

        url = f"{self.TRADER_URL}/accounts/{account_number}/orders"
        try:
            resp = self._submit_order_request(
                "post", url, json=cuerpo, timeout=self.request_timeout_s)
            resp.raise_for_status()
            order_id = resp.headers.get("Location", "").split("/")[-1]
            event_prefix = "STOP" if tipo_orden == "STOP" else "TP1"
            self._audit(
                f"{event_prefix}_SUBMITTED",
                ticker=ticker, lado=tipo_entrada, estado="ACEPTADA_HTTP",
                order_id=order_id, cantidad=cantidad, precio=precio,
                detalle={"tipo_orden": tipo_orden, "duration": cuerpo.get("duration")},
            )
            if not order_id:
                self._audit(
                    f"{event_prefix}_RECONCILE_REQUIRED",
                    ticker=ticker, lado=tipo_entrada, estado="SIN_ORDER_ID",
                    cantidad=cantidad, precio=precio,
                    motivo="Schwab aceptó la salida sin devolver order_id",
                )
                return {
                    "status": "RECONCILIAR", "order_id": "",
                    "tipo_orden": tipo_orden, "precio": precio, "cantidad": cantidad,
                    "mensaje": "Schwab aceptó la salida sin devolver Location/order_id",
                }
            confirmacion = self.esperar_estado_orden(
                account_number,
                order_id,
                deseados=ESTADOS_VISIBLES,
                fallos=ESTADOS_FALLO_ENVIO,
                timeout_s=self.ack_timeout_s,
            )
            status = "OK" if confirmacion["confirmado"] else "RECONCILIAR"
            self._audit(
                f"{event_prefix}_ACKNOWLEDGED" if confirmacion["confirmado"]
                else f"{event_prefix}_RECONCILE_REQUIRED",
                ticker=ticker, lado=tipo_entrada,
                estado=confirmacion.get("broker_status", "DESCONOCIDO"),
                order_id=order_id, cantidad=cantidad, precio=precio,
                latency_ms=confirmacion.get("latency_ms"),
                motivo=confirmacion.get("error", ""),
            )
            return {
                "status": status, "order_id": order_id, "tipo_orden": tipo_orden,
                "precio": precio, "cantidad": cantidad, **confirmacion,
            }
        except requests.exceptions.RequestException as e:
            _ps(f"[ERROR] salida {ticker}: {e}")
            event_prefix = "STOP" if tipo_orden == "STOP" else "TP1"
            self._audit(
                f"{event_prefix}_REJECTED",
                ticker=ticker, lado=tipo_entrada, estado="ERROR",
                cantidad=cantidad, precio=precio, motivo=_err_detalle(e),
            )
            return {"status": "ERROR", "mensaje": str(e)}

    def montar_proteccion_parcial(self, orden: OrdenTradingCalculada,
                                  account_number: str) -> dict:
        """
        Following a confirmed entry fill, place a full-quantity STOP first.

        LIVE SAFETY: The former independent STOP 100% + TP1 LIMIT 70% created
        overlapping closing orders rejected by Schwab. REAL execution retains
        only its verified STOP; the 70/30 TP1 is suspended until a linked,
        quantity-safe strategy is implemented and broker-verified. The engine
        separately blocks new REAL entries that require this partial plan.
        PAPER retains its existing simulated 70/30 workflow.
        """
        # STOP PRIMERO: si el segundo POST falla, la posición sigue protegida.
        # Colocar TP1 antes del stop dejaba una ventana de exposición desnuda.
        sl = self.colocar_salida(orden.ticker, orden.tipo, orden.cantidad,
                                 orden.stop_loss, "STOP", account_number)
        if sl.get("status") != "OK":
            return {"tp1": {}, "sl": sl}
        if not self.paper_trading:
            # Last-resort guard for an already-filled REAL position. Schwab
            # rejected TSLA's unlinked BUY_TO_COVER TP1 2 while its full STOP 3
            # was WORKING. Keep the acknowledged full STOP and never send a
            # second closing order exceeding broker exposure. New partial REAL
            # entries are blocked in the engine until a linked exit is verified.
            self._audit(
                "TP1_SUSPENDED_CONFLICT",
                ticker=orden.ticker, lado=orden.tipo,
                estado="NO_ORDER_SENT",
                parent_order_id=str(sl.get("order_id") or ""),
                cantidad=orden.cantidad_parcial,
                precio=orden.take_profit_parcial,
                motivo="Standalone TP1 conflicts with a full-position STOP; "
                       "Schwab oversold/overbought rejection",
            )
            return {"tp1": {}, "sl": sl, "partial_exit_suspended": True}
        tp1 = self.colocar_salida(orden.ticker, orden.tipo, orden.cantidad_parcial,
                                  orden.take_profit_parcial, "LIMIT", account_number)
        return {"tp1": tp1, "sl": sl}

    def mover_stop_breakeven(self, ticker: str, tipo_entrada: str, cantidad_resto: int,
                             precio_entrada: float, sl_order_id: str,
                             account_number: str) -> dict:
        """
        Mueve la protección del 30% restante a breakeven: cancela el STOP
        anterior (del 100%) y coloca un nuevo STOP del 30% en el precio de
        entrada. Se llama cuando TP1 (el 70%) ya se ejecutó.
        """
        cancelacion = {"confirmado": True, "broker_status": "SIN_ORDEN"}
        if sl_order_id:
            cancelacion = self.cancelar_orden_confirmada(
                account_number, sl_order_id, ticker=ticker)
            if not cancelacion.get("confirmado"):
                # If the stop filled while cancellation raced, never place a new
                # protective order until the broker position is reconciled.
                result = {
                    "status": "CANCEL_FALLO",
                    "order_id": "",
                    "cancelacion": cancelacion,
                    "mensaje": "STOP original no confirmado como cancelado/terminal",
                }
                self._audit(
                    "STOP_BREAKEVEN_FAILED",
                    ticker=ticker, lado=tipo_entrada,
                    estado=cancelacion.get("broker_status", "DESCONOCIDO"),
                    parent_order_id=sl_order_id, cantidad=cantidad_resto,
                    precio=precio_entrada,
                    latency_ms=cancelacion.get("latency_ms"),
                    motivo=result["mensaje"],
                )
                return result
            if cancelacion.get("terminal_by_absence"):
                snapshot = self._position_snapshot(account_number, ticker)
                if snapshot is None:
                    result = {
                        "status": "CANCEL_FALLO",
                        "order_id": "",
                        "cancelacion": cancelacion,
                        "mensaje": (
                            "STOP ya no figura WORKING, pero no se pudo confirmar "
                            "la posición antes de re-proteger"
                        ),
                    }
                    self._audit(
                        "STOP_BREAKEVEN_FAILED", ticker=ticker,
                        lado=tipo_entrada, estado="BROKER_POSITION_UNKNOWN",
                        parent_order_id=sl_order_id, cantidad=cantidad_resto,
                        precio=precio_entrada, motivo=result["mensaje"],
                    )
                    return result
                broker_qty = int(
                    snapshot.get("long", 0) or 0
                ) + int(snapshot.get("short", 0) or 0)
                if broker_qty <= 0:
                    result = {
                        "status": "POSITION_FLAT",
                        "order_id": "",
                        "cancelacion": cancelacion,
                        "mensaje": "El STOP dejó la posición plana; no se coloca otro STOP",
                    }
                    self._audit(
                        "STOP_BREAKEVEN_SKIPPED_FLAT", ticker=ticker,
                        lado=tipo_entrada, estado="FLAT",
                        parent_order_id=sl_order_id, cantidad=0,
                        precio=precio_entrada, motivo=result["mensaje"],
                    )
                    return result
                cantidad_resto = min(int(cantidad_resto), broker_qty)
        nuevo = self.colocar_salida(
            ticker, tipo_entrada, cantidad_resto,
            precio_entrada, "STOP", account_number,
        )
        nuevo["cancelacion"] = cancelacion
        self._audit(
            "STOP_MOVED_TO_BREAKEVEN"
            if nuevo.get("status") in ("OK", "SIMULADA")
            else "STOP_BREAKEVEN_FAILED",
            ticker=ticker, lado=tipo_entrada,
            estado=nuevo.get("broker_status", nuevo.get("status", "")),
            order_id=nuevo.get("order_id", ""),
            parent_order_id=sl_order_id,
            cantidad=cantidad_resto, precio=precio_entrada,
            latency_ms=nuevo.get("latency_ms"),
            motivo=nuevo.get("mensaje", ""),
        )
        return nuevo

    # =========================================================
    # LIQUIDACIÓN (REVERSIÓN + KILL SWITCH)
    # =========================================================

    def preparar_cierre(self, ticker: str, motivo: str = "") -> dict:
        """Read-only preflight BEFORE the engine cancels protective orders."""
        from core.execution_v2 import SmartExit
        if self.paper_trading or SmartExit._is_emergency(motivo):
            return {'ready': True}
        if self._smart_exit is None:
            self._smart_exit = SmartExit(self, self.execution_v2_config)
        return {'ready': bool(self._smart_exit._quote(ticker)),
                'policy': 'LIMIT_ONLY',
                'reason': 'Se requiere bid/ask reciente para cierre LIMIT; protecciones conservadas'}

    def liquidar_posicion(self, ticker: str, tipo_entrada: str, cantidad: int,
                          account_number: str, motivo: str = "") -> dict:
        from core.execution_v2 import SmartExit
        if self.paper_trading or (not self.execution_v2_config.get('enabled', False)
                                 and SmartExit._is_emergency(motivo)):
            return self._liquidar_market_legacy(ticker, tipo_entrada, cantidad, account_number, motivo)
        with self.secuencia_simbolo(ticker):
            if self._smart_exit is None:
                self._smart_exit = SmartExit(self, self.execution_v2_config)
            context = getattr(self._lifecycle_local, 'fields', None) or {}
            try:
                return self._smart_exit.close(ticker, tipo_entrada, cantidad, account_number, motivo,
                                              context.get('exit_signal_price'),
                                              position_id=context.get('position_id'))
            except Exception as exc:
                return {'status': 'RECONCILIAR', 'posicion_plana': False,
                        'mensaje': f'Execution recovery required: {type(exc).__name__}'}

    def _liquidar_market_legacy(self, ticker: str, tipo_entrada: str, cantidad: int,
                          account_number: str, motivo: str = "") -> dict:
        """Close the *actual broker position* and confirm the account is flat.

        v2.9.5 safety rule: Schwab is the source of truth for side and quantity.
        Local state may be stale after a rejected reversal/cancel; using the local
        side in that situation can submit the wrong closing instruction and turn a
        recovery attempt into a new exposure.  This method therefore reads the
        broker position first and derives SELL vs BUY_TO_COVER from that snapshot.
        """
        if cantidad <= 0:
            self._audit(
                "EXIT_REJECTED", ticker=ticker, lado=tipo_entrada,
                estado="ERROR", cantidad=cantidad, motivo="sin cantidad para liquidar",
            )
            return {"status": "ERROR", "mensaje": "sin cantidad para liquidar"}
        if self.paper_trading:
            _ps(f"[PAPER] Liquidando {cantidad}x {ticker} (entrada {tipo_entrada}) {motivo}")
            result = {"status": "SIMULADA", "order_id": f"LIQ-{ticker}", "cantidad": cantidad}
            self._audit(
                "EXIT_SIMULATED", ticker=ticker, lado=tipo_entrada,
                estado="SIMULADA", order_id=result["order_id"],
                cantidad=cantidad, motivo=motivo,
            )
            return result

        snapshot = self._position_snapshot(account_number, ticker)
        if snapshot is None:
            # Compatibility with old/unit-test transport doubles that implement
            # POST but intentionally no GET. A real requests.Session always has
            # GET, so production remains fail-closed when broker position lookup
            # itself fails.
            try:
                transport_has_get = hasattr(self._http_session(), "get")
            except Exception:
                transport_has_get = True
            if not transport_has_get:
                local_side = str(tipo_entrada or "").upper()
                snapshot = {
                    "long": int(cantidad) if local_side == "BUY" else 0,
                    "short": int(cantidad) if local_side == "SELL" else 0,
                    "average_price": 0.0,
                }
            else:
                msg = "no se pudo confirmar lado/cantidad real en Schwab"
                self._audit(
                    "EXIT_REJECTED", ticker=ticker, lado=tipo_entrada,
                    estado="BROKER_POSITION_UNKNOWN", cantidad=cantidad, motivo=msg,
                )
                return {"status": "ERROR", "mensaje": msg}

        long_q = int(snapshot.get("long", 0) or 0)
        short_q = int(snapshot.get("short", 0) or 0)
        if long_q <= 0 and short_q <= 0:
            self._audit(
                "EXIT_ALREADY_FLAT", ticker=ticker, lado=tipo_entrada,
                estado="FLAT", cantidad=0, motivo=motivo,
            )
            return {
                "status": "OK", "order_id": "", "cantidad": 0,
                "broker_status": "FLAT", "latency_ms": 0,
                "posicion_plana": True, "avg_price": 0.0,
                "broker_side": "OUT",
            }
        if long_q > 0 and short_q > 0:
            msg = f"posición broker ambigua: long={long_q}, short={short_q}"
            self._audit(
                "EXIT_REJECTED", ticker=ticker, lado=tipo_entrada,
                estado="AMBIGUOUS_BROKER_POSITION", cantidad=long_q + short_q,
                motivo=msg,
            )
            return {"status": "ERROR", "mensaje": msg}

        broker_tipo = "BUY" if long_q > 0 else "SELL"
        broker_qty = long_q if long_q > 0 else short_q
        if str(tipo_entrada or "").upper() != broker_tipo:
            _ps(
                f"[WARN] {ticker}: estado local={tipo_entrada}, broker={broker_tipo}; "
                "la liquidación usará el broker como fuente de verdad"
            )
            self._audit(
                "EXIT_LOCAL_BROKER_SIDE_MISMATCH", ticker=ticker,
                lado=broker_tipo, estado="BROKER_TRUTH_USED", cantidad=broker_qty,
                motivo=f"local={tipo_entrada}; broker={broker_tipo}",
            )

        instruccion = _instruccion_cierre(broker_tipo)
        cuerpo = {
            "session": "NORMAL", "duration": "DAY",
            "orderStrategyType": "SINGLE", "orderType": "MARKET",
            "orderLegCollection": [{
                "orderLegType": "EQUITY", "legId": 1,
                "instruction": instruccion, "positionEffect": "CLOSING",
                "quantity": broker_qty,
                "instrument": {"symbol": ticker, "assetType": "EQUITY"},
            }],
        }
        url = f"{self.TRADER_URL}/accounts/{account_number}/orders"
        try:
            resp = self._submit_order_request(
                "post", url, json=cuerpo, timeout=self.request_timeout_s)
            resp.raise_for_status()
            order_id = resp.headers.get("Location", "").split("/")[-1]
            self._audit(
                "EXIT_SUBMITTED", ticker=ticker, lado=broker_tipo,
                estado="ACEPTADA_HTTP", order_id=order_id,
                cantidad=broker_qty, motivo=motivo,
                detalle={
                    "tipo_orden": "MARKET", "duration": cuerpo.get("duration"),
                    "broker_side": broker_tipo,
                    "requested_local_side": tipo_entrada,
                    "requested_quantity": cantidad,
                },
            )
            if not order_id:
                self._audit(
                    "EXIT_RECONCILE_REQUIRED", ticker=ticker, lado=broker_tipo,
                    estado="SIN_ORDER_ID", cantidad=broker_qty, motivo=motivo,
                )
                return {
                    "status": "RECONCILIAR", "order_id": "",
                    "cantidad": broker_qty,
                    "mensaje": "Liquidación aceptada sin order_id; no se puede confirmar",
                    "broker_side": broker_tipo,
                }

            llenado = self.esperar_estado_orden(
                account_number, order_id, deseados={"FILLED"},
                fallos=ESTADOS_FALLO_LLENADO, timeout_s=self.fill_timeout_s,
            )
            plano = self.esperar_posicion_plana(
                account_number, ticker, timeout_s=self.flat_timeout_s,
            )
            if plano["confirmado"]:
                detalle_fill = self.detalle_orden(account_number, order_id)
                avg_price = float(detalle_fill.get("avg_price", 0.0) or 0.0)
                _ps(f"[LIQUIDAR] {ticker} confirmado PLANO en "
                    f"{llenado['latency_ms'] + plano['latency_ms']}ms | ID {order_id} | {motivo}")
                price_verified = bool(
                    avg_price > 0 and str(llenado.get("broker_status", "")).upper() == "FILLED"
                )
                result = {
                    "status": "OK", "order_id": order_id, "cantidad": broker_qty,
                    "broker_status": llenado["broker_status"],
                    "latency_ms": llenado["latency_ms"] + plano["latency_ms"],
                    "posicion_plana": True, "avg_price": avg_price,
                    "broker_side": broker_tipo,
                    "fill_price_verified": price_verified,
                }
                self._audit(
                    "EXIT_FILLED" if price_verified else "EXIT_FLAT_PRICE_UNVERIFIED",
                    ticker=ticker, lado=broker_tipo,
                    estado=llenado.get("broker_status", "DESCONOCIDO"),
                    order_id=order_id, cantidad=broker_qty,
                    latency_ms=result["latency_ms"], motivo=motivo,
                    precio=avg_price if price_verified else "",
                    detalle={
                        "posicion_plana": True,
                        "avg_price": avg_price if price_verified else None,
                        "fill_price_verified": price_verified,
                    },
                )
                return result
            status = "ERROR" if (
                llenado["broker_status"] in ESTADOS_FALLO_LLENADO
            ) else "PENDIENTE_CONFIRMACION"
            result = {
                "status": status, "order_id": order_id,
                "cantidad": broker_qty, "broker_status": llenado["broker_status"],
                "latency_ms": llenado["latency_ms"] + plano["latency_ms"],
                "posicion_plana": False, "broker_side": broker_tipo,
                "mensaje": plano.get("error") or llenado.get("error")
                           or "La posición todavía no está confirmada como plana",
            }
            self._audit(
                "EXIT_FAILED" if status == "ERROR" else "EXIT_PENDING_CONFIRMATION",
                ticker=ticker, lado=broker_tipo,
                estado=llenado.get("broker_status", status),
                order_id=order_id, cantidad=broker_qty,
                latency_ms=result["latency_ms"], motivo=result["mensaje"],
                detalle={"posicion_plana": False, "cierre_motivo": motivo},
            )
            return result
        except requests.exceptions.RequestException as e:
            detail = _err_detalle(e)
            _ps(f"[ERROR] liquidando {ticker}: {detail}")
            self._audit(
                "EXIT_REJECTED", ticker=ticker, lado=broker_tipo,
                estado="ERROR", cantidad=broker_qty, motivo=detail,
                detalle={
                    "cierre_motivo": motivo, "broker_side": broker_tipo,
                    "requested_local_side": tipo_entrada,
                    "requested_quantity": cantidad,
                },
            )
            return {
                "status": "ERROR", "mensaje": detail,
                "broker_side": broker_tipo, "cantidad": broker_qty,
            }

    def _position_snapshot(self, account_number: str, ticker: str) -> Optional[dict]:
        """Return authoritative long/short quantities for one Schwab symbol.

        Production prefers the shared SchwabClient position provider so every
        caller gets the same token refresh, account-hash and reconciliation
        semantics. The legacy raw GET remains only as a compatibility fallback
        when no provider was configured (e.g. isolated unit tests).
        """
        wanted = str(ticker or "").upper()

        if self._position_provider is not None:
            try:
                positions = self._position_provider(account_number)
            except Exception as exc:
                _ps(f"[ERROR] provider posición {wanted}: {_err_detalle(exc)}")
                return None

            # None = unknown API state. Never reinterpret it as flat.
            if positions is None:
                return None

            try:
                for p in positions or []:
                    symbol = str((p or {}).get("symbol", "")).upper()
                    if symbol != wanted:
                        continue
                    side = str((p or {}).get("tipo", "")).upper()
                    qty = int(float((p or {}).get("cantidad", 0) or 0))
                    avg = float((p or {}).get("precio", 0) or 0)
                    if side == "BUY":
                        return {"long": qty, "short": 0, "average_price": avg}
                    if side == "SELL":
                        return {"long": 0, "short": qty, "average_price": avg}
                    # Unknown side with nonzero quantity is ambiguous -> fail closed.
                    if qty:
                        return None
                return {"long": 0, "short": 0, "average_price": 0.0}
            except Exception as exc:
                _ps(f"[ERROR] normalizando posición {wanted}: {_err_detalle(exc)}")
                return None

        try:
            r = self._http_session().get(
                f"{self.TRADER_URL}/accounts/{account_number}",
                params={"fields": "positions"}, timeout=self.request_timeout_s,
            )
            r.raise_for_status()
            account = (r.json() or {}).get("securitiesAccount", {})
            for p in account.get("positions", []) or []:
                instrument = p.get("instrument", {}) or {}
                if str(instrument.get("symbol", "")).upper() != wanted:
                    continue
                return {
                    "long": int(float(p.get("longQuantity", 0) or 0)),
                    "short": int(float(p.get("shortQuantity", 0) or 0)),
                    "average_price": float(p.get("averagePrice", 0) or 0),
                }
            return {"long": 0, "short": 0, "average_price": 0.0}
        except Exception as exc:
            _ps(f"[ERROR] snapshot posición {wanted}: {_err_detalle(exc)}")
            return None

    def _cantidad_posicion(self, account_number: str, ticker: str) -> Optional[int]:
        """Cantidad absoluta del ticker; None significa que la consulta falló."""
        snapshot = self._position_snapshot(account_number, ticker)
        if snapshot is None:
            return None
        try:
            return int(
                abs(int(snapshot.get("long", 0) or 0))
                + abs(int(snapshot.get("short", 0) or 0))
            )
        except Exception as exc:
            _ps(f"[ERROR] cantidad posición {ticker}: {_err_detalle(exc)}")
            return None


    def esperar_posicion_plana(self, account_number: str, ticker: str,
                               timeout_s: Optional[float] = None) -> dict:
        if self.paper_trading:
            return {
                "confirmado": True, "cantidad": 0,
                "latency_ms": 0, "timeout": False, "error": "",
            }
        result = self._sync.run_value(
            lambda: self._cantidad_posicion(account_number, ticker),
            predicate=lambda value: value == 0,
            timeout_s=self.flat_timeout_s if timeout_s is None else timeout_s,
        )
        return {
            "confirmado": result.confirmed,
            "cantidad": result.value,
            "latency_ms": result.elapsed_ms,
            "timeout": result.timed_out,
            "error": result.error,
        }

    def _solicitar_cancelacion(self, account_number: str, order_id: str) -> dict:
        """Send DELETE and preserve the broker error detail for reconciliation.

        v3.0.2 deliberately does *not* equate a failed DELETE with a live order.
        Schwab can answer 4xx when the order already transitioned.  The caller
        therefore re-checks the current WORKING snapshot before deciding whether
        the symbol is still blocked.
        """
        if self.paper_trading:
            _ps(f"[PAPER] Cancelando orden simulada: {order_id}")
            return {
                "aceptada": True,
                "http_status": 200,
                "error": "",
            }
        url = f"{self.TRADER_URL}/accounts/{account_number}/orders/{order_id}"
        try:
            # A DELETE has no JSON body.  Removing Content-Type avoids sending an
            # unnecessary entity header to strict broker gateways.
            resp = self._submit_order_request(
                "delete", url,
                headers={"Content-Type": None, "Accept": "application/json"},
                timeout=self.request_timeout_s,
            )
            resp.raise_for_status()
            return {
                "aceptada": True,
                "http_status": int(getattr(resp, "status_code", 0) or 0),
                "error": "",
            }
        except Exception as exc:
            detail = _err_detalle(exc)
            _ps(f"[ERROR] No se pudo cancelar {order_id}: {detail}")
            status_code = None
            try:
                response = getattr(exc, "response", None)
                if response is not None:
                    status_code = int(response.status_code)
            except Exception:
                status_code = None
            terminal_hint = ""
            try:
                match = re.search(
                    r"Order\s+in\s+state\s+([A-Z_]+)\s+cannot\s+be\s+canceled",
                    detail, flags=re.IGNORECASE,
                )
                if match:
                    terminal_hint = normalize_status(match.group(1))
            except Exception:
                terminal_hint = ""
            return {
                "aceptada": False,
                "http_status": status_code,
                "error": detail,
                "terminal_hint": terminal_hint,
            }

    def cancelar_orden_confirmada(
        self, account_number: str, order_id: str, ticker: str = ""
    ) -> dict:
        """
        Request cancellation and confirm that the order is no longer WORKING.

        The old path required the historical/detail endpoint to expose a terminal
        status.  In the Aug-24 incident that endpoint stayed ``DESCONOCIDO`` while
        the current WORKING snapshot was still the reliable broker view.  v3.0.2
        therefore uses two independent facts:

        * terminal status from ``detalle_orden`` when available;
        * absence/presence in a successfully-read current WORKING snapshot.

        ``NOT_WORKING`` is safe for order-conflict purposes, but it is *not*
        interpreted as CANCELED vs FILLED.  The engine must still read the actual
        broker position before sending a liquidation or releasing local state.
        """
        if self.paper_trading:
            result = {
                "confirmado": True, "broker_status": "CANCELED",
                "latency_ms": 0, "timeout": False, "filled": 0.0, "error": "",
                "solicitud_aceptada": True, "terminal_by_absence": False,
            }
            self._audit(
                "CANCEL_CONFIRMED", ticker=ticker, estado="CANCELED",
                order_id=order_id, latency_ms=0,
            )
            return result
        if not order_id:
            result = {
                "confirmado": False, "broker_status": "DESCONOCIDO",
                "latency_ms": 0, "timeout": False, "filled": 0.0,
                "error": "order_id vacío", "solicitud_aceptada": False,
                "terminal_by_absence": False,
            }
            self._audit(
                "CANCEL_FAILED", ticker=ticker, estado="DESCONOCIDO",
                motivo=result["error"],
            )
            return result

        antes = self.detalle_orden(account_number, order_id)
        estado_antes = normalize_status(antes.get("status"))
        if estado_antes in ESTADOS_CANCELACION_OK:
            result = {
                "confirmado": True, "broker_status": estado_antes,
                "latency_ms": 0, "timeout": False,
                "filled": float(antes.get("filled", 0) or 0), "error": "",
                "solicitud_aceptada": False, "terminal_by_absence": False,
            }
            self._audit(
                "CANCEL_CONFIRMED", ticker=ticker, estado=estado_antes,
                order_id=order_id, cantidad=result["filled"],
                motivo="La orden ya estaba en estado terminal",
            )
            return result
        if estado_antes == "FILLED":
            result = {
                "confirmado": False, "broker_status": "FILLED",
                "latency_ms": 0, "timeout": False,
                "filled": float(antes.get("filled", 0) or 0),
                "error": "la orden se llenó antes de cancelar",
                "solicitud_aceptada": False, "terminal_by_absence": False,
            }
            self._audit(
                "CANCEL_RACE_FILLED", ticker=ticker, estado="FILLED",
                order_id=order_id, cantidad=result["filled"],
                motivo=result["error"],
            )
            return result

        # If the current broker WORKING snapshot is readable and the order is
        # already absent, do not send repeated DELETEs against an order that has
        # already transitioned.  Position reconciliation below the engine decides
        # whether that transition was a cancel/reject/expire or a fill.
        working_before = self.orden_trabajando(account_number, order_id)
        if working_before is False:
            result = {
                "confirmado": True,
                "broker_status": "NOT_WORKING",
                "latency_ms": 0,
                "timeout": False,
                "filled": float(antes.get("filled", 0) or 0),
                "error": "",
                "solicitud_aceptada": False,
                "terminal_by_absence": True,
            }
            self._audit(
                "CANCEL_CONFIRMED_BY_ABSENCE",
                ticker=ticker,
                estado="NOT_WORKING",
                order_id=order_id,
                cantidad=result["filled"],
                motivo="order_id ausente del snapshot WORKING actual",
            )
            return result

        solicitud = self._solicitar_cancelacion(account_number, order_id)
        aceptada = bool(solicitud.get("aceptada"))
        terminal_hint = normalize_status(solicitud.get("terminal_hint"))
        self._audit(
            "CANCEL_REQUESTED", ticker=ticker,
            estado="ACEPTADA_HTTP" if aceptada else "SOLICITUD_FALLIDA",
            order_id=order_id,
            motivo=str(solicitud.get("error", "") or ""),
            detalle={
                "http_status": solicitud.get("http_status"),
                "terminal_hint": terminal_hint,
            },
        )

        # Schwab sometimes rejects DELETE because the order already transitioned
        # (e.g. "Order in state FILLED cannot be canceled").  That response is
        # useful terminal evidence for conflict resolution; it is *not* proof that
        # the position is flat, so callers must still reconcile current exposure.
        if terminal_hint in TERMINAL_ORDER_STATES:
            result = {
                "confirmado": terminal_hint in ESTADOS_CANCELACION_OK,
                "broker_status": terminal_hint,
                "latency_ms": 0,
                "timeout": False,
                "filled": 0.0,
                "error": str(solicitud.get("error", "") or ""),
                "solicitud_aceptada": aceptada,
                "cancel_http_status": solicitud.get("http_status"),
                "terminal_by_absence": False,
                "terminal_by_broker_error": True,
            }
            self._audit(
                "CANCEL_TERMINAL_HINT", ticker=ticker, estado=terminal_hint,
                order_id=order_id, motivo=result["error"],
                detalle={"http_status": solicitud.get("http_status")},
            )
            return result

        # Even when DELETE fails (for example a race with a fill), observe the
        # terminal state for the configured grace period.
        resultado = self.esperar_estado_orden(
            account_number,
            order_id,
            deseados=ESTADOS_CANCELACION_OK,
            fallos={"FILLED"},
            timeout_s=self.cancel_timeout_s,
        )
        resultado["solicitud_aceptada"] = aceptada
        resultado["cancel_http_status"] = solicitud.get("http_status")
        resultado["terminal_by_absence"] = False

        # The detail endpoint may remain unknown while the live WORKING snapshot
        # already proves the order cannot conflict with a replacement/close.
        if not resultado.get("confirmado"):
            working_after = self.orden_trabajando(account_number, order_id)
            if working_after is False:
                resultado.update({
                    "confirmado": True,
                    "broker_status": "NOT_WORKING",
                    "timeout": False,
                    "terminal_by_absence": True,
                    "error": "",
                })
            elif working_after is True and solicitud.get("error"):
                resultado["error"] = str(solicitud.get("error") or "")
            elif solicitud.get("error") and not resultado.get("error"):
                resultado["error"] = str(solicitud.get("error") or "")

        if resultado["confirmado"]:
            _ps(
                f"[CANCEL] {order_id} confirmado {resultado['broker_status']} "
                f"en {resultado['latency_ms']}ms"
            )
        else:
            _ps(
                f"[CANCEL] {order_id} NO confirmado | estado "
                f"{resultado['broker_status']} | {resultado['latency_ms']}ms"
            )
        self._audit(
            (
                "CANCEL_CONFIRMED_BY_ABSENCE"
                if resultado.get("terminal_by_absence")
                else "CANCEL_CONFIRMED"
                if resultado["confirmado"]
                else "CANCEL_FAILED"
            ),
            ticker=ticker,
            estado=resultado.get("broker_status", "DESCONOCIDO"),
            order_id=order_id,
            cantidad=resultado.get("filled"),
            latency_ms=resultado.get("latency_ms"),
            motivo=resultado.get("error", ""),
            detalle={
                "solicitud_aceptada": aceptada,
                "http_status": solicitud.get("http_status"),
                "terminal_by_absence": bool(
                    resultado.get("terminal_by_absence")
                ),
            },
        )
        return resultado

    def cancelar_orden(self, account_number: str, order_id: str) -> bool:
        """Compatibilidad: True únicamente tras confirmación terminal real."""
        return bool(self.cancelar_orden_confirmada(account_number, order_id).get("confirmado"))

