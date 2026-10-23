"""Exercise real close orchestration against an in-memory, offline broker.

Quoted prices are only order limits. The broker fixture supplies the fills.
No test contacts Schwab, submits live orders, or touches production state.
"""
import copy
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from api.hybrid_order_executor import EjecutorHibridoDeOrdenes
from core.execution_v2 import SmartExit
from core.real_accounting import RealAccountingGuard
import test_real_accounting as accounting_fixtures


class LimitOnlyExitTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.remaining = 5
        self.side = 'BUY'
        self.orders = {}
        self.outcomes = []
        self.bodies = []
        self.ex = Mock()
        self.ex.TRADER_URL = 'https://offline.invalid'
        self.ex.request_timeout_s = self.ex.flat_timeout_s = self.ex.fill_timeout_s = 1
        self.ex._quote_provider = lambda ticker: {
            'bid': 100, 'ask': 100.1, 'quote_time': time.time(), 'delayed': False}
        self.ex._position_snapshot.side_effect = lambda *a: {
            'long': self.remaining if self.side == 'BUY' else 0,
            'short': self.remaining if self.side == 'SELL' else 0}
        self.ex.ordenes_trabajando_ticker.return_value = []
        self.ex.orden_trabajando.return_value = False
        self.ex.detalle_orden.side_effect = lambda account, oid: dict(self.orders.get(oid, {}))
        self.ex._submit_order_request.side_effect = self.submit
        self.bot = SmartExit(self.ex, {
            'pending_path': str(self.root / 'pending.json'),
            'history_path': str(self.root / 'history.jsonl'),
            'market_fallback': True})
        self.key = hashlib.sha256(b'account').hexdigest()[:16] + ':NKE'

    def submit(self, method, url, json, **kwargs):
        self.bodies.append(copy.deepcopy(json))
        outcome = self.outcomes.pop(0)
        oid = str(len(self.bodies))
        self.orders[oid] = {'status': 'CANCELED', 'filled': 0, 'avg_price': 0, **outcome}
        self.remaining -= int(self.orders[oid]['filled'])
        response = Mock()
        response.headers = {'Location': '/orders/' + oid}
        return response

    def close(self, reason='reversión', position='position-1'):
        return self.bot.close('NKE', self.side, 5, 'account', reason, 777,
                              position_id=position)

    def test_full_long_close_is_limit_and_uses_actual_fill_not_signal_or_quote(self):
        self.outcomes = [{'status': 'FILLED', 'filled': 5, 'avg_price': 99.983}]
        result = self.close()
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['avg_price'], 99.983)
        self.assertEqual(self.bodies[0]['orderType'], 'LIMIT')
        self.assertEqual(self.bodies[0]['orderLegCollection'][0]['instruction'], 'SELL')
        self.assertEqual(self.bodies[0]['price'], '99.95')
        self.ex.esperar_estado_orden.assert_called_once()
        self.assertEqual(self.ex.esperar_estado_orden.call_args.kwargs['timeout_s'], 10)

    def test_short_close_is_buy_to_cover_limit(self):
        self.side = 'SELL'
        self.outcomes = [{'status': 'FILLED', 'filled': 5, 'avg_price': 100.123}]
        self.close()
        self.assertEqual(self.bodies[0]['orderType'], 'LIMIT')
        self.assertEqual(self.bodies[0]['orderLegCollection'][0]['instruction'], 'BUY_TO_COVER')
        self.assertEqual(self.bodies[0]['price'], '100.16')

    def test_unfilled_limit_never_falls_back_even_with_old_flag_true(self):
        self.outcomes = [{}]
        result = self.close()
        self.assertEqual(result['status'], 'LIMIT_DEFERRED')
        self.assertTrue(result['safe_to_reprotect'])
        self.assertEqual(result['remaining_quantity'], 5)
        self.assertEqual(len(self.bodies), 1)
        self.assertIsNotNone(self.bot.pending.get(self.key))
        self.assertFalse(result['posicion_plana'])

    def test_retry_uses_a_fresh_quote_and_another_limit(self):
        self.outcomes = [{}, {'status': 'FILLED', 'filled': 5, 'avg_price': 102}]
        self.close()
        self.ex._quote_provider = lambda ticker: {
            'bid': 102, 'ask': 103, 'quote_time': time.time()}
        result = self.close()
        self.assertEqual(result['avg_price'], 102)
        self.assertEqual([x['orderType'] for x in self.bodies], ['LIMIT', 'LIMIT'])
        self.assertEqual(self.bodies[-1]['price'], '101.94')

    def test_missing_delayed_stale_crossed_or_future_quote_cannot_start_market(self):
        for quote in ({}, {'bid': 100, 'ask': 101, 'quote_time': time.time()-60},
                      {'bid': 100, 'ask': 101, 'quote_time': time.time(), 'delayed': True},
                      {'bid': 102, 'ask': 101, 'quote_time': time.time()},
                      {'bid': 100, 'ask': 101, 'quote_time': time.time()+60}):
            self.ex._quote_provider = lambda ticker, q=quote: q
            result = self.close()
            self.assertEqual(result['status'], 'LIMIT_DEFERRED')
        self.assertEqual(self.bodies, [])

    def test_partial_then_final_limit_has_exact_weighted_broker_price(self):
        self.outcomes = [{'filled': 2, 'avg_price': 99.97},
                         {'status': 'FILLED', 'filled': 3, 'avg_price': 100.02}]
        partial = self.close()
        self.assertEqual(partial['filled_quantity'], 2)
        self.assertEqual(partial['remaining_quantity'], 3)
        self.assertEqual(partial['partial_avg_price'], 99.97)
        result = self.close()
        self.assertEqual(result['cantidad'], 5)
        self.assertAlmostEqual(result['avg_price'], 100)
        self.assertEqual(self.bodies[-1]['orderLegCollection'][0]['quantity'], 3)
        self.assertEqual(len(result['execution_details']['legs']), 2)

    def test_unknown_cancel_never_permits_stop_replacement_or_duplicate_close(self):
        self.outcomes = [{'status': 'WORKING'}]
        self.ex.cancelar_orden_confirmada.return_value = {'confirmado': False}
        self.ex.orden_trabajando.return_value = True
        result = self.close()
        self.assertNotIn('safe_to_reprotect', result)
        self.assertEqual(self.close()['status'], 'RECONCILIAR')
        self.assertEqual(len(self.bodies), 1)

    def test_cancellation_get_recovers_actual_partial_price(self):
        self.outcomes = [{'status': 'WORKING', 'filled': 2, 'avg_price': 100.03}]
        def cancel(account, oid, **kwargs):
            self.orders[oid]['status'] = 'CANCELED'
            return {'broker_status': 'CANCELED', 'confirmado': True, 'filled': 2}
        self.ex.cancelar_orden_confirmada.side_effect = cancel
        result = self.close()
        self.assertEqual(result['partial_avg_price'], 100.03)
        self.assertTrue(result['partial_price_verified'])

    def test_emergency_after_partial_uses_current_remainder_not_original_quantity(self):
        self.outcomes = [{'filled': 2, 'avg_price': 100},
                         {'status': 'FILLED', 'filled': 3, 'avg_price': 99}]
        self.close()
        self.bot.config['market_fallback'] = False
        result = self.close('kill switch')
        self.assertEqual(result['status'], 'OK')
        self.assertEqual([b['orderType'] for b in self.bodies], ['LIMIT', 'MARKET'])
        self.assertEqual(self.bodies[-1]['orderLegCollection'][0]['quantity'], 3)
        self.assertAlmostEqual(result['avg_price'], 99.4)

    def test_emergency_no_quote_still_can_close(self):
        self.ex._quote_provider = None
        self.outcomes = [{'status': 'FILLED', 'filled': 5, 'avg_price': 99}]
        self.assertEqual(self.close('sin proteccion')['status'], 'OK')
        self.assertEqual(self.bodies[0]['orderType'], 'MARKET')

    def test_unverified_partial_price_is_never_supplied_by_the_limit(self):
        self.outcomes = [{'filled': 2, 'avg_price': 999, 'fill_price_verified': False}]
        result = self.close()
        self.assertFalse(result['partial_price_verified'])
        self.assertIsNone(result['partial_avg_price'])

    def test_no_stop_permission_with_unknown_or_conflicting_working_orders(self):
        for working in (None, ['protective-order']):
            self.outcomes = [{}]
            self.ex.ordenes_trabajando_ticker.return_value = working
            result = self.close()
            self.assertNotIn('safe_to_reprotect', result)
        self.assertEqual(self.bodies, [])

    def test_accepted_exit_without_order_identity_never_submits_a_second_order(self):
        self.outcomes = [{}]
        original = self.submit
        def without_identity(*args, **kwargs):
            response = original(*args, **kwargs)
            response.headers = {}
            return response
        self.ex._submit_order_request.side_effect = without_identity
        first = self.close()
        second = self.close()
        self.assertEqual(first['status'], 'RECONCILIAR')
        self.assertEqual(second['status'], 'RECONCILIAR')
        self.assertNotIn('safe_to_reprotect', first)
        self.assertEqual(len(self.bodies), 1)

    def test_pending_other_position_cannot_be_reused_for_a_new_trade(self):
        self.outcomes = [{}]
        self.close()
        self.assertEqual(self.close(position='different')['status'], 'RECONCILIAR')
        self.assertEqual(len(self.bodies), 1)


class ProtectedEngineCloseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = accounting_fixtures.RealCloseAccountingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bot = self.fixture.engine()
        self.pos = self.bot._posiciones['NKE']
        self.pos.update(sl_actual=37.5, sl_order_id='old-stop', tp1_order_id='old-tp')
        self.ex = self.bot.ejecutor_hibrido
        self.ex.preparar_cierre.return_value = {'ready': True}
        self.ex.ordenes_trabajando_ticker.return_value = []
        self.ex.cancelar_orden_confirmada.return_value = {'confirmado': True, 'broker_status': 'CANCELED'}
        self.ex.colocar_salida.return_value = {'status': 'RECONCILIAR', 'order_id': 'new-stop'}

    def deferred(self, filled=0):
        return {'status': 'LIMIT_DEFERRED', 'safe_to_reprotect': True,
            'remaining_quantity': 10-filled, 'filled_quantity': filled,
            'partial_avg_price': 37.60 if filled else None,
            'partial_price_verified': True,
            'execution_details': {'execution_id': 'exit-intent', 'position_id': 'position-1',
                'quantity': 10, 'side': 'BUY', 'legs': [
                    {'order_id': 'limit-1', 'status': 'CANCELED', 'filled': filled, 'avg_price': 37.60}]}}

    def test_no_quote_preserves_both_protections_and_sends_no_exit(self):
        self.ex.preparar_cierre.return_value = {'ready': False}
        self.assertFalse(self.bot._cerrar_posicion_impl('NKE', 999, 'reversión'))
        self.ex.liquidar_posicion.assert_not_called()
        self.ex.cancelar_orden_confirmada.assert_not_called()
        self.assertEqual(self.pos['sl_order_id'], 'old-stop')

    def test_canceled_unfilled_limit_restores_one_stop_and_retains_position(self):
        self.ex.liquidar_posicion.return_value = self.deferred()
        self.assertFalse(self.bot._cerrar_posicion_impl('NKE', 999, 'reversión'))
        self.ex.colocar_salida.assert_called_once_with('NKE', 'BUY', 10, 37.5, 'STOP', 'offline-account')
        self.assertEqual(self.pos['sl_order_id'], 'new-stop')
        self.assertTrue(self.pos['sl_reconcile_pending'])
        self.assertFalse(self.pos['tp1_hit'])
        self.assertIs(self.bot._posiciones['NKE'], self.pos)
        self.bot.orquestador.al_cerrar.assert_not_called()
        self.assertFalse((self.fixture.root/'history.csv').exists())

    def test_flat_broker_without_quote_can_reconcile_but_cannot_invent_a_price(self):
        self.ex.preparar_cierre.return_value = {'ready': False}
        self.bot._cantidad_real_broker.return_value = 0
        self.assertTrue(self.bot._cerrar_posicion_impl('NKE', 999, 'reversión'))
        self.ex.liquidar_posicion.assert_not_called()
        self.assertEqual(self.fixture.last_close()['pnl_usd'], '')
        self.assertTrue(self.bot._real_accounting_status()['blocked'])

    def test_partial_limit_protects_only_broker_remaining_and_blocks_reversal(self):
        self.ex.liquidar_posicion.return_value = self.deferred(4)
        self.bot._cantidad_real_broker.return_value = 6
        self.assertFalse(self.bot._cerrar_posicion_impl('NKE', 999, 'reversión'))
        self.ex.colocar_salida.assert_called_once_with('NKE', 'BUY', 6, 37.5, 'STOP', 'offline-account')
        self.assertAlmostEqual(self.pos['normal_exit_progress']['verified_gross_pnl'], -0.24)
        self.assertTrue(self.bot._real_accounting_status()['blocked'])
        self.assertEqual(self.bot.daily_loss_guard.realized_pnl(False), 0)
        self.bot._last_prices = {'NKE': 37.60}
        self.bot._last_price_times = {'NKE': time.time()}
        risk = self.bot.daily_risk_snapshot()
        self.assertAlmostEqual(risk['known_net_pnl'], -0.60)

    def test_unknown_working_snapshot_prevents_any_replacement_stop(self):
        self.ex.ordenes_trabajando_ticker.return_value = None
        self.assertFalse(self.bot._protect_deferred_limit('NKE', self.pos, self.deferred()))
        self.ex.colocar_salida.assert_not_called()
        self.assertTrue(self.bot._real_accounting_status()['blocked'])

    def test_completed_retry_books_all_exit_legs_once_and_preserves_older_blocks(self):
        partial = self.deferred(4)
        self.ex.liquidar_posicion.return_value = partial
        self.bot._cantidad_real_broker.return_value = 6
        self.bot._cerrar_posicion_impl('NKE', 999, 'reversión')
        self.bot.real_accounting_guard.require_reconciliation('older-BA', ticker='BA', reason='missing ID')
        execution = copy.deepcopy(partial['execution_details'])
        execution['legs'].append({'order_id': 'limit-2', 'status': 'FILLED',
                                  'filled': 6, 'avg_price': 37.65})
        self.ex.liquidar_posicion.return_value = {'status': 'OK', 'posicion_plana': True,
            'cantidad': 10, 'avg_price': 37.63, 'order_id': 'limit-2',
            'fill_price_verified': True, 'execution_details': execution}
        self.assertTrue(self.bot._cerrar_posicion_impl('NKE', 999, 'reversión'))
        self.assertAlmostEqual(self.bot.daily_loss_guard.realized_pnl(False), -0.30)
        self.assertEqual(self.bot._real_accounting_status()['tickers'], ['BA'])
        self.assertAlmostEqual(float(self.fixture.last_close()['pnl_usd']), -0.30)
        self.assertFalse(self.pos['tp1_hit'])

    def test_partial_losses_still_count_when_unrealized_valuation_is_disabled(self):
        self.ex.liquidar_posicion.return_value = self.deferred(4)
        self.bot._cantidad_real_broker.return_value = 6
        self.bot._cerrar_posicion_impl('NKE', 999, 'reversión')
        self.bot.daily_loss_guard.include_unrealized = False
        self.bot._last_prices = {'NKE': 1}
        self.assertAlmostEqual(self.bot.daily_risk_snapshot()['known_net_pnl'], -0.24)

    def test_unknown_partial_price_and_missing_stop_ack_stay_blocked(self):
        result = self.deferred(4)
        result['partial_price_verified'] = False
        self.ex.liquidar_posicion.return_value = result
        self.ex.colocar_salida.return_value = {'status': 'RECONCILIAR', 'order_id': ''}
        self.bot._cantidad_real_broker.return_value = 6
        self.assertFalse(self.bot._cerrar_posicion_impl('NKE', 999, 'reversión'))
        self.assertNotIn('verified_gross_pnl', self.pos['normal_exit_progress'])
        self.assertEqual(self.bot.daily_loss_guard.realized_pnl(False), 0)
        self.assertTrue(self.bot._real_accounting_status()['blocked'])

    def test_prior_unreconciled_quantity_change_uses_actual_exposure_and_stays_blocked(self):
        result = self.deferred()
        result['remaining_quantity'] = 6
        result['execution_details']['quantity'] = 6
        self.bot._cantidad_real_broker.return_value = 6
        self.ex.liquidar_posicion.return_value = result
        self.bot._cerrar_posicion_impl('NKE', 999, 'reversión')
        self.ex.colocar_salida.assert_called_once_with('NKE', 'BUY', 6, 37.5, 'STOP', 'offline-account')
        self.bot._last_prices = {'NKE': 37.60}
        self.assertAlmostEqual(self.bot.daily_risk_snapshot()['known_net_pnl'], -0.36)
        self.assertTrue(self.bot._real_accounting_status()['blocked'])

    def test_monitor_retries_only_after_stop_is_confirmed_and_at_most_three_times(self):
        self.pos.update(salida_parcial=False, sl_reconcile_pending=False)
        self.pos['normal_exit_progress'] = {'filled_quantity': 0, 'attempts': 1,
            'retry_after': 0, 'reason': 'reversión'}
        self.bot._activo = True
        self.bot._cerrar_posicion = Mock()
        self.ex._estado_orden.return_value = 'WORKING'
        self.bot._gestionar_real('NKE', self.pos, 37.6)
        self.bot._cerrar_posicion.assert_called_once_with('NKE', 37.6, motivo='reversión')
        self.pos['normal_exit_progress'].update(attempts=3, retry_after=0)
        self.bot._gestionar_real('NKE', self.pos, 37.6)
        self.assertEqual(self.bot._cerrar_posicion.call_count, 1)

    def test_unknown_cancel_sets_reconciliation_flag_instead_of_forgetting_old_stop(self):
        self.ex.liquidar_posicion.return_value = {'status': 'RECONCILIAR', 'mensaje': 'still working'}
        self.assertFalse(self.bot._cerrar_posicion_impl('NKE', 999, 'reversión'))
        self.ex.colocar_salida.assert_not_called()
        self.assertTrue(self.pos['sl_reconcile_pending'])

    def test_ordinary_exit_cannot_route_to_legacy_market_when_v2_is_disabled(self):
        executor = EjecutorHibridoDeOrdenes.__new__(EjecutorHibridoDeOrdenes)
        executor.paper_trading = False
        executor.execution_v2_config = {'enabled': False}
        executor._smart_exit = Mock()
        executor._smart_exit.close.return_value = {'status': 'LIMIT_DEFERRED'}
        executor._lifecycle_local = Mock(fields={})
        from contextlib import nullcontext
        executor.secuencia_simbolo = lambda *a: nullcontext()
        executor._liquidar_market_legacy = Mock()
        executor.liquidar_posicion('NKE', 'SELL', 10, 'account', 'reversión')
        executor._liquidar_market_legacy.assert_not_called()
        executor._smart_exit.close.assert_called_once()


class ExitProgressGuardTests(unittest.TestCase):
    def test_only_exact_complete_exit_can_resolve_its_own_transient_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = RealAccountingGuard(Path(tmp)/'guard.json')
            first = {'order_id': 'first', 'status': 'CANCELED', 'filled': 4, 'avg_price': 100}
            final = {'position_id': 'p1', 'execution_id': 'e1', 'quantity': 10,
                     'legs': [first, {'order_id': 'last', 'status': 'FILLED', 'filled': 6, 'avg_price': 101}]}
            guard.require_reconciliation('old-BA', ticker='BA', reason='missing ID')
            guard.require_reconciliation('pending_exit:e1', kind='NORMAL_EXIT_PROGRESS',
                position_id='p1', exit_execution_id='e1', quantity=10, legs=[first])
            for change in ({'position_id': 'different'}, {'quantity': 11},
                           {'execution_id': 'other'}, {'legs': [first]}):
                self.assertFalse(guard.complete_exit_progress('pending_exit:e1', position_id='p1',
                                                              execution={**final, **change}))
            self.assertTrue(guard.complete_exit_progress('pending_exit:e1', position_id='p1', execution=final))
            self.assertEqual(guard.snapshot()['unresolved'], 1)
            self.assertEqual(guard.snapshot()['tickers'], ['BA'])
            self.assertFalse(guard.complete_exit_progress('old-BA', position_id='p1', execution=final))
            self.assertTrue(RealAccountingGuard(Path(tmp)/'guard.json').snapshot()['blocked'])


if __name__ == '__main__':
    unittest.main()
