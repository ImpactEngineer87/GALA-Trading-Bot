"""Offline regressions for v3.3.23: MIXED kill switch, REAL capacity and PAPER exits.

Production method bodies are loaded via AST (no broker I/O, credentials or
orders). Run with ``python -m pytest tests/test_v3_3_23_mixed_kill_switch.py``.
"""
import math
import threading
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pandas as pd
import pytest
import yaml

from core.daily_loss_guard import DailyLossGuard
from core.live_capacity import RealCapacityManager
from test_v3_3_15_exit_control import load_methods
from test_v3_3_20_pending_exit_recovery import BOT_SCOPE, TRANSPORT, Clock, bot_fixture
from test_v3_3_22_paper_bar_chronology import bot as paper_bot, position as paper_position

CONTROL = []


@contextmanager
def tracking_control():
    CONTROL.append(True)
    try:
        yield
    finally:
        CONTROL.pop()


Bot = load_methods('engine/bot_engine.py', 'ChocoletraBot', {
    '_daily_guard', 'daily_risk_snapshot', '_evaluar_limite_perdida_diaria',
    '_activar_kill_switch_diario', '_refresh_position_prices',
    '_kill_switch_residual_sweep', '_reserve_real_capacity',
}, {'math': math, 'schwab_control_requests': tracking_control})
SCOPE = Bot._refresh_position_prices.__globals__
NOW = datetime(2026, 10, 12, 11, 0)


def risk_bot(tmp_path, *, real=('NVDA',), tickers=('AAPL', 'NVDA', 'TSLA')):
    b = Bot()
    b.daily_loss_guard = DailyLossGuard(
        {'kill_switch': {'daily_loss': {
            'paper_limit_usd': 700, 'live_limit_usd': 200,
            'state_file': str(Path(tmp_path) / 'guard.json')}}},
        history_path=str(Path(tmp_path) / 'history.csv'), now_provider=lambda: NOW)
    b._lock = threading.RLock()
    b._daily_kill_lock = threading.RLock()
    b._posiciones, b._last_prices, b._last_price_times = {}, {}, {}
    b.tickers = list(tickers)
    b._is_paper_ticker = lambda t, pos=None: (
        (pos or {}).get('execution_mode') or ('REAL' if t in real else 'PAPER')) == 'PAPER'
    b._has_real_tickers = lambda: bool(real)
    b._activar_kill_switch_diario = Mock()
    return b


def open_position(b, ticker, side, entry, qty, price, age=0.0, mode=None):
    b._posiciones[ticker] = {'tipo': side, 'entrada': entry, 'cantidad': qty,
                             'tp1_hit': False, 'execution_mode': mode or (
                                 'PAPER' if b._is_paper_ticker(ticker) else 'REAL')}
    b._last_prices[ticker] = price
    b._last_price_times[ticker] = time.time() - age


# --- Daily kill switch: both books in a MIXED session -----------------------

def test_mixed_paper_breach_trips_the_kill_switch_on_the_paper_book(tmp_path):
    b = risk_bot(tmp_path)
    b.daily_loss_guard.add_realized(True, -650.)
    open_position(b, 'AAPL', 'BUY', 100., 20, 97.)          # -60 floating
    snap = b.daily_risk_snapshot()
    assert snap['mode'] == 'REAL' and snap['mixed_mode']   # REAL keeps the primary fields
    assert snap['books']['PAPER']['net_pnl'] == -710. and snap['books']['PAPER']['breached']
    assert b._evaluar_limite_perdida_diaria()
    status = b._activar_kill_switch_diario.call_args.args[0]
    assert (status['mode'], status['limit_usd']) == ('PAPER', -700.)


def test_mixed_paper_loss_inside_its_limit_does_not_trigger(tmp_path):
    b = risk_bot(tmp_path)
    b.daily_loss_guard.add_realized(True, -642.68)
    assert not b._evaluar_limite_perdida_diaria()
    b._activar_kill_switch_diario.assert_not_called()


def test_real_breach_triggers_on_real_book_and_capacity_room_stays_real(tmp_path):
    b = risk_bot(tmp_path)
    b.daily_loss_guard.add_realized(True, -500.)            # PAPER inside its limit
    b.daily_loss_guard.add_realized(False, -150.)
    open_position(b, 'NVDA', 'SELL', 236.25, 5, 246.25)    # -50 floating
    snap = b.daily_risk_snapshot()
    assert snap['remaining_to_limit_usd'] == 0. and snap['books']['PAPER']['remaining_to_limit_usd'] == 200.
    assert b._evaluar_limite_perdida_diaria()
    assert b._activar_kill_switch_diario.call_args.args[0]['mode'] == 'REAL'


def test_stale_losing_real_price_still_counts_toward_the_kill_switch(tmp_path):
    # Oct 9: an unresolved NVDA short showed REAL $0 with "valoración incompleta".
    b = risk_bot(tmp_path)
    open_position(b, 'NVDA', 'SELL', 236.25, 5, 290., age=600)
    snap = b.daily_risk_snapshot()
    real = snap['books']['REAL']
    assert (real['unpriced_positions'], real['stale_priced_positions']) == (1, 1)
    assert real['net_pnl'] == -268.75 and not snap['pricing_complete']
    assert b._evaluar_limite_perdida_diaria()


def test_stale_gain_cannot_offset_a_fresh_loss(tmp_path):
    b = risk_bot(tmp_path, real=('NVDA', 'TSLA'))
    open_position(b, 'NVDA', 'BUY', 100., 10, 130., age=600)  # stale +300 ignored
    open_position(b, 'TSLA', 'SELL', 400., 3, 470.)             # fresh -210
    assert b.daily_risk_snapshot()['books']['REAL']['net_pnl'] == -210.
    assert b._evaluar_limite_perdida_diaria()


def test_activation_blocks_once_and_liquidates_everything(tmp_path):
    b = risk_bot(tmp_path)
    b._activar_kill_switch_diario = Bot._activar_kill_switch_diario.__get__(b)
    b._emit_log, b._audit_event = Mock(), Mock()
    b._cancelar_todas_ordenes_pendientes, b._liquidar_todo = Mock(), Mock()
    b.daily_loss_guard.add_realized(True, -720.)
    assert b._evaluar_limite_perdida_diaria()
    state = b.daily_loss_guard.snapshot(False)
    assert state['blocked'] and state['trigger_mode'] == 'PAPER'
    assert 'PAPER' in state['reason']
    b._liquidar_todo.assert_called_once()
    assert b._evaluar_limite_perdida_diaria()
    b._liquidar_todo.assert_called_once()


# --- Price freshness -------------------------------------------------------

def test_only_stale_open_positions_are_requoted_in_one_request(tmp_path):
    b = risk_bot(tmp_path)
    open_position(b, 'AAPL', 'BUY', 100., 20, 101.)
    open_position(b, 'NVDA', 'SELL', 236.25, 5, 240., age=200)
    b._posiciones['TSLA'] = {'pendiente': True}
    b._posiciones['MSFT'] = {'reservado': True}
    b.schwab = SimpleNamespace(obtener_multiples_precios=Mock(return_value={'NVDA': 250.}))
    b._refresh_position_prices()
    b.schwab.obtener_multiples_precios.assert_called_once_with(['NVDA'])
    assert b._last_prices['NVDA'] == 250.
    assert time.time() - b._last_price_times['NVDA'] < 5


@pytest.mark.parametrize('reply', [{'NVDA': 0.0}, {'NVDA': float('nan')}, {}, RuntimeError('budget')])
def test_invalid_or_failed_quotes_keep_the_previous_price(tmp_path, reply):
    b = risk_bot(tmp_path)
    open_position(b, 'NVDA', 'SELL', 236.25, 5, 240., age=200)
    stamp = b._last_price_times['NVDA']
    mock = Mock(side_effect=reply) if isinstance(reply, Exception) else Mock(return_value=reply)
    b.schwab = SimpleNamespace(obtener_multiples_precios=mock)
    b._refresh_position_prices()
    assert (b._last_prices['NVDA'], b._last_price_times['NVDA']) == (240., stamp)


# --- Residual sweep while blocked -----------------------------------------

def test_residual_sweep_is_paced_and_needs_remaining_exposure(tmp_path):
    clock = Clock()
    b = risk_bot(tmp_path)
    b._emit_log, b._liquidar_todo = Mock(), Mock()
    b._broker_reconciliation = {'ok': True, 'flat': True}
    with patch.dict(SCOPE, time=SimpleNamespace(monotonic=lambda: clock.mono, time=time.time)):
        b._daily_kill_last_sweep = clock.mono
        open_position(b, 'NVDA', 'SELL', 236.25, 5, 240.)
        b._kill_switch_residual_sweep()
        b._liquidar_todo.assert_not_called()                 # trigger just liquidated
        clock.advance(120)
        b._kill_switch_residual_sweep()
        b._liquidar_todo.assert_called_once()
        clock.advance(60)
        b._kill_switch_residual_sweep()
        b._liquidar_todo.assert_called_once()                # 2-minute cadence
        b._posiciones.clear()
        clock.advance(120)
        b._kill_switch_residual_sweep()
        b._liquidar_todo.assert_called_once()                # flat everywhere: nothing to do
        b._broker_reconciliation = {'ok': False, 'flat': False}
        b._kill_switch_residual_sweep()
        assert b._liquidar_todo.call_count == 2              # untracked broker exposure


# --- Kill-switch REAL close retry window ---------------------------------

@pytest.fixture
def clock():
    value = Clock()
    with patch.dict(BOT_SCOPE, time=SimpleNamespace(
            monotonic=lambda: value.mono, sleep=lambda seconds: value.advance(seconds))):
        yield value


def test_kill_switch_close_keeps_retrying_past_one_hour_until_session_change(clock):
    bot = bot_fixture()                                   # 12:00 New York
    bot._cerrar_posicion('NVDA', 235, 'kill switch — pérdida diaria')
    clock.advance(3630)
    bot._retry_pending_exit('NVDA')
    assert bot._cerrar_posicion_impl.call_count == 2
    assert not bot._posiciones['NVDA'].get('exit_recovery_expired')
    bot._ahora_et = lambda: datetime(2026, 10, 10, 0, 1)
    clock.advance(30)
    assert not bot._retry_pending_exit('NVDA')
    assert bot._cerrar_posicion_impl.call_count == 2
    assert bot._posiciones['NVDA']['exit_recovery_expired']


def test_kill_switch_close_retries_stop_after_the_regular_session(clock):
    bot = bot_fixture()                                   # 12:00 -> deadline 16:05
    bot._cerrar_posicion('NVDA', 235, 'kill switch — pérdida diaria')
    clock.advance(4 * 3600)                               # 16:00: still retrying
    bot._retry_pending_exit('NVDA')
    assert bot._cerrar_posicion_impl.call_count == 2
    clock.advance(600)                                    # 16:10: no after-hours retries
    assert not bot._retry_pending_exit('NVDA')
    assert bot._cerrar_posicion_impl.call_count == 2
    assert bot._posiciones['NVDA']['exit_recovery_expired']


def test_late_kill_switch_still_gets_at_least_one_hour(clock):
    bot = bot_fixture()
    bot._ahora_et = lambda: datetime(2026, 10, 9, 15, 50)
    bot._cerrar_posicion('NVDA', 235, 'kill switch — pérdida diaria')
    clock.advance(3570)
    bot._retry_pending_exit('NVDA')
    assert bot._cerrar_posicion_impl.call_count == 2


def test_kill_switch_revives_an_expired_signal_exit(clock):
    bot = bot_fixture()
    bot._cerrar_posicion('NVDA', 235, 'señal opuesta BUY')
    clock.advance(3600)
    bot._retry_pending_exit('NVDA')
    assert bot._posiciones['NVDA']['exit_recovery_expired']
    bot._cerrar_posicion('NVDA', 235, 'kill switch — pérdida diaria')
    assert bot._cerrar_posicion_impl.call_count == 2
    assert not bot._posiciones['NVDA'].get('exit_recovery_expired')
    clock.advance(30)
    bot._retry_pending_exit('NVDA')
    assert bot._cerrar_posicion_impl.call_count == 3


# --- REAL capacity --------------------------------------------------------

def broker(funds, as_of=None):
    return {'source': 'BROKER', 'as_of_epoch': as_of or time.time(),
            'buying_power': funds * 2, 'available_funds': funds}


def real_pos(age_sec, **extra):
    row = {'execution_mode': 'REAL', 'cantidad': 5, 'entrada': 180., 'sl': 178.,
           'abierta_ts': datetime.now(timezone.utc) - timedelta(seconds=age_sec)}
    row.update(extra)
    return row


def test_settled_real_position_is_not_counted_twice():
    # $3,000 account: NVDA 5 x $180 already deducted by Schwab -> $2,100 left.
    m = RealCapacityManager({'max_positions': 5})
    result = m.reserve('TSLA', 3, 430., 425., broker(2100.), 200., {'NVDA': real_pos(300)})
    assert result['allowed'], result                     # needs 3 x 430 x 1.05 = 1354.5
    assert result['positions_already_in_broker_balance'] == 1
    assert result['remaining_daily_risk'] == 190.       # stop risk still reserved


@pytest.mark.parametrize('extra', [{}, {'pendiente': True}])
def test_fresh_fill_or_working_entry_is_still_reserved(extra):
    m = RealCapacityManager({'max_positions': 5})
    age = 300 if extra else 10
    positions = {'NVDA': real_pos(age, **extra)}
    assert not m.reserve('TSLA', 3, 430., 425., broker(2100.), 200., positions)['allowed']


def test_legacy_position_without_open_time_is_still_reserved():
    row = real_pos(300)
    row.pop('abierta_ts')
    m = RealCapacityManager({'max_positions': 5})
    assert not m.reserve('TSLA', 3, 430., 425., broker(2100.), 200., {'NVDA': row})['allowed']


def test_settled_position_still_takes_a_slot():
    m = RealCapacityManager({'max_positions': 1})
    assert not m.reserve('TSLA', 3, 430., 425., broker(9000.), 200., {'NVDA': real_pos(300)})['allowed']


def test_reservation_is_released_once_its_fill_settles():
    m = RealCapacityManager({'max_positions': 5})
    assert m.reserve('NVDA', 5, 180., 178., broker(3000.), 200., {})['allowed']
    positions = {'NVDA': real_pos(300)}
    assert m.reserve('TSLA', 3, 430., 425., broker(2100.), 200., positions)['allowed']
    assert 'NVDA' not in m.reservations


def test_capacity_balance_read_is_outside_the_engine_lock_and_uses_control(tmp_path):
    b = risk_bot(tmp_path)
    b.config = {'live_trading': {'capacity': {'enabled': True}}}
    b.account_number = 'offline-account'
    b.real_capacity = RealCapacityManager({'max_positions': 5})
    b._audit_event = Mock()
    seen = {}

    def balance(account):
        seen['control'] = bool(CONTROL)
        probe = threading.Thread(target=lambda: seen.update(
            free=b._lock.acquire(timeout=1) and (b._lock.release() or True)))
        probe.start()
        probe.join()
        return broker(5000.)

    b.schwab = SimpleNamespace(obtener_balance=balance)
    order = SimpleNamespace(cantidad=3, precio_limite=430., stop_loss=425.)
    assert b._reserve_real_capacity('TSLA', order)
    assert seen == {'control': True, 'free': True}


def test_unpriced_real_exposure_blocks_and_updates_dashboard_reason(tmp_path):
    b = risk_bot(tmp_path)
    b.config = {'live_trading': {'capacity': {'enabled': True}}}
    b.account_number = 'offline-account'
    b.real_capacity = RealCapacityManager({'max_positions': 5})
    b._audit_event = Mock()
    b._posiciones['NVDA'] = {'tipo': 'SELL', 'entrada': 236.25, 'cantidad': 5,
                             'execution_mode': 'REAL'}           # never priced
    b.schwab = SimpleNamespace(obtener_balance=lambda account: broker(5000.))
    order = SimpleNamespace(cantidad=3, precio_limite=430., stop_loss=425.)
    assert not b._reserve_real_capacity('TSLA', order)
    assert 'unpriced' in b.real_capacity.last['reason']


# --- PAPER exits ------------------------------------------------------------

def bars(*rows, start='2026-10-09T13:31:00Z'):
    index = pd.date_range(start=start, periods=len(rows), freq='min')
    return pd.DataFrame([{'open': 100., 'high': h, 'low': l, 'close': 100.} for h, l in rows],
                        index=index)


def test_stop_touched_on_a_skipped_bar_is_simulated_once():
    b, pos = paper_bot(), paper_position()
    b._gestionar_paper('AAPL', pos, bars((100.5, 98.5), (101., 99.5), (101., 99.6)))
    b._cerrar_posicion.assert_called_once_with('AAPL', 99., motivo='SL')
    assert pd.Timestamp(pos['paper_last_managed_bar_ts']) == pd.Timestamp('2026-10-09T13:31:00Z')


def test_tp1_then_breakeven_on_later_missed_bars_run_in_order():
    b, pos = paper_bot(), paper_position(salida_parcial=True)
    b._ejecutar_parcial.side_effect = lambda ticker, current: current.update(
        tp1_hit=True, sl_actual=100.)
    b._gestionar_paper('AAPL', pos, bars((103., 99.5), (101., 99.8), (101., 100.2)))
    b._ejecutar_parcial.assert_called_once()
    b._cerrar_posicion.assert_called_once_with('AAPL', 100., motivo='SL(breakeven)')


def test_replay_never_reuses_entry_or_already_managed_bars():
    b = paper_bot()
    pos = paper_position(paper_last_managed_bar_ts='2026-10-09T13:32:00+00:00')
    frame = bars((101., 98.), (101., 98.), (101., 99.5), (101., 99.6),
                 start='2026-10-09T13:30:00Z')
    b._gestionar_paper('AAPL', pos, frame)
    b._cerrar_posicion.assert_not_called()
    assert pd.Timestamp(pos['paper_last_managed_bar_ts']) == pd.Timestamp('2026-10-09T13:33:00Z')


def macro_wait_paper_bot():
    bot = bot_fixture(side='BUY')
    bot._posiciones['NVDA']['execution_mode'] = 'PAPER'
    bot._is_paper_ticker = lambda *args, **kwargs: True
    now = pd.Timestamp.now(tz='UTC').floor('min')
    frame = pd.DataFrame({'close': [236.] * 40},
                         index=pd.date_range(end=now, periods=40, freq='min'))
    control = TRANSPORT['_TRAFFIC_CONTEXT']
    seen = {}

    def read(ticker):
        seen['control'] = bool(getattr(control, 'control', False))
        return frame
    bot.schwab = SimpleNamespace(obtener_1m_reciente=Mock(side_effect=read))
    bot._senal_estrategia = Mock(return_value='SELL')
    bot._close_opposite_without_entry = Mock(return_value=True)
    return bot, seen


def test_macro_wait_keeps_paper_stops_running_without_control_capacity():
    bot, seen = macro_wait_paper_bot()
    bot._gestionar_paper = Mock(side_effect=lambda ticker, pos, frame: bot._posiciones.pop(ticker))
    assert bot._check_opposite_exit_without_macro('NVDA', 'MACRO_STALE')
    assert len(bot._gestionar_paper.call_args.args[2]) == 39   # forming bar excluded
    assert seen == {'control': False}
    assert bot._last_prices['NVDA'] == 236.
    bot._senal_estrategia.assert_not_called()


def test_macro_wait_paper_position_still_honours_an_opposite_signal():
    bot, _ = macro_wait_paper_bot()
    bot._gestionar_paper = Mock()
    assert bot._check_opposite_exit_without_macro('NVDA', 'WARMUP_PENDING')
    bot._close_opposite_without_entry.assert_called_once_with('NVDA', 'SELL', 236., 'WARMUP_PENDING')


# --- Dashboard session PnL --------------------------------------------------

def test_session_pnl_includes_tp1_profit_of_open_partial_positions():
    import server
    rows = [{'status': 'PARTIAL', 'mode': 'PAPER', 'floating_pnl': -1., 'realized_pnl': 9.8},
            {'status': 'OPEN', 'mode': 'REAL', 'floating_pnl': 2., 'realized_pnl': 0.}]
    summary = server._position_pnl_summary(rows)
    assert summary['paper_open_realized_pnl'] == 9.8
    history = {'closed_count': 1, 'pnl_total': -5., 'paper_pnl': -5., 'real_pnl': 0.,
               'paper_closed_count': 1, 'real_closed_count': 0, 'paper_realized_today': -5.,
               'real_realized_today': 0., 'paper_closed_today': 1, 'real_closed_today': 0}
    with patch.object(server, '_history_summary', return_value=history), \
            patch.object(server, '_engine_positions_snapshot', return_value=rows), \
            patch.object(server, '_engine_broker_reconciliation_snapshot', return_value={'positions': []}):
        stats = server._dashboard_stats()
    assert stats['paper_session_pnl'] == round(-5. + 9.8 - 1., 2)


# --- Broker balance and sizing wiring -----------------------------------------

def test_cash_account_without_buying_power_uses_cash_available():
    from api.schwab_client import SchwabClient
    client = SchwabClient.__new__(SchwabClient)
    client._paper, client.refresh_token = False, 'offline'
    client._asegurar_token = lambda: True
    payload = {'securitiesAccount': {'currentBalances': {
        'cashAvailableForTrading': 2500., 'cashBalance': 2600.}}}
    client._get_autorizado = lambda url, **kw: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: payload)
    result = client.obtener_balance('offline-account')
    assert result['buying_power'] == result['available_funds'] == 2500.


def test_per_ticker_execution_makes_share_counts_exact(monkeypatch, tmp_path):
    import engine.bot_engine as engine_module
    monkeypatch.chdir(tmp_path)
    for name in ('SchwabClient', 'SheetsClient', 'TelegramBot', 'TOSReader', 'MotorDeCorrelacion'):
        monkeypatch.setattr(engine_module, name, MagicMock())
    monkeypatch.setattr(engine_module, 'OrquestadorAgentes', lambda cfg: SimpleNamespace())
    monkeypatch.setattr(engine_module, 'log_info', lambda *args, **kwargs: None)
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({
        'risk': {'paper_trading': True, 'sizing_mode': 'atr'},
        'live_trading': {'per_ticker_execution': True,
                         'ticker_execution_path': str(tmp_path / 'execution.json')},
    }), encoding='utf-8')
    engine = engine_module.ChocoletraBot(str(path))
    assert engine.ejecutor_hibrido.sizing_mode == engine.ejecutor_paper.sizing_mode == 'fixed'
    base = engine_module.OrdenTradingCalculada(
        ticker='AAPL', tipo='BUY', precio_limite=100., stop_loss=99., trailing_stop_pct=1.,
        cantidad=20, riesgo_usd=20., atr_actual=1., cantidad_parcial=14, cantidad_resto=6,
        salida_parcial_activa=True)
    from dataclasses import replace
    master_signal = replace(base, cantidad=50, cantidad_parcial=35, cantidad_resto=15)
    assert engine.ejecutor_paper._normalizar_cantidad_final(base, master_signal).cantidad == 20
