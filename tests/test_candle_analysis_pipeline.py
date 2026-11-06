"""Real pipeline regressions: broker waits, causal gap analysis and late exits."""
from contextlib import nullcontext, contextmanager
from datetime import datetime
import threading
from types import SimpleNamespace
import csv
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import pandas as pd
from agents.contracts import DatosMultiTemporal
from core.symbol_work import SymbolWork, causal_snapshot
from engine.bot_engine import ChocoletraBot
from tests import test_gala10_threshold_entry_policy as fixtures


class SymbolWorkTests(unittest.TestCase):
    def test_blocked_symbol_cannot_block_another_and_pending_work_is_bounded(self):
        started, release, other = threading.Event(), threading.Event(), threading.Event()
        ran, discarded, failed = [], [], []
        def run(symbol, value):
            ran.append((symbol,value))
            if value == 1:
                started.set()
                if not release.wait(3): raise AssertionError('Test worker was not released')
            if symbol == 'BA': other.set()
        work = SymbolWork(2, run, lambda *a:discarded.append(a), lambda *a:failed.append(a))
        self.addCleanup(work.close)
        self.addCleanup(release.set)
        self.assertTrue(work.submit('TSLA',1));self.assertTrue(started.wait(2))
        work.submit('TSLA',2);work.submit('TSLA',3);work.submit('BA',4)
        self.assertTrue(other.wait(2))
        self.assertEqual(discarded,[('TSLA',2,'NEWER_CANDLE_ANALYZED')])
        release.set();work.close()
        # close cancels any pending snapshot instead of submitting stale work.
        self.assertFalse(work.submit('TSLA',5));self.assertFalse(failed)
        self.assertNotIn(('TSLA',2),ran)

    def test_pending_is_replaced_without_parallel_execution_for_same_symbol(self):
        entered, release, final = threading.Event(),threading.Event(),threading.Event()
        active, calls = set(), []
        def run(symbol,value):
            self.assertNotIn(symbol,active);active.add(symbol)
            try:
                calls.append(value)
                if value == 1:
                    entered.set();self.assertTrue(release.wait(3))
                if value == 3: final.set()
            finally: active.remove(symbol)
        errors=[];work=SymbolWork(3,run,lambda *a:None,lambda *a:errors.append(a))
        self.addCleanup(work.close);self.addCleanup(release.set)
        work.submit('TSLA',1);self.assertTrue(entered.wait(2))
        work.submit('TSLA',2);work.submit('TSLA',3);release.set()
        self.assertTrue(final.wait(2));work.close()
        self.assertEqual(calls,[1,3]);self.assertFalse(errors)

    def test_failure_does_not_lose_next_snapshot(self):
        first,release,done=threading.Event(),threading.Event(),threading.Event()
        def run(symbol,value):
            if value==1:
                first.set();release.wait(3);raise ValueError('offline failure')
            done.set()
        errors=[];work=SymbolWork(1,run,lambda *a:None,lambda *a:errors.append(a))
        self.addCleanup(work.close);self.addCleanup(release.set)
        work.submit('META',1);self.assertTrue(first.wait(2));work.submit('META',2);release.set()
        self.assertTrue(done.wait(2));work.close();self.assertEqual(len(errors),1)


class CandleAnalysisPipelineTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.ThresholdEntryPolicyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.addCleanup(fixture.tearDown)
        self.fixture=fixture
        bot=ChocoletraBot.__new__(ChocoletraBot)
        self.bot=bot
        bot._lock=threading.RLock();bot._activo=True;bot._posiciones={}
        bot._last_prices={};bot._last_price_times={};bot._ultima_vela_procesada={}
        bot._gala10_signal_bar={};bot._gala10_contexto={};bot._latest_gala_decisions={}
        bot.agentes_activos=True;bot.live_allow_short=True;bot.requerir_modelo_aprobado_en_real=True
        bot._is_paper_ticker=Mock(return_value=False)
        bot.orquestador=fixture.orchestrator()
        bot.ejecutor_hibrido=SimpleNamespace(secuencia_simbolo=lambda ticker:nullcontext())
        bot._ahora_et=Mock(return_value=datetime(2026,11,5,11,43,24))
        bot._obtener_mtf=Mock(return_value=fixture.data)
        bot._datos_micro_confirmados=Mock(side_effect=lambda d:d)
        bot._avanzar_aprendizaje_senales=Mock(return_value=[])
        bot.veto_observations=SimpleNamespace(update_bar=Mock(return_value=[]))
        for name in ['_emit_candles','_recover_ticker_quarantine','_quarantine_real_ticker','_emit_log',
                     '_gestionar_posiciones','_registrar_no_op','_registrar_veto_ia',
                     '_registrar_senal_no_ejecutada','_audit_event','_cerrar_posicion','_abrir_o_revertir']:
            setattr(bot,name,Mock())
        bot._evaluar_limite_perdida_diaria=Mock(return_value=False)
        bot._atr_micro=Mock(return_value=1.27346);bot._gsco_features=Mock(return_value={})
        bot._emit_signal=Mock(side_effect=lambda *a,**k:{'signal_id':str(bot._emit_signal.call_count)})
        bot.estrategia=SimpleNamespace(calcular=Mock(side_effect=self.calculate))
        self.sides={fixture.frame.index[-1]:'SELL'}

    def calculate(self, frame):
        result=frame.copy();result['señal']=[self.sides.get(x) for x in result.index]
        result['atr']=1.27346
        return result

    def test_analysis_returns_and_records_probability_while_order_worker_waits(self):
        entered,release=threading.Event(),threading.Event()
        self.bot._gestionar_posiciones.side_effect=lambda *a:(entered.set(),release.wait(3))
        work=SymbolWork(1,self.bot._run_ticker_work,self.bot._discard_ticker_work,self.bot._ticker_work_failed)
        self.bot._ticker_work=work
        self.addCleanup(work.close);self.addCleanup(release.set)
        self.bot._procesar_ticker('TSLA')
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.bot._emit_signal.call_args.kwargs['ai_decision'],'APPROVED')
        self.assertAlmostEqual(self.bot._emit_signal.call_args.args[4],.802)
        self.bot._abrir_o_revertir.assert_not_called()
        release.set()
        work.close()
        self.bot._abrir_o_revertir.assert_called_once()

    def test_every_intermediate_signal_gets_actual_inference_and_no_old_order(self):
        frame=self.fixture.frame
        self.bot._ultima_vela_procesada['TSLA']=frame.index[-5]
        self.sides={frame.index[-4]:'BUY',frame.index[-2]:'SELL',frame.index[-1]:'BUY'}
        self.bot._procesar_ticker('TSLA')
        calls=self.bot._emit_signal.call_args_list
        self.assertEqual([c.kwargs['ai_decision'] for c in calls],['LATE_ANALYZED','LATE_ANALYZED','APPROVED'])
        self.assertTrue(all(c.args[4]==.802 for c in calls))
        self.assertTrue(all(c.kwargs['ai_context']['raw_probability']==.8023396134376526 for c in calls))
        self.bot._abrir_o_revertir.assert_called_once()
        self.assertEqual(self.bot._registrar_senal_no_ejecutada.call_count,2)
        self.bot._procesar_ticker('TSLA')
        self.assertEqual(self.bot._emit_signal.call_count,3)

    def test_delayed_score_cannot_change_when_later_prices_change(self):
        bar=self.fixture.frame.index[-3]
        data=self.fixture.data
        original=causal_snapshot(data,bar+pd.Timedelta(minutes=1))
        altered=self.fixture.frame.copy();altered.loc[altered.index>bar,'close']=99999
        after=causal_snapshot(DatosMultiTemporal('TSLA',{'1m':altered}),bar+pd.Timedelta(minutes=1))
        pd.testing.assert_frame_equal(original.tf('1m'),after.tf('1m'))

    def test_macro_snapshot_excludes_forming_and_future_bars(self):
        now=pd.Timestamp('2026-11-05T16:43:00Z')
        frame=self.fixture.frame.copy()
        data=DatosMultiTemporal('TSLA',{'1m':frame,'1H':frame.copy(),'1D':frame.copy()})
        snap=causal_snapshot(data,now)
        self.assertEqual(snap.tf('1m').index[-1],now-pd.Timedelta(minutes=1))
        self.assertLessEqual(snap.tf('1H').index[-1]+pd.Timedelta(minutes=30),now)
        self.assertIsNone(snap.tf('1D'))

    def test_latest_opposite_analysed_signal_closes_old_position_on_heartbeat_only(self):
        frame=self.fixture.frame
        self.bot._ultima_vela_procesada['TSLA']=frame.index[-4]
        self.sides={frame.index[-2]:'SELL'}
        self.bot._posiciones['TSLA']={'tipo':'BUY','decision':SimpleNamespace(signal_bar_timestamp=frame.index[-5])}
        self.bot._procesar_ticker('TSLA')
        self.bot._cerrar_posicion.assert_called_once()
        self.assertIn('evaluación demorada',self.bot._cerrar_posicion.call_args.kwargs['motivo'])
        self.bot._abrir_o_revertir.assert_not_called()

    def test_newer_opposite_event_cannot_close_a_position_opened_after_it(self):
        frame=self.fixture.frame
        self.bot._ultima_vela_procesada['TSLA']=frame.index[-4]
        self.sides={frame.index[-2]:'SELL'}
        self.bot._posiciones['TSLA']={'tipo':'BUY','decision':SimpleNamespace(signal_bar_timestamp=frame.index[-1])}
        self.bot._procesar_ticker('TSLA')
        self.bot._cerrar_posicion.assert_not_called()
        self.bot._abrir_o_revertir.assert_not_called()

    def test_newer_direction_prevents_replaying_an_older_opposite_close(self):
        frame=self.fixture.frame
        self.bot._ultima_vela_procesada['TSLA']=frame.index[-5]
        self.sides={frame.index[-3]:'SELL',frame.index[-2]:'BUY'}
        self.bot._posiciones['TSLA']={'tipo':'BUY','decision':SimpleNamespace(signal_bar_timestamp=frame.index[-5])}
        self.bot._procesar_ticker('TSLA')
        self.bot._cerrar_posicion.assert_not_called()
        self.bot._abrir_o_revertir.assert_not_called()

    def test_ordinary_model_veto_still_closes_existing_opposite_position(self):
        self.bot.orquestador=self.fixture.orchestrator(.2)
        self.bot._posiciones['TSLA']={'tipo':'BUY'}
        self.bot._procesar_ticker('TSLA')
        self.bot._cerrar_posicion.assert_called_once()
        self.bot._abrir_o_revertir.assert_not_called()
        self.assertEqual(self.bot._emit_signal.call_args.kwargs['ai_decision'],'VETO_CLOSE_ONLY')

    def test_stopped_worker_cannot_submit_or_manage_positions(self):
        self.bot._activo=False
        decision=SimpleNamespace(operar=True,ticker='TSLA',tipo='SELL',signal_id='s')
        self.bot._run_ticker_work('TSLA',(349,self.fixture.frame,decision))
        self.bot._gestionar_posiciones.assert_not_called();self.bot._abrir_o_revertir.assert_not_called()
        self.assertEqual(self.bot._audit_event.call_args.kwargs['motivo'],'ENGINE_STOPPED')

    def test_superseded_snapshot_cannot_manage_or_submit_from_old_prices(self):
        frame=self.fixture.frame
        self.bot._ultima_vela_procesada['TSLA']=frame.index[-1]
        decision=SimpleNamespace(operar=True,ticker='TSLA',tipo='SELL',signal_id='s')
        self.bot._run_ticker_work('TSLA',(349,frame.iloc[:-1],decision))
        self.bot._gestionar_posiciones.assert_not_called();self.bot._abrir_o_revertir.assert_not_called()
        self.assertEqual(self.bot._audit_event.call_args.kwargs['motivo'],'NEWER_CANDLE_ANALYZED')

    def test_stop_drains_running_work_before_allowing_any_new_entry(self):
        entered,release=threading.Event(),threading.Event()
        self.bot._gestionar_posiciones.side_effect=lambda *a:(entered.set(),release.wait(3))
        work=SymbolWork(1,self.bot._run_ticker_work,self.bot._discard_ticker_work,self.bot._ticker_work_failed)
        self.bot._ticker_work=work
        self.addCleanup(work.close);self.addCleanup(release.set)
        self.bot._procesar_ticker('TSLA');self.assertTrue(entered.wait(2))
        self.bot._activo=False
        release.set();self.bot._close_ticker_work()
        self.bot._abrir_o_revertir.assert_not_called()
        self.assertEqual(self.bot._audit_event.call_args.kwargs['motivo'],'ENGINE_STOPPED')

    def test_parallel_reconciliation_cannot_cancel_protection_for_new_tracking(self):
        bot=self.bot;bot.account_number='test-hash';bot.tickers=['BA']
        bot._has_real_tickers=Mock(return_value=True);bot._asegurar_token_trading=Mock(return_value=True)
        order={'orderId':'old-owned-stop','orderLegCollection':[{'instrument':{'symbol':'BA'}}]}
        bot.schwab=SimpleNamespace(obtener_posiciones=Mock(return_value=[]),
            obtener_ordenes_abiertas=Mock(return_value=[order]))
        bot._orden_huerfana_ciclos={'old-owned-stop':1};bot._liquidados_recientes={}
        bot._owned_working_ids=Mock(return_value={'old-owned-stop'})
        @contextmanager
        def lease(symbol):
            bot._posiciones[symbol]={'tipo':'BUY','position_id':'new-owned-position'}
            yield
        bot.ejecutor_hibrido.secuencia_simbolo=lease
        with patch('engine.bot_engine.cancelar_orden_compatible') as cancel:
            bot._reconciliar_huerfanas()
        cancel.assert_not_called()

    def test_delayed_analysis_uses_original_close_for_supplemental_learning_only(self):
        from agents.signal_learning_store import SignalLearningStore
        folder=Path(self.enterContext(tempfile.TemporaryDirectory()))
        store=SignalLearningStore(str(folder/'signals.csv'))
        self.bot.signal_learning=store
        del self.bot._registrar_senal_no_ejecutada
        frame=self.fixture.frame
        self.bot._ultima_vela_procesada['TSLA']=frame.index[-4]
        self.sides={frame.index[-2]:'SELL'}
        self.bot._procesar_ticker('TSLA')
        with (folder/'signals.csv').open(encoding='utf-8-sig') as stream:
            rows=list(csv.DictReader(stream))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['source_kind'],'EXECUTION_MISSED_CAUSAL_ANALYSIS')
        self.assertEqual(rows[0]['signal_bar_utc'],frame.index[-2].isoformat())
        self.assertFalse((folder/'agentes_feedback.csv').exists())
        self.bot._abrir_o_revertir.assert_not_called()
