"""Reproduce the Schwab gateway failure without network or live orders."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from api.hybrid_order_executor import EjecutorHibridoDeOrdenes
from core.execution_v2 import SmartExit
from core.position_state import PositionState, PositionStateMachine
from core.risk_manager import OrdenTradingCalculada
from engine.bot_engine import ChocoletraBot


class BrokerGateway(requests.adapters.BaseAdapter):
    """Reject bodyless JSON requests, supply actual broker fill activities."""
    def __init__(self):
        self.requests = []
        self.orders = {}
        self.short = 3
        self.direct_unavailable = False

    def send(self, request, **kwargs):
        self.requests.append(request)
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = 200
        oid = request.url.split('?', 1)[0].rsplit('/', 1)[-1]
        if request.method == 'GET':
            if request.headers.get('Content-Type'):
                response.status_code, data = 400, {'errors': []}
            elif oid == 'orders':
                data = list(self.orders.values())
            elif self.direct_unavailable or oid not in self.orders:
                response.status_code, data = 404, {}
            else:
                data = self.orders[oid]
        elif request.method == 'POST':
            assert request.headers.get('Content-Type') == 'application/json'
            body = json.loads(request.body)
            leg = body['orderLegCollection'][0]
            oid = str(len(self.orders) + 1)
            qty = leg['quantity']
            self.orders[oid] = {
                'orderId': oid, 'status': 'FILLED', 'quantity': qty,
                'filledQuantity': qty, 'orderType': body['orderType'],
                'orderLegCollection': [leg], 'orderActivityCollection': [{
                    'activityType': 'EXECUTION', 'executionType': 'FILL',
                    'executionLegs': [{'legId': 1, 'quantity': qty, 'price': 612.69}],
                }],
            }
            if leg['instruction'] == 'BUY_TO_COVER':
                self.short = 0
            response.status_code = 201
            response.headers['Location'] = 'https://offline.invalid/orders/' + oid
            data = {}
        else:
            raise AssertionError('Unexpected simulated broker request')
        response._content = json.dumps(data).encode()
        return response

    def close(self):
        pass


class BrokerRequestHeaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(self.temp)
        self.executor = EjecutorHibridoDeOrdenes(False, 'offline-token', {
            'execution_engine': {'pending_path': str(root/'pending.json'),
                                 'history_path': str(root/'history.jsonl')},
            'order_sync': {'poll_interval_ms': 1},
        })
        self.gateway = BrokerGateway()
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.mount('https://', self.gateway)
        self.executor._session = self.session
        self.addCleanup(self.session.close)
        self.network = self.enterContext(patch(
            'requests.adapters.HTTPAdapter.send',
            side_effect=AssertionError('Network forbidden')))

    def test_order_fill_is_visible_without_inherited_content_type(self):
        self.gateway.orders['123'] = {
            'orderId': '123', 'status': 'FILLED', 'filledQuantity': 3,
            'orderActivityCollection': [{'activityType': 'EXECUTION',
                'executionType': 'FILL', 'executionLegs': [
                    {'quantity': 3, 'price': 613.53}]}],
        }
        # Old/custom sessions and token refreshes must lose the stale header too.
        self.session.headers['Content-Type'] = 'application/json'
        self.executor.set_token_provider(lambda: 'refreshed-offline-token')
        result = self.executor.detalle_orden('account', '123')
        self.assertEqual(result['status'], 'FILLED')
        self.assertTrue(result['fill_price_verified'])
        self.assertEqual(result['avg_price'], 613.53)
        self.assertEqual(result['filled'], 3)
        self.assertEqual(len(self.gateway.requests), 1)
        self.assertNotIn('Content-Type', self.gateway.requests[0].headers)
        self.assertEqual(self.gateway.requests[0].headers['Authorization'],
                         'Bearer refreshed-offline-token')

    def test_order_list_fallback_also_has_no_entity_header(self):
        self.gateway.orders['123'] = {'orderId': '123', 'status': 'CANCELED'}
        self.gateway.direct_unavailable = True
        self.assertEqual(self.executor.detalle_orden('account', '123')['status'], 'CANCELED')
        self.assertEqual(len(self.gateway.requests), 2)
        self.assertTrue(all('Content-Type' not in r.headers for r in self.gateway.requests))

    def test_each_worker_session_and_token_update_keeps_get_bodyless(self):
        self.executor._session = self.executor._root_session
        sessions = []
        def worker():
            session = self.executor._http_session()
            session.headers['Content-Type'] = 'application/json'
            self.executor.set_token('rotated-offline-token')
            sessions.append(session)
        thread = threading.Thread(target=worker)
        thread.start(); thread.join()
        sessions.append(self.executor._http_session())
        self.assertIsNot(sessions[0], sessions[1])
        for session in sessions:
            self.assertNotIn('Content-Type', session.headers)
            session.close()
        self.assertNotIn('Content-Type', self.executor._root_session.headers)

    def test_short_reversal_exit_is_verified_with_one_buy_to_cover(self):
        self.executor.set_position_provider(lambda account: ([
            {'symbol': 'META', 'tipo': 'SELL', 'cantidad': self.gateway.short,
             'precio': 613.53}] if self.gateway.short else []))
        self.executor.set_working_orders_provider(lambda account: [])
        self.executor.set_quote_provider(lambda ticker: {
            'bid': 612.65, 'ask': 612.82, 'quote_time': time.time(), 'delayed': False})
        result = SmartExit(self.executor, self.executor.execution_v2_config).close(
            'META', 'SELL', 3, 'account', 'reversión', 612.865,
            position_id='offline-position')
        self.assertEqual(result['status'], 'OK')
        self.assertTrue(result['posicion_plana'])
        self.assertTrue(result['fill_price_verified'])
        self.assertEqual(result['avg_price'], 612.69)
        self.assertEqual(result['cantidad'], 3)
        posts = [r for r in self.gateway.requests if r.method == 'POST']
        self.assertEqual(len(posts), 1)
        self.assertEqual(json.loads(posts[0].body)['orderLegCollection'][0]['instruction'],
                         'BUY_TO_COVER')
        self.assertTrue(all('Content-Type' not in r.headers
                            for r in self.gateway.requests if r.method == 'GET'))
        self.network.assert_not_called()

    def test_engine_reversal_submits_opposite_buy_only_after_verified_short_close(self):
        self.executor.set_position_provider(lambda account: ([
            {'symbol': 'META', 'tipo': 'SELL', 'cantidad': self.gateway.short,
             'precio': 613.53}] if self.gateway.short else []))
        self.executor.set_working_orders_provider(lambda account: [])
        self.executor.set_quote_provider(lambda ticker: {
            'bid': 612.65, 'ask': 612.82, 'quote_time': time.time()})
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot._lock = threading.RLock()
        bot._activo = bot.reversion_activa = bot.live_allow_short = True
        bot._posiciones = {'META': {'tipo': 'SELL', 'position_id': 'old-position'}}
        machine = PositionStateMachine()
        machine.adopt('META', 'SELL', 'old-position')
        bot._state_machine = Mock(return_value=machine)
        bot._daily_blocked = Mock(return_value=False)
        bot._is_paper_ticker = Mock(return_value=False)
        bot._reservar_slot = Mock(return_value=True)
        bot._resumen = {'reversiones': 0}
        bot._emit_log = bot._audit_event = Mock()
        bot.ejecutor_hibrido = self.executor
        bot._registrar_senal_no_ejecutada = Mock()

        def close(ticker, price, motivo):
            result = SmartExit(self.executor, self.executor.execution_v2_config).close(
                ticker, 'SELL', 3, 'account', motivo, price, position_id='old-position')
            if result.get('status') != 'OK' or not result.get('fill_price_verified'):
                return False
            machine.close(ticker, expected_state=PositionState.SHORT,
                          position_id='old-position')
            bot._posiciones.pop(ticker)
            return True

        def open_opposite(decision, price, frame):
            self.assertEqual(self.gateway.short, 0)
            self.assertEqual(machine.snapshot('META').state, PositionState.OUT)
            order = OrdenTradingCalculada(ticker='META', tipo='BUY', precio_limite=price,
                stop_loss=610.0, trailing_stop_pct=0.0, cantidad=3, riesgo_usd=6, atr_actual=1)
            result = self.executor._enviar_orden_real(order, 'LIMIT', 'account', 'reversal')
            self.assertEqual(result['broker_status'], 'FILLED')
            machine.open('META', 'BUY', decision.position_id)
            return result['status'] == 'OK'

        bot._cerrar_posicion = close
        bot._abrir_operacion = open_opposite
        decision = SimpleNamespace(ticker='META', tipo='BUY', signal_id='new-buy')
        bot._abrir_o_revertir(decision, 612.865, None)
        posts = [json.loads(r.body) for r in self.gateway.requests if r.method == 'POST']
        self.assertEqual([p['orderLegCollection'][0]['instruction'] for p in posts],
                         ['BUY_TO_COVER', 'BUY'])
        self.assertEqual(machine.snapshot('META').state, PositionState.LONG)
        self.assertEqual(bot._resumen['reversiones'], 1)
        bot._registrar_senal_no_ejecutada.assert_not_called()
        self.network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
