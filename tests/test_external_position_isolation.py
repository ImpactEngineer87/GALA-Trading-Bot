"""An external symbol cannot disable clean REAL symbols or be adopted by GALA."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from core.daily_loss_guard import DailyLossGuard
from core.position_state import PositionState
from core.real_accounting import RealAccountingGuard
from engine.bot_engine import ChocoletraBot


class ExternalIsolationTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        b=self.bot=ChocoletraBot.__new__(ChocoletraBot)
        b.config={};b._lock=threading.RLock();b._activo=True
        b.real_accounting_guard=RealAccountingGuard(self.root/'accounting.json')
        b._ticker_quarantine={'META':'EXTERNAL_BROKER_POSITION'}
        b._ticker_quarantine_health={'META':0};b._posiciones={}
        b.tickers=['AAPL','META'];b.account_number='offline-account'
        b._is_paper_ticker=Mock(return_value=False)
        b.execution_mode_for=Mock(return_value='REAL')
        b._real_tickers=Mock(return_value=b.tickers)
        b._has_real_tickers=Mock(return_value=True)
        b._daily_blocked=Mock(return_value=False)
        b._state_snapshot=Mock(return_value=SimpleNamespace(state=PositionState.OUT))
        b._assign_entry_lifecycle=Mock(return_value={})
        b._lifecycle_scope=lambda *args:nullcontext()
        b._registrar_senal_no_ejecutada=Mock();b._audit_event=Mock();b._emit_log=Mock()
        b._persist_broker_reconciliation=Mock();b._last_order_failures={}
        b._asegurar_token_trading=Mock(return_value=True)
        b._liquidados_recientes={};b._orden_huerfana_ciclos={}
        b._gala_audited_order_ids=Mock(return_value=set())
        b.schwab=SimpleNamespace(obtener_posiciones=Mock(return_value=[]),
            obtener_ordenes_abiertas=Mock(return_value=[]),
            obtener_ordenes_sesion=Mock(return_value=[]))
        b.ejecutor_hibrido=SimpleNamespace(cancelar_orden=Mock(),liquidar_posicion=Mock())
        b._adoptar_huerfana=Mock()
        b._ahora_et=Mock()

    def decision(self,ticker):
        return SimpleNamespace(ticker=ticker,tipo='BUY',operar=True,
            signal_id='source-'+ticker,propuesta=SimpleNamespace(ticker=ticker,tipo='BUY'))

    def test_external_symbol_does_not_disable_clean_symbol_entry(self):
        b=self.bot
        state=b._real_accounting_status()
        self.assertFalse(state['entry_blocked'])
        self.assertEqual(state['external_isolated_tickers'],['META'])
        b._abrir_operacion=Mock(return_value=True)
        b._abrir_o_revertir(self.decision('AAPL'),350.,None)
        b._abrir_operacion.assert_called_once()
        self.assertEqual(b._abrir_operacion.call_args.args[0].ticker,'AAPL')
        b.ejecutor_hibrido.cancelar_orden.assert_not_called()
        b.ejecutor_hibrido.liquidar_posicion.assert_not_called()

    def test_external_symbol_is_still_blocked_and_never_a_training_trade(self):
        b=self.bot;b._abrir_operacion=Mock(return_value=True)
        b._abrir_o_revertir(self.decision('META'),643.,None)
        b._abrir_operacion.assert_not_called()
        self.assertNotIn('META',b._posiciones)
        self.assertEqual(b._audit_event.call_args.kwargs['motivo'],'EXTERNAL_POSITION_ISOLATED')
        self.assertFalse(b._registrar_senal_no_ejecutada.call_args.kwargs['training_candidate'])

    def test_direct_entry_path_cannot_bypass_external_symbol_isolation(self):
        d=self.decision('META')
        self.assertFalse(self.bot._abrir_operacion(d,643.,None))
        self.assertEqual(d.execution_skip_reason,'EXTERNAL_POSITION_ISOLATED')
        self.bot._adoptar_huerfana.assert_not_called()

    def test_unknown_gala_execution_still_holds_every_real_symbol(self):
        b=self.bot
        b.real_accounting_guard.require_reconciliation('unknown-gala',ticker='TSLA',reason='unverified GALA fill')
        self.assertTrue(b._real_accounting_status()['entry_blocked'])
        d=self.decision('AAPL')
        self.assertFalse(b._abrir_operacion(d,350.,None))
        self.assertEqual(d.execution_skip_reason,'REAL_ACCOUNTING_UNRESOLVED')

    def test_pending_managed_manual_close_still_holds_globally(self):
        b=self.bot;b.manual_close_pending=SimpleNamespace(get=lambda key:{'p':{'ticker':'TSLA'}})
        status=b._real_accounting_status()
        self.assertTrue(status['entry_blocked']);self.assertTrue(status['manual_close_retrying'])
        self.assertEqual(status['manual_close_pending_tickers'],['TSLA'])

    def test_missing_pending_close_state_still_holds_globally(self):
        self.bot.manual_close_pending=SimpleNamespace(get=Mock(side_effect=OSError('unreadable')))
        self.assertTrue(self.bot._real_accounting_status()['entry_blocked'])

    def test_strategy_daily_risk_remains_priced_with_external_position(self):
        b=self.bot
        b.daily_loss_guard=DailyLossGuard({'kill_switch':{'daily_loss':{
            'enabled':True,'live_limit_usd':200,'state_file':str(self.root/'risk.json')}}},
            history_path=str(self.root/'history.csv'))
        b.daily_loss_guard.add_realized(False,-8.67)
        risk=b.daily_risk_snapshot()
        self.assertEqual(risk['net_pnl'],-8.67)
        self.assertEqual(risk['remaining_to_limit_usd'],191.33)
        self.assertEqual(risk['valuation_scope'],'GALA_MANAGED_POSITIONS')
        self.assertEqual(risk['real_accounting']['external_isolated_tickers'],['META'])

    def test_daily_loss_latch_still_blocks_clean_symbol(self):
        b=self.bot
        b.daily_loss_guard=DailyLossGuard({'kill_switch':{'daily_loss':{
            'enabled':True,'live_limit_usd':200,'state_file':str(self.root/'risk.json')}}},
            history_path=str(self.root/'history.csv'))
        b.daily_loss_guard.mark_triggered(False,-201.,0.,'daily loss limit')
        del b._daily_blocked
        d=self.decision('AAPL')
        self.assertFalse(b._abrir_operacion(d,350.,None))
        self.assertEqual(d.execution_skip_reason,'DAILY_LOSS_BLOCK')

    def test_two_fresh_flat_reads_release_only_the_affected_symbol(self):
        b=self.bot;b._ticker_quarantine['TSLA']='REAL_ACCOUNTING_UNRESOLVED'
        b._reconciliar_huerfanas()
        self.assertIn('META',b._ticker_quarantine)
        b._reconciliar_huerfanas()
        self.assertNotIn('META',b._ticker_quarantine)
        self.assertEqual(b._ticker_quarantine['TSLA'],'REAL_ACCOUNTING_UNRESOLVED')
        self.assertTrue(b._broker_reconciliation['flat'])

    def test_working_external_protection_prevents_release_and_is_never_canceled(self):
        b=self.bot
        b.schwab.obtener_ordenes_abiertas.return_value=[{
            'orderId':'external-stop','status':'WORKING','orderType':'STOP',
            'orderLegCollection':[{'instrument':{'symbol':'META'}}]}]
        for _ in range(3):b._reconciliar_huerfanas()
        self.assertIn('META',b._ticker_quarantine)
        b.ejecutor_hibrido.cancelar_orden.assert_not_called()
        self.assertFalse(b._broker_reconciliation['flat'])
        self.assertEqual(b._broker_reconciliation['open_order_symbols'],['META'])

    def test_current_broker_exposure_replaces_old_flat_banner_without_adoption(self):
        b=self.bot;b._ahora_et.return_value=SimpleNamespace(date=lambda:SimpleNamespace(isoformat=lambda:'2026-11-09'))
        b.schwab.obtener_posiciones.return_value=[{'symbol':'META','tipo':'BUY','cantidad':3,'precio':643.1299}]
        b._reconciliar_huerfanas()
        b._adoptar_huerfana.assert_not_called()
        self.assertFalse(b._broker_reconciliation['flat'])
        self.assertEqual(b._broker_reconciliation['positions'],[{'ticker':'META','side':'BUY','quantity':3}])
        self.assertFalse(b._real_accounting_status()['entry_blocked'])

    def test_periodic_position_read_does_not_clear_prior_reconciliation_failure(self):
        b=self.bot;b._broker_reconciliation_ok=False
        b._reconciliar_huerfanas()
        self.assertFalse(b._broker_reconciliation_ok)
        self.assertFalse(b._broker_reconciliation['ok'])

    def test_dashboard_exposes_symbol_hold_without_global_hold(self):
        import server
        b=self.bot;cfg={'live_trading':{'max_shares_per_order':10},'salida_parcial':{'activo':False}}
        rows=[{'ticker':t,'mode':'REAL','effective_mode':'REAL','shares':3} for t in b.tickers]
        with patch.object(server,'_ENGINE',b),patch.object(server,'_read_config_runtime',return_value=cfg),patch.object(
                server,'_ticker_execution_rows',return_value=rows),patch.object(
                server,'_engine_daily_risk_snapshot',return_value={'real_accounting':b._real_accounting_status()}),server.app.test_request_context('/api/ticker-execution'):
            result=server.get_ticker_execution().get_json()
        by_ticker={r['ticker']:r for r in result['tickers']}
        self.assertFalse(by_ticker['AAPL']['entry_blocked'])
        self.assertEqual(by_ticker['META']['entry_blockers'][0]['code'],'EXTERNAL_POSITION_ISOLATED')

    @unittest.skipUnless(shutil.which('node'),'Node required for actual dashboard rendering')
    def test_dashboard_partial_hold_and_global_loss_messages(self):
        source=Path('dashboard/index.html').read_text(encoding='utf-8')
        fn='function updateDailyRisk'+source.split('function updateDailyRisk',1)[1].split('\nfunction updateBrokerReconciliation',1)[0]
        script="const elements={};function el(id){return elements[id]??={style:{},classList:{toggle(){}}}};function money(v){return String(v)};"+fn+"\nupdateDailyRisk({real_accounting:{entry_blocked:false,external_isolated_tickers:['META']}});console.log(JSON.stringify([el('dailyRiskState').textContent,el('dailyRiskDetail').textContent]));updateDailyRisk({blocked:true,real_accounting:{entry_blocked:false,external_isolated_tickers:['META']}});console.log(JSON.stringify(el('dailyRiskState').textContent));"
        result=subprocess.check_output(['node','-e',script],text=True,encoding='utf-8').splitlines()
        state,detail=json.loads(result[0]);self.assertIn('META',state);self.assertIn('solo en esos tickers',detail)
        self.assertIn('PÉRDIDA DIARIA',json.loads(result[1]))


if __name__=='__main__':unittest.main()
