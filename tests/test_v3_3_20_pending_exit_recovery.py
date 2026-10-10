"""Offline regressions using production engine, SmartExit and request-gate code.

No credentials, HTTP, broker orders or full application imports are needed.
AST loading avoids optional Flask/ML/requests dependencies; broker I/O is fake.
"""
import ast
import hashlib
import math
import tempfile
import threading
import unittest
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pandas as pd

from test_schwab_request_budget_offline import (
    Clock, Deferred, GateFixture, MARKET, Response, TRANSPORT, control_requests,
)
from test_v3_3_15_exit_control import load_methods

ROOT = Path(__file__).resolve().parents[1]
METHODS = {'_cerrar_posicion', '_retry_pending_exit', '_exit_recovery_loop',
           '_start_exit_recovery', '_procesar_ticker', '_monitor_orden_ticker',
           '_liquidar_todo', '_detener_impl', '_cantidad_real_broker',
           '_check_opposite_exit_without_macro', '_solo_velas_cerradas',
           '_close_opposite_without_entry'}
Bot = load_methods('engine/bot_engine.py', 'ChocoletraBot', METHODS, {
    'schwab_control_requests': control_requests,
    'schwab_request_pause': lambda: {'retry_after_seconds': 0},
    'pd': pd, 'math': math, 'nullcontext': nullcontext,
    'DatosMultiTemporal': lambda **kw: SimpleNamespace(**kw, tf=lambda tf: kw['velas'].get(tf)),
})
BOT_SCOPE = Bot._cerrar_posicion.__globals__

# Execute the complete SmartExit source; only its requests-dependent transport
# import is replaced with the actual offline-loaded shared gate definitions.
tree = ast.parse((ROOT / 'core/execution_v2.py').read_text())
tree.body = [n for n in tree.body if not (
    isinstance(n, ast.ImportFrom) and n.module == 'api.schwab_transport')]
SMART_SCOPE = {'schwab_control_requests': control_requests,
               'schwab_request': TRANSPORT['schwab_request']}
exec(compile(tree, 'core/execution_v2.py', 'exec'), SMART_SCOPE)
SmartExit = SMART_SCOPE['SmartExit']


def bot_fixture(side='SELL'):
    bot = Bot()
    bot._lock = threading.RLock()
    symbol_lock = threading.RLock()
    bot.ejecutor_hibrido = SimpleNamespace(secuencia_simbolo=lambda ticker: symbol_lock)
    bot._posiciones = {'NVDA': {'tipo': side, 'position_id': 'position-1',
                               'execution_mode': 'REAL', 'entrada': 236.25}}
    bot._activo = True
    bot._cerrando = set()
    bot._exit_recovery = {}
    bot._last_prices, bot._last_price_times = {}, {}
    bot.account_number = 'offline-account'
    bot._ahora_et = lambda: datetime(2026, 10, 9, 12, 0)
    bot._is_paper_ticker = lambda ticker, pos=None: False
    bot._position_lifecycle_context = lambda *args: {}
    bot._lifecycle_scope = lambda fields: nullcontext()
    bot._start_exit_recovery = Mock()  # Tests explicitly advance recovery ticks.
    bot._emit_log = Mock()
    bot._refresh_broker_reconciliation = Mock(return_value={'ok': True, 'flat': True})
    bot._set_ticker_processing = Mock()
    bot._cerrar_posicion_impl = Mock(return_value=False)
    bot._gestionar_real = Mock(side_effect=AssertionError('must only recover the pending exit'))
    bot._gestionar_entrada_pendiente = Mock(side_effect=AssertionError('no entry action'))
    bot._obtener_mtf = Mock(side_effect=AssertionError('no new market-data/AI dependency'))
    bot._abrir_o_revertir = Mock(side_effect=AssertionError('recovery never opens a position'))
    return bot


class ExitIntentTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.timer = patch.dict(BOT_SCOPE, time=SimpleNamespace(
            monotonic=lambda: self.clock.mono, sleep=lambda seconds: self.clock.advance(seconds)))
        self.timer.start()
        self.addCleanup(self.timer.stop)

    def complete(self, bot):
        def close(*args):
            bot._posiciones.pop('NVDA')
            return True
        bot._cerrar_posicion_impl.side_effect = close

    def test_one_shot_opposite_exit_retries_without_a_second_signal_or_ai(self):
        bot = bot_fixture()
        self.assertFalse(bot._cerrar_posicion('NVDA', 235, 'señal opuesta BUY'))
        self.assertTrue(bot._posiciones['NVDA']['exit_pending'])
        bot._procesar_ticker('NVDA')
        self.assertEqual(bot._cerrar_posicion_impl.call_count, 1)
        self.clock.advance(30)
        self.complete(bot)
        bot._procesar_ticker('NVDA')
        self.assertEqual(bot._cerrar_posicion_impl.call_count, 2)
        self.assertNotIn('NVDA', bot._posiciones)
        self.assertEqual(bot._exit_recovery, {})
        bot._obtener_mtf.assert_not_called()
        bot._abrir_o_revertir.assert_not_called()

    def test_direct_kill_attempts_do_not_bypass_30_second_pacing(self):
        bot = bot_fixture()
        for _ in range(5):
            self.assertFalse(bot._cerrar_posicion('NVDA', 235, 'kill switch'))
            self.clock.advance(1)
        self.assertEqual(bot._cerrar_posicion_impl.call_count, 1)
        self.assertEqual(bot._exit_recovery['NVDA']['attempts'], 1)

    def test_monitor_does_close_only_recovery_and_does_not_replace_protection(self):
        bot = bot_fixture()
        bot._cerrar_posicion('NVDA', 235, 'reversión')
        self.clock.advance(30)
        bot._monitor_orden_ticker('NVDA')
        self.assertEqual(bot._cerrar_posicion_impl.call_count, 2)
        bot._gestionar_real.assert_not_called()

    def test_recovery_survives_stopped_strategy_and_updates_only_confirmed_flat(self):
        bot = bot_fixture()
        bot._cerrar_posicion('NVDA', 235, 'kill switch — apagado del motor')
        bot._activo = False
        bot._stop_result = {'stopped': True, 'broker_flat': False, 'ok': False}
        self.clock.advance(30)
        self.assertFalse(bot._retry_pending_exit('NVDA'))
        bot._refresh_broker_reconciliation.assert_not_called()
        self.assertFalse(bot._stop_result['broker_flat'])
        self.complete(bot)
        self.clock.advance(30)
        self.assertTrue(bot._retry_pending_exit('NVDA'))
        self.assertTrue(bot._stop_result['broker_flat'])
        bot._refresh_broker_reconciliation.assert_called_once_with(
            context='EXIT_RECOVERY', require_flat=True)
        bot._abrir_o_revertir.assert_not_called()

    def test_closed_symbol_does_not_claim_whole_account_flat(self):
        bot = bot_fixture()
        bot._cerrar_posicion('NVDA', 235, 'kill switch')
        bot._activo = False
        self.complete(bot)
        bot._refresh_broker_reconciliation.return_value = {'ok': False, 'flat': False}
        self.clock.advance(30)
        self.assertTrue(bot._retry_pending_exit('NVDA'))
        self.assertFalse(bot._stop_result['broker_flat'])

    def test_upstream_cooldown_is_respected_without_repeated_close_attempts(self):
        bot = bot_fixture()
        with patch.dict(BOT_SCOPE, schwab_request_pause=lambda: {'retry_after_seconds': 1200}):
            bot._cerrar_posicion('NVDA', 235, 'kill switch')
        for _ in range(39):
            self.clock.advance(30)
            self.assertFalse(bot._retry_pending_exit('NVDA'))
        self.assertEqual(bot._cerrar_posicion_impl.call_count, 1)
        self.clock.advance(30)
        bot._retry_pending_exit('NVDA')
        self.assertEqual(bot._cerrar_posicion_impl.call_count, 2)

    def test_intent_never_transfers_to_new_position_object(self):
        bot = bot_fixture()
        bot._cerrar_posicion('NVDA', 235, 'reversión')
        bot._posiciones['NVDA'] = {'tipo': 'SELL', 'position_id': 'position-2'}
        self.clock.advance(30)
        self.assertFalse(bot._retry_pending_exit('NVDA'))
        self.assertEqual(bot._cerrar_posicion_impl.call_count, 1)
        self.assertEqual(bot._exit_recovery, {})

    def test_account_side_or_position_id_change_never_replays_close(self):
        for field in ('account', 'side', 'id'):
            with self.subTest(field=field):
                bot = bot_fixture()
                bot._cerrar_posicion('NVDA', 235, 'kill switch')
                if field == 'account':
                    bot.account_number = 'another-account'
                elif field == 'side':
                    bot._posiciones['NVDA']['tipo'] = 'BUY'
                else:
                    bot._posiciones['NVDA']['position_id'] = 'another-position'
                self.clock.advance(30)
                self.assertFalse(bot._retry_pending_exit('NVDA'))
                self.assertEqual(bot._cerrar_posicion_impl.call_count, 1)

    def test_recovery_is_bounded_to_one_hour_and_alert_keeps_exposure_visible(self):
        bot = bot_fixture()
        bot._cerrar_posicion('NVDA', 235, 'señal opuesta BUY')
        self.clock.advance(3600)
        self.assertFalse(bot._retry_pending_exit('NVDA'))
        self.assertFalse(bot._retry_pending_exit('NVDA'))
        self.assertTrue(bot._posiciones['NVDA']['exit_pending'])
        self.assertTrue(bot._posiciones['NVDA']['exit_recovery_expired'])
        self.assertEqual(bot._cerrar_posicion_impl.call_count, 1)
        self.assertEqual(bot._emit_log.call_count, 1)

    def test_recovery_never_crosses_new_york_session_date(self):
        bot = bot_fixture()
        bot._cerrar_posicion('NVDA', 235, 'kill switch')
        bot._ahora_et = lambda: datetime(2026, 10, 10, 0, 1)
        self.clock.advance(30)
        self.assertFalse(bot._retry_pending_exit('NVDA'))
        self.assertEqual(bot._cerrar_posicion_impl.call_count, 1)

    def test_paper_close_does_not_create_real_recovery_or_worker(self):
        bot = bot_fixture()
        bot._is_paper_ticker = lambda *args, **kwargs: True
        bot._cerrar_posicion('NVDA', 235, 'reversión')
        self.assertEqual(bot._exit_recovery, {})
        bot._start_exit_recovery.assert_not_called()

    def test_raised_close_retains_intent_and_releases_closing_guard(self):
        bot = bot_fixture()
        bot._cerrar_posicion_impl.side_effect = RuntimeError('broker unavailable')
        with self.assertRaises(RuntimeError):
            bot._cerrar_posicion('NVDA', 235, 'kill switch')
        self.assertEqual(bot._cerrando, set())
        self.assertTrue(bot._posiciones['NVDA']['exit_pending'])
        bot._start_exit_recovery.assert_called_once()

    def test_signal_and_kill_threads_cannot_dispatch_two_close_sequences(self):
        bot = bot_fixture()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda reason: bot._cerrar_posicion('NVDA', 235, reason),
                                    ('reversión', 'kill switch')))
        self.assertEqual(results, [False, False])
        self.assertEqual(bot._cerrar_posicion_impl.call_count, 1)

    def test_kill_escalates_prior_signal_intent_without_busy_retry(self):
        bot = bot_fixture()
        bot._cerrar_posicion('NVDA', 235, 'reversión')
        bot._cerrar_posicion('NVDA', 235, 'kill switch — cierre de mercado')
        self.assertEqual(bot._cerrar_posicion_impl.call_count, 1)
        self.clock.advance(30)
        bot._retry_pending_exit('NVDA')
        self.assertEqual(bot._cerrar_posicion_impl.call_args.args[-1],
                         'kill switch — cierre de mercado')

    def test_actual_worker_loop_after_stop_runs_only_paced_close_and_exits(self):
        bot = bot_fixture()
        bot._cerrar_posicion('NVDA', 235, 'kill switch')
        bot._activo = False
        self.complete(bot)
        waits = []
        def wait(seconds):
            waits.append(seconds)
            self.clock.advance(seconds)
        with patch.object(threading, 'Event', return_value=SimpleNamespace(wait=wait)):
            bot._exit_recovery_loop()
        self.assertEqual(waits, [30])
        self.assertEqual(bot._exit_recovery, {})
        self.assertIsNone(bot._exit_recovery_thread)

    def test_shutdown_retains_recovery_instead_of_reporting_flat(self):
        bot = bot_fixture()
        bot.ks_liquidar_apagar = True
        bot.ks_max_liquidation_attempts = 5
        bot.ks_retry_delay_sec = 1
        bot._has_real_tickers = lambda: True
        bot._real_tickers = lambda universe: ['NVDA']
        bot.tickers = ['NVDA']
        bot._liquidados_recientes = {}
        bot._fin_sesion_alcanzado = bot._post_session_retrain_scheduled = False
        bot._detener_monitor_ordenes = Mock()
        bot._precio_rapido = lambda ticker, fallback: fallback
        bot.schwab = SimpleNamespace(obtener_posiciones=lambda account: [
            {'symbol': 'NVDA', 'tipo': 'SELL', 'cantidad': 5}])
        bot._broker_reconciliation = {'ok': False, 'flat': False}
        bot._refresh_broker_reconciliation.return_value = {'ok': False, 'flat': False}
        bot.tos_reader = SimpleNamespace(detener=Mock())
        bot.telegram = SimpleNamespace(enviar_fin_sesion=Mock())
        bot._resumen = {}
        result = bot._detener_impl()
        self.assertFalse(result['broker_flat'])
        self.assertFalse(bot._activo)
        self.assertTrue(bot._posiciones['NVDA']['exit_pending'])
        self.assertEqual(bot._cerrar_posicion_impl.call_count, 1)
        bot._detener_monitor_ordenes.assert_called_once()
        self.complete(bot)
        bot._refresh_broker_reconciliation.return_value = {'ok': True, 'flat': True}
        self.clock.advance(30)
        self.assertTrue(bot._retry_pending_exit('NVDA'))

    def test_last_kill_attempt_closes_a_pending_entry_that_filled_during_cancel(self):
        bot = bot_fixture()
        bot._posiciones['NVDA']['pendiente'] = True
        bot.ks_max_liquidation_attempts, bot.ks_retry_delay_sec = 1, 0
        bot._has_real_tickers = lambda: False
        bot._precio_rapido = lambda ticker, fallback: fallback
        def cancel(ticker):
            bot._posiciones[ticker] = {'tipo': 'SELL', 'entrada': 236.25,
                                      'execution_mode': 'REAL', 'position_id': 'adopted-fill'}
            return False
        bot._cancelar_pendiente = cancel
        bot._cerrar_posicion_impl.side_effect = lambda ticker, price, reason: cancel(ticker)
        bot._liquidar_todo('kill switch')
        bot._cerrar_posicion_impl.assert_called_once()
        self.assertTrue(bot._posiciones['NVDA']['exit_pending'])
        self.assertIs(bot._exit_recovery['NVDA']['position'], bot._posiciones['NVDA'])
        self.clock.advance(30)
        self.complete(bot)
        self.assertTrue(bot._retry_pending_exit('NVDA'))

    def test_unconfirmed_pending_entry_cancel_survives_shutdown_for_paced_retry(self):
        bot = bot_fixture()
        bot._posiciones['NVDA']['pendiente'] = True
        bot._cerrar_posicion('NVDA', 0, 'kill switch')
        bot._activo = False
        self.assertTrue(bot._posiciones['NVDA']['exit_pending'])
        self.assertFalse(bot._retry_pending_exit('NVDA'))
        self.clock.advance(30)
        self.complete(bot)
        self.assertTrue(bot._retry_pending_exit('NVDA'))
        self.assertEqual(bot._cerrar_posicion_impl.call_count, 2)


class MacroExitTests(unittest.TestCase):
    def fixture(self, age_minutes=0):
        bot = bot_fixture()
        now = pd.Timestamp.now(tz='UTC').floor('min') - pd.Timedelta(minutes=age_minutes)
        frame = pd.DataFrame({'close': [236.] * 40},
                             index=pd.date_range(end=now, periods=40, freq='min'))
        bot.schwab = SimpleNamespace(obtener_1m_reciente=Mock(return_value=frame))
        bot._senal_estrategia = Mock(return_value='BUY')
        bot._close_opposite_without_entry = Mock(return_value=True)
        return bot

    def test_fresh_native_closed_bars_can_close_short_without_macro_or_ai(self):
        bot = self.fixture()
        self.assertTrue(bot._check_opposite_exit_without_macro('NVDA', 'MACRO_STALE'))
        data = bot._senal_estrategia.call_args.args[0]
        self.assertEqual(set(data.velas), {'1m'})
        self.assertEqual(len(data.tf('1m')), 39)  # forming bar excluded
        bot._close_opposite_without_entry.assert_called_once_with('NVDA', 'BUY', 236., 'MACRO_STALE')
        bot._abrir_o_revertir.assert_not_called()

    def test_stale_minute_data_never_generates_an_exit(self):
        bot = self.fixture(age_minutes=10)
        self.assertFalse(bot._check_opposite_exit_without_macro('NVDA', 'WARMUP_PENDING'))
        bot._senal_estrategia.assert_not_called()
        bot._close_opposite_without_entry.assert_not_called()

    def test_waiting_pipeline_checks_only_a_real_open_position(self):
        bot = self.fixture()
        bot._obtener_mtf = lambda ticker: SimpleNamespace(
            estado_datos='WAITING', motivo_datos='WARMUP_PENDING', reintento_seg=0)
        bot._procesar_ticker('NVDA')
        bot._close_opposite_without_entry.assert_called_once()

    def test_auth_or_micro_failure_does_not_launch_a_backup_data_request(self):
        for reason in ('AUTH_UNAVAILABLE', 'MICRO_REFRESH_UNAVAILABLE', 'request_budget'):
            with self.subTest(reason=reason):
                bot = self.fixture()
                bot._obtener_mtf = lambda ticker: SimpleNamespace(
                    estado_datos='WAITING', motivo_datos=reason, reintento_seg=0)
                bot._procesar_ticker('NVDA')
                bot.schwab.obtener_1m_reciente.assert_not_called()

    def test_no_position_pending_entry_or_stopped_never_fetches_exit_data(self):
        # v3.3.23: an open PAPER position now keeps SL/TP management here.
        for scenario in ('flat', 'pending', 'stopped'):
            with self.subTest(scenario=scenario):
                bot = self.fixture()
                if scenario == 'flat':
                    bot._posiciones.clear()
                elif scenario == 'pending':
                    bot._posiciones['NVDA']['pendiente'] = True
                else:
                    bot._activo = False
                self.assertFalse(bot._check_opposite_exit_without_macro('NVDA', 'WARMUP_PENDING'))
                bot.schwab.obtener_1m_reciente.assert_not_called()

    def test_unavailable_or_invalid_price_data_never_calls_close(self):
        for value in (None, float('nan'), -1):
            with self.subTest(value=value):
                bot = self.fixture()
                if value is None:
                    bot.schwab.obtener_1m_reciente.return_value = None
                else:
                    bot.schwab.obtener_1m_reciente.return_value.loc[:, 'close'] = value
                self.assertFalse(bot._check_opposite_exit_without_macro('NVDA', 'MACRO_STALE'))
                bot._close_opposite_without_entry.assert_not_called()

    def test_macro_waiting_opposite_signal_uses_real_close_wrapper_and_retains_failure(self):
        bot = self.fixture()
        bot._close_opposite_without_entry = Bot._close_opposite_without_entry.__get__(bot)
        bot._emit_signal, bot._audit_event = Mock(), Mock()
        self.assertFalse(bot._check_opposite_exit_without_macro('NVDA', 'WARMUP_PENDING'))
        bot._cerrar_posicion_impl.assert_called_once()
        self.assertTrue(bot._posiciones['NVDA']['exit_pending'])
        self.assertIn('señal opuesta BUY', bot._exit_recovery['NVDA']['reason'])

    def test_missing_or_same_side_signal_never_closes_during_macro_warmup(self):
        for signal in (None, 'SELL'):
            with self.subTest(signal=signal):
                bot = self.fixture()
                bot._senal_estrategia.return_value = signal
                bot._close_opposite_without_entry = Bot._close_opposite_without_entry.__get__(bot)
                self.assertFalse(bot._check_opposite_exit_without_macro('NVDA', 'WARMUP_PENDING'))
                bot._cerrar_posicion_impl.assert_not_called()


class FakeBroker:
    TRADER_URL = 'https://api.schwabapi.com/trader/v1'
    request_timeout_s = fill_timeout_s = flat_timeout_s = .01

    def __init__(self, directory, side='SELL', behavior='fill'):
        self.quantity, self.side, self.behavior = 5, side, behavior
        self.posts, self.orders, self.audit = [], {}, []
        self._quote_provider = lambda ticker: {
            'bid': 236., 'ask': 236.10, 'quote_time': SMART_SCOPE['time'].time(), 'delayed': False}
        self.engine = SmartExit(self, {'pending_path': str(Path(directory) / 'pending.json'),
                                     'history_path': str(Path(directory) / 'history.jsonl')})

    def _position_snapshot(self, account, ticker):
        return {'long': self.quantity if self.side == 'BUY' else 0,
                'short': self.quantity if self.side == 'SELL' else 0}

    def _http_session(self):
        return self

    def post(self, url, json, **kwargs):
        self.posts.append((json, kwargs))
        if isinstance(self.behavior, int):
            return Response(self.behavior)
        oid = str(len(self.posts))
        quantity = json['orderLegCollection'][0]['quantity']
        if self.behavior == 'partial':
            filled = 2 if json['orderType'] == 'LIMIT' else quantity
        else:
            filled = quantity if self.behavior == 'fill' else 0
        self.quantity -= filled
        self.orders[oid] = {'status': 'FILLED' if filled else 'WORKING',
                            'filled': filled, 'avg_price': 236.10}
        if self.behavior == 'timeout':
            raise TimeoutError('unknown outcome')
        return Response(201, headers={} if self.behavior == 'missing_id'
                        else {'Location': '/orders/' + oid})

    def detalle_orden(self, account, order):
        return dict(self.orders[order])

    def esperar_estado_orden(self, *args, **kwargs):
        return {}

    def esperar_posicion_plana(self, *args, **kwargs):
        return {'confirmado': self.quantity == 0}

    def cancelar_orden_confirmada(self, *args, **kwargs):
        oid = args[1]
        if self.behavior == 'partial':
            self.orders[oid]['status'] = 'CANCELED'
            return {'confirmado': True, 'broker_status': 'CANCELED',
                    'filled': self.orders[oid]['filled']}
        if self.behavior == 'cancel_race':
            self.orders[oid].update(status='FILLED', filled=self.quantity)
            self.quantity = 0
            return {'confirmado': True, 'broker_status': 'FILLED',
                    'filled': self.orders[oid]['filled']}
        return {'confirmado': False}

    def _audit(self, event, **fields):
        self.audit.append((event, fields))

    def close(self, reason='señal opuesta BUY'):
        return self.engine.close('NVDA', self.side, 5, 'offline-account', reason, 236.25)


class ExitTrafficTests(GateFixture):
    def setUp(self):
        super().setUp()
        self.old_gate = TRANSPORT.get('_GATE')
        TRANSPORT['_GATE'] = self.gate
        self.addCleanup(lambda: TRANSPORT.__setitem__('_GATE', self.old_gate))

    def exhaust_background(self):
        for _ in range(60):
            self.gate.request(self.send(), MARKET)
            self.clock.advance(.26)
        with self.assertRaises(Deferred):
            self.gate.request(self.send(), MARKET)

    def test_actual_engine_position_read_uses_control_reserve_after_background_full(self):
        self.exhaust_background()
        bot = bot_fixture()
        def get(url, **kwargs):
            return Response(200, payload=[{'symbol': 'NVDA', 'tipo': 'SELL', 'cantidad': 5}])
        bot.schwab = SimpleNamespace(obtener_posiciones=lambda account:
            self.gate.request(get, 'https://api.schwabapi.com/trader/v1/accounts/fake').json())
        bot._cerrar_posicion_impl = Mock(side_effect=lambda *args:
            bot._cantidad_real_broker('NVDA', 'SELL') == 5)
        self.assertTrue(bot._cerrar_posicion('NVDA', 236, 'kill switch'))
        self.assertEqual(self.gate.snapshot()['request_metrics']['sent_last_60_seconds'], 61)

    def test_smart_exit_post_is_budgeted_and_short_uses_buy_to_cover(self):
        self.exhaust_background()
        broker = FakeBroker(self.temp.name)
        result = broker.close()
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(len(broker.posts), 1)
        body, options = broker.posts[0]
        self.assertEqual(body['orderLegCollection'][0]['instruction'], 'BUY_TO_COVER')
        self.assertEqual(body['orderLegCollection'][0]['quantity'], 5)
        self.assertFalse(options['allow_redirects'])
        self.assertEqual(self.gate.snapshot()['request_metrics']['sent_last_60_seconds'], 61)

    def test_long_close_uses_sell_with_control_reserve(self):
        broker = FakeBroker(self.temp.name, side='BUY')
        self.assertEqual(broker.close()['status'], 'OK')
        self.assertEqual(broker.posts[0][0]['orderLegCollection'][0]['instruction'], 'SELL')

    def test_full_global_budget_defers_post_then_durable_intent_retries_once(self):
        with control_requests():
            for _ in range(90):
                self.gate.request(self.send(), MARKET)
                self.clock.advance(.26)
        broker = FakeBroker(self.temp.name)
        self.assertEqual(broker.close()['status'], 'RECONCILIAR')
        self.assertEqual(broker.posts, [])
        key = hashlib.sha256(b'offline-account').hexdigest()[:16] + ':NVDA'
        self.assertEqual(broker.engine.pending.get(key)['legs'], [])
        self.clock.advance(61)
        broker.engine = SmartExit(broker, broker.engine.config)
        self.assertEqual(broker.close()['status'], 'OK')
        self.assertEqual(len(broker.posts), 1)

    def test_local_403_pause_never_dispatches_post_or_leaves_ambiguous_unsent_leg(self):
        self.gate.request(self.send(Response(403)), MARKET)
        broker = FakeBroker(self.temp.name)
        self.assertEqual(broker.close('kill switch')['status'], 'RECONCILIAR')
        self.assertEqual(broker.posts, [])
        self.clock.advance(601)
        # Only a safe read may test route recovery; a POST cannot be a probe.
        self.assertEqual(broker.close('kill switch')['status'], 'RECONCILIAR')
        self.assertEqual(broker.posts, [])
        self.gate.request(self.send(), MARKET)
        self.assertEqual(broker.close('kill switch')['status'], 'OK')
        self.assertEqual(len(broker.posts), 1)

    def test_timeout_and_missing_id_remain_ambiguous_across_restart_without_duplicate(self):
        for behavior in ('timeout', 'missing_id'):
            with self.subTest(behavior=behavior), tempfile.TemporaryDirectory() as directory:
                broker = FakeBroker(directory, behavior=behavior)
                self.assertEqual(broker.close()['status'], 'RECONCILIAR')
                broker.engine = SmartExit(broker, broker.engine.config)
                self.clock.advance(30)
                self.assertEqual(broker.close()['status'], 'RECONCILIAR')
                self.assertEqual(len(broker.posts), 1)

    def test_unknown_broker_position_never_submits(self):
        broker = FakeBroker(self.temp.name)
        broker._position_snapshot = lambda *args: None
        self.assertEqual(broker.close()['status'], 'RECONCILIAR')
        self.assertEqual(broker.posts, [])

    def test_definite_auth_access_rate_rejection_is_not_an_ambiguous_order(self):
        for status in (401, 403, 429):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                self.gate = self.make_gate()
                TRANSPORT['_GATE'] = self.gate
                broker = FakeBroker(directory, behavior=status)
                self.assertEqual(broker.close('kill switch')['status'], 'RECONCILIAR')
                key = hashlib.sha256(b'offline-account').hexdigest()[:16] + ':NVDA'
                self.assertEqual(broker.engine.pending.get(key)['legs'], [])
                broker.behavior = 'fill'
                self.clock.advance(601)
                self.gate.request(self.send(), MARKET)
                self.assertEqual(broker.close('kill switch')['status'], 'OK')
                self.assertEqual(len(broker.posts), 2)

    def test_post_server_error_retains_ambiguous_intent_and_does_not_replay(self):
        broker = FakeBroker(self.temp.name, behavior=503)
        self.assertEqual(broker.close()['status'], 'RECONCILIAR')
        self.clock.advance(30)
        self.assertEqual(broker.close()['status'], 'RECONCILIAR')
        self.assertEqual(len(broker.posts), 1)

    def test_existing_working_exit_is_never_resubmitted_by_recovery(self):
        broker = FakeBroker(self.temp.name, behavior='working')
        self.assertEqual(broker.close('kill switch')['status'], 'RECONCILIAR')
        self.clock.advance(30)
        self.assertEqual(broker.close('kill switch')['status'], 'RECONCILIAR')
        self.assertEqual(len(broker.posts), 1)

    def test_partial_limit_fallback_closes_only_three_remaining_nvda_shares(self):
        broker = FakeBroker(self.temp.name, behavior='partial')
        self.assertEqual(broker.close()['status'], 'OK')
        self.assertEqual([body['orderLegCollection'][0]['quantity'] for body, _ in broker.posts], [5, 3])
        self.assertEqual([body['orderType'] for body, _ in broker.posts], ['LIMIT', 'MARKET'])
        self.assertEqual(self.gate.snapshot()['request_metrics']['sent_total'], 2)

    def test_deferred_market_remainder_keeps_confirmed_partial_and_resumes_only_remainder(self):
        self.gate = self.make_gate(burst_per_second=1)
        TRANSPORT['_GATE'] = self.gate
        broker = FakeBroker(self.temp.name, behavior='partial')
        self.assertEqual(broker.close()['status'], 'RECONCILIAR')
        self.assertEqual(len(broker.posts), 1)
        self.assertEqual(broker.quantity, 3)
        self.clock.advance(30)
        broker.engine = SmartExit(broker, broker.engine.config)
        self.assertEqual(broker.close()['status'], 'OK')
        self.assertEqual([body['orderLegCollection'][0]['quantity'] for body, _ in broker.posts], [5, 3])

    def test_cancel_fill_race_never_sends_market_fallback(self):
        broker = FakeBroker(self.temp.name, behavior='cancel_race')
        self.assertEqual(broker.close()['status'], 'OK')
        self.assertEqual(len(broker.posts), 1)


if __name__ == '__main__':
    unittest.main()
