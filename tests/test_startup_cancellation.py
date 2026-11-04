"""A stop during slow startup must never resurrect a trading engine."""
import threading
import unittest
from unittest.mock import Mock, patch
from contextlib import nullcontext
import pandas as pd

from engine.bot_engine import ChocoletraBot


class StartupCancellationTests(unittest.TestCase):
    def test_stop_during_feed_start_does_not_enable_trading(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot._lock = threading.RLock()
        bot._start_cancel_event = threading.Event()
        bot._activo = False
        bot.per_ticker_execution = False
        bot.paper_trading = True
        bot._has_real_tickers = Mock(return_value=False)
        bot.tickers = ['BA']
        bot.telegram = Mock()
        bot.tos_reader = Mock()
        bot.tos_reader.iniciar_monitoreo.side_effect = lambda *a: bot._start_cancel_event.set()
        bot._preparar_trading_real = Mock()
        with patch('engine.bot_engine.log_inicio_bot'):
            bot.iniciar()
        self.assertFalse(bot._activo)
        bot._preparar_trading_real.assert_not_called()
        bot.tos_reader.detener.assert_called_once()

    def test_late_orphan_proof_cannot_replace_existing_local_position(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot._activo = True
        bot._lock = threading.RLock()
        original = {'position_id': 'managed-entry', 'sl_order_id': 'working-stop'}
        bot._posiciones = {'BA': original}
        bot.ejecutor_hibrido = Mock()
        bot.ejecutor_hibrido.secuencia_simbolo.return_value = nullcontext()
        bot.schwab = Mock()
        bot._adoptar_huerfana_impl = Mock()
        bot._adoptar_huerfana('BA', {'symbol': 'BA', 'cantidad': 5})
        bot._adoptar_huerfana_impl.assert_not_called()
        bot.schwab.obtener_posiciones.assert_not_called()
        self.assertIs(bot._posiciones['BA'], original)

    def test_slow_cycle_records_missed_strategy_signals_without_retroactive_entries(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot._lock = threading.RLock()
        index = pd.date_range('2026-11-04T14:00:00Z', periods=40, freq='min')
        frame = pd.DataFrame({'close': 100., 'atr': 1., 'señal': None}, index=index)
        frame.loc[index[36], 'señal'] = 'SELL'
        frame.loc[index[38], 'señal'] = 'BUY'
        frame.loc[index[39], 'señal'] = 'SELL'
        bot._ultima_vela_procesada = {'BA': index[35]}
        bot._gala10_contexto, bot._gala10_signal_bar = {}, {}
        bot._solo_velas_cerradas = lambda data: data
        bot.estrategia = Mock()
        bot.estrategia.calcular.return_value = frame
        bot.execution_mode_for = Mock(return_value='REAL')
        bot._emit_signal = Mock(return_value={'signal_id': 'missed-bar'})
        bot._audit_event = Mock()
        bot._emit_log = Mock()
        data = Mock()
        data.tf.return_value = frame
        with patch('engine.bot_engine.extraer_features_gala10', return_value={}):
            self.assertEqual(bot._senal_estrategia(data, 'BA', 100), 'SELL')
        self.assertEqual(bot._emit_signal.call_count, 2)
        self.assertTrue(all(call.kwargs['ai_decision'] == 'MISSED_EVALUATION'
                            for call in bot._emit_signal.call_args_list))
        self.assertEqual(bot._gala10_signal_bar['BA'], index[-1])
        self.assertIsNone(bot._senal_estrategia(data, 'BA', 100))
        self.assertEqual(bot._emit_signal.call_count, 2)

    def test_micro_refresh_preserves_indicator_warmup_and_uses_newest_duplicate(self):
        from agents.contracts import DatosMultiTemporal
        bot = ChocoletraBot.__new__(ChocoletraBot)
        index = pd.date_range('2026-11-04T10:00:00Z', periods=120, freq='min')
        original = pd.DataFrame({k: 1. for k in ['open','high','low','close','volume']}, index=index)
        fresh = original.iloc[-60:].copy()
        fresh.loc[index[-1], 'close'] = 2.
        data = DatosMultiTemporal(ticker='BA', velas={'1m': original})
        bot._mtf_cache = {'BA': (1950, data, 1950)}
        bot._mtf_ttl_macro, bot._mtf_ttl = 1000, 10
        bot.schwab = Mock()
        bot.schwab._asegurar_token.return_value = True
        bot.schwab.obtener_1m_reciente.return_value = fresh
        with patch('engine.bot_engine.time.time', return_value=2000):
            result = bot._obtener_mtf('BA')
        self.assertEqual(len(result.velas['1m']), 120)
        self.assertEqual(result.velas['1m'].iloc[-1]['close'], 2.)
