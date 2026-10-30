"""Exercise the real controller/runner failure race with an offline engine."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import server


class ImmediateThread:
    def __init__(self, target, **kwargs):
        self.target = target
    def start(self):
        self.target()
    def is_alive(self):
        return False


class DeferredThread(ImmediateThread):
    def start(self):
        pass
    def is_alive(self):
        return True


class EngineRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cfg = {'kill_switch': {'daily_loss': {
            'enabled': False, 'state_file': str(self.root/'risk.json')}}}
        self.bot = Mock()
        self.bot.config = self.cfg
        self.bot._ticker_quarantine = {}
        self.bot.TICKERS_DEFAULT = ['BA']
        for name, value in (('_ENGINE', None), ('_engine_thread', None),
                            ('_ENGINE_RUNTIME', {'state': 'NOT_STARTED'})):
            self.enterContext(patch.object(server, name, value))
        self.enterContext(patch.dict(server.BOT_STATE, {'running': False, 'mode': 'paper', 'tickers': ['BA']}))
        self.enterContext(patch.dict(server._STARTUP_QUARANTINE, {}, clear=True))
        self.enterContext(patch('engine.bot_engine.ChocoletraBot', return_value=self.bot))
        self.enterContext(patch.object(server, '_get_auto_ai_service', return_value=Mock()))
        self.enterContext(patch.object(server, '_real_tickers_runtime', return_value=[]))
        self.enterContext(patch.object(server, '_read_config_runtime', return_value=self.cfg))
        self.enterContext(patch.object(server, '_ai_training_busy', return_value=False))
        self.enterContext(patch.object(server, '_scheduler_mode', return_value='manual'))
        self.enterContext(patch.object(server, 'schwab_ensure_token', return_value=False))
        self.enterContext(patch.object(server, 'get_env', return_value=''))
        self.log = self.enterContext(patch.object(server, '_log'))

    def test_fast_startup_failure_cannot_be_overwritten_with_running_true(self):
        self.bot.iniciar.side_effect = RuntimeError('private backend failure')
        with patch.object(server.threading, 'Thread', ImmediateThread):
            ok, message = server._start_engine_controlled('dashboard')
        self.assertFalse(ok)
        self.assertFalse(server.BOT_STATE['running'])
        runtime = server._engine_runtime_snapshot()
        self.assertEqual(runtime['state'], 'FAILED')
        self.assertEqual(runtime['last_error_code'], 'ENGINE_RUNTIME_ERROR')
        self.assertIsNotNone(runtime['thread_started_at_ny'])
        self.assertIsNotNone(runtime['thread_ended_at_utc'])
        self.assertNotIn('private', str(runtime))
        self.bot.detener.assert_not_called()

    def test_fast_normal_exit_is_stopped_and_not_announced_as_active(self):
        with patch.object(server.threading, 'Thread', ImmediateThread):
            ok, _ = server._start_engine_controlled('dashboard')
        self.assertFalse(ok)
        self.assertFalse(server.BOT_STATE['running'])
        self.assertEqual(server._engine_runtime_snapshot()['state'], 'STOPPED')

    def test_accepted_launch_does_not_invent_a_thread_start_time(self):
        with patch.object(server.threading, 'Thread', DeferredThread):
            ok, _ = server._start_engine_controlled('dashboard')
        self.assertTrue(ok)
        runtime = server._engine_runtime_snapshot()
        self.assertEqual(runtime['state'], 'STARTING')
        self.assertIsNone(runtime['thread_started_at_ny'])

    def test_delayed_failure_sets_error_and_retains_recovery_object(self):
        with patch.object(server.threading, 'Thread', DeferredThread):
            server._start_engine('paper', 10000)
        self.bot.iniciar.side_effect = RuntimeError('private')
        server._engine_thread.target()
        self.assertFalse(server.BOT_STATE['running'])
        self.assertIs(server._ENGINE, self.bot)
        self.assertEqual(server._engine_runtime_snapshot()['state'], 'FAILED')

    def test_old_runner_cannot_stop_a_new_engine_instance(self):
        with patch.object(server.threading, 'Thread', DeferredThread):
            server._start_engine('paper', 10000)
        old = server._engine_thread
        server._ENGINE = object()
        new_runtime = {'state': 'THREAD_RUNNING'}
        server._ENGINE_RUNTIME = new_runtime
        old.target()
        self.assertTrue(server.BOT_STATE['running'])
        self.assertIs(server._ENGINE_RUNTIME, new_runtime)
        self.assertEqual(new_runtime, {'state': 'THREAD_RUNNING'})

    def test_thread_launch_failure_is_reported_without_triggering_liquidation(self):
        class BrokenThread(ImmediateThread):
            def start(self):
                raise RuntimeError('private thread start failure')
        with patch.object(server.threading, 'Thread', BrokenThread):
            with self.assertRaises(RuntimeError):
                server._start_engine('paper', 10000)
        self.assertFalse(server.BOT_STATE['running'])
        self.assertEqual(server._engine_runtime_snapshot()['last_error_code'], 'ENGINE_THREAD_START_ERROR')
        self.bot.iniciar.assert_not_called()
        self.bot.detener.assert_not_called()

    def test_duplicate_start_keeps_existing_mixed_execution_mode(self):
        # Dashboard still sends legacy mode='paper'. A second START while
        # running must not misreport the actual REAL/PAPER routing.
        with patch.object(server, '_ENGINE', self.bot), patch.dict(
                server.BOT_STATE, {'running': True, 'mode': 'mixed'}):
            with server.app.test_request_context(
                    '/api/bot/start', method='POST', json={'mode': 'paper'}):
                response = server.start_bot()
            self.assertEqual(response.json['status'], 'ok')
            self.assertEqual(response.json['mode'], 'mixed')
            self.assertEqual(server.BOT_STATE['mode'], 'mixed')

    def test_duplicate_start_ignores_client_real_mode_when_running_paper(self):
        # Request body cannot override the server's saved execution routing.
        with patch.object(server, '_ENGINE', self.bot), patch.dict(
                server.BOT_STATE, {'running': True, 'mode': 'paper'}):
            with server.app.test_request_context(
                    '/api/bot/start', method='POST', json={'mode': 'real'}):
                response = server.start_bot()
            self.assertEqual(response.json['status'], 'ok')
            self.assertEqual(response.json['mode'], 'paper')
            self.assertEqual(server.BOT_STATE['mode'], 'paper')

    def test_status_distinguishes_running_python_version_and_thread_scope(self):
        with patch.object(server, '_dashboard_stats', return_value={}), patch.object(
                server, '_engine_daily_risk_snapshot', return_value={}), patch.object(
                server, '_scheduler_status', return_value={}), patch.object(
                server, '_engine_broker_reconciliation_snapshot', return_value={}), server.app.test_client() as client:
            with client.session_transaction() as session:
                session['logged_in'] = True
            response = client.get('/api/status')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['bot']['server_version'], server._SERVER_VERSION)
        self.assertIsNone(response.json['bot']['engine_runtime']['thread_started_at_ny'])


if __name__ == '__main__':
    unittest.main()
