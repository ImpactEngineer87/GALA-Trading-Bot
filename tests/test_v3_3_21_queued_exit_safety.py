"""Offline broker-status regressions: an absent WORKING order may still execute.

Loads production executor methods, SmartExit and the shared request gate. No
credentials, network calls or broker mutations are used.
"""
import hashlib
import threading
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

from test_schwab_request_budget_offline import GateFixture, Response, load_methods
from test_v3_3_20_pending_exit_recovery import FakeBroker, SmartExit, SMART_SCOPE


TERMINAL = {'FILLED', 'CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED'}
ACCOUNT = 'offline-account'
URL = 'https://api.schwabapi.com/trader/v1/accounts/offline-account/orders'
BROKEN = Response(400, {'errors': [{'status': 500, 'id': 'offline-correlation'}]})


def order(oid='old', status='QUEUED', symbol='NVDA'):
    return {'orderId': oid, 'status': status,
            'orderLegCollection': [{'instrument': {'symbol': symbol}}]}


class OrderSafetyTests(GateFixture):
    def setUp(self):
        super().setUp()
        Executor = load_methods('api/hybrid_order_executor.py', 'EjecutorHibridoDeOrdenes', {
            'orden_trabajando', 'ordenes_trabajando_ticker', '_iter_order_nodes',
            '_working_orders_snapshot', '_buscar_orden_en_listado', '_listado_ordenes',
            '_orden_en', '_cancelar_orden_confirmada_impl'}, {
            'TERMINAL_ORDER_STATES': TERMINAL, 'ESTADOS_CANCELACION_OK': TERMINAL - {'FILLED'},
            'normalize_status': lambda status: str(status or '').upper(),
            'schwab_request': self.gate.request, 'nullcontext': nullcontext, '_ps': Mock(),
            'time': SimpleNamespace(monotonic=lambda: self.clock.mono)})
        Executor._iter_order_nodes.__globals__['EjecutorHibridoDeOrdenes'] = Executor
        self.executor = ex = Executor()
        ex.TRADER_URL, ex.request_timeout_s, ex.paper_trading = URL.rsplit('/accounts/', 1)[0], 5, False
        ex._order_list_cache, ex._order_list_lock = None, threading.Lock()
        self.working, self.detail, self.all_orders = [], order(), [order()]
        ex._working_orders_provider = lambda account: self.working
        self.reads = []

        def get(url, **kwargs):
            self.reads.append((url, kwargs))
            data = self.detail if '/orders/' in url else self.all_orders
            return data if isinstance(data, Response) else Response(200, data)
        get.__name__ = 'get'
        ex._http_session = lambda: SimpleNamespace(get=get)
        ex._audit = Mock()

    def test_all_nonterminal_states_block_replacement_even_absent_from_working(self):
        for status in ('QUEUED', 'PENDING_ACTIVATION', 'AWAITING_CONDITION',
                       'AWAITING_RELEASE_TIME', 'PENDING_CANCEL', 'ACCEPTED', 'NEW_VENDOR_STATE'):
            with self.subTest(status=status):
                self.detail = order(status=status)
                self.assertTrue(self.executor.orden_trabajando(ACCOUNT, 'old'))
                self.clock.advance(2)

    def test_working_presence_avoids_extra_detail_reads(self):
        self.working = [order(status='WORKING')]
        self.assertTrue(self.executor.orden_trabajando(ACCOUNT, 'old'))
        self.assertEqual(self.reads, [])

    def test_400_with_json_500_and_empty_working_list_is_unknown_not_terminal(self):
        self.detail = self.all_orders = BROKEN
        self.assertIsNone(self.executor.orden_trabajando(ACCOUNT, 'old'))
        self.assertEqual(len(self.reads), 2)
        self.assertEqual([e['http_status'] for e in self.events], [400, 400])
        self.assertEqual([e['broker_statuses'] for e in self.events], [[500], [500]])
        self.clock.advance(1)
        self.assertIsNone(self.executor.orden_trabajando(ACCOUNT, 'old'))
        self.assertEqual(len(self.reads), 2)  # transport backoff still applies

    def test_definite_terminal_status_allows_position_reconciliation(self):
        for status in TERMINAL:
            with self.subTest(status=status):
                self.detail = order(status=status)
                self.assertFalse(self.executor.orden_trabajando(ACCOUNT, 'old'))
                self.clock.advance(2)

    def test_missing_order_id_and_empty_or_unknown_detail_are_unverified(self):
        self.assertIsNone(self.executor.orden_trabajando(ACCOUNT, ''))
        for status in ('', 'DESCONOCIDO', 'UNKNOWN', 'NOT_WORKING'):
            with self.subTest(status=status):
                self.detail, self.all_orders = order(status=status), []
                self.assertIsNone(self.executor.orden_trabajando(ACCOUNT, 'old'))
                self.clock.advance(2)

    def test_unfiltered_symbol_check_includes_queued_children_and_all_legs(self):
        parent = order('parent', 'WORKING', 'META')
        parent['childOrderStrategies'] = [order('child', 'PENDING_ACTIVATION')]
        multi = order('multi', 'QUEUED', 'BA')
        multi['orderLegCollection'].append({'instrument': {'symbol': 'NVDA'}})
        self.all_orders = [parent, multi, order('done', 'FILLED')]
        self.assertEqual(self.executor.ordenes_trabajando_ticker(ACCOUNT, 'NVDA'), ['child', 'multi'])
        self.assertNotIn('status', self.reads[0][1]['params'])

    def test_unreadable_capped_or_malformed_list_cannot_prove_no_orders(self):
        for data in (BROKEN, [order(str(i)) for i in range(500)], [None],
                     [order(status='')], [dict(order(), childOrderStrategies=[None])],
                     [dict(order(), orderLegCollection=[None])],
                     [dict(order(), orderLegCollection=[{}])],
                     [dict(order(), orderLegCollection=[])], [order(oid='')]):
            with self.subTest(data_type=type(data).__name__, count=len(data) if isinstance(data, list) else 0):
                self.all_orders = data
                self.assertIsNone(self.executor.ordenes_trabajando_ticker(ACCOUNT, 'NVDA'))
                self.clock.advance(61)

    def test_readable_empty_or_terminal_list_has_no_pending_symbol_orders(self):
        for data in ([], [order(status='FILLED')], [order(symbol='META')]):
            self.all_orders = data
            self.assertEqual(self.executor.ordenes_trabajando_ticker(ACCOUNT, 'NVDA'), [])
            self.clock.advance(2)

    def test_unknown_read_cannot_confirm_cancel_by_working_absence(self):
        ex = self.executor
        self.detail = self.all_orders = BROKEN
        ex.detalle_orden = lambda *args: {'status': 'DESCONOCIDO', 'filled': 0}
        ex._solicitar_cancelacion = Mock(return_value={'aceptada': True, 'http_status': 200})
        ex.cancel_timeout_s = 0.01
        ex.esperar_estado_orden = Mock(return_value={
            'confirmado': False, 'broker_status': 'DESCONOCIDO',
            'latency_ms': 10, 'timeout': True, 'filled': 0, 'error': ''})
        result = ex._cancelar_orden_confirmada_impl(ACCOUNT, 'old', 'NVDA')
        self.assertFalse(result['confirmado'])
        self.assertFalse(result['terminal_by_absence'])
        ex._solicitar_cancelacion.assert_called_once()

    def test_terminal_read_can_confirm_cancel_without_another_delete(self):
        ex = self.executor
        self.detail = order(status='CANCELED')
        ex.detalle_orden = lambda *args: {'status': 'DESCONOCIDO', 'filled': 0}
        ex._solicitar_cancelacion = Mock(side_effect=AssertionError('no extra DELETE'))
        result = ex._cancelar_orden_confirmada_impl(ACCOUNT, 'old', 'NVDA')
        self.assertTrue(result['confirmado'])
        self.assertFalse(result['terminal_by_absence'])
        self.assertTrue(result['terminal_by_order_read'])
        ex._solicitar_cancelacion.assert_not_called()

    def broker_with_saved_exit(self, status):
        broker = FakeBroker(self.temp.name, behavior='working')
        state = broker.engine._new_state('NVDA', 'SELL', 5, 'kill switch', 236.25)
        state['legs'] = [{'order_id': 'old', 'type': 'MARKET', 'quantity': 5,
                          'filled': 0, 'avg_price': 0, 'status': 'SUBMITTED'}]
        key = hashlib.sha256(ACCOUNT.encode()).hexdigest()[:16] + ':NVDA'
        broker.engine.pending.put(key, state)
        broker.detalle_orden = lambda *args: {'status': status, 'filled': 0, 'avg_price': 0}
        broker.orden_trabajando = self.executor.orden_trabajando
        broker.ordenes_trabajando_ticker = self.executor.ordenes_trabajando_ticker
        return broker, key

    def test_queued_normal_session_market_close_survives_restart_without_new_post(self):
        broker, key = self.broker_with_saved_exit('QUEUED')
        for _ in range(3):
            result = broker.close('kill switch')
            self.assertEqual(result['status'], 'RECONCILIAR')
            self.assertIn('queued/pending', result['mensaje'])
            self.assertEqual(broker.posts, [])
            self.assertEqual(broker.engine.pending.get(key)['legs'][0]['order_id'], 'old')
            broker.engine = SmartExit(broker, broker.engine.config)
            self.clock.advance(30)

    def test_broken_order_reads_preserve_saved_exit_and_never_resubmit(self):
        self.detail = self.all_orders = BROKEN
        broker, key = self.broker_with_saved_exit('DESCONOCIDO')
        for _ in range(3):
            result = broker.close('kill switch')
            self.assertEqual(result['status'], 'RECONCILIAR')
            self.assertIn('unverified', result['mensaje'])
            self.assertEqual(broker.posts, [])
            self.assertIsNotNone(broker.engine.pending.get(key))
            broker.engine = SmartExit(broker, broker.engine.config)
            self.clock.advance(61)


class PendingExitReconciliationTests(unittest.TestCase):
    def bot(self, pending_orders):
        Bot = load_methods('engine/bot_engine.py', 'ChocoletraBot',
                           {'_refresh_broker_reconciliation',
                            '_resolver_hash_cuenta'}, {})
        bot = Bot()
        bot.account_number, bot.tickers = ACCOUNT, ['NVDA']
        bot._real_tickers = lambda tickers: tickers
        bot._posiciones = {'NVDA': {'execution_mode': 'REAL', 'exit_pending': True}}
        bot._set_broker_reconciliation = lambda **state: state
        bot._broker_read_with_retry = lambda label, read, **kw: (True, read(), '')
        bot.schwab = SimpleNamespace(obtener_posiciones=lambda account: [],
                                    obtener_ordenes_abiertas=lambda account: [])
        bot._iter_broker_orders = lambda rows: iter(rows)
        bot._symbol_from_broker_order = lambda row: 'NVDA'
        bot.ejecutor_hibrido = SimpleNamespace(
            ordenes_trabajando_ticker=Mock(return_value=pending_orders))
        return bot

    def test_queued_close_blocks_flat_confirmation_even_when_positions_are_flat(self):
        state = self.bot(['queued-close'])._refresh_broker_reconciliation(
            context='READ_ONLY', require_flat=True)
        self.assertFalse(state['ok'])
        self.assertFalse(state['flat'])
        self.assertEqual(state['open_order_symbols'], ['NVDA'])

    def test_unknown_full_list_cannot_release_a_pending_close(self):
        state = self.bot(None)._refresh_broker_reconciliation(context='READ_ONLY')
        self.assertFalse(state['ok'])
        self.assertFalse(state['flat'])
        self.assertIn('NVDA', state['message'])

    def test_readable_empty_all_status_list_permits_flat_confirmation(self):
        state = self.bot([])._refresh_broker_reconciliation(context='READ_ONLY', require_flat=True)
        self.assertTrue(state['ok'])
        self.assertTrue(state['flat'])

    def test_no_extra_all_status_read_while_broker_position_is_open(self):
        bot = self.bot(None)
        bot.schwab.obtener_posiciones = lambda account: [
            {'symbol': 'NVDA', 'tipo': 'SELL', 'cantidad': 5}]
        state = bot._refresh_broker_reconciliation(context='READ_ONLY', require_flat=True)
        self.assertFalse(state['flat'])
        bot.ejecutor_hibrido.ordenes_trabajando_ticker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
