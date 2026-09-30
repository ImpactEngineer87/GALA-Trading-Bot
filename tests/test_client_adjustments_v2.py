import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd

from agents.contracts import (
    DatosMultiTemporal,
    Direccion,
    DireccionDia,
    Veredicto,
    VeredictoAuditor,
)
from agents.orchestrator import OrquestadorAgentes
from agents.training import EntrenadorAgentes
from api.hybrid_order_executor import ContextoMercado, calcular_orden_hibrida
from core.risk_manager import OrdenTradingCalculada
from engine.bot_engine import ChocoletraBot


class BarCloseTests(unittest.TestCase):
    def test_wait_targets_current_boundary_when_before_100ms(self):
        wait = ChocoletraBot._segundos_hasta_cierre_confirmado(
            ahora_epoch=120.050,
            delay_ms=100,
        )
        self.assertAlmostEqual(wait, 0.050, places=6)

    def test_wait_targets_next_boundary_when_after_100ms(self):
        wait = ChocoletraBot._segundos_hasta_cierre_confirmado(
            ahora_epoch=120.250,
            delay_ms=100,
        )
        self.assertAlmostEqual(wait, 59.850, places=6)

    def test_only_previous_confirmed_one_minute_bar_is_kept(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        idx = pd.to_datetime(
            ["2026-07-31T14:28:00Z", "2026-07-31T14:29:00Z", "2026-07-31T14:30:00Z"]
        )
        df = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
        closed = bot._solo_velas_cerradas(
            df,
            timeframe_minutes=1,
            ahora_utc=datetime(2026, 7, 31, 14, 30, 0, 100000, tzinfo=timezone.utc),
        )
        self.assertEqual(list(closed["close"]), [1.0, 2.0])
        self.assertEqual(closed.index[-1], idx[-2])

    def test_micro_frames_are_trimmed_independently(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        now = datetime(2026, 7, 31, 14, 30, 0, 100000, tzinfo=timezone.utc)
        m1 = pd.DataFrame(
            {"close": [1.0, 2.0]},
            index=pd.to_datetime(["2026-07-31T14:29:00Z", "2026-07-31T14:30:00Z"]),
        )
        m5 = pd.DataFrame(
            {"close": [10.0, 20.0]},
            index=pd.to_datetime(["2026-07-31T14:25:00Z", "2026-07-31T14:30:00Z"]),
        )
        data = DatosMultiTemporal("TEST", {"1m": m1, "5m": m5})
        trimmed = bot._datos_micro_confirmados(data, ahora_utc=now)
        self.assertEqual(len(trimmed.tf("1m")), 1)
        self.assertEqual(len(trimmed.tf("5m")), 1)


class TransparentModelStatusTests(unittest.TestCase):
    def test_missing_model_returns_none_not_fake_50_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {
                "agents": {
                    "entrenamiento": {
                        "activo": False,
                        "modelos_dir": os.path.join(tmp, "models"),
                        "feedback_path": os.path.join(tmp, "feedback.csv"),
                    },
                    "shadow_mode": {
                        "enabled": True,
                        "modelos_dir": os.path.join(tmp, "shadow"),
                    },
                }
            }
            trainer = EntrenadorAgentes(cfg)
            decision = type("D", (), {"ticker": "MSFT"})()
            prob, approved = trainer.evaluar(decision)
            shadow = trainer.evaluar_shadow(decision)
            self.assertIsNone(prob)
            self.assertTrue(approved)
            self.assertFalse(shadow["model_loaded"])
            self.assertIsNone(shadow["probability"])
            self.assertIsNone(shadow["threshold"])

    def test_hybrid_sizing_accepts_na_probability(self):
        order = OrdenTradingCalculada(
            ticker="MSFT",
            tipo="BUY",
            precio_limite=100.0,
            stop_loss=99.0,
            trailing_stop_pct=1.0,
            cantidad=10,
            riesgo_usd=10.0,
            atr_actual=1.0,
            take_profit=102.0,
            salida_parcial_activa=False,
        )
        context = ContextoMercado(
            atr_relativo=0.25,
            es_rango_lateral=False,
            prob_ia=None,
            es_senal_maestra=False,
        )
        adjusted, _, reason = calcular_orden_hibrida(order, context)
        self.assertEqual(adjusted.cantidad, 10)
        self.assertIn("IA N/A", reason)


class _Macro:
    def __init__(self, direction):
        self.direction = direction

    def analizar(self, datos, **kwargs):
        return DireccionDia(
            ticker=datos.ticker,
            direccion=self.direction,
            confianza=0.0 if self.direction == Direccion.NEUTRAL else 0.8,
        )


class _Micro:
    def analizar(self, *args, **kwargs):
        return None


class _Auditor:
    def __init__(self, lateral=False):
        self.lateral = lateral

    def auditar(self, *args, **kwargs):
        return VeredictoAuditor(
            veredicto=Veredicto.VETADA if self.lateral else Veredicto.APROBADA,
            atr_relativo=0.2,
            es_horario_institucional=True,
            es_rango_lateral=self.lateral,
            datos_suficientes=True,
        )


class _Trainer:
    def __init__(self, production_threshold=0.0):
        self.umbral_meta = production_threshold

    def evaluar(self, decision, requerir_modelo=False):
        return 0.90, True

    def evaluar_shadow(self, decision):
        return {
            "enabled": True,
            "model_loaded": True,
            "probability": 0.90,
            "threshold": 0.55,
            "would_approve": True,
            "source": "test-model",
        }

    def modelo_disponible(self, ticker):
        return False


class HardVetoTests(unittest.TestCase):
    def _orchestrator(self, direction, lateral=False, production_threshold=0.0):
        orq = OrquestadorAgentes.__new__(OrquestadorAgentes)
        orq.config = {
            "risk": {"stop_loss_factor": 2.0, "sl_min_pct_precio": 0.15},
            "agents": {"agente2": {"ratio_rb_minimo": 1.8}},
        }
        orq.activo = True
        orq.requiere_confluencia = False
        orq.agente1 = _Macro(direction)
        orq.agente2 = _Micro()
        orq.agente3 = _Auditor(lateral=lateral)
        orq.entrenador = _Trainer(production_threshold)
        orq.hard_veto_enabled = True
        orq.hard_veto_macro_neutral = True
        orq.hard_veto_lateral_range = True
        orq.hard_veto_probability_cap = 0.30
        return orq

    @patch("agents.orchestrator.calcular_features_volumen", return_value={})
    def test_macro_neutral_caps_shadow_but_does_not_block_shadow_execution(self, _):
        orq = self._orchestrator(Direccion.NEUTRAL)
        data = DatosMultiTemporal("MSFT", {})
        decision = orq.auditar_gala10(
            data,
            precio_actual=100.0,
            senal_gala10="BUY",
            atr=1.0,
            actual_en_formacion=False,
            macro_en_formacion=True,
            micro_en_formacion=False,
        )
        self.assertTrue(decision.operar)
        self.assertTrue(decision.hard_veto)
        self.assertEqual(decision.hard_veto_reason, "MACRO_NEUTRAL")
        self.assertAlmostEqual(decision.shadow_prob, 0.30)
        self.assertFalse(decision.shadow_would_approve)

    @patch("agents.orchestrator.calcular_features_volumen", return_value={})
    def test_lateral_rule_blocks_when_production_veto_is_enabled(self, _):
        orq = self._orchestrator(
            Direccion.ALCISTA,
            lateral=True,
            production_threshold=0.50,
        )
        data = DatosMultiTemporal("MSFT", {})
        decision = orq.auditar_gala10(
            data,
            precio_actual=100.0,
            senal_gala10="BUY",
            atr=1.0,
            actual_en_formacion=False,
            macro_en_formacion=True,
            micro_en_formacion=False,
        )
        self.assertFalse(decision.operar)
        self.assertEqual(decision.hard_veto_reason, "LATERAL_RANGE")
        self.assertAlmostEqual(decision.prob_meta, 0.30)


class IndependentAtrTargetTests(unittest.TestCase):
    def test_gala10_master_proposal_uses_35_sl_and_45_tp_atr(self):
        orchestrator = OrquestadorAgentes.__new__(OrquestadorAgentes)
        orchestrator.config = {
            "risk": {
                "stop_loss_factor": 3.5,
                "take_profit_factor": 4.5,
                "sl_min_pct_precio": 0.15,
            }
        }

        proposal = orchestrator._propuesta_market(
            "MSFT", "BUY", 100.0, 2.0, datetime(2026, 8, 6, 10, 0)
        )

        self.assertEqual(proposal.precio_entrada, 100.0)
        self.assertEqual(proposal.stop_loss, 93.0)
        self.assertEqual(proposal.take_profit, 109.0)
        self.assertAlmostEqual(proposal.ratio_rb, 9.0 / 7.0)
        self.assertIn("SL 3.50×ATR | TP 4.50×ATR", proposal.razones)


if __name__ == "__main__":
    unittest.main()
