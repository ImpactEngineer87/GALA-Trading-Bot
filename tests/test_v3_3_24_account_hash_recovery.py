"""Offline regressions for v3.3.24: account hash recovery after an OAuth outage.

If OAuth was down when the engine started, the plain account number stayed in
use. Every Schwab account read then returned HTTP 400, so a stopped engine could
neither confirm flatness nor be restarted after Schwab was reconnected.
Production method bodies are loaded via AST; no broker I/O or credentials.
"""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_v3_3_15_exit_control import load_methods

PLAIN, HASH = '12345678', 'ENCRYPTED-HASH-0001'
Bot = load_methods('engine/bot_engine.py', 'ChocoletraBot', {
    '_resolver_hash_cuenta', '_refresh_broker_reconciliation', '_preparar_trading_real',
}, {'schwab_control_requests': nullcontext})
SCOPE = Bot._resolver_hash_cuenta.__globals__


class Clock:
    def __init__(self):
        self.mono = 100.

    def advance(self, seconds):
        self.mono += seconds


def schwab(hash_value=HASH):
    def account_read(account):
        # Schwab rejects the plain number on /accounts/{account} with HTTP 400.
        return [] if account == HASH else None
    return SimpleNamespace(
        obtener_account_hash=Mock(return_value=hash_value),
        obtener_posiciones=Mock(side_effect=account_read),
        obtener_ordenes_abiertas=Mock(side_effect=account_read),
        token_es_usuario=True,
    )


def stuck_engine(hash_value=HASH):
    bot = Bot()
    bot.account_number, bot._account_plain = PLAIN, PLAIN
    bot.tickers = ['NVDA']
    bot.schwab = schwab(hash_value)
    bot._emit_log = Mock()
    bot._real_tickers = lambda tickers: tickers
    bot._posiciones = {}
    bot._set_broker_reconciliation = lambda **state: state
    bot._broker_read_with_retry = lambda label, read, **kw: (
        (True, value, '') if (value := read()) is not None else (False, None, 'HTTP 400: HTTPError'))
    bot._iter_broker_orders = lambda rows: iter(rows or [])
    bot._symbol_from_broker_order = lambda row: ''
    bot.ejecutor_hibrido = SimpleNamespace(ordenes_trabajando_ticker=Mock(return_value=[]))
    return bot


def test_reconnected_stopped_engine_resolves_hash_and_confirms_flat():
    bot = stuck_engine()
    state = bot._refresh_broker_reconciliation(context='DASHBOARD_READ_ONLY', require_flat=True)
    assert state['ok'] and state['flat'], state
    assert bot.account_number == HASH
    bot.schwab.obtener_posiciones.assert_called_once_with(HASH)


def test_resolved_hash_is_not_looked_up_again():
    bot = stuck_engine()
    assert bot._resolver_hash_cuenta() and bot._resolver_hash_cuenta()
    bot.schwab.obtener_account_hash.assert_called_once_with(PLAIN)


def test_failed_lookup_stays_fail_closed_and_is_paced():
    clock = Clock()
    bot = stuck_engine(hash_value=None)
    with patch.dict(SCOPE, time=SimpleNamespace(monotonic=lambda: clock.mono)):
        state = bot._refresh_broker_reconciliation(context='KILL_SWITCH', require_flat=True)
        assert not state['ok'] and not state['flat']      # never treated as flat
        assert bot.account_number == PLAIN
        bot._resolver_hash_cuenta()
        assert bot.schwab.obtener_account_hash.call_count == 1  # paced
        clock.advance(15)
        bot.schwab.obtener_account_hash.return_value = HASH
        assert bot._resolver_hash_cuenta()
    assert bot.account_number == HASH


def test_engine_without_a_plain_number_never_queries_schwab():
    bot = stuck_engine()
    bot._account_plain = ''
    assert not bot._resolver_hash_cuenta()
    bot.schwab.obtener_account_hash.assert_not_called()


def test_startup_preparation_uses_the_shared_resolver_once():
    bot = stuck_engine()
    bot._account_plain = ''
    bot._has_real_tickers = lambda: True

    def token(forzar=False):
        bot._resolver_hash_cuenta()          # the token check resolves the hash
        return True
    bot._asegurar_token_trading = token
    bot._preparar_trading_real()
    assert bot.account_number == HASH and bot._trading_listo
    bot.schwab.obtener_account_hash.assert_called_once_with(PLAIN)


def test_startup_preparation_without_hash_blocks_real_orders():
    bot = stuck_engine(hash_value=None)
    bot._account_plain = ''
    bot._has_real_tickers = lambda: True
    bot._asegurar_token_trading = lambda forzar=False: bot._resolver_hash_cuenta() or True
    bot._preparar_trading_real()
    assert bot.account_number == PLAIN and bot._trading_listo is False
