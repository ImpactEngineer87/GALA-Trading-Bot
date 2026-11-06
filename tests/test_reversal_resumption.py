"""A deferred reversal must be re-scored, tied to its position and executed once."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
import unittest
import pandas as pd

from core.position_state import PositionState
from tests import test_candle_analysis_pipeline as pipeline_fixtures
from tests import test_real_accounting as accounting_fixtures


class DeferredReversalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = pipeline_fixtures.CandleAnalysisPipelineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bot = self.fixture.bot
        del self.bot._abrir_o_revertir
        self.bot.reversion_activa = True
        self.bot._daily_blocked = Mock(return_value=False)
        self.bot._state_snapshot = Mock(return_value=SimpleNamespace(state=PositionState.LONG))
        self.bot._assign_entry_lifecycle = Mock(return_value={})
        self.bot._reservar_slot = Mock(return_value=True)
        self.bot._liberar_reserva = Mock()
        self.bot._lifecycle_scope = lambda *a:nullcontext()
        self.bot._resumen = {'reversiones':0}
        self.position = {'tipo':'BUY','position_id':'original-long'}
        self.bot._posiciones['TSLA'] = self.position
        self.bot._cerrar_posicion = Mock(return_value=False)
        self.bot._abrir_operacion = Mock(side_effect=self.open)
        self.bot._procesar_ticker('TSLA')
        self.assertIn('TSLA', self.bot._pending_reversal_intents)
        self.bot._abrir_operacion.assert_not_called()

    def open(self, decision, price, frame):
        self.bot._posiciones['TSLA'] = {'tipo':decision.tipo,'position_id':'new-short'}
        return True

    def close(self, *args, **kwargs):
        self.bot._posiciones.pop('TSLA', None)
        self.bot._pending_reversal_intents['TSLA']['prior_closed'] = True
        return True

    def next_bar(self):
        data = self.fixture.fixture.data
        frame = data.velas['1m']
        last = frame.iloc[-1:].copy()
        last.index = last.index + pd.Timedelta(minutes=1)
        last['close'] = 350.
        data.velas['1m'] = pd.concat([frame,last])
        return data.velas['1m']

    def test_quote_deferred_close_resumes_with_fresh_model_price_and_only_one_entry(self):
        parent = self.bot._pending_reversal_intents['TSLA']['source_signal_id']
        self.next_bar()
        self.bot.orquestador = self.fixture.fixture.orchestrator(.67)
        self.bot._cerrar_posicion.side_effect = self.close
        self.bot._procesar_ticker('TSLA')
        self.bot._abrir_operacion.assert_called_once()
        decision,price,frame = self.bot._abrir_operacion.call_args.args
        self.assertEqual(price,350.)
        self.assertEqual(decision.propuesta.precio_entrada,350.)
        self.assertEqual(decision.prob_meta,.67)
        self.assertEqual(decision.reversal_parent_signal_id,parent)
        context = self.bot._emit_signal.call_args.kwargs['ai_context']
        self.assertEqual(context['execution_intent'],{'kind':'REVERSAL_RETRY','parent_signal_id':parent})
        self.assertNotIn('TSLA',self.bot._pending_reversal_intents)
        self.bot._procesar_ticker('TSLA')
        self.bot._abrir_operacion.assert_called_once()

    def test_retry_below_threshold_can_close_but_cannot_open(self):
        self.next_bar()
        self.bot.orquestador = self.fixture.fixture.orchestrator(.2)
        self.bot._cerrar_posicion.side_effect = self.close
        self.bot._procesar_ticker('TSLA')
        self.bot._abrir_operacion.assert_not_called()
        self.assertEqual(self.bot._emit_signal.call_args.kwargs['ai_decision'],'VETO_CLOSE_ONLY')
        self.assertNotIn('TSLA',self.bot._pending_reversal_intents)

    def test_new_master_direction_cancels_old_intent(self):
        frame=self.next_bar()
        self.fixture.sides[frame.index[-1]]='BUY'
        self.bot._procesar_ticker('TSLA')
        self.assertNotIn('TSLA',self.bot._pending_reversal_intents)
        self.bot._abrir_operacion.assert_not_called()

    def test_manual_or_unverified_prior_close_cannot_restart_old_reversal(self):
        self.next_bar()
        self.bot._posiciones.pop('TSLA')
        self.bot._procesar_ticker('TSLA')
        self.bot._abrir_operacion.assert_not_called()
        self.assertNotIn('TSLA',self.bot._pending_reversal_intents)

    def test_previously_analyzed_retry_is_rejected_if_position_changes_before_execution(self):
        frame=self.next_bar()
        data=self.fixture.fixture.data
        decision=self.bot._analizar_reversion_pendiente('TSLA',data,350.,frame)
        self.bot._ultima_vela_procesada['TSLA']=frame.index[-1]
        self.bot._posiciones['TSLA']={'tipo':'BUY','position_id':'different-entry'}
        self.bot._run_ticker_work('TSLA',(350.,frame,decision))
        self.bot._abrir_operacion.assert_not_called()
        self.assertEqual(self.bot._audit_event.call_args.kwargs['motivo'],'REVERSAL_INTENT_SUPERSEDED')

    def test_same_bar_does_not_create_repeated_retry_ratings(self):
        data=self.fixture.fixture.data
        self.assertIsNone(self.bot._analizar_reversion_pendiente('TSLA',data,349.,data.tf('1m')))
        self.assertEqual(self.bot._emit_signal.call_count,1)

    def test_veto_after_verified_close_cancels_waiting_reverse_entry(self):
        self.close()
        self.next_bar()
        self.bot.orquestador=self.fixture.fixture.orchestrator(.2)
        self.bot._procesar_ticker('TSLA')
        self.bot._abrir_operacion.assert_not_called()
        self.assertNotIn('TSLA',self.bot._pending_reversal_intents)

    def test_superseding_candle_during_close_waits_for_fresh_analysis(self):
        frame=self.next_bar()
        def closing(*args,**kwargs):
            self.close()
            self.bot._ultima_vela_procesada['TSLA']=frame.index[-1]+pd.Timedelta(minutes=1)
            return True
        self.bot._cerrar_posicion.side_effect=closing
        self.bot._procesar_ticker('TSLA')
        self.bot._abrir_operacion.assert_not_called()
        self.assertTrue(self.bot._pending_reversal_intents['TSLA']['prior_closed'])


class OppositePendingCancellationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=pipeline_fixtures.CandleAnalysisPipelineTests()
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.bot=self.fixture.bot
        self.bot.orquestador=self.fixture.fixture.orchestrator(.2)
        self.pos={'tipo':'BUY','pendiente':True,'decision':SimpleNamespace(
            signal_bar_timestamp=self.fixture.fixture.frame.index[-5])}
        self.bot._posiciones['TSLA']=self.pos
        self.bot._cancelar_pendiente=Mock(return_value=True)

    def test_ai_veto_of_new_side_still_cancels_opposite_pending_entry(self):
        self.bot._procesar_ticker('TSLA')
        self.bot._cancelar_pendiente.assert_called_once_with('TSLA')
        self.bot._abrir_o_revertir.assert_not_called()
        self.bot._cerrar_posicion.assert_not_called()

    def test_filled_entry_winning_cancel_race_is_closed_without_opening_vetoed_side(self):
        def adopted(symbol):
            self.pos['pendiente']=False
            return False
        self.bot._cancelar_pendiente.side_effect=adopted
        self.bot._procesar_ticker('TSLA')
        self.bot._cerrar_posicion.assert_called_once()
        self.bot._abrir_o_revertir.assert_not_called()

    def test_delayed_opposite_retries_cancel_on_heartbeat_without_replaying_entry(self):
        frame=self.fixture.fixture.frame
        self.bot._ultima_vela_procesada['TSLA']=frame.index[-4]
        self.fixture.sides={frame.index[-2]:'SELL'}
        self.bot._procesar_ticker('TSLA')
        self.bot._cancelar_pendiente.assert_called_once_with('TSLA')
        self.bot._abrir_o_revertir.assert_not_called()


class ReversalCloseProofTests(unittest.TestCase):
    def setUp(self):
        self.fixture=accounting_fixtures.RealCloseAccountingTests()
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)

    def test_only_verified_intended_close_enables_followup_entry(self):
        for verified in (True,False):
            with self.subTest(verified=verified):
                bot=self.fixture.engine(entry_verified=verified)
                bot._activo=True
                pos=bot._posiciones['NKE']
                bot._pending_reversal_intents={'NKE':{'position':pos,'prior_closed':False}}
                self.assertTrue(bot._cerrar_posicion_impl('NKE',999.,'reversión'))
                if verified:
                    self.assertTrue(bot._pending_reversal_intents['NKE']['prior_closed'])
                else:
                    self.assertNotIn('NKE',bot._pending_reversal_intents)


if __name__=='__main__': unittest.main()
