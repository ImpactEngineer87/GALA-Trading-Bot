"""
=============================================================
MÓDULO: agents/orchestrator.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Orquestador del sistema de 3 agentes. Coordina el flujo de
    decisión y confluencia:

        Agente 1 (Macro/Liquidez)  → Dirección del Día
              │  (si NEUTRAL → no operar)
              ▼
        Agente 2 (Micro/FVG)       → Propuesta de operación
              │  (si None → no operar)
              ▼
        Agente 3 (Auditor/Riesgo)  → APROBADA / VETADA
              │  (si VETADA → no operar)
              ▼
        Cruce con core/strategy.py (alerta matemática original)
              │
              ▼
        Meta-modelo de agentes (filtro dinámico aprendido)
              │
              ▼
        DecisionFinal  → la consume el motor (bot_engine)

    También expone el ciclo de aprendizaje: al_abrir() guarda las
    features de la operación y al_cerrar() registra el resultado y
    dispara el reentrenamiento automático del meta-modelo.
=============================================================
"""

from datetime import datetime
from typing import Optional

from agents.contracts import (
    Direccion, DatosMultiTemporal, DecisionFinal, DireccionDia, PropuestaOperacion,
)
from agents.agent1_macro import AgenteMacroLiquidez
from agents.agent2_micro import AgenteMicroFrancotirador
from agents.agent3_auditor import AgenteAuditorRiesgo
from agents.feedback_store import FeedbackStore
from agents.shadow_store import ShadowDecisionStore
from agents.training import EntrenadorAgentes
from agents.volume_features import calcular_features_volumen
from agents.market_regime import AgenteRegimenMercado


class OrquestadorAgentes:
    """Coordina los 3 agentes SMC y el aprendizaje continuo."""

    def __init__(self, config: dict):
        self.config = config
        agents_cfg = config.get("agents", {})
        self.activo: bool = agents_cfg.get("activo", True)
        self.requiere_confluencia: bool = agents_cfg.get("requiere_confluencia_estrategia", False)

        self.agente1 = AgenteMacroLiquidez(config)
        self.agente2 = AgenteMicroFrancotirador(config)
        self.agente3 = AgenteAuditorRiesgo(config)
        # v3.1: context-only market regime agent.  GALA10 remains the sole
        # BUY/SELL trigger; this agent can only recommend/perform a veto.
        self.regime_agent = AgenteRegimenMercado(config)

        ent_cfg = agents_cfg.get("entrenamiento", {})
        self.entrenador = EntrenadorAgentes(config)
        self.feedback = FeedbackStore(ent_cfg.get("feedback_path", "data/agentes_feedback.csv"))
        shadow_cfg = agents_cfg.get("shadow_mode", {})
        self.shadow = ShadowDecisionStore(
            shadow_cfg.get("decisions_path", "data/shadow_decisions.csv")
        )

        # Reglas deterministas de veto solicitadas por el cliente. Mientras el
        # filtro de producción esté desactivado (umbral_meta_filtro <= 0), estas
        # reglas solo modifican la recomendación Shadow y nunca bloquean GALA10.
        hard_cfg = agents_cfg.get("hard_veto", {})
        self.hard_veto_enabled = bool(hard_cfg.get("enabled", True))
        self.hard_veto_macro_neutral = bool(
            hard_cfg.get("macro_neutral", True)
        )
        self.hard_veto_lateral_range = bool(
            hard_cfg.get("lateral_range", True)
        )
        self.hard_veto_probability_cap = float(
            hard_cfg.get("probability_cap", 0.30)
        )

    # =========================================================
    # DECISIÓN
    # =========================================================

    def decidir(self, datos: DatosMultiTemporal, precio_actual: float,
                senal_estrategia: Optional[str] = None,
                ahora: Optional[datetime] = None,
                actual_en_formacion: bool = True) -> DecisionFinal:
        """
        Ejecuta el flujo completo de los 3 agentes y devuelve una
        DecisionFinal. 'senal_estrategia' es la señal de core/strategy.py
        para el mismo ticker ('BUY'/'SELL'/'OM-R'/'OM-T'/None).
        """
        ts = ahora or datetime.now()
        ticker = datos.ticker

        # Sistema desactivado → no operar (deja el control a la lógica previa)
        if not self.activo:
            return DecisionFinal.no_operar(ticker, "Sistema de agentes desactivado", timestamp=ts)

        # --- AGENTE 1: dirección macro ---
        direccion_dia = self.agente1.analizar(
            datos, precio_actual=precio_actual,
            actual_en_formacion=actual_en_formacion, ahora=ts,
        )
        if direccion_dia.direccion == Direccion.NEUTRAL:
            return DecisionFinal.no_operar(
                ticker, "Agente 1: dirección NEUTRAL — sin vía libre",
                direccion_dia=direccion_dia, timestamp=ts,
            )

        # --- AGENTE 2: propuesta micro (FVG) ---
        propuesta = self.agente2.analizar(datos, direccion_dia, precio_actual, ahora=ts,
                                          en_formacion=actual_en_formacion)
        if propuesta is None:
            return DecisionFinal.no_operar(
                ticker, "Agente 2: sin FVG válido con R/B suficiente",
                direccion_dia=direccion_dia, timestamp=ts,
            )

        # --- AGENTE 3: auditoría de riesgo ---
        veredicto = self.agente3.auditar(
            datos, propuesta, ahora=ts, hora_et_override=ts,
            actual_en_formacion=actual_en_formacion,
        )
        if not veredicto.aprobada:
            return DecisionFinal.no_operar(
                ticker, f"Agente 3: {veredicto.motivo}",
                direccion_dia=direccion_dia, propuesta=propuesta,
                veredicto=veredicto, timestamp=ts,
            )

        # --- CONFLUENCIA con la alerta matemática (core/strategy.py) ---
        confluencia = (senal_estrategia is not None and senal_estrategia == propuesta.tipo)
        if self.requiere_confluencia and not confluencia:
            return DecisionFinal.no_operar(
                ticker, f"Sin confluencia con estrategia (señal={senal_estrategia}, propuesta={propuesta.tipo})",
                direccion_dia=direccion_dia, propuesta=propuesta,
                veredicto=veredicto, timestamp=ts,
            )

        # --- Construir decisión positiva y aplicar meta-filtro dinámico ---
        decision = DecisionFinal(
            ticker=ticker, operar=True, tipo=propuesta.tipo,
            propuesta=propuesta, direccion_dia=direccion_dia, veredicto=veredicto,
            confluencia_estrategia=confluencia, timestamp=ts,
            resumen=f"{propuesta.tipo} {ticker} @ ${propuesta.precio_entrada:.2f} "
                    f"| R/B {propuesta.ratio_rb:.2f} | dir {direccion_dia.direccion.value}",
        )
        volumen = calcular_features_volumen(
            datos, actual_en_formacion=actual_en_formacion)
        for nombre, valor in volumen.items():
            setattr(decision, nombre, valor)

        prob, aprueba = self.entrenador.evaluar(decision)
        decision.prob_meta = prob
        if not aprueba:
            prob_text = "N/A" if prob is None else f"{prob:.2f}"
            return DecisionFinal.no_operar(
                ticker, f"Meta-modelo descartó la decisión (prob={prob_text})",
                direccion_dia=direccion_dia, propuesta=propuesta,
                veredicto=veredicto, timestamp=ts,
            )

        decision.resumen += (
            " | meta N/A" if prob is None else f" | meta {prob:.2f}"
        )
        return decision

    # =========================================================
    # DECISIÓN REORIENTADA: Gala 10 = disparador, XGBoost/ticker = veto
    # =========================================================

    def auditar_gala10(self, datos: DatosMultiTemporal, precio_actual: float,
                       senal_gala10: Optional[str], atr: float,
                       ahora: Optional[datetime] = None,
                       actual_en_formacion: bool = True,
                       macro_en_formacion: Optional[bool] = None,
                       micro_en_formacion: Optional[bool] = None,
                       gsco: Optional[dict] = None,
                       gala10_contexto: Optional[dict] = None,
                       requerir_modelo: bool = False,
                       execution_mode: str = "") -> DecisionFinal:
        """
        Flujo acordado con el cliente:
          Gala 10 dispara la señal (senal_gala10 = 'BUY'/'SELL').
          Los agentes SMC dan CONTEXTO (features): dirección macro, FVG, ATR, rango.
          El XGBoost INDIVIDUAL del ticker VETA (baja probabilidad → descarta).
        Fuera de RTH y falta de datos siguen siendo vetos operativos inmediatos.
        Además, macro NEUTRAL y compresión lateral son reglas deterministas del
        filtro IA: en Shadow Mode se registran como VETO sin bloquear GALA10; al
        habilitar el filtro de producción sí bloquean.
        """
        ts = ahora or datetime.now()
        ticker = datos.ticker
        effective_mode = str(execution_mode or "").strip().upper()
        if effective_mode not in {"PAPER", "REAL"}:
            # Backward-compatible fallback for older callers. New engine code
            # always passes the ticker's explicit per-ticker execution mode.
            effective_mode = "REAL" if requerir_modelo else "PAPER"
        # Compatibilidad: los callers antiguos solo pasan
        # ``actual_en_formacion``. El motor nuevo separa macro y micro porque la
        # vela 1m ya fue recortada al cierre confirmado, mientras que los
        # periodos diario/semanal pueden seguir en formación durante la sesión.
        macro_forming = (
            bool(actual_en_formacion)
            if macro_en_formacion is None
            else bool(macro_en_formacion)
        )
        micro_forming = (
            bool(actual_en_formacion)
            if micro_en_formacion is None
            else bool(micro_en_formacion)
        )

        if not self.activo:
            return DecisionFinal.no_operar(ticker, "Sistema de agentes desactivado", timestamp=ts)
        if senal_gala10 not in ("BUY", "SELL"):
            return DecisionFinal.no_operar(ticker, "Sin señal de Gala 10 (no hay disparador)", timestamp=ts)

        # Agente 1 → contexto macro (feature: confianza, hueco, niveles)
        direccion_dia = self.agente1.analizar(
            datos, precio_actual=precio_actual,
            actual_en_formacion=macro_forming, ahora=ts,
        )

        # Agente 2 → FVG EN LA DIRECCIÓN DE GALA 10, exclusivamente como
        # CONTEXTO/feature de veto. No puede cambiar el precio, tipo ni momento
        # de la entrada maestra.
        dir_gala = DireccionDia(
            ticker=ticker,
            direccion=Direccion.ALCISTA if senal_gala10 == "BUY" else Direccion.BAJISTA,
            confianza=1.0, niveles_liquidez=direccion_dia.niveles_liquidez,
            hueco_apertura_pct=direccion_dia.hueco_apertura_pct, timestamp=ts,
        )
        propuesta_contexto = self.agente2.analizar(
            datos, dir_gala, precio_actual, ahora=ts,
            en_formacion=micro_forming,
        )
        # La ejecución nace SIEMPRE de Gala10. Antes, un FVG del Agente 2 podía
        # convertir la señal en una orden LIMIT de retroceso, lo que hacía que
        # el agente generase/modificase entradas en contra del alcance acordado.
        propuesta = self._propuesta_market(
            ticker, senal_gala10, precio_actual, atr, ts)
        fvg_contexto_tam_rel = 0.0
        if (
            propuesta_contexto is not None
            and propuesta_contexto.fvg is not None
            and precio_actual > 0
        ):
            fvg_contexto_tam_rel = (
                propuesta_contexto.fvg.tamano / precio_actual * 100.0)

        # Agente 3 → features de riesgo (atr, rango). VETO operativo en dos casos:
        #   (a) sin microcontexto auditable (datos insuficientes) → fail-closed;
        #   (b) fuera de horario institucional (RTH).
        # La compresión lateral se aplica más abajo como regla determinista del
        # filtro; en Shadow no cambia la ejecución maestra de GALA10.
        veredicto = self.agente3.auditar(
            datos, propuesta, ahora=ts, hora_et_override=ts,
            actual_en_formacion=micro_forming,
        )
        if not veredicto.datos_suficientes:
            return DecisionFinal.no_operar(
                ticker, "Sin microcontexto auditable (datos micro insuficientes)",
                direccion_dia=direccion_dia, propuesta=propuesta,
                veredicto=veredicto, timestamp=ts,
            )
        if not veredicto.es_horario_institucional:
            return DecisionFinal.no_operar(
                ticker, "Fuera de horario institucional (RTH)",
                direccion_dia=direccion_dia, propuesta=propuesta,
                veredicto=veredicto, timestamp=ts,
            )

        confluencia = direccion_dia.permite(senal_gala10)   # ¿el macro acompaña? (feature)
        decision = DecisionFinal(
            ticker=ticker, operar=True, tipo=senal_gala10,
            execution_mode=effective_mode,
            propuesta=propuesta, direccion_dia=direccion_dia, veredicto=veredicto,
            fvg_contexto_tam_rel=fvg_contexto_tam_rel,
            confluencia_estrategia=confluencia, timestamp=ts,
            resumen=f"Gala10 {senal_gala10} {ticker} @ ${precio_actual:.2f} | R/B {propuesta.ratio_rb:.2f}",
        )
        if gsco:
            decision.gsco_match = gsco.get("match", float("nan"))
            decision.gsco_dist_pct = gsco.get("dist_pct", float("nan"))
        volumen = calcular_features_volumen(
            datos, actual_en_formacion=micro_forming)
        for nombre, valor in volumen.items():
            setattr(decision, nombre, valor)
        # Estado del MISMO cálculo GALA10 que disparó la entrada. Se copia después
        # de confirmar la señal; estos valores jamás actúan como disparador.
        for nombre, valor in (gala10_contexto or {}).items():
            if hasattr(decision, nombre):
                setattr(decision, nombre, valor)

        # v3.1 — broader market-regime/choppiness analysis.  The older
        # ``es_rango_lateral`` flag only catches narrow compression over a few
        # bars.  This layer also measures directional efficiency, alternating
        # returns, net movement in ATRs and breakout confirmation so an AI model
        # cannot approve every alternating signal inside a wider sideways box.
        regime_agent = getattr(self, "regime_agent", None)
        if regime_agent is not None:
            try:
                regime = regime_agent.evaluar(
                    datos,
                    senal_gala10,
                    gala10_contexto=gala10_contexto,
                    actual_en_formacion=micro_forming,
                )
                decision.market_regime_enabled = bool(regime_agent.enabled)
                decision.market_regime_enforcement = str(regime_agent.enforcement)
                decision.market_regime_policy = str(regime_agent.policy_for(effective_mode))
                decision.market_regime = str(regime.regime)
                decision.market_regime_confidence = float(regime.confidence)
                decision.market_regime_range_pct = float(regime.range_pct)
                decision.market_regime_efficiency = float(regime.efficiency_ratio)
                decision.market_regime_flip_ratio = float(regime.flip_ratio)
                decision.market_regime_net_move_atr = float(regime.net_move_atr)
                decision.market_regime_atr_ratio = float(regime.atr_regime)
                decision.market_regime_ema_slope_atr = float(regime.ema_slope_atr)
                decision.market_regime_breakout_atr = float(regime.breakout_atr)
                decision.market_regime_signal_aligned = regime.signal_aligned
                decision.market_regime_would_block = bool(regime.would_block)
                decision.market_regime_reason = str(regime.reason or "")
            except Exception:
                # Regime analytics are not allowed to break the established
                # GALA10/model pipeline.  REAL blocking is fail-safe only after
                # a valid regime decision has actually been produced.
                decision.market_regime_enabled = False
                decision.market_regime_enforcement = "off"
                decision.market_regime_policy = "off"
                decision.market_regime = "INSUFFICIENT_DATA"
                decision.market_regime_would_block = False
                decision.market_regime_reason = "REGIME_EVALUATION_ERROR"

        # Veto determinista: macro NEUTRAL o compresión lateral. La regla se
        # registra siempre, pero en la fase Shadow no puede bloquear la orden.
        hard_veto_reasons = []
        # ``auditar_gala10`` is also exercised by legacy integrations and test
        # doubles created with ``__new__``. Use safe defaults instead of assuming
        # that ``__init__`` has already populated the new v2 attributes.
        hard_veto_enabled = bool(
            getattr(self, "hard_veto_enabled", True)
        )
        hard_veto_macro_neutral = bool(
            getattr(self, "hard_veto_macro_neutral", True)
        )
        hard_veto_lateral_range = bool(
            getattr(self, "hard_veto_lateral_range", True)
        )
        hard_veto_probability_cap = float(
            getattr(self, "hard_veto_probability_cap", 0.30)
        )
        if hard_veto_enabled:
            if (
                hard_veto_macro_neutral
                and direccion_dia.direccion == Direccion.NEUTRAL
            ):
                hard_veto_reasons.append("MACRO_NEUTRAL")
            if (
                hard_veto_lateral_range
                and bool(getattr(veredicto, "es_rango_lateral", False))
            ):
                hard_veto_reasons.append("LATERAL_RANGE")
        decision.hard_veto = bool(hard_veto_reasons)
        decision.hard_veto_reason = "|".join(hard_veto_reasons)

        # Production veto and Shadow Mode are deliberately independent.
        # Shadow may recommend VETO, but it can never change decision.operar
        # while ``umbral_meta_filtro`` remains at 0.
        if requerir_modelo:
            prob, aprueba = self.entrenador.evaluar(
                decision, requerir_modelo=True)
        else:
            # Maintains compatibility with old test doubles.
            prob, aprueba = self.entrenador.evaluar(decision)
        if hasattr(self.entrenador, "evaluar_shadow"):
            shadow = self.entrenador.evaluar_shadow(decision)
        else:
            # Compatibility with old integrations/test doubles.
            shadow = {
                "enabled": False, "model_loaded": False,
                "probability": None, "threshold": None,
                "would_approve": None, "source": "",
            }

        # El hard-veto domina la recomendación aprendida. Si existe una
        # probabilidad real, se limita a 0.30; sin modelo permanece N/A.
        if decision.hard_veto:
            shadow_prob = shadow.get("probability")
            if shadow_prob is not None:
                shadow["probability"] = min(
                    float(shadow_prob), hard_veto_probability_cap
                )
            shadow["would_approve"] = False

        decision.shadow_enabled = bool(shadow.get("enabled", False))
        decision.shadow_model_loaded = bool(shadow.get("model_loaded", False))
        shadow_probability = shadow.get("probability")
        shadow_threshold = shadow.get("threshold")
        decision.shadow_prob = (
            float(shadow_probability) if shadow_probability is not None else None
        )
        decision.shadow_threshold = (
            float(shadow_threshold) if shadow_threshold is not None else None
        )
        decision.shadow_would_approve = shadow.get("would_approve")
        decision.shadow_model_source = str(shadow.get("source", "") or "")

        # Regime policy is independent from the learned model and from the
        # PAPER/REAL execution route. ``observe`` records the counterfactual;
        # ``enforce`` is allowed to veto in that ticker's execution mode.
        regime_policy = str(getattr(decision, "market_regime_policy", "off") or "off").lower()
        if (
            bool(getattr(decision, "market_regime_would_block", False))
            and regime_policy in {"observe", "enforce"}
        ):
            decision.shadow_would_approve = False

        decision.prob_meta = prob
        if not aprueba:
            decision.operar = False
            if prob is None:
                decision.execution_veto_reason = "AI_MODEL_UNAVAILABLE"
                decision.resumen = (
                    f"IA no disponible para {ticker}; el modo real exige un "
                    "modelo aprobado y cargado"
                )
            else:
                decision.execution_veto_reason = "AI_THRESHOLD"
                decision.resumen = (
                    f"IA (modelo aprobado {ticker}) vetó la señal de Gala 10 "
                    f"(prob={prob:.2f})"
                )
            return decision

        # Las reglas duras bloquean producción solo cuando el filtro de veto
        # está realmente habilitado. En PAPER/Shadow (umbral=0) GALA10 sigue
        # operando para medir los falsos positivos y falsos negativos.
        production_veto_enabled = bool(
            requerir_modelo
            or float(getattr(self.entrenador, "umbral_meta", 0.0)) > 0
        )
        if decision.hard_veto and production_veto_enabled:
            if decision.prob_meta is not None:
                decision.prob_meta = min(
                    float(decision.prob_meta), hard_veto_probability_cap
                )
            decision.operar = False
            decision.execution_veto_reason = decision.hard_veto_reason
            decision.resumen = (
                "VETO DURO: " + decision.hard_veto_reason
            )
            return decision

        # v3.1.1 mode-aware rollout. PAPER/REAL are independent execution
        # authorities and can coexist per ticker. A regime veto is applied only
        # when the explicit policy for this ticker's effective mode is ENFORCE.
        if (
            bool(getattr(decision, "market_regime_would_block", False))
            and regime_policy == "enforce"
        ):
            decision.operar = False
            decision.execution_veto_reason = str(
                getattr(decision, "market_regime_reason", "REGIME_POLICY")
            )
            decision.resumen = (
                "VETO RÉGIMEN: "
                + str(getattr(decision, "market_regime_reason", "REGIME_POLICY"))
                + f" | regime={getattr(decision, 'market_regime', 'UNKNOWN')}"
            )
            return decision

        if decision.shadow_model_loaded:
            # In PAPER/Shadow the dashboard may surface the candidate score while
            # production veto is disabled. In REAL (requerir_modelo=True), never
            # overwrite the probability that actually made the live decision with
            # a Shadow probability that cannot affect execution.
            if (
                not requerir_modelo
                and (
                    not self.entrenador.modelo_disponible(ticker)
                    or self.entrenador.umbral_meta <= 0
                )
            ):
                decision.prob_meta = decision.shadow_prob
                decision.ai_model_snapshot = shadow.get('model_snapshot', {})
                decision.ai_raw_probability = shadow.get('raw_probability', decision.shadow_prob)
            proposed = "APROBARÍA" if decision.shadow_would_approve else "VETARÍA"
            decision.resumen += (
                f" | SHADOW {proposed} prob={decision.shadow_prob:.2f} "
                f"umbral={decision.shadow_threshold:.2f} (sin bloquear)"
            )
        else:
            decision.resumen += " | IA N/A (modelo no cargado)"
        if decision.hard_veto:
            decision.resumen += (
                f" | REGLA VETO {decision.hard_veto_reason} "
                f"cap={hard_veto_probability_cap:.2f} (sin bloquear)"
            )
        if bool(getattr(decision, "market_regime_enabled", False)):
            regime_name = str(getattr(decision, "market_regime", "UNKNOWN"))
            regime_reason = str(getattr(decision, "market_regime_reason", "") or "")
            if bool(getattr(decision, "market_regime_would_block", False)):
                decision.resumen += (
                    f" | REGIME {regime_name} → VETARÍA ({regime_reason}, "
                    f"exec={effective_mode}, policy={regime_policy})"
                )
            else:
                decision.resumen += f" | REGIME {regime_name} OK"
        return decision

    def _propuesta_market(self, ticker: str, tipo: str, precio: float,
                          atr: float, ts: datetime) -> PropuestaOperacion:
        """Propuesta de ENTRADA A MERCADO (reactiva a Gala 10) con SL/TP por ATR
        cuando no hay un FVG preciso del Agente 2."""
        risk_cfg = self.config.get("risk", {})
        factor_sl = float(risk_cfg.get("stop_loss_factor", 2.0))
        factor_tp = float(risk_cfg.get("take_profit_factor", factor_sl * 2.0))
        # Piso del riesgo ESCALADO AL PRECIO (no un céntimo fijo). Un SL de 0.01
        # sobre una acción de $400 se barre por ruido y, al dividir
        # capital_riesgo/distancia_sl, dispara un tamaño ENORME; además la salida
        # casi garantizada ensucia el CSV de entrenamiento con 'perdedoras' falsas.
        # El piso asegura un stop mínimo sensato proporcional al precio.
        piso_pct = float(self.config.get("risk", {}).get("sl_min_pct_precio", 0.15)) / 100.0
        # Sanear ATR: NaN (x!=x) o negativo → 0. 'float(atr or 0.0)' NO filtra NaN
        # (NaN es truthy) y max(NaN, piso) puede propagar NaN al SL/TP (Schwab lo
        # rechaza o deja la posición sin protección). El piso (>0) sostiene el riesgo.
        atr_val = float(atr or 0.0)
        if atr_val != atr_val or atr_val < 0.0:
            atr_val = 0.0
        piso = max(precio * piso_pct, 0.01)
        riesgo = max(factor_sl * atr_val, piso)
        beneficio = max(factor_tp * atr_val, 0.01)
        ratio = beneficio / riesgo if riesgo > 0 else 0.0
        if tipo == "BUY":
            sl = precio - riesgo
            tp = precio + beneficio
        else:
            sl = precio + riesgo
            tp = precio - beneficio
        return PropuestaOperacion(
            ticker=ticker, tipo=tipo, precio_entrada=round(precio, 2),
            stop_loss=round(sl, 2), take_profit=round(tp, 2), ratio_rb=ratio,
            fvg=None,
            razones=[
                "Entrada a mercado por señal de Gala 10",
                f"SL {factor_sl:.2f}×ATR | TP {factor_tp:.2f}×ATR",
            ],
            timestamp=ts,
        )

    # =========================================================
    # CICLO DE APRENDIZAJE
    # =========================================================

    def al_abrir(self, decision: DecisionFinal, precio_entrada: float,
                 stop_loss: Optional[float] = None,
                 lifecycle: Optional[dict] = None):
        """Register the actual trade and its non-blocking shadow recommendation."""
        try:
            self.feedback.abrir(
                decision, precio_entrada, stop_loss, lifecycle=lifecycle
            )
        except Exception as e:
            print(f"[Agentes] Error registrando apertura: {e}")
        try:
            self.shadow.abrir(decision, precio_entrada, stop_loss)
        except Exception as e:
            print(f"[SHADOW] Error registrando apertura: {e}")

    def al_cerrar(self, ticker: str, precio_salida: float,
                  lifecycle: Optional[dict] = None) -> Optional[dict]:
        """
        Registra el cierre real de una operación y dispara el
        reentrenamiento automático del meta-modelo si toca.
        """
        fila = None
        try:
            fila = self.feedback.cerrar(
                ticker, precio_salida, lifecycle=lifecycle
            )
        except Exception as e:
            print(f"[Agentes] Error registrando cierre: {e}")
        # Shadow observation cleanup is independent from training eligibility.
        # A restart-adopted or incomplete execution may be excluded from model
        # training, but it must not leave a stale open Shadow decision in memory.
        try:
            self.shadow.cerrar(ticker, precio_salida)
        except Exception as e:
            print(f"[SHADOW] Error registrando cierre: {e}")
        if fila is None:
            print(
                f"[Agentes] {ticker}: cierre sin apertura de feedback en memoria; "
                "no se crea una observación incompleta"
            )
            return None
        if not int(fila.get("execution_valid", 0) or 0):
            print(
                f"[Agentes] {ticker}: ejecución excluida del entrenamiento "
                f"({fila.get('exclusion_reason', 'integridad incompleta')})"
            )
            return fila
        try:
            # PAPER y REAL comparten el MISMO contador válido por ticker. El modo
            # queda persistido en la fila para auditoría, pero nunca crea/reseteará
            # un ciclo x/50 separado.
            total_ticker = self.feedback.total_cerradas_ticker(ticker)
            modo = str(fila.get("modo", "") or "").upper() or "UNKNOWN"
            print(
                f"[Agentes] {ticker}: observación {modo} válida registrada | "
                f"cerradas válidas acumuladas={total_ticker}"
            )
            # Reentrena el modelo INDIVIDUAL de este ticker (no un modelo global).
            self.entrenador.quizas_reentrenar_ticker(ticker, total_ticker)
        except Exception as e:
            print(f"[Agentes] Error en reentrenamiento: {e}")
        return fila

    def al_fin_sesion(self, tickers) -> bool:
        """Queue only tickers that reached the 50-observation threshold."""
        try:
            return self.entrenador.solicitar_reentreno_todos(
                tickers, solo_elegibles=True
            )
        except Exception as exc:
            print(f"[Agentes] Reentrenamiento fin de sesión: {exc}")
            return False
