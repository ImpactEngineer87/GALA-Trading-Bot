"""
=============================================================
MÓDULO: core/signals.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Motor de señales del bot. Gestiona el estado de cada ticker,
    registra las señales generadas y las señales omitidas.

    Este módulo actúa como intermediario entre la estrategia
    (strategy.py) y el ejecutor de órdenes (order_executor.py).
=============================================================
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict
from datetime import datetime
import pandas as pd


# =============================================================
# ESTRUCTURA DE UNA SEÑAL
# =============================================================

@dataclass
class Señal:
    """
    Representa una señal generada por la estrategia GALA6.

    Atributos:
        ticker      → Símbolo del activo (ej: 'BA', 'AAPL')
        tipo        → 'BUY', 'SELL', 'OM-R', 'OM-T'
        timestamp   → Fecha y hora de la señal
        precio      → Precio de cierre al momento de la señal
        atr         → ATR en el momento de la señal (para SL/TP)
        macd_hist   → Valor del histograma MACD
        ema_slope   → Pendiente de la EMA en ese momento
        direccion   → Dirección activa: 1=alcista, -1=bajista
        es_valida   → True si es BUY o SELL (no omitida)
        motivo_omision → Razón si la señal fue omitida
    """
    ticker: str
    tipo: str
    timestamp: datetime
    precio: float
    atr: float
    macd_hist: float
    ema_slope: float
    direccion: int
    es_valida: bool = False
    motivo_omision: Optional[str] = None

    def __post_init__(self):
        self.es_valida = self.tipo in ('BUY', 'SELL')
        if self.tipo == 'OM-R':
            self.motivo_omision = "Rango insuficiente (compresión de precio)"
        elif self.tipo == 'OM-T':
            self.motivo_omision = "Tendencia EMA insuficiente"


# =============================================================
# ESTRUCTURA DEL ESTADO DE UN TICKER
# =============================================================

@dataclass
class EstadoTicker:
    """
    Mantiene el estado actual de un ticker monitorizado por el bot.

    Este estado se actualiza en cada nueva vela de 1 minuto.
    """
    ticker: str
    ultima_señal: Optional[Señal] = None
    en_posicion: bool = False
    direccion_posicion: Optional[str] = None  # 'LONG' o 'SHORT'
    precio_entrada: Optional[float] = None
    stop_loss: Optional[float] = None
    trailing_stop: Optional[float] = None
    señales_historico: List[Señal] = field(default_factory=list)


# =============================================================
# MOTOR DE SEÑALES
# =============================================================

class MotorDeSeñales:
    """
    Procesa el DataFrame de una vela y genera señales de trading.

    Uso:
        motor = MotorDeSeñales()
        señal = motor.procesar_ultima_vela(df, ticker="BA")
    """

    def __init__(self):
        # Diccionario de estados por ticker
        self._estados: Dict[str, EstadoTicker] = {}

    def obtener_estado(self, ticker: str) -> EstadoTicker:
        """
        Retorna el estado actual del ticker.
        Si el ticker no existe aún, lo inicializa.
        """
        if ticker not in self._estados:
            self._estados[ticker] = EstadoTicker(ticker=ticker)
        return self._estados[ticker]

    def procesar_ultima_vela(self, df: pd.DataFrame, ticker: str) -> Optional[Señal]:
        """
        Analiza la ÚLTIMA fila del DataFrame (vela más reciente)
        y retorna una Señal si existe, o None si no hay señal.

        Parámetros:
            df     → DataFrame con los resultados de strategy.calcular()
            ticker → Símbolo del activo analizado

        Retorna:
            Señal o None
        """
        if df.empty or len(df) < 30:
            return None

        # Obtener la última vela
        ultima = df.iloc[-1]
        tipo = ultima.get('señal')

        if tipo is None:
            return None

        # Construir objeto Señal
        señal = Señal(
            ticker=ticker,
            tipo=tipo,
            timestamp=ultima.name if isinstance(ultima.name, datetime) else datetime.now(),
            precio=float(ultima['close']),
            atr=float(ultima['atr']),
            macd_hist=float(ultima['hist_line']),
            ema_slope=float(ultima['ema_slope']),
            direccion=int(ultima['direccion_activa']),
        )

        # Guardar en historial del ticker
        estado = self.obtener_estado(ticker)
        estado.ultima_señal = señal
        estado.señales_historico.append(señal)

        return señal

    def registrar_entrada(self, ticker: str, precio: float,
                          direccion: str, stop_loss: float, trailing_stop: float):
        """
        Registra que se ha abierto una posición en un ticker.

        Parámetros:
            ticker        → Símbolo del activo
            precio        → Precio de entrada
            direccion     → 'LONG' o 'SHORT'
            stop_loss     → Precio de stop loss
            trailing_stop → Precio inicial de trailing stop
        """
        estado = self.obtener_estado(ticker)
        estado.en_posicion = True
        estado.direccion_posicion = direccion
        estado.precio_entrada = precio
        estado.stop_loss = stop_loss
        estado.trailing_stop = trailing_stop

    def registrar_salida(self, ticker: str):
        """
        Registra que se ha cerrado la posición de un ticker.
        Limpia el estado de posición.
        """
        estado = self.obtener_estado(ticker)
        estado.en_posicion = False
        estado.direccion_posicion = None
        estado.precio_entrada = None
        estado.stop_loss = None
        estado.trailing_stop = None

    def obtener_resumen(self) -> List[Dict]:
        """
        Retorna un resumen del estado de todos los tickers monitorizados.
        Útil para mostrar en consola o logs.
        """
        resumen = []
        for ticker, estado in self._estados.items():
            ultima = estado.ultima_señal
            resumen.append({
                'ticker': ticker,
                'en_posicion': estado.en_posicion,
                'direccion': estado.direccion_posicion or '-',
                'precio_entrada': estado.precio_entrada or '-',
                'stop_loss': estado.stop_loss or '-',
                'ultima_señal': ultima.tipo if ultima else '-',
                'ultima_señal_precio': ultima.precio if ultima else '-',
                'total_señales': len(estado.señales_historico),
            })
        return resumen
