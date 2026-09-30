"""
=============================================================
MÓDULO: core/risk_manager.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Gestión de riesgo del bot. Calcula precios de entrada,
    stop loss y trailing stop usando ATR. Siempre usa órdenes
    LIMIT para evitar slippage (problema identificado con n8n).

    Decisiones tomadas según conversación con el cliente:
      - Órdenes LIMIT (nunca Market) para control de precio
      - IA calculará el precio de entrada + SL óptimo
      - Trailing stop dinámico basado en ATR
=============================================================
"""

from dataclasses import dataclass
from typing import Optional
import yaml


# =============================================================
# ESTRUCTURA DE UNA ORDEN DE TRADING
# =============================================================

@dataclass
class OrdenTradingCalculada:
    """
    Resultado del cálculo del gestor de riesgo para una señal.

    Contiene todos los parámetros necesarios para ejecutar
    una orden LIMIT con stop loss y trailing stop.

    Atributos:
        ticker           → Símbolo del activo (ej: 'BA')
        tipo             → 'BUY' o 'SELL'
        precio_limite    → Precio de la orden LIMIT (nunca Market)
        stop_loss        → Precio de stop loss fijo
        trailing_stop_pct → Porcentaje de trailing stop
        cantidad         → Número de acciones (a calcular según capital)
        riesgo_usd       → Riesgo máximo en USD para esta operación
        atr_actual       → ATR usado para calcular los niveles
    """
    ticker: str
    tipo: str
    precio_limite: float
    stop_loss: float
    trailing_stop_pct: float
    cantidad: int
    riesgo_usd: float
    atr_actual: float
    # --- Gestión de salida parcial (70/30) ---
    take_profit: float = 0.0            # Objetivo final (30% restante)
    take_profit_parcial: float = 0.0    # Primer objetivo (venta del 70%)
    cantidad_parcial: int = 0           # Acciones a cerrar en el primer objetivo (70%)
    cantidad_resto: int = 0             # Acciones que continúan (30%)
    modo_resto: str = "breakeven"       # "breakeven" o "fijo" para el 30% restante
    salida_parcial_activa: bool = False
    stop_loss_atr_factor: float = 0.0
    take_profit_atr_factor: float = 0.0

    def __str__(self):
        base = (
            f"[{self.tipo}] {self.ticker} | "
            f"LIMIT: ${self.precio_limite:.2f} | "
            f"SL: ${self.stop_loss:.2f} | "
            f"Qty: {self.cantidad} | "
            f"Riesgo: ${self.riesgo_usd:.2f}"
        )
        if self.salida_parcial_activa:
            base += (f" | TP1 ${self.take_profit_parcial:.2f} ({self.cantidad_parcial}) "
                     f"→ resto {self.cantidad_resto} [{self.modo_resto}]")
        return base


# =============================================================
# GESTOR DE RIESGO
# =============================================================

class GestorDeRiesgo:
    """
    Calcula los parámetros de riesgo para cada señal de trading.

    Usa ATR para determinar niveles dinámicos de stop loss
    y trailing stop. Nunca usa órdenes Market para evitar slippage.

    Uso:
        gestor = GestorDeRiesgo(config)
        orden = gestor.calcular_orden(
            ticker='BA',
            tipo='BUY',
            precio_actual=227.49,
            atr=1.85,
            capital_disponible=10000
        )
    """

    def __init__(self, config: dict):
        """
        Inicializa con los parámetros de riesgo del config.yaml.

        Parámetros:
            config → Diccionario con la sección 'risk' del config.yaml
        """
        risk = config.get("risk", {})
        self.trailing_stop_factor = risk.get("trailing_stop_factor", 1.5)
        self.stop_loss_factor = float(risk.get("stop_loss_factor", 2.0))
        # Independent TP distance.  Legacy configurations keep the previous
        # 2R behavior until the migration writes an explicit factor.
        self.take_profit_factor = float(
            risk.get("take_profit_factor", self.stop_loss_factor * 2.0)
        )
        self.max_posiciones = risk.get("max_simultaneous_positions", 15)
        self.paper_trading = risk.get("paper_trading", True)
        self.max_acciones_por_orden = int(risk.get("max_acciones_por_orden", 0))  # 0 = sin límite
        self.sizing_mode = str(risk.get("sizing_mode", "atr")).strip().lower()
        self.fixed_shares = max(1, int(risk.get("fixed_shares", 50)))
        self.risk_per_trade_pct = float(risk.get("risk_per_trade_pct", 1.0))

        # --- Configuración de salida parcial (70/30) ---
        sp = config.get("salida_parcial", {})
        self.salida_parcial_activa = sp.get("activo", True)
        self.pct_primer_objetivo = sp.get("pct_primer_objetivo", 0.70)
        self.modo_resto = sp.get("modo_resto", "breakeven")
        self.rb_primer_objetivo = sp.get("rb_primer_objetivo", 1.0)

        # Modo paper trading activo — protección importante
        if self.paper_trading:
            print("⚠️  MODO PAPER TRADING ACTIVO — No se ejecutarán órdenes reales")

    def calcular_orden(
        self,
        ticker: str,
        tipo: str,
        precio_actual: float,
        atr: float,
        capital_disponible: float,
        riesgo_por_operacion_pct: Optional[float] = None,
        precio_entrada_override: Optional[float] = None,
        stop_loss_override: Optional[float] = None,
        take_profit_override: Optional[float] = None,
        cantidad_override: Optional[int] = None,
    ) -> Optional[OrdenTradingCalculada]:
        """
        Calcula los parámetros completos de una orden LIMIT, incluyendo el
        plan de SALIDA PARCIAL 70/30.

        Lógica de cálculo:
          → Precio LIMIT = entrada (override del agente o precio actual)
          → Stop Loss    = override del agente, o precio ± (ATR × factor)
          → Cantidad     = capital_riesgo / distancia_stop
          → 70% de las acciones cierran en el primer objetivo (TP parcial)
          → 30% restante continúa protegido en breakeven o SL fijo

        Los parámetros *_override permiten que los agentes SMC impongan
        sus niveles de entrada/SL/TP (FVG); si son None se usa el cálculo
        clásico por ATR (compatibilidad con el flujo anterior).

        Retorna OrdenTradingCalculada o None si es inválido.
        """
        if precio_actual <= 0 or atr <= 0:
            return None
        if tipo not in ('BUY', 'SELL'):
            return None

        # --- Entrada (LIMIT) ---
        precio_limite = precio_entrada_override if precio_entrada_override is not None else precio_actual
        if precio_limite <= 0:
            return None

        # --- Stop Loss ---
        distancia_sl_atr = atr * self.stop_loss_factor
        if stop_loss_override is not None:
            stop_loss = stop_loss_override
        else:
            stop_loss = (precio_limite - distancia_sl_atr) if tipo == 'BUY' \
                else (precio_limite + distancia_sl_atr)

        # Distancia real del stop (riesgo por acción)
        distancia_sl = abs(precio_limite - stop_loss)
        if distancia_sl <= 0:
            return None

        # --- Trailing stop (compatibilidad; el 30% usa modo_resto) ---
        trailing_stop_pct = (atr * self.trailing_stop_factor / precio_limite) * 100

        # --- Cantidad según riesgo de capital ---
        risk_pct = (
            self.risk_per_trade_pct
            if riesgo_por_operacion_pct is None
            else float(riesgo_por_operacion_pct)
        )
        capital_riesgo = capital_disponible * (risk_pct / 100)
        if cantidad_override is not None:
            # v2.9.2: an explicit per-ticker share count is exact.  This keeps
            # execution sizing independent from the retraining/model state.
            cantidad = max(1, int(cantidad_override))
        elif self.sizing_mode == "fixed":
            cantidad = self.fixed_shares
        else:
            cantidad = max(1, int(capital_riesgo / distancia_sl))
        # Tope de validación en real con tamaño mínimo (1 = arranque seguro)
        if self.max_acciones_por_orden > 0:
            cantidad = min(cantidad, self.max_acciones_por_orden)
        riesgo_estimado = distancia_sl * cantidad

        # --- Objetivo final (30% restante) ---
        # TP is an independent ATR distance.  It must not be derived from the
        # widened stop because SL 3.5×ATR + the old 2R rule would silently turn
        # the requested TP 4.5×ATR into TP 7.0×ATR.
        if take_profit_override is not None:
            take_profit = take_profit_override
        else:
            distancia_tp_atr = atr * self.take_profit_factor
            take_profit = (precio_limite + distancia_tp_atr) if tipo == 'BUY' \
                else (precio_limite - distancia_tp_atr)

        # --- Primer objetivo parcial (venta del 70%) ---
        take_profit_parcial = (precio_limite + distancia_sl * self.rb_primer_objetivo) if tipo == 'BUY' \
            else (precio_limite - distancia_sl * self.rb_primer_objetivo)

        # Clamp: TP1 SIEMPRE debe caer estrictamente entre la entrada y el TP final,
        # si no, la salida parcial no tiene sentido (TP1 no puede superar el objetivo).
        salida_parcial_ok = self.salida_parcial_activa and cantidad >= 3
        if salida_parcial_ok:
            if tipo == 'BUY':
                if take_profit > precio_limite:
                    take_profit_parcial = min(max(take_profit_parcial, precio_limite + 0.01),
                                              take_profit - 0.01)
                else:
                    salida_parcial_ok = False
                if take_profit_parcial <= precio_limite:
                    salida_parcial_ok = False
            else:  # SELL
                if take_profit < precio_limite:
                    take_profit_parcial = max(min(take_profit_parcial, precio_limite - 0.01),
                                              take_profit + 0.01)
                else:
                    salida_parcial_ok = False
                if take_profit_parcial >= precio_limite:
                    salida_parcial_ok = False

        # --- Reparto 70/30 ---
        cantidad_parcial, cantidad_resto = self._repartir_cantidad(cantidad, salida_parcial_ok)

        # Redondear a 2 decimales y GARANTIZAR separación mínima de 1 centavo entre
        # entrada y SL. distancia_sl se validó con valores SIN redondear; en un ticker
        # barato round(entrada,2) y round(sl,2) pueden colapsar al MISMO precio → un
        # STOP en la entrada (se dispara al instante o no protege). Se empuja 1 tick.
        pl_r = round(precio_limite, 2)
        sl_r = round(stop_loss, 2)
        # Comparar en CENTAVOS enteros (evita el ruido de coma flotante de '< 0.01')
        if abs(round(pl_r * 100) - round(sl_r * 100)) < 1:
            sl_r = round(pl_r - 0.01, 2) if tipo == 'BUY' else round(pl_r + 0.01, 2)

        return OrdenTradingCalculada(
            ticker=ticker,
            tipo=tipo,
            precio_limite=pl_r,
            stop_loss=sl_r,
            trailing_stop_pct=round(trailing_stop_pct, 4),
            cantidad=cantidad,
            riesgo_usd=round(riesgo_estimado, 2),
            atr_actual=round(atr, 4),
            take_profit=round(take_profit, 2),
            take_profit_parcial=round(take_profit_parcial, 2),
            cantidad_parcial=cantidad_parcial,
            cantidad_resto=cantidad_resto,
            modo_resto=self.modo_resto,
            salida_parcial_activa=salida_parcial_ok,
            stop_loss_atr_factor=round(self.stop_loss_factor, 4),
            take_profit_atr_factor=round(self.take_profit_factor, 4),
        )

    def _repartir_cantidad(self, cantidad: int, activa: bool):
        """
        Divide la cantidad en (parcial ~70%, resto ~30%).

        Requiere >= 3 acciones para que el 70/30 sea representable (con 2
        acciones el reparto degeneraría a 50/50). Deja al menos 1 acción en
        cada tramo.
        """
        if cantidad < 3 or not activa:
            return cantidad, 0
        parcial = int(round(cantidad * self.pct_primer_objetivo))
        parcial = max(1, min(parcial, cantidad - 1))  # deja al menos 1 para el resto
        resto = cantidad - parcial
        return parcial, resto

    def verificar_limite_posiciones(self, posiciones_abiertas: int) -> bool:
        """
        Verifica si se puede abrir una nueva posición.

        Retorna True si se puede abrir, False si se alcanzó el límite.
        El límite máximo de posiciones simultáneas se configura en config.yaml.
        """
        puede_abrir = posiciones_abiertas < self.max_posiciones
        if not puede_abrir:
            print(f"⛔ Límite de posiciones simultáneas alcanzado ({self.max_posiciones})")
        return puede_abrir
