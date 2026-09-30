import unittest
from datetime import datetime
from unittest.mock import patch
import csv
import os
import tempfile

import numpy as np
import pandas as pd

from agents.contracts import (
    DatosMultiTemporal,
    Direccion,
    DireccionDia,
    Veredicto,
    VeredictoAuditor,
)
from agents.market_regime import (
    AgenteRegimenMercado,
    REGIME_RANGE,
    REGIME_TREND_UP,
)
from agents.orchestrator import OrquestadorAgentes
from agents.shadow_store import ShadowDecisionStore, COLUMNS as SHADOW_COLUMNS
from engine.bot_engine import ChocoletraBot


def _ohlcv(closes):
    closes = np.asarray(closes, dtype=float)
    idx = pd.date_range("2026-09-04 13:30:00+00:00", periods=len(closes), freq="min")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + 0.08,
            "low": closes - 0.08,
            "close": closes,
            "volume": np.full(len(closes), 10000.0),
        },
        index=idx,
    )


def _range_data():
    pattern = [100.00, 100.22, 99.96, 100.18, 99.94, 100.16]
    closes = (pattern * 5)[:30]
    return DatosMultiTemporal("META", {"1m": _ohlcv(closes)})


def _trend_data():
    closes = np.linspace(100.0, 103.5, 30)
    return DatosMultiTemporal("META", {"1m": _ohlcv(closes)})


class MarketRegimeClassifierTests(unittest.TestCase):
    def test_broad_choppy_box_is_range_even_when_not_tiny_compression(self):
        cfg = {"agents": {"market_regime": {"enabled": True, "enforcement": "shadow"}}}
        agent = AgenteRegimenMercado(cfg)
        decision = agent.evaluar(
            _range_data(),
            "BUY",
            gala10_contexto={"gala_breakout_atr": 0.05},
        )
        self.assertEqual(decision.regime, REGIME_RANGE)
        self.assertTrue(decision.would_block)
        self.assertEqual(decision.reason, "REGIME_RANGE_CHOP")
        self.assertGreater(decision.range_pct, 0.18)
        self.assertGreater(decision.flip_ratio, 0.45)

    def test_range_breakout_can_pass(self):
        cfg = {"agents": {"market_regime": {"enabled": True, "enforcement": "shadow"}}}
        agent = AgenteRegimenMercado(cfg)
        decision = agent.evaluar(
            _range_data(),
            "BUY",
            gala10_contexto={"gala_breakout_atr": 0.35},
        )
        self.assertEqual(decision.regime, REGIME_RANGE)
        self.assertFalse(decision.would_block)
        self.assertEqual(decision.reason, "REGIME_RANGE_BREAKOUT_CONFIRMED")

    def test_clean_directional_move_is_trend_up(self):
        cfg = {"agents": {"market_regime": {"enabled": True, "enforcement": "shadow"}}}
        agent = AgenteRegimenMercado(cfg)
        decision = agent.evaluar(
            _trend_data(),
            "BUY",
            gala10_contexto={"gala_breakout_atr": 0.30},
        )
        self.assertEqual(decision.regime, REGIME_TREND_UP)
        self.assertFalse(decision.would_block)
        self.assertTrue(decision.signal_aligned)


class _Macro:
    def analizar(self, datos, precio_actual, actual_en_formacion, ahora):
        return DireccionDia(
            ticker=datos.ticker,
            direccion=Direccion.ALCISTA,
            confianza=0.8,
            timestamp=ahora,
        )


class _Micro:
    def analizar(self, *args, **kwargs):
        return None


class _Auditor:
    def auditar(self, *args, **kwargs):
        return VeredictoAuditor(
            veredicto=Veredicto.APROBADA,
            motivo="ok",
            atr_relativo=0.25,
            es_horario_institucional=True,
            es_rango_lateral=False,  # old 8-bar compression detector says "not lateral"
            datos_suficientes=True,
        )


class _Trainer:
    def __init__(self):
        self.umbral_meta = 0.0

    def evaluar(self, decision, requerir_modelo=False):
        return 0.91, True

    def evaluar_shadow(self, decision):
        return {
            "enabled": True,
            "model_loaded": True,
            "probability": 0.91,
            "threshold": 0.55,
            "would_approve": True,
            "source": "fixture",
        }

    def modelo_disponible(self, ticker):
        return True


class _TrainerVeto(_Trainer):
    def evaluar(self, decision, requerir_modelo=False):
        return 0.20, False


class RegimeOrchestrationTests(unittest.TestCase):
    def _orchestrator(self, enforcement):
        cfg = {
            "agents": {
                "market_regime": {
                    "enabled": True,
                    "enforcement": enforcement,
                    "block_range": True,
                },
                "agente2": {"ratio_rb_minimo": 1.8},
            },
            "risk": {
                "stop_loss_factor": 3.0,
                "take_profit_factor": 4.0,
                "sl_min_pct_precio": 0.15,
            },
        }
        orq = OrquestadorAgentes.__new__(OrquestadorAgentes)
        orq.config = cfg
        orq.activo = True
        orq.requiere_confluencia = False
        orq.agente1 = _Macro()
        orq.agente2 = _Micro()
        orq.agente3 = _Auditor()
        orq.regime_agent = AgenteRegimenMercado(cfg)
        orq.entrenador = _Trainer()
        orq.hard_veto_enabled = True
        orq.hard_veto_macro_neutral = True
        orq.hard_veto_lateral_range = True
        orq.hard_veto_probability_cap = 0.30
        return orq

    @patch("agents.orchestrator.calcular_features_volumen", return_value={})
    def test_shadow_regime_veto_does_not_change_execution(self, _):
        orq = self._orchestrator("shadow")
        decision = orq.auditar_gala10(
            _range_data(),
            100.10,
            "BUY",
            0.20,
            ahora=datetime(2026, 9, 4, 10, 15),
            actual_en_formacion=False,
            macro_en_formacion=False,
            micro_en_formacion=False,
            gala10_contexto={"gala_breakout_atr": 0.05},
            requerir_modelo=False,
        )
        self.assertTrue(decision.operar)
        self.assertTrue(decision.market_regime_would_block)
        self.assertFalse(decision.shadow_would_approve)
        self.assertIn("REGIME RANGE", decision.resumen)

    @patch("agents.orchestrator.calcular_features_volumen", return_value={})
    def test_real_regime_veto_blocks_after_model_approval(self, _):
        orq = self._orchestrator("real")
        decision = orq.auditar_gala10(
            _range_data(),
            100.10,
            "BUY",
            0.20,
            ahora=datetime(2026, 9, 4, 10, 15),
            actual_en_formacion=False,
            macro_en_formacion=False,
            micro_en_formacion=False,
            gala10_contexto={"gala_breakout_atr": 0.05},
            requerir_modelo=True,
        )
        self.assertFalse(decision.operar)
        self.assertEqual(decision.market_regime_reason, "REGIME_RANGE_CHOP")
        self.assertIn("VETO RÉGIMEN", decision.resumen)
        self.assertEqual(
            ChocoletraBot._ai_decision_reason(decision, approved=False),
            "REGIME_RANGE_CHOP",
        )

    @patch("agents.orchestrator.calcular_features_volumen", return_value={})
    def test_exact_veto_cause_prefers_model_when_model_failed_first(self, _):
        orq = self._orchestrator("real")
        orq.entrenador = _TrainerVeto()
        decision = orq.auditar_gala10(
            _range_data(),
            100.10,
            "BUY",
            0.20,
            ahora=datetime(2026, 9, 4, 10, 15),
            actual_en_formacion=False,
            macro_en_formacion=False,
            micro_en_formacion=False,
            gala10_contexto={"gala_breakout_atr": 0.05},
            requerir_modelo=True,
        )
        self.assertFalse(decision.operar)
        self.assertEqual(decision.execution_veto_reason, "AI_THRESHOLD")
        self.assertEqual(
            ChocoletraBot._ai_decision_reason(decision, approved=False),
            "AI_THRESHOLD",
        )


class RegimePerExecutionModeTests(unittest.TestCase):
    def _orchestrator(self, paper_policy="enforce", real_policy="observe"):
        cfg = {
            "agents": {
                "market_regime": {
                    "enabled": True,
                    "execution_policy": {
                        "PAPER": paper_policy,
                        "REAL": real_policy,
                    },
                    "block_range": True,
                },
                "agente2": {"ratio_rb_minimo": 1.8},
            },
            "risk": {
                "stop_loss_factor": 3.0,
                "take_profit_factor": 4.0,
                "sl_min_pct_precio": 0.15,
            },
        }
        orq = OrquestadorAgentes.__new__(OrquestadorAgentes)
        orq.config = cfg
        orq.activo = True
        orq.requiere_confluencia = False
        orq.agente1 = _Macro()
        orq.agente2 = _Micro()
        orq.agente3 = _Auditor()
        orq.regime_agent = AgenteRegimenMercado(cfg)
        orq.entrenador = _Trainer()
        orq.hard_veto_enabled = True
        orq.hard_veto_macro_neutral = True
        orq.hard_veto_lateral_range = True
        orq.hard_veto_probability_cap = 0.30
        return orq

    @patch("agents.orchestrator.calcular_features_volumen", return_value={})
    def test_paper_can_enforce_regime_without_becoming_real(self, _):
        orq = self._orchestrator(paper_policy="enforce", real_policy="observe")
        decision = orq.auditar_gala10(
            _range_data(), 100.10, "BUY", 0.20,
            ahora=datetime(2026, 9, 4, 10, 15),
            actual_en_formacion=False, macro_en_formacion=False, micro_en_formacion=False,
            gala10_contexto={"gala_breakout_atr": 0.05},
            requerir_modelo=False, execution_mode="PAPER",
        )
        self.assertEqual(decision.execution_mode, "PAPER")
        self.assertEqual(decision.market_regime_policy, "enforce")
        self.assertFalse(decision.operar)
        self.assertEqual(decision.execution_veto_reason, "REGIME_RANGE_CHOP")

    @patch("agents.orchestrator.calcular_features_volumen", return_value={})
    def test_real_observe_does_not_block_live_route(self, _):
        orq = self._orchestrator(paper_policy="enforce", real_policy="observe")
        decision = orq.auditar_gala10(
            _range_data(), 100.10, "BUY", 0.20,
            ahora=datetime(2026, 9, 4, 10, 15),
            actual_en_formacion=False, macro_en_formacion=False, micro_en_formacion=False,
            gala10_contexto={"gala_breakout_atr": 0.05},
            requerir_modelo=True, execution_mode="REAL",
        )
        self.assertEqual(decision.execution_mode, "REAL")
        self.assertEqual(decision.market_regime_policy, "observe")
        self.assertTrue(decision.operar)
        self.assertTrue(decision.market_regime_would_block)
        self.assertIn("REGIME_OBSERVE_VETO", ChocoletraBot._ai_decision_reason(decision, approved=True))

    @patch("agents.orchestrator.calcular_features_volumen", return_value={})
    def test_real_enforce_does_not_depend_on_model_requirement_proxy(self, _):
        orq = self._orchestrator(paper_policy="observe", real_policy="enforce")
        decision = orq.auditar_gala10(
            _range_data(), 100.10, "BUY", 0.20,
            ahora=datetime(2026, 9, 4, 10, 15),
            actual_en_formacion=False, macro_en_formacion=False, micro_en_formacion=False,
            gala10_contexto={"gala_breakout_atr": 0.05},
            requerir_modelo=False, execution_mode="REAL",
        )
        self.assertEqual(decision.execution_mode, "REAL")
        self.assertEqual(decision.market_regime_policy, "enforce")
        self.assertFalse(decision.operar)
        self.assertEqual(decision.execution_veto_reason, "REGIME_RANGE_CHOP")


class RegimeShadowLedgerTests(unittest.TestCase):
    def test_regime_only_shadow_observation_is_joined_to_trade_outcome(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "shadow.csv")
            store = ShadowDecisionStore(path)
            decision = type(
                "D",
                (),
                {
                    "ticker": "META",
                    "tipo": "BUY",
                    "timestamp": datetime(2026, 9, 4, 10, 15),
                    "shadow_enabled": False,
                    "shadow_model_loaded": False,
                    "market_regime_enabled": True,
                    "market_regime_enforcement": "shadow",
                    "market_regime": "RANGE",
                    "market_regime_confidence": 0.875,
                    "market_regime_would_block": True,
                    "market_regime_reason": "REGIME_RANGE_CHOP",
                },
            )()
            opened = store.abrir(decision, precio_entrada=100.0, stop_loss=99.0)
            closed = store.cerrar("META", precio_salida=98.5)
            self.assertIsNotNone(opened)
            self.assertIsNotNone(closed)
            self.assertEqual(closed["ganadora"], 0)
            self.assertEqual(closed["regime_veto_would_help"], 1)
            self.assertEqual(closed["regime_false_block"], 0)
            self.assertEqual(closed["regime_correct"], 1)

    def test_shadow_csv_header_is_migrated_to_v31_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "shadow.csv")
            old_columns = [
                "event", "signal_id", "timestamp", "ticker", "tipo",
                "shadow_prob", "shadow_threshold", "shadow_would_approve",
                "shadow_model_source", "precio_entrada", "stop_loss",
                "precio_salida", "r_multiple", "ganadora",
                "shadow_correct", "veto_would_help",
            ]
            with open(path, "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=old_columns)
                writer.writeheader()
                writer.writerow({
                    "event": "CLOSE",
                    "signal_id": "legacy-1",
                    "ticker": "META",
                    "tipo": "BUY",
                    "r_multiple": -1.0,
                    "ganadora": 0,
                })

            ShadowDecisionStore(path)

            with open(path, "r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
                self.assertEqual(reader.fieldnames, SHADOW_COLUMNS)
            self.assertEqual(rows[0]["signal_id"], "legacy-1")
            self.assertIn("market_regime", rows[0])
            self.assertTrue(os.path.exists(path + ".before_v3_1_1"))


if __name__ == "__main__":
    unittest.main()
