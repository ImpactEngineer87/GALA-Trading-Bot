"""
=============================================================
MÓDULO: agents/contracts.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Contratos de datos compartidos entre los 3 agentes SMC, el
    orquestador y el motor. Este módulo es la "API interna" del
    sistema de agentes: define las estructuras que cada agente
    produce y consume. NO contiene lógica de trading — solo tipos.

    Cadena de datos:
        DatosMultiTemporal  → entrada (velas de varias temporalidades)
        DireccionDia        → salida del Agente 1
        PropuestaOperacion  → salida del Agente 2
        VeredictoAuditor    → salida del Agente 3
        DecisionFinal       → salida del Orquestador (lo que ve el motor)
=============================================================
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict

import pandas as pd


# =============================================================
# ENUMERACIONES
# =============================================================

class Direccion(str, Enum):
    """Dirección permitida del día emitida por el Agente Macro."""
    ALCISTA = "ALCISTA"   # Solo se permiten compras (BUY / LONG)
    BAJISTA = "BAJISTA"   # Solo se permiten ventas (SELL / SHORT)
    NEUTRAL = "NEUTRAL"   # Sin sesgo claro → no operar


class Veredicto(str, Enum):
    """Decisión final del Agente Auditor de Riesgo."""
    APROBADA = "APROBADA"
    VETADA = "VETADA"


# =============================================================
# ENTRADA — DATOS MULTI-TEMPORAL
# =============================================================

@dataclass
class DatosMultiTemporal:
    """
    Contenedor de velas OHLCV de un ticker en varias temporalidades.

    Claves de temporalidad esperadas (según disponibilidad):
        "1W"  → Semanal
        "1D"  → Diario
        "4H"  → 4 horas
        "1H"  → 1 hora
        "15m" → 15 minutos
        "5m"  → 5 minutos
        "1m"  → 1 minuto (contexto inmediato)

    Cada valor es un DataFrame con índice de tiempo y columnas
    open, high, low, close, volume ordenadas cronológicamente.
    """
    ticker: str
    velas: Dict[str, pd.DataFrame] = field(default_factory=dict)
    sintetico: bool = False   # True = datos de fallback (demo); NO apto para operar en real
    estado_datos: str = "READY"  # WAITING is never a synthetic/demo price source.
    motivo_datos: str = ""
    reintento_seg: int = 0

    def tf(self, clave: str) -> Optional[pd.DataFrame]:
        """Devuelve el DataFrame de la temporalidad o None si no existe."""
        df = self.velas.get(clave)
        if df is None or len(df) == 0:
            return None
        return df

    def disponibles(self) -> List[str]:
        """Lista de temporalidades con datos utilizables."""
        return [k for k, v in self.velas.items() if v is not None and len(v) > 0]


# =============================================================
# ESTRUCTURAS SMC
# =============================================================

@dataclass
class NivelLiquidez:
    """
    Un nivel de liquidez institucional (zona donde se acumulan órdenes).

    Ejemplos de 'nombre':
        PDH / PDL / PDC → Previous Day High / Low / Close
        PWH / PWL / PWC → Previous Week High / Low / Close
        EQH / EQL       → Equal Highs / Equal Lows (liquidez agrupada)
        SWH / SWL       → Swing High / Swing Low
        OG              → Opening Gap (nivel de la brecha de apertura)
    """
    nombre: str
    precio: float
    tipo: str            # "resistencia" (arriba) o "soporte" (abajo)
    mitigado: bool = False   # True si el precio ya alcanzó/barrió este nivel

    def __str__(self) -> str:
        estado = "mitigado" if self.mitigado else "activo"
        return f"{self.nombre}=${self.precio:.2f} ({self.tipo}, {estado})"


@dataclass
class FairValueGap:
    """
    Brecha de Valor Justo (FVG): desequilibrio de 3 velas donde el
    precio se movió tan rápido que dejó un hueco sin negociar.

    FVG alcista  → low[i] > high[i-2]  (hueco entre esas dos velas)
    FVG bajista  → high[i] < low[i-2]

    'fondo' y 'tope' delimitan el rango del hueco (fondo < tope).
    'indice' es la posición de la vela central (i-1) que crea el hueco.
    """
    tf: str
    direccion: str        # "alcista" o "bajista"
    fondo: float          # Límite inferior del hueco
    tope: float           # Límite superior del hueco
    indice: int           # Índice de la vela central del patrón
    timestamp: datetime
    mitigado: bool = False

    @property
    def medio(self) -> float:
        """Punto medio del hueco (entrada de referencia)."""
        return (self.tope + self.fondo) / 2.0

    @property
    def tamano(self) -> float:
        """Altura del hueco en precio."""
        return abs(self.tope - self.fondo)

    def contiene(self, precio: float) -> bool:
        """True si el precio está dentro del hueco."""
        return self.fondo <= precio <= self.tope


# =============================================================
# SALIDA — AGENTE 1 (MACRO / LIQUIDEZ)
# =============================================================

@dataclass
class DireccionDia:
    """
    Veredicto del Agente 1: la dirección permitida del día más el
    mapa de liquidez que usarán los agentes 2 y 3.
    """
    ticker: str
    direccion: Direccion
    confianza: float                          # 0.0 – 1.0
    niveles_liquidez: List[NivelLiquidez] = field(default_factory=list)
    hueco_apertura_pct: Optional[float] = None   # Brecha de apertura (% vs cierre previo)
    objetivo_liquidez: Optional[float] = None    # Próximo nivel objetivo en la dirección
    razones: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime(1970, 1, 1))

    def permite(self, tipo: str) -> bool:
        """¿La dirección del día permite este tipo de operación (BUY/SELL)?"""
        if self.direccion == Direccion.ALCISTA:
            return tipo == "BUY"
        if self.direccion == Direccion.BAJISTA:
            return tipo == "SELL"
        return False   # NEUTRAL no permite nada


# =============================================================
# SALIDA — AGENTE 2 (MICRO / FRANCOTIRADOR)
# =============================================================

@dataclass
class PropuestaOperacion:
    """
    Propuesta de operación del Agente 2, alineada con la dirección
    macro y con una Relación Beneficio/Riesgo (R/B) calculada.
    """
    ticker: str
    tipo: str                     # "BUY" o "SELL"
    precio_entrada: float
    stop_loss: float
    take_profit: float
    ratio_rb: float
    fvg: Optional[FairValueGap] = None
    razones: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime(1970, 1, 1))

    @property
    def riesgo_por_accion(self) -> float:
        return abs(self.precio_entrada - self.stop_loss)

    @property
    def beneficio_por_accion(self) -> float:
        return abs(self.take_profit - self.precio_entrada)


# =============================================================
# SALIDA — AGENTE 3 (AUDITOR / RIESGO)
# =============================================================

@dataclass
class VeredictoAuditor:
    """Resultado del filtro de riesgo del Agente 3."""
    veredicto: Veredicto
    motivo: str = ""
    atr_relativo: float = 0.0
    es_horario_institucional: bool = True
    es_rango_lateral: bool = False
    datos_suficientes: bool = True   # False = no se pudo auditar (micro sin velas)
    razones: List[str] = field(default_factory=list)

    @property
    def aprobada(self) -> bool:
        return self.veredicto == Veredicto.APROBADA


# =============================================================
# SALIDA — ORQUESTADOR (LO QUE CONSUME EL MOTOR)
# =============================================================

@dataclass
class DecisionFinal:
    """
    Decisión consolidada del sistema de agentes. Es la única
    estructura que el motor (bot_engine) necesita interpretar.
    """
    ticker: str
    operar: bool
    tipo: Optional[str] = None                       # "BUY" / "SELL" / None
    propuesta: Optional[PropuestaOperacion] = None
    # El FVG del Agente Micro es contexto para la IA, no una instrucción para
    # modificar la entrada maestra de Gala10. Se guarda por separado para que
    # siga disponible como feature sin convertir la orden en una LIMIT/FVG.
    fvg_contexto_tam_rel: float = 0.0
    direccion_dia: Optional[DireccionDia] = None
    veredicto: Optional[VeredictoAuditor] = None
    confluencia_estrategia: bool = False             # Coincide con core/strategy.py
    # ``None`` significa que no hay un modelo compatible cargado. Antes se
    # usaba 0.50 como valor por defecto, pero eso parecía una inferencia real en
    # el dashboard. Mantener el estado explícito permite mostrar ``N/A``.
    prob_meta: Optional[float] = None                 # Prob. del meta-modelo de agentes
    # Shadow Mode: el modelo candidato calcula una recomendación, pero nunca
    # puede cambiar ``operar`` ni bloquear la señal maestra de GALA10.
    shadow_enabled: bool = False
    shadow_model_loaded: bool = False
    shadow_prob: Optional[float] = None
    shadow_threshold: Optional[float] = None
    shadow_would_approve: Optional[bool] = None
    shadow_model_source: str = ""
    # Regla determinista solicitada por el cliente. En Shadow Mode solo anota
    # lo que habría ocurrido; no puede cambiar ``operar`` mientras el veto de
    # producción permanezca desactivado.
    hard_veto: bool = False
    hard_veto_reason: str = ""
    # Exact gate that changed ``operar`` from True to False.  This prevents the
    # dashboard from guessing when more than one veto condition is present.
    execution_veto_reason: str = ""
    # Effective order-routing authority for this ticker.  v2.9.2+ can run
    # PAPER and REAL tickers concurrently, so this must be carried explicitly.
    execution_mode: str = ""                         # PAPER / REAL
    # v3.1 — market-regime context.  This is intentionally kept OUT of the
    # XGBoost feature schema so existing approved per-ticker model artifacts
    # remain structurally compatible.  The regime guard is a separate,
    # explainable policy layer around the model score.
    market_regime_enabled: bool = False
    # Legacy v3.1.0 field retained for backwards-compatible logs/readers.
    market_regime_enforcement: str = "off"          # legacy off / shadow / real
    # v3.1.1 mode-aware policy for the ticker's effective execution mode.
    market_regime_policy: str = "off"               # off / observe / enforce
    market_regime: str = "INSUFFICIENT_DATA"
    market_regime_confidence: float = 0.0
    market_regime_range_pct: float = float("nan")
    market_regime_efficiency: float = float("nan")
    market_regime_flip_ratio: float = float("nan")
    market_regime_net_move_atr: float = float("nan")
    market_regime_atr_ratio: float = float("nan")
    market_regime_ema_slope_atr: float = float("nan")
    market_regime_breakout_atr: float = float("nan")
    market_regime_signal_aligned: Optional[bool] = None
    market_regime_would_block: bool = False
    market_regime_reason: str = ""
    # Features GSCO (institucional) — OPCIONALES: NaN si no hay posteo en la ventana.
    gsco_match: float = float("nan")                 # 1=coincide dirección+precio, 0=hay datos sin match, NaN=sin datos
    gsco_dist_pct: float = float("nan")              # distancia (%) al nivel GSCO más cercano
    # Features causales de volumen/liquidez de la última vela 1m cerrada.
    volumen_relativo_20: float = float("nan")
    volumen_zscore_20: float = float("nan")
    liquidez_dolar_log: float = float("nan")
    spread_proxy_pct: float = float("nan")
    impacto_amihud_20: float = float("nan")
    # Estado causal de la vela que ya disparó GALA10. Son features del veto;
    # nunca pueden crear, cambiar de dirección ni adelantar una entrada.
    gala_hist_strength_atr: float = float("nan")
    gala_hist_progress_atr: float = float("nan")
    gala_ema_slope_atr: float = float("nan")
    gala_range_pct: float = float("nan")
    gala_cross_bars: float = float("nan")
    gala_fast_entry: float = float("nan")
    gala_breakout_atr: float = float("nan")
    gala_atr_regime_100: float = float("nan")
    gala_body_atr: float = float("nan")
    gala_close_location: float = float("nan")
    gala_session_minute: float = float("nan")
    timestamp: datetime = field(default_factory=lambda: datetime(1970, 1, 1))
    resumen: str = ""

    @classmethod
    def no_operar(cls, ticker: str, resumen: str,
                  direccion_dia: Optional[DireccionDia] = None,
                  propuesta: Optional[PropuestaOperacion] = None,
                  veredicto: Optional[VeredictoAuditor] = None,
                  timestamp: Optional[datetime] = None) -> "DecisionFinal":
        """Fábrica para una decisión negativa (no se abre operación)."""
        return cls(
            ticker=ticker,
            operar=False,
            tipo=None,
            propuesta=propuesta,
            direccion_dia=direccion_dia,
            veredicto=veredicto,
            timestamp=timestamp or datetime(1970, 1, 1),
            resumen=resumen,
        )
