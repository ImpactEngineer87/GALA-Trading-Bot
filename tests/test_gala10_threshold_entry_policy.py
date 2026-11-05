"""GALA10/model approval reaches execution; context cannot add an entry veto."""
from contextlib import nullcontext
from datetime import datetime
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import numpy as np
import pandas as pd

from agents.contracts import DatosMultiTemporal,Direccion,DireccionDia,Veredicto,VeredictoAuditor
from agents.market_regime import MarketRegimeDecision
from agents.orchestrator import OrquestadorAgentes
from agents.training import EntrenadorAgentes
from engine.bot_engine import ChocoletraBot


class ThresholdEntryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.network=self.enterContext(patch('requests.sessions.Session.request',
            side_effect=AssertionError('Entry policy tests must not contact broker')))
        self.frame=pd.DataFrame({'open':[349.]*40,'high':[350.]*40,'low':[348.]*40,
            'close':[349.]*40,'volume':[1000.]*40},
            index=pd.date_range(end='2026-11-05T16:42:00Z',periods=40,freq='min'))
        self.data=DatosMultiTemporal('TSLA',{'1m':self.frame})

    def tearDown(self):self.network.assert_not_called()

    def orchestrator(self,probability=.8023396134376526,threshold=.55,*,missing=False,lateral=True):
        # Exercise actual production inference and threshold comparison, with a
        # deterministic classifier and causal, audited context fixtures.
        trainer=EntrenadorAgentes.__new__(EntrenadorAgentes)
        trainer._lock=threading.RLock();trainer.activo=True;trainer.umbral_meta=.55
        trainer._modelos={} if missing else {'TSLA':SimpleNamespace(
            predict_proba=lambda x:np.array([[1-probability,probability]]))}
        trainer._umbrales={'TSLA':threshold};trainer._production_snapshots={'TSLA':{'model_version':'test-model'}}
        trainer.manual_threshold_overrides=SimpleNamespace(active=lambda *args:None)
        trainer.shadow_enabled=False
        orch=OrquestadorAgentes.__new__(OrquestadorAgentes)
        orch.config={};orch.activo=True;orch.entrenador=trainer
        orch.agente1=SimpleNamespace(analizar=Mock(return_value=DireccionDia('TSLA',Direccion.NEUTRAL,.2)))
        orch.agente2=SimpleNamespace(analizar=Mock(return_value=None))
        orch.agente3=SimpleNamespace(auditar=Mock(return_value=VeredictoAuditor(
            Veredicto.VETADA,atr_relativo=.2,es_rango_lateral=lateral)))
        orch.hard_veto_enabled=True;orch.hard_veto_macro_neutral=True;orch.hard_veto_lateral_range=True
        orch.hard_veto_probability_cap=.3
        orch.regime_agent=SimpleNamespace(enabled=True,enforcement='real',policy_for=lambda mode:'enforce',
            evaluar=Mock(return_value=MarketRegimeDecision(regime='RANGE',would_block=True,reason='REGIME_RANGE')))
        return orch

    def decide(self,orch,side='SELL',mode='REAL'):
        return orch.auditar_gala10(self.data,349.16,side,1.27346,
            ahora=datetime(2026,11,5,11,43,24),actual_en_formacion=False,
            requerir_modelo=mode=='REAL',execution_mode=mode)

    def test_above_threshold_ignores_macro_range_and_legacy_regime_enforcement(self):
        for mode in ('REAL','PAPER'):
            for side in ('BUY','SELL'):
                with self.subTest(mode=mode,side=side):
                    decision=self.decide(self.orchestrator(),side,mode)
                    self.assertTrue(decision.operar)
                    self.assertEqual(decision.tipo,side)
                    self.assertAlmostEqual(decision.ai_raw_probability,.8023396134376526)
                    self.assertEqual(decision.prob_meta,.802)
                    self.assertEqual(decision.entry_policy,'GALA10_MODEL_THRESHOLD_V1')
                    self.assertEqual(decision.context_filter_reasons,'MACRO_NEUTRAL|LATERAL_RANGE')
                    self.assertFalse(decision.hard_veto)
                    self.assertEqual(decision.execution_veto_reason,'')
                    self.assertTrue(decision.market_regime_would_block)
                    self.assertEqual(decision.market_regime_policy,'observe')

    def test_exact_threshold_passes_below_threshold_and_unavailable_model_still_fail(self):
        self.assertTrue(self.decide(self.orchestrator(.55)).operar)
        below=self.decide(self.orchestrator(.54999))
        self.assertFalse(below.operar);self.assertEqual(below.execution_veto_reason,'AI_THRESHOLD')
        missing=self.decide(self.orchestrator(missing=True))
        self.assertFalse(missing.operar);self.assertEqual(missing.execution_veto_reason,'AI_MODEL_UNAVAILABLE')

    def test_context_does_not_cap_or_reject_shadow_model(self):
        orch=self.orchestrator()
        orch.entrenador.evaluar_shadow=Mock(return_value={'enabled':True,'model_loaded':True,
            'probability':.9,'raw_probability':.9,'threshold':.6,'would_approve':True,'source':'candidate'})
        decision=self.decide(orch)
        self.assertEqual(decision.shadow_prob,.9);self.assertTrue(decision.shadow_would_approve)
        self.assertEqual(decision.prob_meta,.802)

    def test_missing_data_outside_hours_and_absent_gala_signal_are_still_blocked(self):
        for field in ('datos_suficientes','es_horario_institucional'):
            orch=self.orchestrator();setattr(orch.agente3.auditar.return_value,field,False)
            self.assertFalse(self.decide(orch).operar)
        self.assertFalse(self.decide(self.orchestrator(),side='OM-R').operar)

    def test_actual_engine_attempts_entry_and_receipt_explains_observation_only_context(self):
        orch=self.orchestrator();bot=ChocoletraBot.__new__(ChocoletraBot)
        bot._lock=threading.RLock();bot._posiciones={};bot._last_prices={};bot._last_price_times={}
        bot._gala10_signal_bar={'TSLA':self.frame.index[-1]};bot._gala10_contexto={}
        bot.agentes_activos=True;bot.live_allow_short=True;bot.requerir_modelo_aprobado_en_real=True
        bot._is_paper_ticker=Mock(return_value=False)
        bot._obtener_mtf=Mock(return_value=self.data);bot._datos_micro_confirmados=Mock(return_value=self.data)
        for name in ('_emit_candles','_recover_ticker_quarantine','_quarantine_real_ticker','_emit_log',
                     '_gestionar_posiciones','_registrar_no_op','_registrar_veto_ia'):
            setattr(bot,name,Mock())
        bot._evaluar_limite_perdida_diaria=Mock(return_value=False)
        bot._avanzar_aprendizaje_senales=Mock(return_value=[])
        bot.veto_observations=SimpleNamespace(update_bar=Mock(return_value=[]))
        bot._senal_estrategia=Mock(return_value='SELL');bot._atr_micro=Mock(return_value=1.27346)
        bot._gsco_features=Mock(return_value={});bot._ahora_et=Mock(return_value=datetime(2026,11,5,11,43,24))
        bot._emit_signal=Mock(return_value={'signal_id':'qualified-signal'})
        bot._abrir_o_revertir=Mock();bot.ejecutor_hibrido=SimpleNamespace(secuencia_simbolo=lambda ticker:nullcontext())
        bot.orquestador=orch
        bot._procesar_ticker('TSLA')
        bot._abrir_o_revertir.assert_called_once()
        bot._registrar_veto_ia.assert_not_called()
        receipt=bot._emit_signal.call_args
        self.assertEqual(receipt.args[0],'SELL');self.assertEqual(receipt.kwargs['ai_decision'],'APPROVED')
        self.assertEqual(receipt.kwargs['ai_threshold'],.55)
        self.assertEqual(receipt.kwargs['ai_context']['context_filter_reasons'],'MACRO_NEUTRAL|LATERAL_RANGE')
        self.assertEqual(receipt.kwargs['ai_context']['market_regime_policy'],'observe')
        self.assertIn('ENTRY_POLICY=GALA10_MODEL_THRESHOLD_V1',receipt.kwargs['motivo'])

