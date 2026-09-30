# 📗 GALA6 Chocoletra Bot — Documento de Handover (Traspaso)

> Guía de arquitectura para desarrolladores futuros. Explica qué hace cada
> parte del sistema, cómo encajan los 3 Agentes SMC con el motor, y cómo
> operar/mantener el bot. Escrito para que cualquier informático pueda
> continuar el proyecto sin trabas.

---

## 1. Visión general

El bot opera acciones de EE.UU. vía **Charles Schwab API**. El cerebro de
decisión es un **sistema dinámico de 3 Agentes de IA** basado en *Smart Money
Concepts* (SMC), que cruza el historial multi-temporal de cada acción con la
alerta matemática de la estrategia GALA6 original.

```
Dashboard (Flask)  ──►  server.py  ──►  engine/bot_engine.py (Motor Real)
                                             │
                 ┌───────────────────────────┼─────────────────────────────┐
                 ▼                            ▼                             ▼
        core/strategy.py (MACD)      agents/ (3 Agentes SMC)      api/schwab_client.py
                                             │                    (datos multi-temporal)
                                             ▼
                         agents/orchestrator.py (confluencia + meta-modelo)
                                             │
                                             ▼
                   core/risk_manager.py (70/30)  +  api/hybrid_order_executor.py
                                    (SL/TP, cantidad)     (LIMIT/MARKET, chase, bracket)
```

---

## 2. El flujo de una decisión (lo más importante)

Por cada ticker y en cada ciclo, `engine/bot_engine.py::_procesar_ticker` hace:

1. **Datos** — `schwab.obtener_multitemporal(ticker)` devuelve velas de
   `1W, 1D, 4H, 1H, 15m, 5m, 1m` (con caché por TTL).
2. **Gestión de la posición abierta** — parciales 70/30, breakeven, cierre.
3. **Señal matemática** — `core/strategy.py` sobre 1m (para la *confluencia*).
4. **Decisión de los agentes** — `orchestrator.decidir(...)`:
   - **Agente 1** (`agents/agent1_macro.py`) → *Dirección del Día*
     (ALCISTA/BAJISTA/NEUTRAL) analizando estructura en W/D/4H/1H + niveles de
     liquidez (PDH/PDL/PDC, PWH/PWL/PWC, EQH/EQL, swings) + brecha de apertura.
     Si NEUTRAL → no se opera.
   - **Agente 2** (`agents/agent2_micro.py`) → baja a 15m/5m, busca un **FVG**
     (Fair Value Gap) no mitigado alineado con la dirección macro y calcula
     entrada / SL / TP con una **Relación Beneficio/Riesgo (R/B)**. Si ningún
     FVG cumple el R/B mínimo → no hay propuesta.
   - **Agente 3** (`agents/agent3_auditor.py`) → audita volatilidad (ATR),
     horario institucional (RTH) y **trampa de rango lateral**. Si hay peligro
     → **VETA**.
   - **Confluencia** con `core/strategy.py` y **meta-modelo** dinámico.
5. **Ejecución** — si hay vía libre: guardián de duplicados, reversión si la
   señal giró, y apertura con `risk_manager` + `hybrid_order_executor`.

---

## 3. Mapa de archivos

| Archivo | Rol |
|---|---|
| `server.py` | Dashboard Flask + OAuth2 Schwab. El **botón de encendido** llama a `_start_engine()` que arranca el **motor real**. |
| `engine/bot_engine.py` | **Motor real** multihilo. Orquesta datos, agentes, riesgo, ejecución y el ciclo de vida de cada posición. |
| `core/strategy.py` | Estrategia GALA6 (MACD/ATR/EMA) migrada de ThinkScript. Genera la alerta matemática de confluencia. |
| `core/signals.py` | Estado por ticker y objetos `Señal`. |
| `core/risk_manager.py` | Cálculo de orden: cantidad, SL, y **plan de salida parcial 70/30**. Acepta *overrides* de entrada/SL/TP de los agentes. |
| `core/time_logic.py` | Ventanas horarias intradía (09:42, 10:30, 12:00, 12:30). |
| `agents/contracts.py` | **Contratos de datos** compartidos (tipos que producen/consumen los agentes). |
| `agents/smc_primitives.py` | Matemática SMC compartida: FVG, liquidez, swings, estructura. **Sin look-ahead**. |
| `agents/agent1_macro.py` | Agente 1 — Macro/Liquidez. |
| `agents/agent2_micro.py` | Agente 2 — Micro/FVG (francotirador). |
| `agents/agent3_auditor.py` | Agente 3 — Auditor de Riesgo. |
| `agents/orchestrator.py` | Coordina los 3 agentes + confluencia + meta-modelo + ciclo de aprendizaje. |
| `agents/feedback_store.py` | Guarda cada decisión y su resultado real (thread-safe). |
| `agents/training.py` | **Meta-modelo dinámico** (XGBoost) que se reentrena solo. |
| `api/schwab_client.py` | Cliente Schwab: OAuth2, velas por temporalidad, **paginado multi-temporal + 1 año**. |
| `api/hybrid_order_executor.py` | Ejecución híbrida LIMIT/MARKET, **persecución de límites (chase)**, bracket 70/30, liquidación. |
| `api/order_executor.py` | Ejecutor LIMIT simple (heredado). |
| `ai_filter/` | Filtro MACD heredado (XGBoost) + recolector de datos (1 año). |
| `gsco/` | Correlación institucional vía Google Sheets. |
| `notifications/telegram_bot.py` | Alertas de Telegram. |
| `engine/scheduler.py` | Encendido/apagado por horario (kill switch por tiempo). |

---

## 4. Los 5 requisitos y las mejoras — dónde están

1. **Motor real conectado al Dashboard** → `server.py::_start_engine` +
   `engine/bot_engine.py`. El botón arranca `ChocoletraBot`, no el bucle de prueba.
2. **Historial de 1 año** → `api/schwab_client.py::obtener_historico` /
   `obtener_multitemporal` (paginado) + `ai_filter/data_collector.py`
   (`dias_historia_schwab: 365`, 20+ tickers). *Nota:* Schwab da 1 año nativo en
   velas **diarias**; el histórico de **minutos** está limitado por la API (~semanas)
   y el recolector **registra la cobertura real** obtenida.
3. **3 Agentes SMC** → paquete `agents/`.
4. **Salida parcial 70/30** → `core/risk_manager.py` + gestión en
   `bot_engine.py::_ejecutar_parcial` / `_cerrar_posicion`.
5. **Ejecución híbrida + chase** → `api/hybrid_order_executor.py::ejecutar_con_persecucion`.

**Extras acordados:**
- **Reversión reactiva** (cierra y voltea al girar la señal, pasando por el
  Agente 3) → `bot_engine.py::_abrir_o_revertir`.
- **Guardián de entradas duplicadas** (misma dirección no adiciona) → idem.
- **Kill switch** (liquida todo al apagar o al cierre de mercado) →
  `bot_engine.py::detener` / `_es_hora_cierre` / `_liquidar_todo`.
- **Entrenamiento dinámico** (no estático) → `agents/training.py`
  reentrena el meta-modelo cada N operaciones cerradas.
- **Vigilancia de cierres/aperturas previas** → niveles PDH/PDL/PDC, PWH/PWL/PWC
  en `agents/smc_primitives.py`, usados por el Agente 1.

---

## 5. Configuración (`config.yaml`)

Secciones clave:
- `risk.paper_trading` — **`true` = simulado, `false` = dinero real**. (Ver §6.)
- `agents` — activación y parámetros de los 3 agentes + entrenamiento dinámico.
- `salida_parcial` — 70/30, breakeven vs SL fijo.
- `reversion` — reversión reactiva.
- `chase` — persecución de límites.
- `kill_switch` — liquidación al apagar/cierre.
- `ai.dias_historia_schwab` — profundidad de historia (365).

---

## 6. Paso a cuenta REAL (⚠️ dinero real)

1. Validar varios días en **paper trading** (`risk.paper_trading: true`).
2. Confirmar tokens OAuth2 de Schwab y `SCHWAB_ACCOUNT_NUMBER` en `.env`.
3. Cambiar en `config.yaml`: `risk.paper_trading: false`.
4. Reiniciar el motor desde el Dashboard.

**Seguridad implementada:** el Dashboard solo puede **forzar** paper trading,
nunca habilita dinero real en silencio; el modo real se controla **únicamente**
en `config.yaml`. Por defecto el sistema arranca en paper.

---

## 7. Cómo ejecutar

```bash
pip install -r requirements.txt
python server.py              # Dashboard en http://<host>:5000
# En el Dashboard: conectar Schwab (OAuth) y pulsar ENCENDER.

# Recolectar datos y (re)entrenar el filtro MACD heredado:
python -m ai_filter.data_collector
python -m ai_filter.train_model
```
El meta-modelo de los agentes se entrena **solo** con los resultados reales
(no requiere ejecución manual), pero se puede forzar con
`OrquestadorAgentes.entrenador.entrenar()`.

---

## 8. Aprendizaje dinámico de los agentes

- Al abrir: `orchestrator.al_abrir(decision, entrada)` guarda las *features*.
- Al cerrar: `orchestrator.al_cerrar(ticker, precio)` registra el resultado real
  (R múltiple, ganadora/perdedora) en `data/agentes_feedback.csv` y dispara el
  **reentrenamiento automático** cada `reentrenar_cada_n` operaciones.
- Es **un modelo compartido** entre las 20+ acciones (features relativas/normalizadas),
  por lo que más acciones ⇒ aprende más rápido, sin mantener 20 modelos.
- En **paper trading**, el ciclo de vida de cada operación se **simula** contra
  las velas reales, generando datos de entrenamiento incluso antes de operar en real.

---

## 9. Notas de mantenimiento

- **Sin look-ahead**: `agents/smc_primitives.py` solo usa velas cerradas; los
  swings se confirman con velas posteriores reales. Mantener esta disciplina.
- **Thread-safety**: el motor procesa tickers en paralelo; `_posiciones`,
  el `FeedbackStore` y el reentrenamiento están protegidos con locks.
- **Prints seguros**: `api/hybrid_order_executor.py::_ps` evita que un error de
  codificación de consola enmascare una orden ya enviada.
- El bucle de prueba heredado (`server.py::_bot_loop`) queda **inerte**
  (ya no se invoca); puede eliminarse en una limpieza futura.

---

## 10. Endurecimiento aplicado (revisión adversaria)

Tras una revisión multi-agente adversaria se corrigieron, entre otros:

- **Sin look-ahead en FVG**: la detección de FVG excluye la vela en formación
  (`fvgs_validos(..., en_formacion=True)`); un FVG cuya vela de impulso es la
  vela en curso no genera entradas (evita divergencia backtest/real).
- **Protección real coherente**:
  - La orden de entrada es SINGLE (sin trailing adjunto): se eliminó la
    triple-protección (antes: trailing 100% + TP1 70% + SL 100%).
  - Tras el TP1, el STOP del 100% SIEMPRE se cancela y se recoloca por el 30%
    (breakeven o SL fijo), evitando vender de más y abrir una posición opuesta.
  - Al cerrar/revertir, se cancelan las protecciones vivas y, en reversión/kill
    switch, se liquida ANTES de soltar el estado (si falla, la posición se
    mantiene y se reintenta).
  - Toda posición real tiene siempre al menos un STOP.
- **Persecución (chase)**: verifica que la cancelación tuvo éxito antes de
  re-cotizar (no duplica órdenes) y limita la deriva acumulada.
- **Concurrencia**: lock reentrante; reserva atómica del hueco de posición
  (límite y anti-duplicado), contadores y CSV de feedback bajo lock, guardia de
  reentrenamiento único.
- **Seguridad de modo real**: el dashboard no puede cambiar `paper_trading`
  (solo `config.yaml`); no se opera con datos sintéticos de fallback en real.

## 11. Limitaciones conocidas y recomendaciones antes de operar en real

- **Autenticación del dashboard DESACTIVADA** (decisión heredada del cliente:
  `server.py::check_auth` es un `pass`). Antes de operar con dinero real,
  **reactivar la autenticación** para que solo personal autorizado pueda
  encender el motor o cambiar tickers.
- **Bracket gestionado por el motor (no OCO nativo)**: el 70/30 se administra
  activamente (el motor mueve el SL a breakeven al llenarse el TP1). Si el motor
  se cae a mitad de una operación, el STOP inicial del 100% sigue protegiendo la
  posición completa en el stop original (comportamiento seguro), pero el
  breakeven del 30% no se aplicará hasta reiniciar. *Mejora futura*: usar OCO
  nativo de Schwab.
- **Historia de minutos limitada por la API** (~semanas). Las velas diarias y
  semanales sí cubren 1 año+; el recolector registra la cobertura real.
- **Validar en paper trading varios días** antes de `paper_trading: false`.

*Documento de handover — proyecto GALA6 Chocoletra Bot.*
