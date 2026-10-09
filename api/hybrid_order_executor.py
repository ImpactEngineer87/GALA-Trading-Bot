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

import os
import re
import requests
import threading
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from typing import Optional, Tuple
from dataclasses import dataclass
from dotenv import load_dotenv
from core.risk_manager import OrdenTradingCalculada
from api.order_sync import AsyncOrderSynchronizer, normalize_status
from api.schwab_transport import (
    SchwabRequestDeferred, SchwabRequestPaused, schwab_control_requests, schwab_request,
)

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
    """Instrucción Schwab para ABRIR una posición."""
    return "BUY" if tipo == "BUY" else "SELL_SHORT"


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
        if access_token:
            self._session.headers.update({
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
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
        # Background observation has a separate clock from short acknowledgement
        # waits. A 100ms synchronizer setting must not become perpetual HTTP polling.
        self.monitor_interval_ms = max(1000, int(sync_cfg.get("monitor_interval_ms", 2000)))
        # v3.3.18: the open-position monitor reuses one account order list for
        # this long (every position and exit order shares it). Confirmations
        # and other callers always read fresh.
        self.monitor_status_max_age_s = max(
            self.monitor_interval_ms, int(sync_cfg.get("monitor_status_max_age_ms", 5000))) / 1000.0
        self._order_list_cache = None   # (monotonic, account, orders)
        self._order_list_lock = threading.Lock()
        self._working_cache = None      # (monotonic, account, WORKING orders)
        self._working_lock = threading.Lock()

        if self.paper_trading:
            _ps("[HIBRIDO] Modo PAPER TRADING — ordenes simuladas")

    def set_audit_hook(self, hook):
        """Injects a best-effort lifecycle audit callback from the engine."""
        self._audit_hook = hook

    def set_quote_provider(self, provider):
        self._quote_provider = provider

    def retire_pending_exit_after_authoritative_flat(self, account, ticker, reason="broker confirmed flat"):
        """Clear a durable pending exit after a read-only broker flat proof.

        No broker request that can mutate the account is sent here.  The caller
        is responsible for proving there is no position and no WORKING order.
        """
        if not self.execution_v2_config.get('enabled', False):
            return False
        from core.execution_v2 import SmartExit
        with self.secuencia_simbolo(str(ticker).upper()):
            if self._smart_exit is None:
                self._smart_exit = SmartExit(self, self.execution_v2_config)
            return self._smart_exit.retire_pending_after_authoritative_flat(
                account, str(ticker).upper(), reason
            )

    def has_pending_exit(self, account, ticker):
        if not self.execution_v2_config.get('enabled', False):
            return False
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
        if access_token:
            session.headers.update({
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
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
            return round(float(orden.precio_limite), 4)
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
            resp = schwab_request(self._http_session().post,
                url, json=cuerpo, timeout=self.request_timeout_s, recovery_safe=False)
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

    @staticmethod
    def _orden_en(orders, order_id) -> Optional[dict]:
        objetivo = str(order_id)
        for o in orders or []:
            if (isinstance(o, dict) and str(o.get("orderId", "")) == objetivo
                    and isinstance(o.get("status"), str) and o["status"]):
                return o
        return None

    def _listado_ordenes(self, account_number: str, max_age_s: float = 0,
                         visto=None) -> Optional[list]:
        """Account orders of the last two days, any status; None if unreadable.

        With ``max_age_s`` a list read that recently is reused, and concurrent
        callers wait for one shared read instead of each sending their own.
        ``visto`` is a list the caller already searched; it is never reused.
        """
        with (self._order_list_lock if max_age_s > 0 else nullcontext()):
            cached = self._order_list_cache
            if (max_age_s > 0 and cached and cached[1] == account_number
                    and cached[2] is not visto
                    and time.monotonic() - cached[0] <= max_age_s):
                return cached[2]
            ahora = datetime.utcnow()
            desde = ahora - timedelta(days=2)
            # El límite SUPERIOR se adelanta 1 día: si el reloj del VPS va ATRASADO,
            # la 'enteredTime' real (reloj de Schwab) de una orden recién puesta sería
            # POSTERIOR a 'ahora' y quedaría FUERA de la ventana. El colchón evita que
            # un reloj desfasado oculte órdenes recientes de la consulta.
            hasta = ahora + timedelta(days=1)
            fmt = "%Y-%m-%dT%H:%M:%S.000Z"
            url = f"{self.TRADER_URL}/accounts/{account_number}/orders"
            try:
                r = schwab_request(self._http_session().get, url, params={
                    "fromEnteredTime": desde.strftime(fmt),
                    "toEnteredTime": hasta.strftime(fmt),
                    "maxResults": 500,
                }, timeout=self.request_timeout_s)
                r.raise_for_status()
                data = r.json()
            except (SchwabRequestPaused, SchwabRequestDeferred):
                return None
            except Exception:
                # The transport persists redacted HTTP failures once per sent call.
                # No response body/account/order URL is printed on each local poll.
                return None
            if not isinstance(data, list):
                return None
            self._order_list_cache = (time.monotonic(), account_number, data)
            return data

    def _buscar_orden_en_listado(self, account_number: str, order_id: str,
                                 max_age_s: float = 0) -> Optional[dict]:
        """Obtiene UNA orden, usando primero el endpoint por id y luego el listado.

        El listado conserva el workaround de errores del endpoint por id. Since
        v3.3.18 each has its own transport backoff, so while the per-order
        endpoint is backing off a lookup goes straight to the list instead of
        staying unknown. ``max_age_s`` lets the position monitor reuse a recent
        list; confirmations pass 0 and always read fresh.
        Una pausa local o un fallo sin recuperación queda desconocido.
        """
        if not order_id:
            return None
        visto = None
        if max_age_s > 0:
            cached = self._order_list_cache
            if (cached and cached[1] == account_number
                    and time.monotonic() - cached[0] <= max_age_s):
                found = self._orden_en(cached[2], order_id)
                if found is not None:
                    return found
                visto = cached[2]   # The order is newer than this list.
        direct_url = f"{self.TRADER_URL}/accounts/{account_number}/orders/{order_id}"
        try:
            direct = schwab_request(self._http_session().get,
                direct_url, timeout=self.request_timeout_s)
            if direct.status_code in (401, 403, 429):
                return None  # Shared transport records both HTTP and broker status.
            if not (direct.status_code == 400 or direct.status_code >= 500):
                direct.raise_for_status()
                data = direct.json()
                if (isinstance(data, dict) and str(data.get("orderId", "")) == str(order_id)
                        and isinstance(data.get("status"), str) and data["status"]):
                    return data
        except SchwabRequestPaused:
            return None
        except SchwabRequestDeferred as exc:
            if exc.reason != "order_read_backoff":
                return None  # Rolling/burst limits would refuse the list as well.
        except requests.RequestException as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) != 404:
                return None
        except Exception:
            pass
        return self._orden_en(self._listado_ordenes(account_number, max_age_s, visto), order_id)


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
            except (SchwabRequestPaused, SchwabRequestDeferred):
                return None
            except Exception as exc:
                _ps(f"[ERROR] provider órdenes WORKING: {type(exc).__name__}")
                return None
            if data is None:
                return None
            return data if isinstance(data, list) else None
        ahora = datetime.utcnow()
        desde = ahora - timedelta(days=7)
        hasta = ahora + timedelta(days=1)
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        url = f"{self.TRADER_URL}/accounts/{account_number}/orders"
        try:
            r = schwab_request(self._http_session().get,
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
            return data if isinstance(data, list) else None
        except (SchwabRequestPaused, SchwabRequestDeferred):
            return None
        except Exception as exc:
            _ps(f"[ERROR] snapshot órdenes WORKING: {type(exc).__name__}")
            return None

    @staticmethod
    def _iter_order_nodes(orders):
        for order in orders or []:
            if not isinstance(order, dict):
                continue
            yield order
            children = order.get("childOrderStrategies", []) or []
            yield from EjecutorHibridoDeOrdenes._iter_order_nodes(children)

    def _working_snapshot_reciente(self, account_number: str, max_age_s: float = 0) -> Optional[list]:
        """WORKING snapshot reused for ``max_age_s``; one shared read at a time."""
        if max_age_s <= 0:
            return self._working_orders_snapshot(account_number)
        with self._working_lock:
            cached = self._working_cache
            if (cached and cached[1] == account_number
                    and time.monotonic() - cached[0] <= max_age_s):
                return cached[2]
            data = self._working_orders_snapshot(account_number)
            if data is not None:
                self._working_cache = (time.monotonic(), account_number, data)
            return data

    def estado_orden_vigilada(self, account_number: str, order_id: str,
                              max_age_s: float = 0) -> str:
        """Order status for the open-position monitor from reliable sources.

        WORKING while the order is in a recent WORKING list (one read shared by
        every position for ``max_age_s``). Once it leaves that list, its final
        status if Schwab returns the order, otherwise NOT_WORKING: the caller
        must then use the broker position to tell a fill from a cancellation.
        DESCONOCIDO only when the WORKING list itself cannot be read (v3.3.19:
        Schwab's single-order and full-list reads can fail for an account).
        """
        if not order_id:
            return "DESCONOCIDO"
        snapshot = self._working_snapshot_reciente(account_number, max_age_s)
        if snapshot is None:
            return "DESCONOCIDO"
        objetivo = str(order_id)
        if any(str(o.get("orderId", "") or "") == objetivo for o in self._iter_order_nodes(snapshot)):
            return "WORKING"
        d = self._buscar_orden_en_listado(account_number, order_id)
        return str((d or {}).get("status", "") or "").upper() or "NOT_WORKING"

    def orden_trabajando(
        self, account_number: str, order_id: str
    ) -> Optional[bool]:
        """Whether an order may still execute; ``None`` means unverified.

        A WORKING snapshot proves presence, never terminality by absence. Queued,
        pending-activation and other nonterminal orders can be absent from that
        filtered list. Return False only after reading a terminal broker status.
        Callers must still reconcile positions before replacing a terminal order.
        """
        if not order_id:
            return None
        snapshot = self._working_orders_snapshot(account_number)
        if snapshot is None:
            return None
        objetivo = str(order_id)
        if any(
            str(order.get("orderId", "") or "") == objetivo
            for order in self._iter_order_nodes(snapshot)
        ):
            return True
        detail = self._buscar_orden_en_listado(account_number, order_id)
        status = str((detail or {}).get("status", "") or "").upper()
        if not status or status in ("DESCONOCIDO", "UNKNOWN", "NOT_WORKING"):
            return None
        return status not in TERMINAL_ORDER_STATES

    def ordenes_trabajando_ticker(
        self, account_number: str, ticker: str
    ) -> Optional[list]:
        """Return all potentially executable symbol order ids, or None if unknown.

        The legacy method name is retained for callers. The recent unfiltered
        list includes QUEUED/conditional/pending orders, not just status=WORKING.
        A capped or malformed list cannot prove absence. Known older orders also
        need their individual terminal status from ``orden_trabajando``.
        """
        snapshot = self._listado_ordenes(account_number)
        if not isinstance(snapshot, list) or len(snapshot) >= 500:
            return None
        if any(not isinstance(order, dict) for order in snapshot):
            return None
        wanted = str(ticker or "").upper()
        ids = []
        seen = set()
        for order in self._iter_order_nodes(snapshot):
            try:
                children = order.get("childOrderStrategies", []) or []
                if not isinstance(children, list) or any(not isinstance(c, dict) for c in children):
                    return None
                status = str(order.get("status", "") or "").upper()
                if not status or status in ("DESCONOCIDO", "UNKNOWN", "NOT_WORKING"):
                    return None
                if status in TERMINAL_ORDER_STATES:
                    continue
                legs = order.get("orderLegCollection", []) or []
                if not isinstance(legs, list) or any(not isinstance(leg, dict) for leg in legs):
                    return None
                if not legs and not children:
                    return None
                symbols = set()
                for leg in legs:
                    instrument = (leg or {}).get("instrument", {}) or {}
                    symbol = str(instrument.get("symbol", "") or "").upper()
                    if not symbol:
                        return None
                    symbols.add(symbol)
                if wanted not in symbols:
                    continue
                oid = str(order.get("orderId", "") or "")
                if not oid:
                    return None
                if oid not in seen:
                    seen.add(oid)
                    ids.append(oid)
            except Exception:
                return None
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
            working = self.orden_trabajando(account_number, order_id)
            if working is True:
                return {
                    "status": "WORKING",
                    "filled": 0.0,
                    "avg_price": 0.0,
                    "status_source": "WORKING_SNAPSHOT",
                }
            return {"status": "DESCONOCIDO", "filled": 0.0, "avg_price": 0.0}
        status = str(d.get("status", "DESCONOCIDO")).upper()
        # Acciones ejecutadas + precio medio desde las PATAS de ejecución. Schwab
        # a veces NO reporta filledQuantity en una orden CANCELED tras un llenado
        # parcial, pero las patas SÍ registran la ejecución: se toma el MÁXIMO
        # entre ambas fuentes para no ignorar acciones ya adquiridas.
        precios, cantidades = [], []
        for act in (d.get("orderActivityCollection", []) or []):
            for leg in (act.get("executionLegs", []) or []):
                precios.append(float(leg.get("price", 0) or 0))
                cantidades.append(float(leg.get("quantity", 0) or 0))
        filled_field = float(d.get("filledQuantity", 0) or 0)
        filled_legs = sum(cantidades)
        filled = max(filled_field, filled_legs)
        if filled_legs > 0:
            avg = sum(p * q for p, q in zip(precios, cantidades)) / filled_legs
        else:
            # Sin patas pero con llenado (filledQuantity>0): usa el precio de la
            # orden (límite) como aproximación en vez de 0.0, para no mal-calcular
            # SL/TP/PnL de una posición realmente llenada.
            # A limit is an instruction, never evidence of an execution price.
            avg = 0.0
        return {"status": status, "filled": filled, "avg_price": avg}

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
        def fetch_control_state():
            with schwab_control_requests():
                return self.detalle_orden(account_number, order_id)

        result = self._sync.run_order(
            fetch_control_state,
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

    def _estado_orden(self, account_number: str, order_id: str, max_age_s: float = 0) -> str:
        """Consulta el estado de una orden (WORKING/FILLED/...) vía el LISTADO."""
        if not order_id:
            return "DESCONOCIDO"
        d = self._buscar_orden_en_listado(account_number, order_id, max_age_s)
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
            resp = schwab_request(self._http_session().post,
                url, json=cuerpo, timeout=self.request_timeout_s, recovery_safe=False)
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
        Tras el llenado de la entrada, monta la protección 70/30:
          - TP1: LIMIT de salida del 70% en take_profit_parcial
          - SL : STOP de salida del 100% en stop_loss
        Devuelve los ids para que el motor gestione el breakeven al saltar TP1.
        """
        # STOP PRIMERO: si el segundo POST falla, la posición sigue protegida.
        # Colocar TP1 antes del stop dejaba una ventana de exposición desnuda.
        sl = self.colocar_salida(orden.ticker, orden.tipo, orden.cantidad,
                                 orden.stop_loss, "STOP", account_number)
        if sl.get("status") != "OK":
            return {"tp1": {}, "sl": sl}
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

    def liquidar_posicion(self, ticker: str, tipo_entrada: str, cantidad: int,
                          account_number: str, motivo: str = "") -> dict:
        with schwab_control_requests():
            return self._liquidar_posicion_controlada(
                ticker, tipo_entrada, cantidad, account_number, motivo)

    def _liquidar_posicion_controlada(self, ticker: str, tipo_entrada: str, cantidad: int,
                                    account_number: str, motivo: str = "") -> dict:
        if self.paper_trading or not self.execution_v2_config.get('enabled', False):
            return self._liquidar_market_legacy(ticker, tipo_entrada, cantidad, account_number, motivo)
        from core.execution_v2 import SmartExit
        with self.secuencia_simbolo(ticker):
            if self._smart_exit is None:
                self._smart_exit = SmartExit(self, self.execution_v2_config)
            context = getattr(self._lifecycle_local, 'fields', None) or {}
            try:
                return self._smart_exit.close(ticker, tipo_entrada, cantidad, account_number, motivo,
                                              context.get('exit_signal_price'))
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
            resp = schwab_request(self._http_session().post,
                url, json=cuerpo, timeout=self.request_timeout_s, recovery_safe=False)
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
                result = {
                    "status": "OK", "order_id": order_id, "cantidad": broker_qty,
                    "broker_status": llenado["broker_status"],
                    "latency_ms": llenado["latency_ms"] + plano["latency_ms"],
                    "posicion_plana": True, "avg_price": avg_price,
                    "broker_side": broker_tipo,
                }
                self._audit(
                    "EXIT_FILLED", ticker=ticker, lado=broker_tipo,
                    estado=llenado.get("broker_status", "FILLED"),
                    order_id=order_id, cantidad=broker_qty,
                    latency_ms=result["latency_ms"], motivo=motivo,
                    precio=avg_price,
                    detalle={"posicion_plana": True, "avg_price": avg_price},
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
            r = schwab_request(self._http_session().get,
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
            resp = schwab_request(self._http_session().delete,
                url,
                headers={"Content-Type": None, "Accept": "application/json"},
                timeout=self.request_timeout_s,
                recovery_safe=False,
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
        """Cancel and confirm with the order-action request reserve (v3.3.19).

        A busy background budget or a failing background poll must not leave a
        cancellation, and the close or kill switch waiting on it, unconfirmed.
        """
        with schwab_control_requests():
            return self._cancelar_orden_confirmada_impl(account_number, order_id, ticker)

    def _cancelar_orden_confirmada_impl(
        self, account_number: str, order_id: str, ticker: str = ""
    ) -> dict:
        """
        Request cancellation and require terminal broker evidence.

        ``orden_trabajando=False`` now also requires a terminal order read; absence
        from WORKING alone is insufficient. ``NOT_WORKING`` does not distinguish
        cancellation from a fill. Position reconciliation remains mandatory.
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

        # A terminal order read proves that another DELETE is unnecessary.
        # Position reconciliation still distinguishes a cancellation from a fill.
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
                "terminal_by_absence": False,
                "terminal_by_order_read": True,
            }
            self._audit(
                "CANCEL_CONFIRMED",
                ticker=ticker,
                estado="NOT_WORKING",
                order_id=order_id,
                cantidad=result["filled"],
                motivo="Estado terminal confirmado por lectura de la orden",
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

        # A fresh terminal order read can confirm a transition missed by the wait.
        if not resultado.get("confirmado"):
            working_after = self.orden_trabajando(account_number, order_id)
            if working_after is False:
                resultado.update({
                    "confirmado": True,
                    "broker_status": "NOT_WORKING",
                    "timeout": False,
                    "terminal_by_absence": False,
                    "terminal_by_order_read": True,
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
