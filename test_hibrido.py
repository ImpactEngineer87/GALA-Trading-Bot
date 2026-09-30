import sys
sys.path.insert(0, '.')
from api.hybrid_order_executor import EjecutorHibridoDeOrdenes, ContextoMercado
from core.risk_manager import OrdenTradingCalculada

ejecutor = EjecutorHibridoDeOrdenes(paper_trading=True)

# Caso 1: Rango lateral -> 50% acciones, LIMIT
orden1 = OrdenTradingCalculada('BA','BUY',227.50,225.00,1.5,10, riesgo_usd=200, atr_actual=0.45)
ctx1 = ContextoMercado(atr_relativo=0.10, es_rango_lateral=True, prob_ia=0.65, es_senal_maestra=False)
r1 = ejecutor.ejecutar_hibrido(orden1, ctx1, 'TEST')
print("CASO 1 RANGO:", r1.get("tipo_orden"), "| cant:", r1.get("cantidad"), "| motivo:", r1.get("motivo_ajuste"))

# Caso 2: Alta prob IA -> 200% acciones
orden2 = OrdenTradingCalculada('TSLA','BUY',248.00,245.00,1.5,10, riesgo_usd=200, atr_actual=0.74)
ctx2 = ContextoMercado(atr_relativo=0.30, es_rango_lateral=False, prob_ia=0.85, es_senal_maestra=False)
r2 = ejecutor.ejecutar_hibrido(orden2, ctx2, 'TEST')
print("CASO 2 ALTA IA:", r2.get("tipo_orden"), "| cant:", r2.get("cantidad"), "| motivo:", r2.get("motivo_ajuste"))

# Caso 3: Alta volatilidad -> MARKET
orden3 = OrdenTradingCalculada('NVDA','BUY',118.00,115.00,1.5,10, riesgo_usd=200, atr_actual=0.83)
ctx3 = ContextoMercado(atr_relativo=0.70, es_rango_lateral=False, prob_ia=0.72, es_senal_maestra=False)
r3 = ejecutor.ejecutar_hibrido(orden3, ctx3, 'TEST')
print("CASO 3 MARKET:", r3.get("tipo_orden"), "| cant:", r3.get("cantidad"), "| motivo:", r3.get("motivo_ajuste"))

# Caso 4: Senal Maestra -> 250% acciones
orden4 = OrdenTradingCalculada('AAPL','BUY',192.00,189.00,1.5,10, riesgo_usd=200, atr_actual=0.48)
ctx4 = ContextoMercado(atr_relativo=0.25, es_rango_lateral=False, prob_ia=0.90, es_senal_maestra=True)
r4 = ejecutor.ejecutar_hibrido(orden4, ctx4, 'TEST')
print("CASO 4 MAESTRA:", r4.get("tipo_orden"), "| cant:", r4.get("cantidad"), "| motivo:", r4.get("motivo_ajuste"))
