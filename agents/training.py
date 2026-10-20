"""
=============================================================
MÓDULO: agents/training.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Meta-modelo de VETO — reorientado al enfoque acordado con el cliente:

      Gala 10 = disparador único (core/strategy.py).
      XGBoost INDIVIDUAL POR TICKER = filtro de veto de cada señal de Gala 10.

    - UN modelo XGBoost por acción (TSLA, AMD, ...), entrenado ÚNICAMENTE con
      el histórico de ESA acción → captura la personalidad de cada activo.
    - Features: el análisis SMC (dirección, FVG, ATR, rango, hueco) MÁS GSCO
      como feature OPCIONAL. XGBoost maneja de forma NATIVA los valores
      faltantes (NaN), así que si un día no hay dato de GSCO el modelo decide
      con el resto de features y el bot NO se detiene.
    - No se escala (StandardScaler): los árboles no lo necesitan y el escalado
      rompería el manejo nativo de NaN. Se pasan las features crudas a XGBoost.
    - Reentrenamiento continuo por ticker con los resultados REALES.

    Mientras un ticker no tenga modelo entrenado (arranque en frío), su veto
    deja pasar TODAS las señales de Gala 10 para poder recolectar datos, pero
    devuelve probabilidad ``None``. El dashboard la representa como ``N/A`` en
    lugar de inventar un 50% que pudiera confundirse con una inferencia real.
=============================================================
"""

import os
import pickle
import shutil
import threading
import json
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from agents.contracts import DecisionFinal
from agents.feedback_store import construir_features
from agents.gala10_features import GALA10_FEATURE_NAMES
from agents.learning_dataset import LEARNING_POLICY_VERSION, eligible_closed_trades
from agents.learning_economics import (
    net_trade_returns, economic_metrics, economic_rejection_reasons,
)
from agents.retraining_lifecycle import RetrainingLifecycle
from agents.volume_features import VOLUME_FEATURE_NAMES
from core.project_paths import resolve_runtime_path


# AI v4: only causal, non-constant model inputs.  GSCO has no aligned
# historical coverage, ``ratio_rb`` is fixed by execution policy, and ``hora``
# duplicates the causal ``gala_session_minute`` feature.
FEATURES = [
    "tipo_num", "confianza_macro", "atr_relativo",
    "fvg_tam_rel", "hueco_apertura", "confluencia", "es_rango_lateral",
    *VOLUME_FEATURE_NAMES,
    *GALA10_FEATURE_NAMES,
]

TRAINING_POLICY_VERSION = 4
FEATURE_VERSION = 4


def _only_execution_valid(df: pd.DataFrame) -> pd.DataFrame:
    """v2.9 trains only rows linked to confirmed broker lifecycle records.

    Test fixtures and pre-v2.9 external datasets without the column retain the
    legacy behavior.  The v2.9 migration adds the column and quarantines every
    unlinked historical row with value 0.
    """

    return eligible_closed_trades(df, allow_legacy=True)


class EntrenadorAgentes:
    """
    Entrena y sirve UN modelo XGBoost POR TICKER (veto de las señales de
    Gala 10). Thread-safe para el reentrenamiento en caliente mientras el
    motor sigue operando varios tickers en paralelo.
    """

    def __init__(self, config: dict):
        from core.model_identity import ManualRealAuthorization
        self.manual_real_authorization = ManualRealAuthorization(
            (config.get('live_trading', {}) or {}).get('manual_authorization', {}) or {})
        self._production_snapshots = {}
        self._shadow_snapshots = {}
        agents_cfg = config.get("agents", {})
        cfg = agents_cfg.get("entrenamiento", {})
        self.reportes_dir = resolve_runtime_path(cfg.get('reportes_dir', 'data/reportes'))
        shadow_cfg = agents_cfg.get("shadow_mode", {})
        self.activo: bool = cfg.get("activo", True)
        self.shadow_enabled: bool = bool(shadow_cfg.get("enabled", False))
        self.shadow_models_dir: str = resolve_runtime_path(shadow_cfg.get(
            "modelos_dir", "data/modelos_shadow"))
        self.shadow_tickers = {
            str(t).upper() for t in shadow_cfg.get("tickers", []) if str(t).strip()
        }
        self.shadow_default_threshold: float = float(
            shadow_cfg.get("default_threshold", 0.50))
        self.shadow_save_all_candidates: bool = bool(
            shadow_cfg.get("save_all_candidates", True))
        self.shadow_replace_requires_improvement: bool = bool(
            shadow_cfg.get("replace_requires_improvement", False))
        self.shadow_min_balanced_accuracy: float = float(
            shadow_cfg.get("min_balanced_accuracy", 0.0))
        self.shadow_min_improvement: float = float(
            shadow_cfg.get("min_improvement_pp", 0.0)) / 100.0
        # Carpeta para los modelos por ticker (uno por acción).
        self.modelos_dir: str = resolve_runtime_path(cfg.get("modelos_dir", "data/modelos"))
        self.feedback_path: str = resolve_runtime_path(cfg.get("feedback_path", "data/agentes_feedback.csv"))
        # v3.3.7: supplemental signal familiarization.  x/50 and all
        # validation/test metrics remain based on execution-valid CLOSED trades.
        # Resolved non-executed signals may augment FIT only with a lower weight.
        learning_cfg = agents_cfg.get("signal_learning", {}) or {}
        self.signal_learning_enabled: bool = bool(learning_cfg.get("enabled", True))
        self.signal_learning_training_enabled: bool = bool(
            learning_cfg.get("use_for_training", True)
        )
        self.signal_learning_path: str = resolve_runtime_path(
            learning_cfg.get("path", "data/signal_learning_observations.csv")
        )
        self.signal_learning_weight: float = min(1.0, max(0.0, float(
            learning_cfg.get("counterfactual_weight", 0.35)
        )))
        self.signal_learning_max_ratio: float = max(0.0, float(
            learning_cfg.get("max_counterfactual_to_actual_fit_ratio", 1.0)
        ))
        auto_cfg = cfg.get("automatic_retraining", {}) or {}
        self.reentrenar_cada_n: int = int(cfg.get("reentrenar_cada_n", 50))
        self.auto_retrain_enabled: bool = bool(
            auto_cfg.get("enabled", self.activo))
        self.auto_retrain_only_after_session: bool = bool(
            auto_cfg.get("only_after_session", False))
        self.auto_activate_production: bool = bool(
            auto_cfg.get("auto_activate_production", False))
        self.retrain_warning_samples: int = int(
            auto_cfg.get("warning_samples", 30))
        self.retrain_trigger_samples: int = int(
            auto_cfg.get("retrain_samples", self.reentrenar_cada_n))
        self.retrain_state_path: str = resolve_runtime_path(
            auto_cfg.get("state_path", "data/retraining_state.json"))
        self.min_muestras: int = cfg.get("min_muestras", 40)
        self.umbral_meta: float = cfg.get("umbral_meta_filtro", 0.50)
        self.min_test_muestras: int = int(cfg.get("min_test_muestras", 20))
        self.min_validation_muestras: int = int(
            cfg.get("min_validation_muestras", self.min_test_muestras))
        # The product contract says 50 new eligible trades trigger the first
        # post-session retraining.  The older 20/20 holdouts required at least
        # 80 rows and therefore consumed the 50-row batch without training.
        # Automatic runs use an explicit 30/10/10-capable split; manual/full
        # historical training retains the stricter configured holdouts above.
        self.auto_min_fit_samples: int = int(
            auto_cfg.get("min_fit_samples", 25))
        self.auto_min_validation_samples: int = int(
            auto_cfg.get("min_validation_samples", 10))
        self.auto_min_test_samples: int = int(
            auto_cfg.get("min_test_samples", 10))
        # Accuracy simple NO es una puerta válida con clases desbalanceadas:
        # en el primer reporte un modelo que vetaba casi todo obtuvo 65.9%,
        # aunque "vetar siempre" lograba 67.2%. La política v3 exige separar
        # ganadoras/perdedoras y conservar una fracción útil de señales.
        self.min_balanced_accuracy: float = float(
            cfg.get("min_balanced_accuracy_activacion", 0.55))
        self.min_recall_ganadoras: float = float(
            cfg.get("min_recall_ganadoras", 0.20))
        self.min_precision_lift: float = float(
            cfg.get("min_precision_lift_pct", 5.0)) / 100.0
        self.min_approval_rate: float = float(
            cfg.get("min_approval_rate_pct", 10.0)) / 100.0
        self.max_approval_rate: float = float(
            cfg.get("max_approval_rate_pct", 80.0)) / 100.0
        self.min_perdedoras_filtradas: float = float(
            cfg.get("min_perdedoras_filtradas_pct", 55.0)) / 100.0
        self.max_balanced_accuracy_drop: float = float(
            cfg.get("max_balanced_accuracy_drop", 0.02))
        economics = cfg.get("economic_validation", {}) or {}
        self.round_trip_cost_bps = max(0.0, float(economics.get("round_trip_cost_bps", 2.0)))
        self.economic_min_trades = max(1, int(economics.get("min_accepted_test_trades", 10)))
        self.economic_min_expectancy_r = max(0.0, float(economics.get("min_expectancy_r", 0.0)))
        self.economic_min_profit_factor = max(1.0, float(economics.get("min_profit_factor", 1.10)))
        self.economic_max_drawdown_r = max(0.0, float(economics.get("max_drawdown_r", 6.0)))
        self.economic_min_net_r_lift = max(0.0, float(economics.get("min_net_r_lift", 0.0)))

        self._lock = threading.Lock()
        self._status_feedback_signature = None
        self._status_feedback_rows = pd.DataFrame()
        # Only one dashboard request may parse/prepare the full feedback CSV
        # after it changes. Other requests wait for the refreshed cache.
        self._status_feedback_load_lock = threading.Lock()
        self._modelos: Dict[str, object] = {}     # approved production models
        self.model_load_errors: Dict[str, str] = {}
        self._umbrales: Dict[str, float] = {}
        self._modelos_shadow: Dict[str, object] = {}
        self._umbrales_shadow: Dict[str, float] = {}
        self._shadow_sources: Dict[str, str] = {}
        self._ultimo_reentreno: Dict[str, int] = {}   # ticker -> nº filas al último entreno
        self._entrenando: set = set()             # tickers con entreno en curso
        self._cargar_si_existe()
        self._cargar_shadow_si_existe()
        self.retraining = RetrainingLifecycle(
            feedback_path=self.feedback_path,
            state_path=self.retrain_state_path,
            shadow_models_dir=self.shadow_models_dir,
            warning_samples=self.retrain_warning_samples,
            retrain_samples=self.retrain_trigger_samples,
        )
        # Loading a trainer for status/model verification must not rewrite
        # lifecycle state owned by the active training process.
        self.retraining.refresh(self.shadow_tickers, persist=False)

    # =========================================================
    # INFERENCIA (filtro de veto por ticker)
    # =========================================================

    def evaluar(
        self,
        decision: DecisionFinal,
        requerir_modelo: bool = False,
    ) -> Tuple[Optional[float], bool]:
        """
        Devuelve (probabilidad_exito, aprueba) para la señal de Gala 10 del
        ticker de la decisión, usando el modelo INDIVIDUAL de ese ticker. En
        arranque en frío (ese ticker aún sin modelo) devuelve (None, True) para
        no bloquear la recolección de datos ni la operativa y para que la UI
        muestre ``N/A`` de forma transparente.
        """
        ticker = getattr(decision, "ticker", "") or ""
        with self._lock:
            modelo = self._modelos.get(ticker)
            umbral = self._umbrales.get(ticker, self.umbral_meta)
            decision.ai_model_snapshot = dict(getattr(self, '_production_snapshots', {}).get(ticker, {}))

        if modelo is None:
            # Sin artefacto aprobado no hay inferencia válida. En PAPER se deja
            # pasar para seguir recolectando ejemplos; en REAL se falla cerrado.
            return (None, False) if requerir_modelo else (None, True)

        if not self.activo and not requerir_modelo:
            # ``entrenamiento.activo`` controla el filtro de producción normal y
            # la activación de modelos, pero NO debe inutilizar un modelo aprobado
            # cuando el motor entra deliberadamente en modo REAL. En paper/shadow
            # se conserva el comportamiento histórico: medir sin bloquear.
            return (None, True)

        feats = construir_features(decision)
        try:
            # NaN explícito para features ausentes (p. ej. GSCO sin dato hoy):
            # XGBoost aprende una dirección por defecto para el faltante.
            x = np.array([[feats.get(f, np.nan) for f in FEATURES]], dtype=float)
            prob = float(modelo.predict_proba(x)[0][1])
            if not np.isfinite(prob) or not 0 <= prob <= 1:
                raise ValueError('Invalid model probability')
            decision.ai_raw_probability = prob
        except Exception:
            decision.ai_inference_error = True
            return (None, False) if requerir_modelo else (None, True)

        # En PAPER/Shadow, umbral_meta <= 0 sigue siendo el interruptor global
        # que permite observar sin vetar. En REAL con ``requerir_modelo=True`` el
        # modelo aprobado SIEMPRE aplica su umbral por ticker, aunque el
        # entrenamiento/auto-activación permanezcan deshabilitados.
        production_filter_enabled = bool(requerir_modelo or self.umbral_meta > 0)
        aprueba = prob >= umbral if production_filter_enabled else True
        authorization = getattr(self, 'manual_real_authorization', None)
        if requerir_modelo and not aprueba and authorization:
            try:
                row = authorization.approve_below_threshold(ticker,
                    decision.ai_model_snapshot.get('model_version'), prob, umbral)
                if row:
                    aprueba = True
                    decision.ai_manual_authorization = row
            except Exception:
                aprueba = False
        return round(prob, 3), aprueba

    def evaluar_shadow(self, decision: DecisionFinal) -> dict:
        """Evaluate a candidate model without ever affecting execution."""
        ticker = str(getattr(decision, "ticker", "") or "").upper()
        result = {
            "enabled": self.shadow_enabled,
            "model_loaded": False,
            "probability": None,
            "threshold": None,
            "would_approve": None,
            "source": "",
        }
        if not self.shadow_enabled or not ticker:
            return result
        if self.shadow_tickers and ticker not in self.shadow_tickers:
            return result
        with self._lock:
            model = self._modelos_shadow.get(ticker)
            snapshot = dict(getattr(self, '_shadow_snapshots', {}).get(ticker, {}))
            threshold = self._umbrales_shadow.get(
                ticker, self.shadow_default_threshold)
            source = self._shadow_sources.get(ticker, "")
        if model is None:
            return result
        result["threshold"] = float(threshold)
        result["source"] = source
        feats = construir_features(decision)
        try:
            x = np.array([[feats.get(f, np.nan) for f in FEATURES]], dtype=float)
            probability = float(model.predict_proba(x)[0][1])
        except Exception:
            return result
        result.update({
            "model_loaded": True,
            "raw_probability": probability,
            "model_snapshot": snapshot,
            "probability": round(probability, 6),
            "would_approve": bool(probability >= threshold),
        })
        return result

    def modelo_disponible(self, ticker: str) -> bool:
        """True only when an approved artifact is loaded for this ticker."""
        with self._lock:
            return bool(ticker and ticker in self._modelos)

    def production_snapshot(self, ticker):
        with self._lock:
            return dict(getattr(self, '_production_snapshots', {}).get(ticker, {}))

    def shadow_snapshot(self, ticker):
        """Return the currently installed SHADOW artifact identity/acceptance snapshot.

        This score belongs to the historical evaluation window in which the
        current SHADOW model was accepted.  It is intentionally separate from
        ``last_shadow_incumbent_balanced_accuracy``, which re-evaluates that same
        artifact on the *latest candidate's* test window.  Keeping both values
        prevents a rejected retraining run from looking as if it downgraded the
        already accepted model.
        """
        with self._lock:
            return dict(getattr(self, '_shadow_snapshots', {}).get(ticker, {}))

    def modelos_faltantes(self, tickers) -> list:
        with self._lock:
            return [ticker for ticker in (tickers or []) if ticker not in self._modelos]

    # =========================================================
    # ENTRENAMIENTO (por ticker)
    # =========================================================

    def quizas_reentrenar_ticker(self, ticker: str, total_cerradas_ticker: int) -> Optional[dict]:
        """Update the per-ticker counter and schedule only when policy permits.

        In v2.1 the default deployment is end-of-session only.  Legacy configs
        without ``automatic_retraining.only_after_session`` retain the former
        background behavior for backwards compatibility.
        """
        ticker = str(ticker or "").upper()
        if not ticker:
            return None
        progress = self.retraining.register_closed(ticker, total_cerradas_ticker)
        if not self.auto_retrain_enabled:
            return progress
        if self.auto_retrain_only_after_session:
            return progress
        if int(progress.get("new_observations", 0)) < self.retrain_trigger_samples:
            return progress
        return self._schedule_retrain_ticker(ticker, total_cerradas_ticker)

    def _schedule_retrain_ticker(
        self,
        ticker: str,
        total_count: Optional[int] = None,
    ) -> Optional[dict]:
        with self._lock:
            if ticker in self._entrenando:
                return None
            self._entrenando.add(ticker)
        hilo = threading.Thread(
            target=self._reentrenar_background,
            args=(ticker, total_count),
            name=f"retrain-{ticker}",
            daemon=True,
        )
        hilo.start()
        return {"ticker": ticker, "programado": True}

    def _reentrenar_background(
        self,
        ticker: str,
        total_count: Optional[int] = None,
    ):
        
        metricas = None
        
        try:
            self.retraining.mark_training(ticker, total_count)

            metricas = self.entrenar_ticker(
                ticker,
                allow_production_activation=self.auto_activate_production,
                automatic=True,
            )
            accepted = bool(metricas and metricas.get("shadow_candidate_accepted"))
            reason = (
                str(metricas.get("shadow_candidate_reason", ""))
                if metricas else "muestras/clases insuficientes"
            )
            self.retraining.mark_result(
                ticker,
                total_count=total_count,
                accepted=accepted,
                reason=reason,
                metrics=metricas or {},
                consume_batch=bool(metricas),
                deferred=metricas is None,
            )
        except Exception as exc:
            print(f"[Agentes] Reentrenamiento {ticker} falló: {exc}")
            self.retraining.mark_result(
                ticker,
                total_count=total_count,
                accepted=False,
                error=str(exc),
                metrics=metricas or {},
                consume_batch=False,
            )
        finally:
            with self._lock:
                self._entrenando.discard(ticker)

    def _chronological_split_sizes(
        self, total_rows: int, *, automatic: bool
    ) -> Optional[Tuple[int, int, int]]:
        """Return FIT/validation/test sizes without contradicting the 50 trigger.

        Manual historical runs retain their stricter configured 20/20 holdouts.
        Automatic runs use the dedicated policy values so a balanced 50-row
        eligible batch can actually produce a 30/10/10 evaluation.
        """

        total_rows = max(0, int(total_rows))
        if automatic:
            min_fit = max(1, int(self.auto_min_fit_samples))
            min_validation = max(1, int(self.auto_min_validation_samples))
            min_test = max(1, int(self.auto_min_test_samples))
        else:
            min_fit = max(40, int(self.min_muestras))
            min_validation = max(1, int(self.min_validation_muestras))
            min_test = max(1, int(self.min_test_muestras))
        n_test = max(min_test, int(round(total_rows * 0.20)))
        n_validation = max(
            min_validation, int(round(total_rows * 0.20))
        )
        n_fit = total_rows - n_validation - n_test
        if n_fit < min_fit:
            return None
        return n_fit, n_validation, n_test

    @staticmethod
    def _timestamp_to_utc(value):
        """Interpret legacy naive feedback timestamps as New York time."""
        try:
            ts = pd.Timestamp(value)
            if pd.isna(ts):
                return pd.NaT
            if ts.tzinfo is None:
                ts = ts.tz_localize("America/New_York", ambiguous="NaT", nonexistent="shift_forward")
            return ts.tz_convert("UTC")
        except Exception:
            return pd.NaT

    def _supplemental_fit_rows(
        self, ticker: str, *, fit_cutoff, actual_fit_count: int
    ) -> pd.DataFrame:
        """Load clean non-executed TP/SL outcomes for FIT only.

        Counterfactual rows never enter validation/test, never advance x/50 and
        are capped/weighted so they cannot dominate verified execution evidence.
        The outcome must have been resolved before the actual FIT cutoff to avoid
        leaking future label information into the candidate.
        """
        empty = pd.DataFrame(columns=[*FEATURES, "ganadora", "timestamp", "resolved_utc"])
        if (
            not self.signal_learning_enabled
            or not self.signal_learning_training_enabled
            or self.signal_learning_weight <= 0
            or self.signal_learning_max_ratio <= 0
            or not os.path.exists(self.signal_learning_path)
        ):
            return empty
        try:
            sup = pd.read_csv(self.signal_learning_path)
        except Exception:
            return empty
        cutoff_utc = self._timestamp_to_utc(fit_cutoff)
        if pd.isna(cutoff_utc):
            return empty
        required = {"ticker", "event", "training_eligible", "ganadora", "resolution", "resolved_utc"}
        if not required.issubset(sup.columns):
            return empty
        sup = sup[
            (sup["ticker"].astype(str).str.upper() == str(ticker).upper())
            & (sup["event"].astype(str).str.upper() == "RESOLVED")
            & (pd.to_numeric(sup["training_eligible"], errors="coerce").fillna(0) == 1)
            & (sup["resolution"].astype(str).str.upper().isin(["TP", "SL"]))
        ].copy()
        if sup.empty:
            return empty
        if "observation_id" in sup.columns:
            sup = sup.drop_duplicates(subset=["observation_id"], keep="last")
        for col in FEATURES:
            if col not in sup.columns:
                sup[col] = np.nan
        for col in FEATURES + ["ganadora"]:
            sup[col] = pd.to_numeric(sup[col], errors="coerce")
        sup[FEATURES] = sup[FEATURES].replace([np.inf, -np.inf], np.nan)
        sup = sup[sup["ganadora"].isin([0, 1])]
        sup = sup.dropna(subset=[
            "ganadora", "volumen_relativo_20", "liquidez_dolar_log",
            *GALA10_FEATURE_NAMES,
        ])
        if sup.empty:
            return empty
        sup["_resolved_utc"] = pd.to_datetime(
            sup["resolved_utc"], errors="coerce", utc=True
        )
        sup = sup[sup["_resolved_utc"].notna() & (sup["_resolved_utc"] <= cutoff_utc)]
        if sup.empty:
            return empty
        # Re-label small gross winners as losses when the configured fee
        # reserve consumes their entire return. No synthetic row reaches TEST.
        sup_returns = net_trade_returns(sup, self.round_trip_cost_bps)
        known = np.isfinite(sup_returns)
        sup.loc[known, "ganadora"] = (sup_returns[known] > 0).astype(int)
        sup = sup.sort_values("_resolved_utc", kind="stable")
        cap = max(0, int(round(max(0, actual_fit_count) * self.signal_learning_max_ratio)))
        if cap <= 0:
            return empty
        if len(sup) > cap:
            sup = sup.tail(cap)
        return sup

    def _prepare_training_rows(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Apply the same identity/verification policy as the closed counter."""
        df = _only_execution_valid(frame).copy()
        if not {"ticker", "tipo", "ganadora", "timestamp"}.issubset(df.columns):
            return df.iloc[:0]
        df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
        df["tipo"] = df["tipo"].astype(str).str.upper()
        df = df[df["tipo"].isin(["BUY", "SELL"])].copy()
        df["_timestamp"] = pd.to_datetime(
            df["timestamp"].map(self._timestamp_to_utc), errors="coerce", utc=True
        )
        df["_closed_utc"] = pd.to_datetime(
            df.get("closed_utc", pd.Series(pd.NaT, index=df.index)),
            errors="coerce", utc=True,
        )
        df = df[df["_timestamp"].notna() & (
            df["_closed_utc"].isna() | (df["_closed_utc"] >= df["_timestamp"])
        )].copy()
        df["tipo_num"] = (df["tipo"] == "BUY").astype(float)
        for col in FEATURES + ["ganadora"]:
            df[col] = pd.to_numeric(df.get(col, np.nan), errors="coerce")
        df[FEATURES] = df[FEATURES].replace([np.inf, -np.inf], np.nan)
        df = df[df["ganadora"].isin([0, 1])].dropna(subset=[
            "volumen_relativo_20", "liquidez_dolar_log", *GALA10_FEATURE_NAMES,
        ]).copy()
        df["_net_r"] = net_trade_returns(df, self.round_trip_cost_bps)
        known = np.isfinite(df["_net_r"])
        df.loc[known, "ganadora"] = (df.loc[known, "_net_r"] > 0).astype(int)
        return df.sort_values("_timestamp", kind="stable").reset_index(drop=True)

    def _causal_blocks(self, df: pd.DataFrame, *, automatic: bool):
        sizes = self._chronological_split_sizes(len(df), automatic=automatic)
        if sizes is None:
            return None
        n_fit, n_val, _ = sizes
        fit, val, test = (df.iloc[:n_fit].copy(), df.iloc[n_fit:n_fit+n_val].copy(),
                          df.iloc[n_fit+n_val:].copy())
        validation_start, test_start = val["_timestamp"].min(), test["_timestamp"].min()
        original_fit, original_val = len(fit), len(val)
        # Never train on a known outcome that arrived after a holdout decision.
        # Legacy missing close times may build SHADOW evidence, but cannot pass
        # the temporal production gate until genuine timestamps are available.
        fit = fit[(fit["_timestamp"] < validation_start) & (
            fit["_closed_utc"].isna() | (fit["_closed_utc"] < validation_start))]
        val = val[(val["_timestamp"] < test_start) & (
            val["_closed_utc"].isna() | (val["_closed_utc"] < test_start))]
        min_fit = max(1, self.auto_min_fit_samples) if automatic else max(40, self.min_muestras)
        min_val = max(1, self.auto_min_validation_samples if automatic else self.min_validation_muestras)
        if len(fit) < min_fit or len(val) < min_val:
            return None
        temporal = {
            "temporal_integrity_passed": bool(df["_closed_utc"].notna().all()),
            "unknown_close_timestamps": int(df["_closed_utc"].isna().sum()),
            "purged_fit_rows": original_fit - len(fit),
            "purged_validation_rows": original_val - len(val),
        }
        return fit, val, test, validation_start - pd.Timedelta(nanoseconds=1), temporal

    def entrenar_ticker(
        self,
        ticker: str,
        *,
        allow_production_activation: bool = True,
        automatic: bool = False,
    ) -> Optional[dict]:
        """
        Entrena el modelo XGBoost de UN ticker con SOLO sus filas del CSV de
        feedback y lo activa en caliente. Devuelve métricas o None.
        """
        if not ticker or not os.path.exists(self.feedback_path):
            return None
        try:
            df = pd.read_csv(self.feedback_path)
        except Exception:
            return None
        df = self._prepare_training_rows(df)
        if "ticker" not in df.columns:
            return None
        ticker = str(ticker).strip().upper()
        df = df[df["ticker"] == ticker].copy()
        if len(df) < self.min_muestras:
            return None

        try:
            from xgboost import XGBClassifier
        except ImportError:
            print("[Agentes] Instala xgboost y scikit-learn para el meta-modelo por ticker")
            return None

        y = df["ganadora"].astype(int).values
        if len(np.unique(y)) < 2:
            return None   # aún no hay ganadoras Y perdedoras para este ticker

        # Se conservan NaN opcionales: XGBoost aprende su rama por defecto.
        # Política v3: FIT (60%) -> VALIDACIÓN (20%, selecciona umbral) ->
        # TEST (20%, auditoría intocable). Elegir el umbral sobre el mismo test
        # que reportamos inflaría las métricas y sería data leakage.
        blocks = self._causal_blocks(df, automatic=automatic)
        if blocks is None:
            return None
        fit_rows, val_rows, test_rows, fit_cutoff, temporal = blocks
        x_fit_actual, x_val, x_te = (
            block[FEATURES].values for block in (fit_rows, val_rows, test_rows)
        )
        y_fit_actual, y_val, y_te = (
            block["ganadora"].astype(int).values for block in (fit_rows, val_rows, test_rows)
        )
        if any(len(np.unique(block)) < 2 for block in (y_fit_actual, y_val, y_te)):
            return None

        # v3.3.7: supplement FIT with lower-trust resolved non-executed signals.
        # Validation/test remain 100% actual execution-valid trades.
        supplemental = self._supplemental_fit_rows(
            ticker, fit_cutoff=fit_cutoff, actual_fit_count=len(y_fit_actual)
        )
        supplemental_count = int(len(supplemental))
        if supplemental_count:
            x_sup = supplemental[FEATURES].values.astype(float)
            y_sup = supplemental["ganadora"].astype(int).values
            x_fit = np.vstack([x_fit_actual, x_sup])
            y_fit = np.concatenate([y_fit_actual, y_sup])
            fit_weights = np.concatenate([
                np.ones(len(y_fit_actual), dtype=float),
                np.full(supplemental_count, self.signal_learning_weight, dtype=float),
            ])
        else:
            x_fit = x_fit_actual
            y_fit = y_fit_actual
            fit_weights = np.ones(len(y_fit_actual), dtype=float)

        # v3.2: every percentage shown in the dashboard belongs to one immutable
        # chronological evaluation snapshot.  Persist the exact run id and test
        # window so values from different retraining dates are never presented as
        # if they were directly comparable.
        evaluation_id = uuid.uuid4().hex
        evaluation_utc = datetime.now(timezone.utc).isoformat()
        test_window_start = ""
        test_window_end = ""
        if "_timestamp" in df.columns and len(df) >= 1:
            try:
                first_ts = test_rows.iloc[0]["_timestamp"]
                last_ts = test_rows.iloc[-1]["_timestamp"]
                if pd.notna(first_ts):
                    test_window_start = first_ts.isoformat()
                if pd.notna(last_ts):
                    test_window_end = last_ts.isoformat()
            except Exception:
                test_window_start = ""
                test_window_end = ""

        # Compensar el desbalance solo con el bloque FIT. La primera versión
        # aprendió a predecir casi todo como perdedora porque había ~2x más
        # pérdidas; scale_pos_weight obliga al árbol a aprender ambas clases.
        # Class balance is anchored to verified actual FIT rows, so a burst of
        # counterfactuals cannot redefine the production class prior.
        positivas_fit = int((y_fit_actual == 1).sum())
        negativas_fit = int((y_fit_actual == 0).sum())
        scale_pos_weight = (
            float(negativas_fit / positivas_fit) if positivas_fit else 1.0
        )
        modelo_eval = self._crear_modelo(
            XGBClassifier, scale_pos_weight=scale_pos_weight)
        modelo_eval.fit(x_fit, y_fit, sample_weight=fit_weights, verbose=False)

        prob_val = modelo_eval.predict_proba(x_val)[:, 1]
        umbral, metricas_val = self._seleccionar_umbral(
            y_val, prob_val, net_returns=val_rows["_net_r"].values)
        prob_te = modelo_eval.predict_proba(x_te)[:, 1]
        eval_test = self._metricas_probabilidades(y_te, prob_te, umbral)
        economic_test = economic_metrics(
            test_rows["_net_r"].values, prob_te, umbral, self.round_trip_cost_bps)
        economic_validation = economic_metrics(
            val_rows["_net_r"].values, prob_val, umbral, self.round_trip_cost_bps)
        economic_reasons = economic_rejection_reasons(
            economic_test, min_trades=self.economic_min_trades,
            min_expectancy_r=self.economic_min_expectancy_r,
            min_profit_factor=self.economic_min_profit_factor,
            max_drawdown_r=self.economic_max_drawdown_r,
            min_net_r_lift=self.economic_min_net_r_lift)
        cost_label_integrity = all(
            np.isfinite(block["_net_r"].values).all()
            for block in (fit_rows, val_rows, test_rows)
        )
        if not cost_label_integrity:
            economic_reasons.append("FIT/validación/TEST contienen cierres sin riesgo/costes verificables")
        acc = eval_test["accuracy"]
        precision = eval_test["precision"]
        recall = eval_test["recall"]
        f1 = eval_test["f1"]
        tn, fp, fn, tp = (
            eval_test["tn"], eval_test["fp"],
            eval_test["fn"], eval_test["tp"],
        )
        perdedoras_filtradas = eval_test["specificity"]
        ganadoras_vetadas = 1.0 - recall

        with self._lock:
            incumbent = self._modelos.get(ticker)
            incumbent_umbral = self._umbrales.get(ticker, self.umbral_meta)
            shadow_incumbent = self._modelos_shadow.get(ticker)
            shadow_incumbent_umbral = self._umbrales_shadow.get(
                ticker, self.shadow_default_threshold)
        incumbent_balanced = None
        incumbent_economics = None
        if incumbent is not None:
            try:
                prob_inc = incumbent.predict_proba(x_te)[:, 1]
                incumbent_balanced = self._metricas_probabilidades(
                    y_te, prob_inc, incumbent_umbral)["balanced_accuracy"]
                incumbent_economics = economic_metrics(
                    test_rows["_net_r"].values, prob_inc, incumbent_umbral,
                    self.round_trip_cost_bps)
            except Exception:
                incumbent_balanced = None
        shadow_incumbent_balanced = None
        if shadow_incumbent is not None:
            try:
                prob_shadow_inc = shadow_incumbent.predict_proba(x_te)[:, 1]
                shadow_incumbent_balanced = self._metricas_probabilidades(
                    y_te, prob_shadow_inc, shadow_incumbent_umbral
                )["balanced_accuracy"]
            except Exception:
                shadow_incumbent_balanced = None

        razones = self._razones_rechazo(eval_test)
        razones.extend(economic_reasons)
        if not temporal["temporal_integrity_passed"]:
            razones.append("Cierres históricos sin closed_utc: causalidad no verificable")
        if incumbent_balanced is not None and eval_test["balanced_accuracy"] < (
                incumbent_balanced - self.max_balanced_accuracy_drop):
            razones.append(
                "balanced_accuracy "
                f"{eval_test['balanced_accuracy']:.3f} degrada incumbente "
                f"{incumbent_balanced:.3f}"
            )
        if incumbent_economics and incumbent_economics["evaluated"] and (
                economic_test["net_r"] < incumbent_economics["net_r"]):
            razones.append("R neto TEST degrada modelo de producción incumbente")
        # Keep the exact production-gate reasons from THIS evaluation.  Older
        # builds exposed only a boolean, which made a SHADOW-accepted candidate
        # look contradictory when REAL promotion was still correctly blocked by
        # another production rule (recall, precision lift, approval rate, etc.).
        production_gate_reasons = list(razones)
        production_gate_passed = not production_gate_reasons
        activar = bool(production_gate_passed and allow_production_activation)
        if activar:
            motivo_activacion = "política v3 cumplida"
        elif production_gate_passed and not allow_production_activation:
            motivo_activacion = "activación automática de producción deshabilitada"
        else:
            motivo_activacion = "; ".join(razones)

        metricas = {
            "ticker": ticker,
            "evaluation_id": evaluation_id,
            "evaluation_model_policy": "causal_fit_only_cost_aware_v2",
            "model_version": evaluation_id,
            "evaluation_utc": evaluation_utc,
            "evaluation_test_start": test_window_start,
            "evaluation_test_end": test_window_end,
            "muestras": int(len(df)),
            "muestras_fit": int(len(y_fit_actual)),
            "muestras_fit_reales": int(len(y_fit_actual)),
            "muestras_fit_suplementarias": int(supplemental_count),
            "muestras_fit_total": int(len(y_fit)),
            "muestras_validation": int(len(y_val)),
            "muestras_train": int(len(y_fit_actual) + len(y_val)),
            "signal_learning_enabled": bool(self.signal_learning_training_enabled),
            "signal_learning_weight": round(float(self.signal_learning_weight), 4),
            "signal_learning_max_ratio": round(float(self.signal_learning_max_ratio), 4),
            "validation_test_actual_only": True,
            "economic_validation": economic_validation,
            "economic_production_incumbent": incumbent_economics,
            "economic_test": economic_test,
            "economic_gate_passed": not economic_reasons,
            "economic_reasons": list(economic_reasons),
            "cost_label_integrity_passed": bool(cost_label_integrity),
            "target_policy": "net_R_when_cost_evidence_available_legacy_gross_shadow_only",
            **temporal,
            "learning_policy_version": LEARNING_POLICY_VERSION,
            "muestras_test": int(len(y_te)),
            "accuracy": round(acc * 100, 2),
            "baseline_accuracy": round(eval_test["baseline_accuracy"] * 100, 2),
            "accuracy_lift_pp": round(
                (acc - eval_test["baseline_accuracy"]) * 100, 2),
            "balanced_accuracy": round(
                eval_test["balanced_accuracy"] * 100, 2),
            "precision": round(precision * 100, 2),
            "base_win_rate_pct": round(eval_test["base_win_rate"] * 100, 2),
            "precision_lift_pp": round(
                eval_test["precision_lift"] * 100, 2),
            "recall": round(recall * 100, 2),
            "f1": round(f1 * 100, 2),
            "approval_rate_pct": round(
                eval_test["approval_rate"] * 100, 2),
            "perdedoras_filtradas_pct": round(perdedoras_filtradas * 100, 2),
            "ganadoras_vetadas_pct": round(ganadoras_vetadas * 100, 2),
            "decision_threshold": round(float(umbral), 4),
            "validation_balanced_accuracy": round(
                metricas_val["balanced_accuracy"] * 100, 2),
            "scale_pos_weight": round(scale_pos_weight, 4),
            "matriz_confusion": {
                "tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp),
            },
            "ganadoras": int(y.sum()),
            "perdedoras": int((y == 0).sum()),
            # PRODUCTION and SHADOW incumbents are intentionally separate.
            # Both are evaluated on this candidate's untouched TEST block.
            "incumbent_balanced_accuracy": (
                round(incumbent_balanced * 100, 2)
                if incumbent_balanced is not None else None
            ),
            "production_incumbent_balanced_accuracy": (
                round(incumbent_balanced * 100, 2)
                if incumbent_balanced is not None else None
            ),
            "shadow_incumbent_balanced_accuracy": (
                round(shadow_incumbent_balanced * 100, 2)
                if shadow_incumbent_balanced is not None else None
            ),
            "incumbente_cargado": incumbent is not None,
            "production_gate_passed": bool(production_gate_passed),
            "production_gate_reasons": list(production_gate_reasons),
            "production_gate_policy": {
                "min_balanced_accuracy": round(self.min_balanced_accuracy, 6),
                "min_recall_ganadoras": round(self.min_recall_ganadoras, 6),
                "min_precision_lift": round(self.min_precision_lift, 6),
                "min_approval_rate": round(self.min_approval_rate, 6),
                "max_approval_rate": round(self.max_approval_rate, 6),
                "min_perdedoras_filtradas": round(self.min_perdedoras_filtradas, 6),
                "max_balanced_accuracy_drop": round(self.max_balanced_accuracy_drop, 6),
                "economic_validation": {
                    "round_trip_cost_bps": self.round_trip_cost_bps,
                    "min_accepted_test_trades": self.economic_min_trades,
                    "min_expectancy_r": self.economic_min_expectancy_r,
                    "min_profit_factor": self.economic_min_profit_factor,
                    "max_drawdown_r": self.economic_max_drawdown_r,
                    "min_net_r_lift": self.economic_min_net_r_lift,
                },
            },
            "automatic_retraining": bool(automatic),
            "production_activation_allowed": bool(allow_production_activation),
            "activado": bool(activar),
            "motivo_activacion": motivo_activacion,
            "features": list(FEATURES),
            "feature_version": FEATURE_VERSION,
            "training_policy_version": TRAINING_POLICY_VERSION,
            "entrenado_utc": evaluation_utc,
            "modelo_path": self._ruta_modelo(ticker),
        }
        importancias = getattr(modelo_eval, "feature_importances_", None)
        if importancias is not None and len(importancias) == len(FEATURES):
            metricas["feature_importance"] = {
                nombre: round(float(valor), 8)
                for nombre, valor in zip(FEATURES, importancias)
            }
            metricas["top_features"] = [
                nombre for nombre, _ in sorted(
                    zip(FEATURES, importancias),
                    key=lambda item: float(item[1]),
                    reverse=True,
                )[:8]
            ]
        # A Shadow artifact is deliberately separate from production.  For an
        # automatic 30/50 retrain, an existing Shadow model is replaced only
        # when the candidate meets the configured quality floor and does not
        # degrade the incumbent on the same untouched chronological test block.
        shadow_eligible = (
            self.shadow_enabled
            and (not self.shadow_tickers or ticker in self.shadow_tickers)
            and (self.shadow_save_all_candidates or production_gate_passed)
        )
        shadow_reason = "shadow mode disabled or ticker not configured"
        shadow_accept = False
        if shadow_eligible:
            candidate_balanced = float(eval_test["balanced_accuracy"])
            reasons_shadow = []
            if candidate_balanced < self.shadow_min_balanced_accuracy:
                reasons_shadow.append(
                    f"balanced_accuracy {candidate_balanced:.3f} < mínimo Shadow "
                    f"{self.shadow_min_balanced_accuracy:.3f}"
                )
            if (
                self.shadow_replace_requires_improvement
                and shadow_incumbent_balanced is not None
                and candidate_balanced
                < shadow_incumbent_balanced + self.shadow_min_improvement
            ):
                reasons_shadow.append(
                    f"candidate {candidate_balanced:.3f} no mejora incumbente "
                    f"{shadow_incumbent_balanced:.3f} por "
                    f"{self.shadow_min_improvement:.3f}"
                )
            shadow_accept = not reasons_shadow
            shadow_reason = (
                "candidato Shadow aceptado"
                if shadow_accept else "; ".join(reasons_shadow)
            )
            metricas["shadow_incumbent_balanced_accuracy"] = (
                round(shadow_incumbent_balanced * 100, 2)
                if shadow_incumbent_balanced is not None else None
            )
            metricas["shadow_candidate_balanced_accuracy"] = round(
                candidate_balanced * 100, 2
            )
            if shadow_accept:
                # Persist the exact FIT model evaluated on validation/test.
                # Refitting on those holdouts would make the published metrics
                # describe a different model and leak evaluation data into it.
                shadow_model = modelo_eval
                shadow_metrics = dict(metricas)
                shadow_metrics.update({
                    "shadow_candidate": True,
                    "shadow_candidate_accepted": True,
                    "shadow_candidate_reason": shadow_reason,
                    "production_approved": bool(activar),
                    "production_gate_passed": bool(production_gate_passed),
                    "shadow_model_path": self._ruta_shadow_modelo(ticker),
                })
                saved = self._guardar_shadow(ticker, shadow_model, shadow_metrics)
                metricas["shadow_model_saved"] = bool(saved)
                if saved:
                    with self._lock:
                        self._modelos_shadow[ticker] = shadow_model
                        from core.model_identity import model_snapshot
                        self._shadow_snapshots[ticker] = model_snapshot(metricas, self._ruta_shadow_modelo(ticker), True, 'SHADOW')
                        self._umbrales_shadow[ticker] = float(umbral)
                        self._shadow_sources[ticker] = self._ruta_shadow_modelo(ticker)
                else:
                    shadow_accept = False
                    shadow_reason = "falló la persistencia atómica Shadow"
            else:
                metricas["shadow_model_saved"] = False
        else:
            metricas["shadow_model_saved"] = False
            metricas["shadow_incumbent_balanced_accuracy"] = (
                round(shadow_incumbent_balanced * 100, 2)
                if shadow_incumbent_balanced is not None else None
            )
        metricas["shadow_candidate_accepted"] = bool(
            shadow_accept and metricas.get("shadow_model_saved")
        )
        metricas["shadow_candidate_reason"] = shadow_reason
        metricas["shadow_model_path"] = self._ruta_shadow_modelo(ticker)

        # Keep every evaluated artifact, including rejected candidates.
        candidate_dir = os.path.join(self.reportes_dir, 'candidates', ticker, evaluation_id)
        self._persist_model_pair(ticker=ticker, modelo=modelo_eval, metricas=metricas,
            models_dir=candidate_dir, model_path=os.path.join(candidate_dir, 'model.pkl'),
            meta_path=os.path.join(candidate_dir, 'model.meta.json'), tier='CANDIDATE')
        metricas['candidate_archive'] = candidate_dir
        if activar:
            # Production replacement remains a separate explicit policy.
            modelo_final = modelo_eval
            metricas["modelo_cargado"] = True
            if self._guardar(ticker, modelo_final, metricas):
                with self._lock:
                    self._modelos[ticker] = modelo_final
                    from core.model_identity import model_snapshot
                    self._production_snapshots[ticker] = model_snapshot(metricas, self._ruta_modelo(ticker), True)
                    self._umbrales[ticker] = float(umbral)
                    self._ultimo_reentreno[ticker] = len(df)
            else:
                metricas["activado"] = False
                metricas["motivo_activacion"] = "falló la persistencia atómica"
                metricas["modelo_cargado"] = incumbent is not None
        else:
            metricas["modelo_cargado"] = incumbent is not None
        print(f"[Agentes] Modelo de {ticker} evaluado | muestras={metricas['muestras']} | "
              f"balanced_accuracy={metricas['balanced_accuracy']}% | "
              f"recall={metricas['recall']}% | activado={metricas['activado']}")
        return metricas

    def entrenar_todos(
        self,
        tickers,
        *,
        allow_production_activation: Optional[bool] = None,
    ) -> dict:
        """Train all requested tickers while preserving the production safety gate."""
        if allow_production_activation is None:
            allow_production_activation = bool(self.activo and self.umbral_meta > 0)
        res = {}
        diagnostico = {}
        esquema_features = False
        try:
            all_df = pd.read_csv(self.feedback_path)
            all_df = _only_execution_valid(all_df)
            esquema_features = all(
                c in all_df.columns
                for c in (
                    "volumen_relativo_20", "liquidez_dolar_log",
                    *GALA10_FEATURE_NAMES,
                )
            )
            if esquema_features:
                all_df = all_df.dropna(subset=[
                    "volumen_relativo_20", "liquidez_dolar_log",
                    *GALA10_FEATURE_NAMES, "ganadora",
                ])
            if "ticker" in all_df.columns:
                for ticker, group in all_df.groupby("ticker"):
                    y = pd.to_numeric(group.get("ganadora"), errors="coerce").dropna()
                    diagnostico[str(ticker)] = {
                        "muestras": int(len(group)),
                        "ganadoras": int((y == 1).sum()),
                        "perdedoras": int((y == 0).sum()),
                    }
        except Exception:
            pass
        for t in tickers:
            with self._lock:
                if t in self._entrenando:
                    continue
                self._entrenando.add(t)
            try:
                m = self.entrenar_ticker(
                    t,
                    allow_production_activation=bool(allow_production_activation),
                    automatic=False,
                )
                if m:
                    res[t] = m
                else:
                    diag = diagnostico.get(t, {})
                    with self._lock:
                        cargado = t in self._modelos
                    res[t] = {
                        "ticker": t,
                        "muestras": int(diag.get("muestras", 0)),
                        "muestras_train": 0,
                        "muestras_test": 0,
                        "ganadoras": int(diag.get("ganadoras", 0)),
                        "perdedoras": int(diag.get("perdedoras", 0)),
                        "activado": False,
                        "modelo_cargado": cargado,
                        "incumbente_cargado": cargado,
                        "motivo_activacion": (
                            "esquema antiguo: faltan features de volumen/GALA10"
                            if not esquema_features else
                            "muestras/clases insuficientes para validación cronológica"
                        ),
                        "modelo_path": self._ruta_modelo(t),
                        "feature_version": FEATURE_VERSION,
                        "training_policy_version": TRAINING_POLICY_VERSION,
                        "features": list(FEATURES),
                        "entrenado_utc": datetime.now(timezone.utc).isoformat(),
                    }
            finally:
                with self._lock:
                    self._entrenando.discard(t)
        return res

    def solicitar_reentreno_todos(
        self,
        tickers,
        *,
        solo_elegibles: bool = False,
    ) -> bool:
        """Schedule retraining without blocking the trading engine.

        ``solo_elegibles=True`` is used by the end-of-session workflow and
        trains only tickers that accumulated the configured 50 new observations.
        The manual dashboard action keeps the explicit all-ticker behavior.
        """
        lista = list(dict.fromkeys(str(t).upper() for t in (tickers or []) if str(t).strip()))
        if solo_elegibles:
            if not self.auto_retrain_enabled:
                return False
            lista = self.retraining.eligible_tickers(lista)
        elif not self.activo and not self.shadow_enabled:
            return False
        if not lista:
            return False
        target = self._retrain_eligible_batch if solo_elegibles else self.entrenar_todos
        threading.Thread(
            target=target,
            args=(lista,),
            name="retrain-fin-sesion" if solo_elegibles else "retrain-manual",
            daemon=not solo_elegibles,
        ).start()
        return True

    def _retrain_eligible_batch(self, tickers) -> dict:
        results = {}

        for ticker in tickers:
            ticker = str(ticker or "").upper()
            if not ticker:
                continue

            total = None
            metrics = None
            claimed = False

            try:
                print(f"[Agentes] {ticker}: iniciando reentrenamiento post-sesión")

                progress = self.retraining.register_closed(ticker)
                total = int(progress.get("total_observations", 0) or 0)

                print(
                    f"[Agentes] {ticker}: observaciones elegibles={total}"
                )

                with self._lock:
                    if ticker in self._entrenando:
                        print(
                            f"[Agentes] {ticker}: omitido; "
                            "ya figura en _entrenando"
                        )
                        continue

                    self._entrenando.add(ticker)
                    claimed = True

                self.retraining.mark_training(ticker, total)

                print(
                    f"[Agentes] {ticker}: estado cambiado a TRAINING"
                )

                metrics = self.entrenar_ticker(
                    ticker,
                    allow_production_activation=self.auto_activate_production,
                    automatic=True,
                )

                accepted = bool(
                    metrics and metrics.get("shadow_candidate_accepted")
                )

                reason = (
                    str(metrics.get("shadow_candidate_reason", ""))
                    if metrics
                    else "muestras/clases insuficientes"
                )

                self.retraining.mark_result(
                    ticker,
                    total_count=total,
                    accepted=accepted,
                    reason=reason,
                    metrics=metrics or {},
                    consume_batch=bool(metrics),
                    deferred=metrics is None,
                )

                results[ticker] = metrics or {
                    "ticker": ticker,
                    "shadow_candidate_accepted": False,
                    "shadow_candidate_reason": reason,
                }

                print(
                    f"[Agentes] {ticker}: reentrenamiento terminado | "
                    f"accepted={accepted} | reason={reason}"
                )

            except Exception as exc:
                error_text = f"{type(exc).__name__}: {exc}"

                print(
                    f"[Agentes] ERROR reentrenando {ticker}: "
                    f"{error_text}"
                )

                if total is not None:
                    try:
                        self.retraining.mark_result(
                            ticker,
                            total_count=total,
                            accepted=False,
                            error=error_text,
                            metrics=metrics or {},
                            consume_batch=False,
                        )
                    except Exception as state_exc:
                        print(
                            f"[Agentes] ERROR actualizando estado "
                            f"{ticker}: {type(state_exc).__name__}: "
                            f"{state_exc}"
                        )

                results[ticker] = {
                    "ticker": ticker,
                    "error": error_text,
                }

            finally:
                if claimed:
                    with self._lock:
                        self._entrenando.discard(ticker)

        return results

    def retraining_status(self, tickers) -> list:
        """Dashboard-safe per-ticker progress and model state."""
        with self._lock:
            production_loaded = set(self._modelos)
            shadow_loaded = set(self._modelos_shadow)
        rows = self.retraining.snapshot(
            tickers,
            production_model_loaded=lambda ticker: ticker in production_loaded,
            shadow_model_loaded=lambda ticker: ticker in shadow_loaded,
        )
        prepared = self._training_rows_for_status()
        minimum = max(self.min_muestras, next(
            (n for n in range(1, max(1000, self.min_muestras * 5))
             if self._chronological_split_sizes(n, automatic=True) is not None),
            self.auto_min_fit_samples + self.auto_min_validation_samples + self.auto_min_test_samples))
        for row in rows:
            ticker = str(row.get("ticker", "")).upper()
            group = prepared[prepared["ticker"] == ticker] if "ticker" in prepared else prepared
            ready = False
            reason = ""
            if len(group) < minimum:
                reason = f"Faltan {minimum - len(group)} cierres con features válidas (mínimo {minimum})"
            else:
                blocks = self._causal_blocks(group, automatic=True)
                if blocks is None:
                    reason = "Muestras insuficientes tras excluir cierres posteriores a validación/TEST"
                elif any(block["ganadora"].nunique() < 2 for block in blocks[:3]):
                    reason = "FIT, validación y TEST necesitan ganadoras y perdedoras"
                else:
                    ready = True
                    reason = "Datos listos; candidato pendiente de evaluación"
                    if not blocks[4]["temporal_integrity_passed"]:
                        reason += "; cierres antiguos sin fecha de resolución: sólo evidencia SHADOW"
            row["training_readiness"] = {
                "ready": ready, "reason": reason,
                "min_samples": int(minimum), "eligible_samples": len(group),
            }
        return rows

    def _training_rows_for_status(self) -> pd.DataFrame:
        """Reuse prepared evidence between polls while the feedback file is unchanged."""
        def signature():
            try:
                stat = os.stat(self.feedback_path)
                return (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            except OSError:
                return None
        current = signature()
        with self._lock:
            if current == self._status_feedback_signature:
                return self._status_feedback_rows

        # Serialize expensive cache misses across concurrent /api/ai/status and
        # /api/ticker-analysis calls. Recheck after acquiring the load lock:
        # another request may have completed the refresh while we waited.
        with self._status_feedback_load_lock:
            current = signature()
            with self._lock:
                if current == self._status_feedback_signature:
                    return self._status_feedback_rows
            try:
                prepared = self._prepare_training_rows(pd.read_csv(self.feedback_path))
            except Exception:
                # Never cache a transient partial append or read error.
                return pd.DataFrame()
            if signature() == current:
                with self._lock:
                    self._status_feedback_rows = prepared
                    self._status_feedback_signature = current
            return prepared

    # =========================================================
    # PERSISTENCIA (un archivo por ticker)
    # =========================================================

    @staticmethod
    def _metricas_probabilidades(y_true, probabilidades, umbral: float) -> dict:
        """Métricas binarias explícitas para auditar el filtro de veto.

        Clase 1 = operación ganadora/aprobable; clase 0 = perdedora/vetable.
        Se incluye el baseline "vetar siempre" para que accuracy nunca vuelva
        a ocultar un clasificador degenerado por desbalance.
        """
        y_arr = np.asarray(y_true, dtype=int)
        probs = np.asarray(probabilidades, dtype=float)
        pred = (probs >= float(umbral)).astype(int)
        tn = int(((y_arr == 0) & (pred == 0)).sum())
        fp = int(((y_arr == 0) & (pred == 1)).sum())
        fn = int(((y_arr == 1) & (pred == 0)).sum())
        tp = int(((y_arr == 1) & (pred == 1)).sum())
        total = max(1, len(y_arr))
        ganadoras = tp + fn
        perdedoras = tn + fp
        aprobadas = tp + fp
        accuracy = float((tn + tp) / total)
        precision = float(tp / aprobadas) if aprobadas else 0.0
        recall = float(tp / ganadoras) if ganadoras else 0.0
        specificity = float(tn / perdedoras) if perdedoras else 0.0
        f1 = (
            float(2 * precision * recall / (precision + recall))
            if (precision + recall) else 0.0
        )
        base_win_rate = float(ganadoras / total)
        baseline_accuracy = float(max(ganadoras, perdedoras) / total)
        return {
            "tn": tn, "fp": fp, "fn": fn, "tp": tp,
            "accuracy": accuracy,
            "baseline_accuracy": baseline_accuracy,
            "balanced_accuracy": (recall + specificity) / 2.0,
            "precision": precision,
            "recall": recall,
            "specificity": specificity,
            "f1": f1,
            "approval_rate": float(aprobadas / total),
            "base_win_rate": base_win_rate,
            "precision_lift": precision - base_win_rate,
        }

    def _seleccionar_umbral(self, y_val, probabilidades, *, net_returns=None) -> Tuple[float, dict]:
        """Selecciona umbral SOLO en validación usando retornos netos conocidos.

        Se evita un umbral degenerado que apruebe casi cero señales. El test
        cronológico posterior no participa en esta selección.
        """
        probs = np.asarray(probabilidades, dtype=float)
        returns = np.asarray(net_returns, dtype=float) if net_returns is not None else None
        economic_available = bool(returns is not None and np.isfinite(returns).all())
        candidatos = np.unique(np.concatenate([
            np.linspace(0.05, 0.95, 91),
            np.quantile(probs, np.linspace(0.05, 0.95, 19)),
        ]))
        mejor = None
        mejor_umbral = 0.50
        # Para escoger umbral se permite la mitad del approval mínimo final;
        # la puerta de activación completa se aplica después sobre TEST.
        min_aprobacion_busqueda = max(0.02, self.min_approval_rate / 2.0)
        for umbral in candidatos:
            m = self._metricas_probabilidades(y_val, probs, float(umbral))
            if not (
                min_aprobacion_busqueda
                <= m["approval_rate"]
                <= self.max_approval_rate
            ):
                continue
            # Balanced accuracy es el criterio principal; precision y recall
            # rompen empates sin convertir "aprobar casi nada" en una ventaja.
            classification_score = (
                m["balanced_accuracy"],
                m["precision_lift"],
                m["recall"],
            )
            if economic_available:
                economic = economic_metrics(returns, probs, float(umbral), self.round_trip_cost_bps)
                # Maximize aggregate net validation R with useful coverage.
                # TEST outcomes never participate in threshold selection.
                score = (economic["net_r"], *classification_score)
            else:
                score = classification_score
            if mejor is None or score > mejor:
                mejor = score
                mejor_umbral = float(umbral)
        metricas = self._metricas_probabilidades(
            y_val, probs, mejor_umbral)
        return mejor_umbral, metricas

    def _razones_rechazo(self, metricas: dict) -> list:
        """Devuelve todas las puertas de aceptación que el candidato incumple."""
        reglas = [
            (
                "balanced_accuracy",
                metricas["balanced_accuracy"],
                self.min_balanced_accuracy,
            ),
            (
                "recall_ganadoras",
                metricas["recall"],
                self.min_recall_ganadoras,
            ),
            (
                "precision_lift",
                metricas["precision_lift"],
                self.min_precision_lift,
            ),
            (
                "approval_rate",
                metricas["approval_rate"],
                self.min_approval_rate,
            ),
            (
                "perdedoras_filtradas",
                metricas["specificity"],
                self.min_perdedoras_filtradas,
            ),
        ]
        razones = [
            f"{nombre} {valor:.3f} < mínimo {minimo:.3f}"
            for nombre, valor, minimo in reglas
            if valor < minimo
        ]
        if metricas["approval_rate"] > self.max_approval_rate:
            razones.append(
                f"approval_rate {metricas['approval_rate']:.3f} > máximo "
                f"{self.max_approval_rate:.3f}"
            )
        return razones

    @staticmethod
    def _crear_modelo(xgb_classifier, scale_pos_weight: float = 1.0):
        """Modelo pequeño/regularizado para reducir sobreajuste por ticker."""
        return xgb_classifier(
            n_estimators=80, max_depth=3, learning_rate=0.06,
            subsample=0.9, colsample_bytree=0.9,
            min_child_weight=2, reg_lambda=1.5,
            eval_metric="logloss", random_state=42,
            scale_pos_weight=float(scale_pos_weight),
            missing=np.nan,
        )

    def _ruta_modelo(self, ticker: str) -> str:
        return os.path.join(self.modelos_dir, f"modelo_{ticker}.pkl")

    def _ruta_meta(self, ticker: str) -> str:
        return os.path.join(self.modelos_dir, f"modelo_{ticker}.meta.json")

    def _ruta_shadow_modelo(self, ticker: str) -> str:
        return os.path.join(self.shadow_models_dir, f"modelo_{ticker}.pkl")

    def _ruta_shadow_meta(self, ticker: str) -> str:
        return os.path.join(self.shadow_models_dir, f"modelo_{ticker}.meta.json")

    def _archivar_modelo_incompatible(self, ticker: str, motivo: str):
        """Fail closed WITHOUT renaming operator-owned production artifacts.

        Prior versions moved a model and its metadata away on any load error.
        That can make an otherwise recoverable deployment issue look like a
        missing model. Preservation is mandatory: diagnosis is read-only.
        """
        self.model_load_errors[ticker] = str(motivo)
        print(
            f"[Agentes] PRODUCTION {ticker}: no cargable ({motivo}). "
            "Artefactos conservados en su ruta original; REAL bloqueado."
        )

    def _cargar_si_existe(self):
        try:
            if not os.path.isdir(self.modelos_dir):
                print(f"[Agentes] Directorio de modelos PRODUCTION ausente: {self.modelos_dir}")
                return
            for nombre in sorted(os.listdir(self.modelos_dir)):
                if not (nombre.startswith("modelo_") and nombre.endswith(".pkl")):
                    continue
                ticker = nombre[len("modelo_"):-len(".pkl")].upper()
                try:
                    meta_path = self._ruta_meta(ticker)
                    if not os.path.isfile(meta_path):
                        self._archivar_modelo_incompatible(ticker, "metadata faltante")
                        continue
                    with open(meta_path, "r", encoding="utf-8") as handle:
                        meta = json.load(handle)
                    if (
                        meta.get("feature_version") != FEATURE_VERSION
                        or meta.get("features") != FEATURES
                        or meta.get("training_policy_version") != TRAINING_POLICY_VERSION
                    ):
                        self._archivar_modelo_incompatible(
                            ticker, "versión/esquema de modelo incompatible"
                        )
                        continue
                    with open(self._ruta_modelo(ticker), "rb") as handle:
                        modelo = pickle.load(handle)
                    if int(getattr(modelo, "n_features_in_", -1)) != len(FEATURES):
                        self._archivar_modelo_incompatible(ticker, "número de features distinto")
                        continue
                    self._modelos[ticker] = modelo
                    from core.model_identity import model_snapshot
                    self._production_snapshots[ticker] = model_snapshot(
                        meta, self._ruta_modelo(ticker), True
                    )
                    self._umbrales[ticker] = float(
                        meta.get("decision_threshold", self.umbral_meta)
                    )
                    self._ultimo_reentreno[ticker] = int(meta.get("muestras", 0))
                except Exception as exc:
                    self._archivar_modelo_incompatible(
                        ticker, f"error de lectura: {type(exc).__name__}"
                    )
                    continue
            if self._modelos:
                print(f"[Agentes] Modelos por ticker cargados: {sorted(self._modelos.keys())}")
        except Exception as exc:
            print(f"[Agentes] No se pudieron listar los modelos por ticker: {exc}")

    def _cargar_shadow_si_existe(self):
        if not self.shadow_enabled or not os.path.isdir(self.shadow_models_dir):
            return
        try:
            loaded = []
            for name in os.listdir(self.shadow_models_dir):
                if not (name.startswith("modelo_") and name.endswith(".pkl")):
                    continue
                ticker = name[len("modelo_"):-len(".pkl")].upper()
                if self.shadow_tickers and ticker not in self.shadow_tickers:
                    continue
                model_path = self._ruta_shadow_modelo(ticker)
                meta_path = self._ruta_shadow_meta(ticker)
                if not os.path.exists(meta_path):
                    continue
                try:
                    with open(meta_path, "r", encoding="utf-8") as handle:
                        meta = json.load(handle)
                    if (
                        meta.get("feature_version") != FEATURE_VERSION
                        or meta.get("features") != FEATURES
                        or meta.get("training_policy_version") != TRAINING_POLICY_VERSION
                    ):
                        continue
                    with open(model_path, "rb") as handle:
                        model = pickle.load(handle)
                    if int(getattr(model, "n_features_in_", -1)) != len(FEATURES):
                        continue
                    self._modelos_shadow[ticker] = model
                    from core.model_identity import model_snapshot
                    self._shadow_snapshots[ticker] = model_snapshot(meta, model_path, True, 'SHADOW')
                    self._umbrales_shadow[ticker] = float(
                        meta.get("decision_threshold", self.shadow_default_threshold))
                    self._shadow_sources[ticker] = model_path
                    loaded.append(ticker)
                except Exception:
                    continue
            if loaded:
                print(f"[SHADOW] Modelos candidatos cargados: {sorted(loaded)}")
        except Exception as exc:
            print(f"[SHADOW] No se pudieron cargar modelos candidatos: {exc}")

    @staticmethod
    def _archive_model_pair(
        *, models_dir: str, ticker: str, model_path: str,
        meta_path: str, tier: str,
    ) -> str:
        """Create an immutable copy of the incumbent before any replacement.

        Retraining may promote a better candidate, but it must never erase the
        prior working artifact.  The archive is committed as one directory so a
        model and its metadata always remain together and can be restored.
        """

        sources = [path for path in (model_path, meta_path) if os.path.exists(path)]
        if not sources:
            return ""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive_parent = os.path.join(models_dir, "archive", str(ticker).upper())
        archive_dir = os.path.join(
            archive_parent, f"{stamp}_{uuid.uuid4().hex[:8]}"
        )
        temporary = archive_dir + ".tmp"
        os.makedirs(archive_parent, exist_ok=True)
        os.makedirs(temporary, exist_ok=False)
        try:
            for source in sources:
                shutil.copy2(source, os.path.join(temporary, os.path.basename(source)))
            manifest = {
                "ticker": str(ticker).upper(),
                "tier": tier,
                "archived_utc": datetime.now(timezone.utc).isoformat(),
                "files": [os.path.basename(path) for path in sources],
                "reason": "INCUMBENT_REPLACEMENT",
            }
            with open(
                os.path.join(temporary, "archive_manifest.json"),
                "w", encoding="utf-8",
            ) as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, archive_dir)
            return archive_dir
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _persist_model_pair(
        self, *, ticker: str, modelo, metricas: dict,
        models_dir: str, model_path: str, meta_path: str, tier: str,
    ) -> str:
        """Atomically promote one model pair after versioning the incumbent."""

        os.makedirs(models_dir, exist_ok=True)
        token = uuid.uuid4().hex
        model_tmp = f"{model_path}.{token}.tmp"
        meta_tmp = f"{meta_path}.{token}.tmp"
        archive_dir = ""
        had_model = os.path.exists(model_path)
        had_meta = os.path.exists(meta_path)
        try:
            with open(model_tmp, "wb") as handle:
                pickle.dump(modelo, handle)
                handle.flush()
                os.fsync(handle.fileno())
            archive_dir = self._archive_model_pair(
                models_dir=models_dir,
                ticker=ticker,
                model_path=model_path,
                meta_path=meta_path,
                tier=tier,
            )
            persisted_metrics = dict(metricas)
            persisted_metrics["previous_model_archive"] = archive_dir
            with open(meta_tmp, "w", encoding="utf-8") as handle:
                json.dump(persisted_metrics, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(model_tmp, model_path)
            try:
                os.replace(meta_tmp, meta_path)
            except Exception:
                # Restore the complete incumbent if the pair commit was split.
                if archive_dir:
                    archived_model = os.path.join(
                        archive_dir, os.path.basename(model_path)
                    )
                    archived_meta = os.path.join(
                        archive_dir, os.path.basename(meta_path)
                    )
                    if os.path.exists(archived_model):
                        shutil.copy2(archived_model, model_path)
                    if os.path.exists(archived_meta):
                        shutil.copy2(archived_meta, meta_path)
                elif not had_model and os.path.exists(model_path):
                    os.remove(model_path)
                if not had_meta and os.path.exists(meta_path):
                    os.remove(meta_path)
                raise
            return archive_dir
        finally:
            for temporary in (model_tmp, meta_tmp):
                try:
                    if os.path.exists(temporary):
                        os.remove(temporary)
                except OSError:
                    pass

    def _guardar_shadow(self, ticker: str, modelo, metricas: dict) -> bool:
        try:
            model_path = self._ruta_shadow_modelo(ticker)
            meta_path = self._ruta_shadow_meta(ticker)
            self._persist_model_pair(
                ticker=ticker, modelo=modelo, metricas=metricas,
                models_dir=self.shadow_models_dir,
                model_path=model_path, meta_path=meta_path, tier="SHADOW",
            )
            return True
        except Exception as exc:
            print(f"[SHADOW] Error guardando candidato {ticker}: {exc}")
            return False

    def _guardar(self, ticker: str, modelo, metricas: dict) -> bool:
        try:
            modelo_path = self._ruta_modelo(ticker)
            meta_path = self._ruta_meta(ticker)
            self._persist_model_pair(
                ticker=ticker, modelo=modelo, metricas=metricas,
                models_dir=self.modelos_dir,
                model_path=modelo_path, meta_path=meta_path, tier="PRODUCTION",
            )
            return True
        except Exception as e:
            print(f"[Agentes] Error guardando el modelo de {ticker}: {e}")
            return False
