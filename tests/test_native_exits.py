"""Native 70/30 lifecycle tests against a deterministic broker, without networking."""
import copy
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from api.hybrid_order_executor import EjecutorHibridoDeOrdenes
from core.native_exits import NativeExits, execution_summary, new_plan, oco_body, read_group
from core.live_guard import validate_live_start_partitioned
from engine.bot_engine import ChocoletraBot


def order(side='BUY', quantity=10, partial=7):
    sign = 1 if side == 'BUY' else -1
    return SimpleNamespace(ticker='BA', tipo=side, cantidad=quantity,
        cantidad_parcial=partial, cantidad_resto=quantity-partial, precio_limite=100.,
        take_profit_parcial=100.+2*sign, take_profit=100.+4*sign, stop_loss=100.-2*sign)


class Broker:
    TRADER_URL = 'https://offline-broker'
    request_timeout_s = 1
    _detalle_desde_orden = staticmethod(EjecutorHibridoDeOrdenes._detalle_desde_orden)

    def __init__(self, side='BUY'):
        self.side, self.quantity = side, 10
        self.orders, self.posts = {}, []
        self._audit = Mock()
        self.cancel_confirmed = True
        self.unknown_post = False

    def _position_snapshot(self, account, ticker):
        return {'long': self.quantity if self.side == 'BUY' else 0,
                'short': self.quantity if self.side == 'SELL' else 0}

    def _submit_order_request(self, method, url, **kwargs):
        assert method == 'post'
        body = copy.deepcopy(kwargs['json']); self.posts.append(body)
        if self.unknown_post:
            raise TimeoutError('Ambiguous broker acknowledgement')
        oid = f'oco-{len(self.posts)}'; body['orderId'] = oid
        for n, child in enumerate(body['childOrderStrategies']):
            child.update(orderId=f'{oid}-{n}', status='WORKING', filledQuantity=0,
                quantity=child['orderLegCollection'][0]['quantity'])
        self.orders[oid] = body
        return SimpleNamespace(headers={'Location': f'/orders/{oid}'}, raise_for_status=lambda: None)

    def _buscar_orden_en_listado(self, account, oid):
        return copy.deepcopy(self.orders.get(oid))

    def ordenes_trabajando_ticker(self, account, ticker):
        return [c['orderId'] for o in self.orders.values() for c in o['childOrderStrategies']
                if c['status'] in {'WORKING', 'PENDING_CANCEL'}]

    def cancelar_orden_confirmada(self, account, oid, ticker=''):
        if not self.cancel_confirmed:
            return {'confirmado': False, 'broker_status': 'PENDING_CANCEL'}
        for root in self.orders.values():
            for child in root['childOrderStrategies']:
                if (oid == root['orderId'] or oid == child['orderId']) and child['status'] == 'WORKING':
                    child['status'] = 'CANCELED'
        return {'confirmado': True, 'broker_status': 'CANCELED'}

    def fill(self, root_id, kind, quantity=None, price=None):
        root = self.orders[root_id]
        for child in root['childOrderStrategies']:
            if child['orderType'] == kind:
                total = child['orderLegCollection'][0]['quantity']
                quantity = total if quantity is None else quantity
                price = float(child['price' if kind == 'LIMIT' else 'stopPrice']) if price is None else price
                child.update(status='FILLED' if quantity == total else 'WORKING', filledQuantity=quantity,
                    orderActivityCollection=[{'activityType':'EXECUTION','executionType':'FILL',
                        'executionId':child['orderId']+'-fill','executionLegs':[{
                        'price':price,'quantity':quantity,'time':datetime.now(timezone.utc).isoformat()}]}])
                self.quantity -= quantity
            elif child['status'] == 'WORKING':
                child['status'] = 'CANCELED'


class NativeExitTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('requests.sessions.Session.request',side_effect=AssertionError('Native exit tests are offline')))
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.broker = Broker()
        self.control = NativeExits(self.broker, 'offline-account', str(Path(self.tmp.name)/'plans.json'))
        self.plan = self.control.install(order(), 'entry-1')

    def test_each_slice_has_linked_target_and_stop_and_total_reservation_is_ten(self):
        self.assertEqual(len(self.broker.posts), 2)
        self.assertEqual(self.plan['status'], 'VERIFIED')
        allocations = []
        for body in self.broker.posts:
            self.assertEqual(body['orderStrategyType'], 'OCO')
            leaves = body['childOrderStrategies']
            self.assertEqual({c['orderType'] for c in leaves}, {'LIMIT', 'STOP'})
            amounts = [c['orderLegCollection'][0]['quantity'] for c in leaves]
            self.assertEqual(amounts[0], amounts[1]); allocations.append(amounts[0])
        self.assertEqual(allocations, [7, 3]); self.assertEqual(sum(allocations), 10)

    def test_repeated_registration_preserves_exact_parent_ids_without_another_post(self):
        again = self.control.install(order(), 'entry-1')
        self.assertEqual([g['root_id'] for g in again['groups']], ['oco-1','oco-2'])
        self.assertEqual(len(self.broker.posts), 2)
        with self.assertRaises(ValueError):self.control.install(order(quantity=9), 'entry-1')
        self.assertEqual(len(self.broker.posts), 2)

    def test_partial_target_repairs_only_unfilled_shares_and_keeps_original_fill_price(self):
        self.broker.fill('oco-1', 'LIMIT', quantity=2, price=102.125)
        self.control.refresh(self.plan)
        self.assertTrue(self.control.repair(self.plan))
        self.assertEqual([g['quantity'] for g in self.plan['groups']], [5,3])
        summary = execution_summary(self.plan)
        self.assertEqual(summary['quantity'],2);self.assertEqual(summary['avg_price'],102.125)
        self.assertEqual(sum(g['quantity'] for g in self.plan['groups']), self.broker.quantity)

    def test_unconfirmed_cancel_blocks_replacement_even_if_working_query_is_empty(self):
        self.broker.cancel_confirmed=False
        self.broker.ordenes_trabajando_ticker=Mock(return_value=[])
        self.assertFalse(self.control.repair(self.plan, stop=100))
        self.assertEqual(len(self.broker.posts),2)

    def test_fresh_position_mismatch_cannot_create_replacement_orders(self):
        self.broker.quantity=9
        self.assertFalse(self.control.repair(self.plan, stop=100))
        self.assertEqual(len(self.broker.posts),2)

    def test_missing_execution_price_cannot_become_realized_profit(self):
        self.broker.fill('oco-1','LIMIT',price=0)
        self.control.refresh(self.plan)
        self.assertIsNone(execution_summary(self.plan))
        self.assertFalse(self.control.repair(self.plan));self.assertEqual(len(self.broker.posts),2)

    def test_unknown_post_is_durable_and_not_retried(self):
        self.broker.quantity=10
        self.broker.orders.clear();self.broker.posts.clear();self.broker.unknown_post=True
        plan=self.control.install(order(),'entry-2')
        self.assertEqual(plan['status'],'RECONCILE')
        again=self.control.install(order(),'entry-2')
        self.assertFalse(self.control.repair(again))
        self.assertEqual(len(self.broker.posts),1)

    def test_malformed_child_identity_or_quantity_is_not_acknowledgement(self):
        for field,value in [('orderId','wrong-id'),('orderStrategyType','SINGLE')]:
            bad=copy.deepcopy(self.broker.orders['oco-1']);bad[field]=value
            with self.assertRaises(ValueError):read_group(bad,self.plan['groups'][0],'BA','BUY',self.broker._detalle_desde_orden)
        bad=copy.deepcopy(self.broker.orders['oco-1'])
        bad['childOrderStrategies'][0]['orderLegCollection'][0]['quantity']=10
        with self.assertRaises(ValueError):read_group(bad,self.plan['groups'][0],'BA','BUY',self.broker._detalle_desde_orden)

    def test_short_three_share_allocation_uses_buy_to_cover_two_plus_one(self):
        plan=new_plan(order('SELL',3,2),'short-entry')
        self.assertEqual([g['quantity'] for g in plan['groups']],[2,1])
        for g in plan['groups']:
            body=oco_body('BA','SELL',g['quantity'],g['target'],g['stop'])
            self.assertTrue(all(c['orderLegCollection'][0]['instruction']=='BUY_TO_COVER' for c in body['childOrderStrategies']))

    def test_invalid_split_or_price_geometry_is_rejected_before_submission(self):
        for bad in [order(quantity=7,partial=7),order()]:
            if bad.cantidad==10:bad.take_profit_parcial=bad.take_profit
            with self.assertRaises(ValueError):new_plan(bad,'entry-bad')
        for qty in [0,-1,3.5,float('nan')]:
            with self.assertRaises(ValueError):oco_body('BA','BUY',qty,102,98)

    def engine(self):
        b=ChocoletraBot.__new__(ChocoletraBot);b._lock=threading.RLock()
        b.account_number='offline-account';b._activo=True;b.breakeven_offset_pct=0
        b.ejecutor_hibrido=SimpleNamespace(native_exits=lambda account:self.control)
        b._is_paper_ticker=Mock(return_value=False);b._cantidad_real_broker=lambda *args:self.broker.quantity
        b._asegurar_token_trading=Mock(return_value=True);b._audit_event=Mock();b._emit_log=Mock()
        b._emit_trade=Mock();b._registrar_pnl_realizado_diario=Mock(return_value=True)
        b._evaluar_limite_perdida_diaria=Mock(return_value=False);b._cerrar_posicion=Mock()
        b._resumen={'parciales':0};b._lifecycle_scope=lambda fields:nullcontext()
        pos={'tipo':'BUY','orden':order(),'entrada':100.,'sl':98.,'sl_actual':98.,'tp1':102.,'tp_final':104.,
             'sl_order_id':'oco-1-1','tp1_order_id':'oco-1-0','cantidad_resto':3,'tp1_hit':False,'pnl_realizado':0.,
             'modo_resto':'breakeven','native_exit_plan':self.plan,'entry_integrity':{'entry_confirmed':True}}
        b._posiciones={'BA':pos}
        return b,pos

    def test_engine_records_tp1_once_and_reprotects_only_three_runner_shares(self):
        b,pos=self.engine();self.broker.fill('oco-1','LIMIT',price=102.125)
        b._gestionar_real('BA',pos,102.2)
        self.assertTrue(pos['tp1_hit']);self.assertEqual(pos['pnl_realizado'],14.875)
        self.assertEqual(b._resumen['parciales'],1)
        active = [g for g in self.plan['groups'] if sum(x.get('filled',0) for x in g['leaves'].values()) < g['quantity']]
        self.assertEqual([(g['stage'],g['quantity'],g['stop']) for g in active],[('FINAL',3,100.)])
        b._gestionar_real('BA',pos,102.2)
        self.assertEqual(b._resumen['parciales'],1);self.assertEqual(len(self.broker.posts),3)
        b._cerrar_posicion.assert_not_called()

    def test_engine_final_target_uses_verified_remainder_price_without_reselling_ten(self):
        b,pos=self.engine();self.broker.fill('oco-1','LIMIT')
        b._gestionar_real('BA',pos,102.2);self.broker.fill('oco-3','LIMIT',price=104.125)
        b._gestionar_real('BA',pos,104.2)
        proof=b._recuperar_cierre_stop_llenado('BA',pos,3)
        self.assertEqual(proof['filled'],3);self.assertEqual(proof['avg_price'],104.125)
        self.assertEqual(b._cerrar_posicion.call_args.kwargs['motivo'],'TP final nativo')
        self.assertEqual(len(self.broker.posts),3)

    def test_two_native_stops_close_without_fabricating_a_tp1_fill(self):
        b,pos=self.engine();self.broker.fill('oco-1','STOP');self.broker.fill('oco-2','STOP')
        b._gestionar_real('BA',pos,97.9)
        self.assertFalse(pos['tp1_hit']);self.assertEqual(b._resumen['parciales'],0)
        proof=b._recuperar_cierre_stop_llenado('BA',pos,10)
        self.assertEqual(proof['filled'],10);self.assertEqual(proof['avg_price'],98)
        self.assertEqual(b._cerrar_posicion.call_args.kwargs['motivo'],'SL nativo')

    def test_fill_race_at_both_targets_records_partial_then_final_once(self):
        b,pos=self.engine();self.broker.fill('oco-1','LIMIT');self.broker.fill('oco-2','LIMIT')
        b._gestionar_real('BA',pos,105)
        self.assertTrue(pos['tp1_hit']);self.assertEqual(b._resumen['parciales'],1)
        self.assertEqual(len(self.broker.posts),2)
        self.assertEqual(b._recuperar_cierre_stop_llenado('BA',pos,3)['avg_price'],104)

    def test_partial_repair_keeps_the_other_allocation_protected(self):
        self.broker.fill('oco-1','LIMIT',quantity=2)
        self.control.refresh(self.plan);self.assertTrue(self.control.repair(self.plan))
        runner=self.broker.orders['oco-2']['childOrderStrategies']
        self.assertTrue(all(c['status']=='WORKING' for c in runner))
        self.assertEqual(len(self.broker.posts),3)

    def test_pending_cancel_in_detail_cannot_become_replacement_permission(self):
        def pending(account,oid,ticker=''):
            for child in self.broker.orders['oco-2']['childOrderStrategies']:
                child['status']='PENDING_CANCEL'
            return {'confirmado':True,'broker_status':'NOT_WORKING'}
        self.broker.cancelar_orden_confirmada=pending
        self.broker.ordenes_trabajando_ticker=Mock(return_value=[])
        self.assertFalse(self.control.repair(self.plan,stop=100))
        self.assertEqual(len(self.broker.posts),2)

    def test_preflight_allows_linked_policy_but_legacy_unlinked_plan_stays_blocked(self):
        from tests.test_real_partial_exit_safety import _read_only_startup_config
        cfg=_read_only_startup_config();cfg['live_trading']['real_exit_policy']='LINKED_70_30'
        result=validate_live_start_partitioned(cfg,get_env=lambda key,default='':default,
            real_tickers=['TSLA'],ticker_settings={'TSLA':{'shares':3}})
        self.assertEqual(result['approved_real'],['TSLA']);self.assertEqual(result['global_errors'],[])
        cfg['live_trading']['real_exit_policy']='CONFIGURED'
        result=validate_live_start_partitioned(cfg,get_env=lambda key,default='':default,
            real_tickers=['TSLA'],ticker_settings={'TSLA':{'shares':3}})
        self.assertEqual(result['blocked_real'],['TSLA'])

    def test_changed_broker_stop_is_reconciled_without_a_blind_new_order(self):
        self.broker.orders['oco-1']['childOrderStrategies'][1]['stopPrice']='99.00'
        self.control.refresh(self.plan);self.assertEqual(self.plan['status'],'RECONCILE')
        self.assertEqual(len(self.broker.posts),2)

    def test_monitor_reuses_one_shared_order_snapshot_and_does_not_rewrite_unchanged_state(self):
        self.broker.native_order_snapshot=lambda account:copy.deepcopy(list(self.broker.orders.values()))
        self.broker._buscar_orden_en_listado=Mock(side_effect=AssertionError('Healthy snapshot needs no per-order GET'))
        path=next(self.control.path.glob('*.json'));modified=path.stat().st_mtime_ns
        self.control.refresh(self.plan);self.control.refresh(self.plan)
        self.assertEqual(path.stat().st_mtime_ns,modified)
        self.assertEqual(self.plan['status'],'VERIFIED')

    def test_forced_receipt_refresh_detects_a_fill_that_arrived_after_cancellation(self):
        self.control.cancel(self.plan);self.control.refresh(self.plan,force=True)
        self.broker.fill('oco-1','LIMIT',quantity=2,price=102.125)
        self.control.refresh(self.plan,force=True)
        self.assertEqual(execution_summary(self.plan)['quantity'],2)

    def test_broker_mutation_invalidates_the_monitors_native_order_snapshot(self):
        x=EjecutorHibridoDeOrdenes.__new__(EjecutorHibridoDeOrdenes)
        x._order_observation_local=threading.local();x._order_observation_lock=threading.Lock()
        x._order_observation_generation=0
        x._working_orders_snapshot=Mock(return_value=[{'orderId':'fresh'}])
        observed=[{'orderId':'old','status':'WORKING'}]
        with x.order_observation_scope(observed,account_number='offline-account'):
            self.assertIs(x.native_order_snapshot('offline-account'),observed)
            x._invalidate_order_observations()
            self.assertEqual(x.native_order_snapshot('offline-account'),[{'orderId':'fresh'}])
        x._working_orders_snapshot.assert_called_once()


class NativeAccountingTests(unittest.TestCase):
    from tests import test_real_accounting as fixtures
    setUp=fixtures.RealCloseAccountingTests.setUp
    engine=fixtures.RealCloseAccountingTests.engine
    last_close=fixtures.RealCloseAccountingTests.last_close

    def native_engine(self):
        bot=self.engine();pos=bot._posiciones['NKE'];broker=Broker()
        spec=order();spec.ticker='NKE';spec.precio_limite=37.66
        spec.stop_loss=36.66;spec.take_profit_parcial=38.66;spec.take_profit=39.66
        control=NativeExits(broker,bot.account_number,str(self.root/'native.json'))
        plan=control.install(spec,'entry-1')
        pos.update(orden=spec,native_exit_plan=plan,tipo='BUY',entrada=37.66,sl=36.66,sl_actual=36.66,
            sl_order_id='oco-1-1',tp1_order_id='oco-1-0',tp1=38.66,tp_final=39.66,modo_resto='fixed',cantidad_resto=3)
        bot._resumen={'parciales':0};bot.breakeven_offset_pct=0
        bot._lifecycle_scope=lambda fields:nullcontext()
        bot.ejecutor_hibrido.native_exits=lambda account:control
        bot.ejecutor_hibrido.cancelar_orden_confirmada.side_effect=broker.cancelar_orden_confirmada
        bot._cantidad_real_broker=lambda *args:broker.quantity
        bot._recuperar_cierre_stop_llenado=ChocoletraBot._recuperar_cierre_stop_llenado.__get__(bot)
        return bot,pos,broker,control

    def test_actual_close_accounts_native_tp1_and_final_once(self):
        bot,pos,broker,control=self.native_engine()
        broker.fill('oco-1','LIMIT');broker.fill('oco-2','LIMIT')
        self.assertTrue(bot._cerrar_posicion_impl('NKE',999,'TP final nativo'))
        self.assertTrue(pos['tp1_hit']);self.assertEqual(bot._resumen['parciales'],1)
        self.assertAlmostEqual(float(self.last_close()['pnl_usd']),13.)
        self.assertAlmostEqual(bot.daily_loss_guard.realized_pnl(False),13.)
        bot.ejecutor_hibrido.liquidar_posicion.assert_not_called()
        self.assertFalse(bot._real_accounting_status()['entry_blocked'])

    def test_native_stop_then_real_remainder_close_keeps_both_fill_prices(self):
        bot,pos,broker,control=self.native_engine();broker.fill('oco-1','STOP')
        def close(*args):
            broker.quantity=0
            return {'status':'OK','posicion_plana':True,'order_id':'remainder-close',
                'cantidad':3,'avg_price':37.595,'fill_price_verified':True}
        bot.ejecutor_hibrido.liquidar_posicion.side_effect=close
        self.assertTrue(bot._cerrar_posicion_impl('NKE',999,'SL nativo'))
        self.assertFalse(pos['tp1_hit'])
        self.assertAlmostEqual(float(self.last_close()['pnl_usd']),-7.195)
        self.assertAlmostEqual(bot.daily_loss_guard.realized_pnl(False),-7.195)
        self.assertFalse(bot._real_accounting_status()['entry_blocked'])

    def test_missing_native_fill_price_retains_unknown_accounting(self):
        bot,pos,broker,control=self.native_engine()
        broker.fill('oco-1','STOP',price=0);broker.fill('oco-2','STOP')
        bot._recuperar_cierre_manual_verificado=Mock(return_value={})
        self.assertTrue(bot._cerrar_posicion_impl('NKE',999,'SL nativo'))
        self.assertEqual(self.last_close()['pnl_usd'],'')
        self.assertTrue(bot._real_accounting_status()['entry_blocked'])

    def test_manual_close_after_a_partial_target_fill_reconciles_combined_risk(self):
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        from tests.test_manual_close_reconciliation import order as broker_order
        bot,pos,broker,control=self.native_engine()
        broker.fill('oco-1','LIMIT',quantity=2,price=38.66)
        now=datetime.now(ZoneInfo('America/New_York'))
        entry=broker_order('entry-1','BUY',37.66,now-timedelta(seconds=5))
        manual=broker_order('manual-close','SELL',37.595,now+timedelta(seconds=1),tag='API_TOS:CHART',order_type='LIMIT')
        for raw,qty in [(entry,10),(manual,8)]:
            raw['orderLegCollection'][0]['instrument']['symbol']='NKE'
            raw['orderLegCollection'][0]['quantity']=qty
            raw['quantity']=raw['filledQuantity']=qty
            raw['orderActivityCollection'][0]['executionLegs'][0]['quantity']=qty
        bot.schwab=Mock()
        bot.schwab.obtener_ordenes_sesion.side_effect=lambda account:[entry,manual]+copy.deepcopy(list(broker.orders.values()))
        broker.quantity=0
        self.assertTrue(bot._cerrar_posicion_impl('NKE',999,'manual close'))
        self.assertAlmostEqual(float(self.last_close()['pnl_usd']),1.48)
        self.assertAlmostEqual(bot.daily_loss_guard.realized_pnl(False),1.48)
        self.assertFalse(bot._real_accounting_status()['entry_blocked'])
        bot.ejecutor_hibrido.liquidar_posicion.assert_not_called()
        self.assertIsNone(bot.orquestador.al_cerrar.call_args.args[1])


if __name__=='__main__':unittest.main()
