"""Offline regressions for PAPER entry/exit candle chronology.

Loads actual production method bodies with real pandas; no broker I/O, full
application imports or credentials. Profitability is not asserted by these tests.
"""
import math
import tempfile
import threading
import unittest
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd

from test_v3_3_15_exit_control import load_methods
from core.daily_loss_guard import DailyLossGuard
from core.position_state import PositionState, state_for_signal, entry_action_for_state


Bot = load_methods('engine/bot_engine.py', 'ChocoletraBot', {
    '_gestionar_paper', '_gestionar_paper_bar', '_paper_exit_bar_timestamp',
    '_registrar_posicion_abierta', '_registrar_posicion_abierta_impl', '_abrir_operacion',
    '_daily_guard', 'daily_risk_snapshot', '_evaluar_limite_perdida_diaria',
}, {'pd': pd, 'math': math, 'state_for_signal': state_for_signal,
    'entry_action_for_state': entry_action_for_state,
    'ContextoMercado': lambda **fields: SimpleNamespace(**fields)})


def frame(timestamp='2026-10-09T13:30:00Z', high=102., low=98., close=100.):
    return pd.DataFrame([{'open': 100., 'high': high, 'low': low, 'close': close}],
                        index=pd.to_datetime([timestamp]))


def position(side='BUY', **overrides):
    base = {'execution_mode': 'PAPER', 'tipo': side, 'entrada': 100.,
            'sl_actual': 99. if side == 'BUY' else 101.,
            'tp1_hit': False, 'salida_parcial': False, 'tp1': 102. if side == 'BUY' else 98.,
            'tp_final': 105. if side == 'BUY' else 95.,
            'abierta_ts': datetime.fromisoformat('2026-10-09T09:31:05-04:00'),
            'paper_entry_bar_ts': '2026-10-09T13:30:00Z'}
    base.update(overrides)
    return base


def bot():
    instance = Bot()
    instance._cerrar_posicion = Mock(return_value=True)
    instance._ejecutar_parcial = Mock()
    return instance


class PaperChronologyTests(unittest.TestCase):
    def test_pre_entry_stop_touch_cannot_close_a_new_long_or_short(self):
        for side in ('BUY', 'SELL'):
            with self.subTest(side=side):
                b, pos = bot(), position(side)
                b._gestionar_paper('AAPL', pos, frame())
                b._cerrar_posicion.assert_not_called()
                self.assertNotIn('paper_last_managed_bar_ts', pos)

    def test_entry_candle_cannot_award_a_pre_entry_take_profit_or_partial(self):
        for side in ('BUY', 'SELL'):
            for partial in (True, False):
                with self.subTest(side=side, partial=partial):
                    b, pos = bot(), position(side, salida_parcial=partial)
                    candle = frame(high=106., low=100.) if side == 'BUY' else frame(high=100., low=94.)
                    b._gestionar_paper('AAPL', pos, candle)
                    b._cerrar_posicion.assert_not_called()
                    b._ejecutar_parcial.assert_not_called()

    def test_a_genuine_later_candle_still_closes_at_the_original_stop(self):
        for side, price in (('BUY', 99.), ('SELL', 101.)):
            with self.subTest(side=side):
                b, pos = bot(), position(side)
                b._gestionar_paper('AAPL', pos, frame('2026-10-09T13:31:00Z'))
                b._cerrar_posicion.assert_called_once_with('AAPL', price, motivo='SL')

    def test_later_take_profit_still_uses_original_target_and_stop_precedence(self):
        b, pos = bot(), position()
        b._gestionar_paper('AAPL', pos, frame('2026-10-09T13:31:00Z', high=106., low=100.))
        b._cerrar_posicion.assert_called_once_with('AAPL', 105., motivo='TP')
        b, pos = bot(), position()
        b._gestionar_paper('AAPL', pos, frame('2026-10-09T13:31:00Z', high=106., low=98.))
        b._cerrar_posicion.assert_called_once_with('AAPL', 99., motivo='SL')

    def test_partial_does_not_reuse_the_same_pre_breakeven_candle_on_next_poll(self):
        b, pos = bot(), position(salida_parcial=True)
        candle = frame('2026-10-09T13:31:00Z', high=103., low=99.5)
        def partial(ticker, current):
            current.update(tp1_hit=True, sl_actual=100.)
        b._ejecutar_parcial.side_effect = partial
        b._gestionar_paper('AAPL', pos, candle)
        b._gestionar_paper('AAPL', pos, candle.copy())
        b._ejecutar_parcial.assert_called_once()
        b._cerrar_posicion.assert_not_called()
        b._gestionar_paper('AAPL', pos, frame('2026-10-09T13:32:00Z', high=101., low=99.5))
        b._cerrar_posicion.assert_called_once_with('AAPL', 100., motivo='SL(breakeven)')

    def test_repeated_or_out_of_order_later_bars_do_not_repeat_management(self):
        b, pos = bot(), position()
        candle = frame('2026-10-09T13:32:00Z', high=101., low=100.)
        b._gestionar_paper('AAPL', pos, candle)
        b._gestionar_paper('AAPL', pos, frame('2026-10-09T13:32:00Z'))
        b._gestionar_paper('AAPL', pos, frame('2026-10-09T13:31:00Z'))
        b._cerrar_posicion.assert_not_called()

    def test_failed_bar_handler_does_not_advance_the_processed_timestamp(self):
        b, pos = bot(), position()
        b._cerrar_posicion.side_effect = RuntimeError('offline handler failure')
        with self.assertRaises(RuntimeError):
            b._gestionar_paper('AAPL', pos, frame('2026-10-09T13:31:00Z'))
        self.assertNotIn('paper_last_managed_bar_ts', pos)
        b._cerrar_posicion.side_effect = None
        b._gestionar_paper('AAPL', pos, frame('2026-10-09T13:31:00Z'))
        self.assertEqual(b._cerrar_posicion.call_count, 2)

    def test_legacy_position_open_time_rejects_a_wholly_pre_entry_candle(self):
        for opened in ('2026-10-09T09:31:05-04:00', '2026-10-09T09:31:05'):
            b, pos = bot(), position(paper_entry_bar_ts='', abierta_ts=datetime.fromisoformat(opened))
            b._gestionar_paper('AAPL', pos, frame())
            b._cerrar_posicion.assert_not_called()
            b._gestionar_paper('AAPL', pos, frame('2026-10-09T13:31:00Z'))
            b._cerrar_posicion.assert_called_once()

    def test_naive_utc_and_aware_utc_bar_timestamps_are_equivalent(self):
        b, pos = bot(), position()
        b._gestionar_paper('AAPL', pos, frame('2026-10-09T13:30:00'))
        b._cerrar_posicion.assert_not_called()
        b._gestionar_paper('AAPL', pos, frame('2026-10-09T13:31:00'))
        b._cerrar_posicion.assert_called_once()

    def test_late_execution_cannot_use_another_candle_already_closed_before_entry(self):
        b, pos = bot(), position(abierta_ts=datetime.fromisoformat('2026-10-09T09:33:05-04:00'))
        b._gestionar_paper('AAPL', pos, frame('2026-10-09T13:31:00Z'))
        b._cerrar_posicion.assert_not_called()

    def test_new_position_resets_bar_deduplication_for_same_symbol(self):
        b = bot()
        first = position()
        b._gestionar_paper('AAPL', first, frame('2026-10-09T13:31:00Z'))
        second = position(paper_entry_bar_ts='2026-10-09T13:31:00Z',
                          abierta_ts=datetime.fromisoformat('2026-10-09T09:32:05-04:00'))
        b._gestionar_paper('AAPL', second, frame('2026-10-09T13:31:00Z'))
        self.assertEqual(b._cerrar_posicion.call_count, 1)
        b._gestionar_paper('AAPL', second, frame('2026-10-09T13:32:00Z'))
        self.assertEqual(b._cerrar_posicion.call_count, 2)

    def test_unknown_or_malformed_timing_never_invents_a_stop_or_target_fill(self):
        cases = [(position(), frame().iloc[:0]),
                 (position(paper_entry_bar_ts='bad'), frame('2026-10-09T13:31:00Z')),
                 (position(paper_last_managed_bar_ts='bad'), frame('2026-10-09T13:31:00Z')),
                 (position(paper_entry_bar_ts='', abierta_ts=None), frame('2026-10-09T13:31:00Z')),
                 (position(), frame().reset_index(drop=True)),
                 (position(), frame().set_axis(pd.Index([1.5]))) ]
        for pos, candle in cases:
            b = bot()
            b._gestionar_paper('AAPL', pos, candle)
            b._cerrar_posicion.assert_not_called()
            b._ejecutar_parcial.assert_not_called()


class MixedGuardDiagnosticTests(unittest.TestCase):
    def test_mixed_mode_paper_loss_now_trips_the_kill_switch(self):
        # v3.3.23 replaced the documented limitation: a MIXED session checks the
        # PAPER book against its own limit, and any breach stops everything.
        with tempfile.TemporaryDirectory() as directory:
            b = bot()
            config = {'kill_switch': {'daily_loss': {
                'paper_limit_usd': 700, 'live_limit_usd': 200,
                'state_file': str(Path(directory) / 'guard.json')}}}
            b.daily_loss_guard = DailyLossGuard(config,
                history_path=str(Path(directory) / 'history.csv'),
                now_provider=lambda: datetime(2026, 10, 9, 13, 59))
            b.daily_loss_guard.add_realized(True, -800.)
            b._has_real_tickers = lambda: True
            b._lock, b._posiciones, b._last_prices = threading.RLock(), {}, {}
            b._activar_kill_switch_diario = Mock()
            self.assertEqual(b.daily_risk_snapshot()['mode'], 'REAL')
            self.assertTrue(b._evaluar_limite_perdida_diaria())
            status = b._activar_kill_switch_diario.call_args.args[0]
            self.assertEqual((status['mode'], status['limit_usd'], status['net_pnl']),
                             ('PAPER', -700., -800.))


class PaperEntryIntegrationTests(unittest.TestCase):
    def setup_entry(self):
        b = bot()
        b._is_paper_ticker = lambda ticker, pos=None: True
        b._daily_blocked = lambda: False
        b.config, b.capital, b.account_number = {'atr': {'period': 14}}, 10000., 'offline-account'
        b._lock, b._posiciones, b._resumen = threading.RLock(), {}, {'buy': 0, 'sell': 0}
        b.shares_for_ticker = lambda ticker: 20
        order = SimpleNamespace(tipo='BUY', cantidad=20, cantidad_resto=20,
                                precio_limite=100., stop_loss=99., take_profit=105.,
                                take_profit_parcial=102., salida_parcial_activa=False,
                                modo_resto='breakeven', trailing_stop_pct=1.)
        b.gestor_riesgo = SimpleNamespace(calcular_orden=Mock(return_value=order))
        b.ejecutor_paper = SimpleNamespace(ejecutar_con_persecucion=Mock(return_value={
            'status': 'SIMULADA', 'order_id': 'paper-entry', 'orden_ajustada': order}))
        b._state_machine = lambda: SimpleNamespace(open=lambda *args: SimpleNamespace())
        b._assign_entry_lifecycle = lambda *args: {
            'position_id': 'paper-position', 'execution_id': 'paper-execution',
            'signal_id': 'paper-signal', 'reversal_id': '', 'correlation_id': 'paper-correlation'}
        b._lifecycle_scope = lambda context: nullcontext()
        b._ahora_et = lambda: datetime.fromisoformat('2026-10-09T09:31:05-04:00')
        b.motor_señales = SimpleNamespace(registrar_entrada=Mock())
        b._record_entry_quality = Mock(return_value={'linked': True})
        b._audit_event = Mock(return_value={'linked': True})
        b._history_event = Mock(return_value={'linked': True})
        b._emit_log, b._emit_trade, b._set_ticker_processing = Mock(), Mock(), Mock()
        b.orquestador = SimpleNamespace(al_abrir=Mock())
        b.telegram = SimpleNamespace(enviar_alerta_temporal=Mock())
        prop = SimpleNamespace(ticker='AAPL', tipo='BUY', precio_entrada=100.,
                               stop_loss=99., take_profit=105., fvg=None)
        decision = SimpleNamespace(propuesta=prop, veredicto=None, prob_meta=.6,
                                   confluencia_estrategia=False, direccion_dia=None,
                                   resumen='offline')
        candles = pd.concat([frame()] * 40, ignore_index=True)
        candles.index = pd.date_range(end='2026-10-09T13:30:00Z', periods=40, freq='min')
        return b, decision, candles

    def test_actual_opening_path_persists_signal_bar_without_changing_size_or_stops(self):
        b, decision, candles = self.setup_entry()
        self.assertTrue(b._abrir_operacion(decision, 100., candles))
        pos = b._posiciones['AAPL']
        self.assertEqual(pd.Timestamp(pos['paper_entry_bar_ts']), candles.index[-1])
        self.assertEqual((pos['cantidad'], pos['sl_actual'], pos['tp_final']), (20, 99., 105.))
        self.assertEqual(b.gestor_riesgo.calcular_orden.call_args.kwargs['cantidad_override'], 20)
        b._gestionar_paper('AAPL', pos, candles)
        b._cerrar_posicion.assert_not_called()
        b._gestionar_paper('AAPL', pos, frame('2026-10-09T13:31:00Z'))
        b._cerrar_posicion.assert_called_once_with('AAPL', 99., motivo='SL')

    def test_missing_candle_time_blocks_before_the_simulated_fill(self):
        b, decision, candles = self.setup_entry()
        candles = candles.reset_index(drop=True)
        self.assertFalse(b._abrir_operacion(decision, 100., candles))
        b.ejecutor_paper.ejecutar_con_persecucion.assert_not_called()
        self.assertEqual(decision.execution_skip_reason, 'PAPER_BAR_TIME_UNAVAILABLE')
        self.assertEqual(b._posiciones, {})


if __name__ == '__main__':
    unittest.main()
