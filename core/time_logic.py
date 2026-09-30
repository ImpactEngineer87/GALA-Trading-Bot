"""
=============================================================
MÓDULO: core/time_logic.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Cerebro temporal del bot. Gestiona las ventanas horarias
    clave del mercado y aplica la jerarquía de cruces.

    VENTANAS IMPLEMENTADAS:
      - Pre-Market     (< 09:30)   → Guardar dirección base
      - Marcador 09:42 (>= 09:42)  → Primer cruce del día, sin alerta
      - Jerarquía      (Cruce 2)   → ¿Secuestra al Jefe?
      - Confirmación   (>= 10:30)  → ¿Coincide con el Jefe? → Telegram
      - Rango Lateral              → Cruce atrapado en pabilo previo
      - Rebote del Muro            → 3er cruce no rompe nivel del 2º
      - Mediodía 12:00 (>= 12:00)  → Crear muro de mediodía
      - Validación 12:30(>= 12:30) → Traspaso o No Traspaso
=============================================================
"""

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional, List, Dict
from enum import Enum


# =============================================================
# ENUMERACIONES
# =============================================================

class TipoHito(str, Enum):
    PRE_MARKET  = "Pre-Market"
    MARCADOR_09 = "Marcador 09:42"
    CRUCE_2     = "Cruce 2"
    CRUCE_3     = "Cruce 3"
    CONFIRMACION_10 = "Confirmacion 10:30"
    MEDIODIA_12  = "Mediodia 12:00"
    VALIDACION_12 = "Validacion 12:30"


class ResultadoHito(str, Enum):
    TENDENCIA_CONFIRMADA  = "tendencia_confirmada"
    RANGO_LATERAL         = "rango_lateral"
    REBOTE_DETECTADO      = "rebote_detectado"
    TRASPASO              = "traspaso"
    NO_TRASPASO           = "no_traspaso"
    NUEVO_JEFE            = "nuevo_jefe"
    SIN_RESULTADO         = "sin_resultado"


# =============================================================
# ESTRUCTURA DE UN CRUCE
# =============================================================

@dataclass
class CruceMACD:
    """
    Representa un cruce de MACD validado (con patas abiertas).

    Atributos:
        timestamp   → Hora exacta del cruce
        ticker      → Símbolo del activo
        direccion   → 'BULL' (alcista) o 'BEAR' (bajista)
        close       → Precio de cierre de la vela de confirmación
        high        → High de la vela del cruce original (el Muro)
        low         → Low de la vela del cruce original (el Muro)
        hito        → Rol que toma este cruce (09:42, Cruce 2, etc.)
        resultado   → Resultado aplicado tras la comparación
    """
    timestamp: datetime
    ticker: str
    direccion: str        # 'BULL' o 'BEAR'
    close: float
    high: float
    low: float
    hito: Optional[TipoHito] = None
    resultado: Optional[ResultadoHito] = None

    @property
    def hora(self) -> time:
        return self.timestamp.time()

    def es_alcista(self) -> bool:
        return self.direccion == 'BULL'

    def es_bajista(self) -> bool:
        return self.direccion == 'BEAR'


# =============================================================
# MEMORIA DIARIA DEL BOT (Estado por ticker)
# =============================================================

@dataclass
class MemoriaDiaria:
    """
    Mantiene el estado completo del día para un ticker.

    Reiniciar al inicio de cada jornada de mercado.
    """
    ticker: str
    fecha: str = ""
    cruces_del_dia: List[CruceMACD] = field(default_factory=list)
    jefe_actual: Optional[CruceMACD] = None      # El "Jefe" dominante
    pre_market: Optional[CruceMACD] = None        # Último cruce pre-market
    estado_rango: bool = False                     # True si detectado rango lateral
    alertas_enviadas: List[str] = field(default_factory=list)


# =============================================================
# MOTOR DE LÓGICA TEMPORAL
# =============================================================

class MotorTemporal:
    """
    Procesa cada cruce MACD validado y aplica las reglas
    de las ventanas temporales del mercado.

    Uso:
        motor = MotorTemporal()
        resultado = motor.procesar_cruce(cruce, memoria)
    """

    # Ventanas horarias (hora del mercado de EE.UU. — ET)
    HORA_PREMARKET_FIN   = time(9, 30)
    HORA_MARCADOR_09     = time(9, 42)
    HORA_CONFIRMACION_10 = time(10, 30)
    HORA_MEDIODIA_12     = time(12, 0)
    HORA_VALIDACION_12   = time(12, 30)

    def __init__(self):
        self._memorias: Dict[str, MemoriaDiaria] = {}

    def obtener_memoria(self, ticker: str, fecha: str) -> MemoriaDiaria:
        """
        Retorna la memoria diaria del ticker.
        Si es un nuevo día, reinicia la memoria automáticamente.
        """
        key = f"{ticker}_{fecha}"
        if key not in self._memorias:
            self._memorias[key] = MemoriaDiaria(ticker=ticker, fecha=fecha)
        return self._memorias[key]

    def procesar_cruce(self, cruce: CruceMACD) -> Optional[Dict]:
        """
        Punto de entrada principal. Recibe un cruce validado y
        determina qué regla aplicar según la hora.

        Retorna un diccionario con:
            hito     → Qué evento es este cruce
            resultado → Qué se determinó
            alerta   → Texto de la alerta para Telegram (o None)
            prioridad → 'alta', 'media', 'baja'
        """
        fecha = cruce.timestamp.strftime("%Y-%m-%d")
        memoria = self.obtener_memoria(cruce.ticker, fecha)
        hora = cruce.hora

        # -------------------------------------------------------
        # PRE-MARKET (antes de las 09:30)
        # -------------------------------------------------------
        if hora < self.HORA_PREMARKET_FIN:
            return self._procesar_premarket(cruce, memoria)

        # -------------------------------------------------------
        # MARCADOR 09:42 — Primer cruce del día desde las 09:42
        # -------------------------------------------------------
        if hora >= self.HORA_MARCADOR_09 and memoria.jefe_actual is None:
            return self._procesar_marcador_09(cruce, memoria)

        # -------------------------------------------------------
        # CONFIRMACIÓN 10:30
        # -------------------------------------------------------
        if hora >= self.HORA_CONFIRMACION_10 and len(memoria.cruces_del_dia) == 1:
            return self._procesar_confirmacion_10(cruce, memoria)

        # -------------------------------------------------------
        # CRUCE 2 (entre 09:42 y 10:30) → Jerarquía / Secuestro
        # -------------------------------------------------------
        if (hora >= self.HORA_MARCADOR_09
                and hora < self.HORA_CONFIRMACION_10
                and len(memoria.cruces_del_dia) == 1):
            return self._procesar_cruce_2(cruce, memoria)

        # -------------------------------------------------------
        # CRUCE 3 → Rebote del Muro
        # -------------------------------------------------------
        if (hora < self.HORA_MEDIODIA_12
                and len(memoria.cruces_del_dia) == 2):
            return self._procesar_cruce_3(cruce, memoria)

        # -------------------------------------------------------
        # MEDIODÍA 12:00
        # -------------------------------------------------------
        if hora >= self.HORA_MEDIODIA_12 and hora < self.HORA_VALIDACION_12:
            return self._procesar_mediodia_12(cruce, memoria)

        # -------------------------------------------------------
        # VALIDACIÓN 12:30
        # -------------------------------------------------------
        if hora >= self.HORA_VALIDACION_12:
            return self._procesar_validacion_12(cruce, memoria)

        return None

    # ==========================================================
    # REGLAS INDIVIDUALES
    # ==========================================================

    def _procesar_premarket(self, cruce: CruceMACD, memoria: MemoriaDiaria) -> Dict:
        """
        Pre-Market: guarda el último cruce antes de las 09:30.
        Si hay varios, siempre se queda con el más reciente.
        """
        cruce.hito = TipoHito.PRE_MARKET
        memoria.pre_market = cruce   # Sobrescribe si ya había uno
        return {
            "hito": TipoHito.PRE_MARKET,
            "resultado": ResultadoHito.SIN_RESULTADO,
            "alerta": None,
            "prioridad": "info",
            "log": f"[PRE-MARKET] {cruce.ticker} | {cruce.direccion} | Close: {cruce.close}"
        }

    def _procesar_marcador_09(self, cruce: CruceMACD, memoria: MemoriaDiaria) -> Dict:
        """
        Marcador 09:42: Primer cruce validado del día.
        No envía alerta — solo guarda como Jefe Inicial.
        """
        cruce.hito = TipoHito.MARCADOR_09
        memoria.jefe_actual = cruce
        memoria.cruces_del_dia.append(cruce)
        return {
            "hito": TipoHito.MARCADOR_09,
            "resultado": ResultadoHito.SIN_RESULTADO,
            "alerta": None,
            "prioridad": "info",
            "log": f"[MARCADOR 09:42] {cruce.ticker} | {cruce.direccion} guardado como Jefe"
        }

    def _procesar_cruce_2(self, cruce: CruceMACD, memoria: MemoriaDiaria) -> Dict:
        """
        Cruce 2 — Lógica de Jerarquía (Secuestro del Jefe):
          - Si el Close del Cruce 2 traspasa el High/Low del Marcador → Nuevo Jefe
          - Si queda dentro del pabilo → Rango Lateral detectado
        """
        cruce.hito = TipoHito.CRUCE_2
        jefe = memoria.jefe_actual
        memoria.cruces_del_dia.append(cruce)

        # Verificar si traspasa el muro del Jefe actual
        traspasa = (
            (cruce.es_alcista() and cruce.close > jefe.high) or
            (cruce.es_bajista() and cruce.close < jefe.low)
        )

        # Verificar rango lateral: cierre atrapado dentro del pabilo del Jefe
        rango_lateral = jefe.low <= cruce.close <= jefe.high

        if traspasa:
            cruce.resultado = ResultadoHito.NUEVO_JEFE
            memoria.jefe_actual = cruce  # Cruce 2 se convierte en el nuevo Jefe
            # Verificar si además rompe el Pre-Market
            pm_context = ""
            if memoria.pre_market:
                pm_txt = "y supera también el nivel Pre-Market" if (
                    cruce.es_alcista() and cruce.close > memoria.pre_market.high or
                    cruce.es_bajista() and cruce.close < memoria.pre_market.low
                ) else ""
                pm_context = f" {pm_txt}"
            return {
                "hito": TipoHito.CRUCE_2,
                "resultado": ResultadoHito.NUEVO_JEFE,
                "alerta": f"🔄 [{cruce.ticker}] Cruce 2 SECUESTRA al Jefe 09:42{pm_context}. Nuevo Jefe: {cruce.direccion}",
                "prioridad": "media",
                "log": f"[JERARQUIA] {cruce.ticker} | Cruce 2 es el nuevo Jefe"
            }
        elif rango_lateral:
            cruce.resultado = ResultadoHito.RANGO_LATERAL
            memoria.estado_rango = True
            return {
                "hito": TipoHito.CRUCE_2,
                "resultado": ResultadoHito.RANGO_LATERAL,
                "alerta": (f"⚠️ [{cruce.ticker}] Rango lateral detectado. "
                           f"El precio se mantiene entre ${jefe.low:.2f} y ${jefe.high:.2f}. "
                           f"Buscar dirección contraria al primer cruce."),
                "prioridad": "media",
                "log": f"[RANGO] {cruce.ticker} | Cruce 2 atrapado en pabilo del Jefe"
            }
        else:
            return {
                "hito": TipoHito.CRUCE_2,
                "resultado": ResultadoHito.SIN_RESULTADO,
                "alerta": None,
                "prioridad": "baja",
                "log": f"[CRUCE 2] {cruce.ticker} | Sin resultado definitivo aún"
            }

    def _procesar_confirmacion_10(self, cruce: CruceMACD, memoria: MemoriaDiaria) -> Dict:
        """
        Confirmación de las 10:30:
          - Si la dirección del cruce 10:30 == dirección del Jefe actual → CONFIRMADO
        """
        cruce.hito = TipoHito.CONFIRMACION_10
        jefe = memoria.jefe_actual
        memoria.cruces_del_dia.append(cruce)

        if cruce.direccion == jefe.direccion:
            cruce.resultado = ResultadoHito.TENDENCIA_CONFIRMADA
            emoji = "🟢" if cruce.es_alcista() else "🔴"
            return {
                "hito": TipoHito.CONFIRMACION_10,
                "resultado": ResultadoHito.TENDENCIA_CONFIRMADA,
                "alerta": (f"{emoji} [{cruce.ticker}] Tendencia 09:42 CONFIRMADA (80% probabilidad). "
                           f"Dirección: {cruce.direccion} | Precio: ${cruce.close:.2f}"),
                "prioridad": "alta",
                "log": f"[10:30 CONFIRMADO] {cruce.ticker} | {cruce.direccion}"
            }
        else:
            return {
                "hito": TipoHito.CONFIRMACION_10,
                "resultado": ResultadoHito.SIN_RESULTADO,
                "alerta": f"⚠️ [{cruce.ticker}] Cruce 10:30 en dirección contraria al Jefe. Sin confirmación.",
                "prioridad": "baja",
                "log": f"[10:30 NO CONFIRMADO] {cruce.ticker} | Direcciones opuestas"
            }

    def _procesar_cruce_3(self, cruce: CruceMACD, memoria: MemoriaDiaria) -> Dict:
        """
        Cruce 3 — Rebote del Muro:
          - El 3er cruce intenta romper en dirección contraria al 2º
          - Si NO supera el High/Low del 2º → Rebote detectado
        """
        cruce.hito = TipoHito.CRUCE_3
        muro = memoria.cruces_del_dia[-1]  # El 2º cruce es el muro actual
        memoria.cruces_del_dia.append(cruce)

        # Rebote alcista: el 3er cruce bajista no logra romper el Low del 2º alcista
        if muro.es_alcista() and cruce.es_bajista() and cruce.close > muro.low:
            cruce.resultado = ResultadoHito.REBOTE_DETECTADO
            return {
                "hito": TipoHito.CRUCE_3,
                "resultado": ResultadoHito.REBOTE_DETECTADO,
                "alerta": (f"🟢 [{cruce.ticker}] 3er cruce NO traspasa nivel del 2º. "
                           f"REBOTE DETECTADO — La dirección ALCISTA la manda el 2º cruce. "
                           f"Soporte: ${muro.low:.2f}"),
                "prioridad": "alta",
                "log": f"[REBOTE] {cruce.ticker} | Close {cruce.close} > Low muro {muro.low}"
            }

        # Rebote bajista: el 3er cruce alcista no logra romper el High del 2º bajista
        elif muro.es_bajista() and cruce.es_alcista() and cruce.close < muro.high:
            cruce.resultado = ResultadoHito.REBOTE_DETECTADO
            return {
                "hito": TipoHito.CRUCE_3,
                "resultado": ResultadoHito.REBOTE_DETECTADO,
                "alerta": (f"🔴 [{cruce.ticker}] 3er cruce NO traspasa nivel del 2º. "
                           f"REBOTE DETECTADO — La dirección BAJISTA la manda el 2º cruce. "
                           f"Resistencia: ${muro.high:.2f}"),
                "prioridad": "alta",
                "log": f"[REBOTE] {cruce.ticker} | Close {cruce.close} < High muro {muro.high}"
            }
        else:
            return {
                "hito": TipoHito.CRUCE_3,
                "resultado": ResultadoHito.SIN_RESULTADO,
                "alerta": f"📊 [{cruce.ticker}] 3er cruce supera el muro. Cambio de dirección confirmado.",
                "prioridad": "media",
                "log": f"[CRUCE 3] {cruce.ticker} | Muro roto — cambio de dirección"
            }

    def _procesar_mediodia_12(self, cruce: CruceMACD, memoria: MemoriaDiaria) -> Dict:
        """
        Mediodía 12:00: Crear nuevo muro de mediodía.
        """
        cruce.hito = TipoHito.MEDIODIA_12
        memoria.cruces_del_dia.append(cruce)
        return {
            "hito": TipoHito.MEDIODIA_12,
            "resultado": ResultadoHito.SIN_RESULTADO,
            "alerta": None,
            "prioridad": "info",
            "log": f"[12:00 MURO] {cruce.ticker} | {cruce.direccion} | H:{cruce.high} L:{cruce.low}"
        }

    def _procesar_validacion_12(self, cruce: CruceMACD, memoria: MemoriaDiaria) -> Dict:
        """
        Validación 12:30: Compara el Close actual con el High/Low del cruce de las 12:00.
        """
        cruce.hito = TipoHito.VALIDACION_12
        # Buscar el cruce de las 12:00
        cruce_12 = next(
            (c for c in reversed(memoria.cruces_del_dia) if c.hito == TipoHito.MEDIODIA_12), None
        )
        if not cruce_12:
            return {
                "hito": TipoHito.VALIDACION_12,
                "resultado": ResultadoHito.SIN_RESULTADO,
                "alerta": None,
                "prioridad": "baja",
                "log": f"[12:30] {cruce.ticker} | Sin muro de 12:00 registrado"
            }

        memoria.cruces_del_dia.append(cruce)

        traspaso_alcista = cruce.close > cruce_12.high
        traspaso_bajista = cruce.close < cruce_12.low

        if traspaso_alcista:
            cruce.resultado = ResultadoHito.TRASPASO
            return {
                "hito": TipoHito.VALIDACION_12,
                "resultado": ResultadoHito.TRASPASO,
                "alerta": (f"🟢 [{cruce.ticker}] TRASPASO ALCISTA a las 12:30. "
                           f"Precio ${cruce.close:.2f} supera muro 12:00 (${cruce_12.high:.2f}). "
                           f"Seguir dirección actual."),
                "prioridad": "alta",
                "log": f"[12:30 TRASPASO] {cruce.ticker} | Alcista confirmado"
            }
        elif traspaso_bajista:
            cruce.resultado = ResultadoHito.TRASPASO
            return {
                "hito": TipoHito.VALIDACION_12,
                "resultado": ResultadoHito.TRASPASO,
                "alerta": (f"🔴 [{cruce.ticker}] TRASPASO BAJISTA a las 12:30. "
                           f"Precio ${cruce.close:.2f} rompe muro 12:00 (${cruce_12.low:.2f}). "
                           f"Seguir dirección actual."),
                "prioridad": "alta",
                "log": f"[12:30 TRASPASO] {cruce.ticker} | Bajista confirmado"
            }
        else:
            cruce.resultado = ResultadoHito.NO_TRASPASO
            emoji = "🟢" if cruce_12.es_alcista() else "🔴"
            return {
                "hito": TipoHito.VALIDACION_12,
                "resultado": ResultadoHito.NO_TRASPASO,
                "alerta": (f"{emoji} [{cruce.ticker}] 12:30 NO supera nivel de 12:00. "
                           f"La dirección válida es la de las 12:00: {cruce_12.direccion}"),
                "prioridad": "alta",
                "log": f"[12:30 NO TRASPASO] {cruce.ticker} | Dirección 12:00 prevalece"
            }
