# GALA6 Chocoletra — Python Bot Migration Plan

## Objetivo
Migrar la estrategia ThinkScript GALA6 (MACD Bot Chocoletra) a Python modular, con documentación en español, lista para integración con Schwab API e IA.

## Fase 1 — Migración Core (EN EJECUCIÓN)

### Archivos a crear

#### core/strategy.py
- MACD (EMA rápida/lenta, señal, histograma)
- Filtro ATR Wilder-14
- Tendencia EMA-11 con slope mínimo
- Memoria de cruce (crossHigh, crossLow, crossBars)
- Filtro de rango (últimas 8 velas, mínimo 0.18%)
- Histograma progresivo (bull/bear)
- Calidad de vela (impulso fuerte)
- Ruptura del muro (breakout buffer)
- Cooldown de 8 velas

#### core/signals.py
- Motor de señales BUY/SELL
- Señales omitidas (OM-R, OM-T)
- Estado del bot por ticker

#### core/risk_manager.py
- Trailing stop dinámico
- Stop loss calculado por ATR
- Límite de órdenes simultáneas

#### api/schwab_client.py
- OAuth2 skeleton
- Streaming de velas 1-min

#### api/order_executor.py
- Órdenes LIMIT (no Market)
- Trailing stop automático

#### engine/bot_engine.py
- Multi-threading (20+ activos)
- Loop principal

#### main.py + config.yaml + requirements.txt

## Verificación
- Backtesting con datos CSV de ejemplo
- Comparación señal por señal con ThinkScript original
