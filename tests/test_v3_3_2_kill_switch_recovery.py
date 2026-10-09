import hashlib
import json
import pytest
from types import SimpleNamespace

from core.execution_v2 import SmartExit
from api.schwab_transport import SchwabRequestGate


@pytest.fixture(autouse=True)
def isolated_request_gate(tmp_path, monkeypatch):
    monkeypatch.setattr('api.schwab_transport._GATE', SchwabRequestGate(
        tmp_path / 'request_pause.json', diagnostic_sink=lambda event: None))


class RecoveryBroker:
    TRADER_URL = 'https://test.invalid'
    request_timeout_s = fill_timeout_s = flat_timeout_s = 0.01

    def __init__(self, tmp_path, side='BUY', qty=3):
        self.side = side
        self.qty = qty
        self.posts = []
        self.audit = []
        self.orders = {'old': {'status': 'DESCONOCIDO', 'filled': 0, 'avg_price': 0}}
        self.old_working = False
        self.ticker_working = []
        self.working_readable = True
        self.config = {
            'pending_path': str(tmp_path / 'pending.json'),
            'history_path': str(tmp_path / 'history.jsonl'),
            'market_fallback': True,
            'limit_wait_sec': 0.01,
        }
        self.engine = SmartExit(self, self.config)

    def key(self):
        return hashlib.sha256(b'fake-account').hexdigest()[:16] + ':META'

    def seed_pending(self, side='BUY', qty=3, kind='MARKET'):
        state = {
            'execution_id': 'old-exec', 'ticker': 'META', 'side': side,
            'quantity': qty, 'reason': 'old exit', 'created_utc': '2026-09-15T00:00:00Z',
            'reference_price': 100.0, 'quote': {}, 'limit': None,
            'route_reason': 'QUOTE_UNAVAILABLE',
            'legs': [{'order_id': 'old', 'type': kind, 'quantity': qty,
                      'limit_price': None, 'filled': 0, 'avg_price': 0,
                      'status': 'DESCONOCIDO'}],
        }
        self.engine.pending.put(self.key(), state)

    def _position_snapshot(self, account, ticker):
        return {
            'long': self.qty if self.side == 'BUY' else 0,
            'short': self.qty if self.side == 'SELL' else 0,
        }

    def orden_trabajando(self, account, oid):
        if not self.working_readable:
            return None
        return self.old_working if oid == 'old' else False

    def ordenes_trabajando_ticker(self, account, ticker):
        if not self.working_readable:
            return None
        return list(self.ticker_working)

    def detalle_orden(self, account, oid):
        return dict(self.orders.get(oid, {'status': 'DESCONOCIDO', 'filled': 0, 'avg_price': 0}))

    def _http_session(self):
        return self

    def post(self, url, json, timeout, allow_redirects=False):
        assert allow_redirects is False
        self.posts.append(json)
        oid = 'new'
        qty = int(json['orderLegCollection'][0]['quantity'])
        self.orders[oid] = {'status': 'FILLED', 'filled': qty, 'avg_price': 99.75}
        self.qty = 0
        return SimpleNamespace(headers={'Location': '/orders/new'}, raise_for_status=lambda: None)

    def esperar_estado_orden(self, *args, **kwargs):
        return {'confirmado': True, 'broker_status': 'FILLED'}

    def esperar_posicion_plana(self, *args, **kwargs):
        return {'confirmado': self.qty == 0}

    def cancelar_orden_confirmada(self, *args, **kwargs):
        return {'confirmado': False, 'broker_status': 'DESCONOCIDO'}

    def _audit(self, event, **fields):
        self.audit.append((event, fields))


def test_emergency_retires_stale_nonworking_exit_and_flattens_current_broker_position(tmp_path):
    b = RecoveryBroker(tmp_path, side='BUY', qty=3)
    b.seed_pending(side='BUY', qty=3)

    result = b.engine.close('META', 'BUY', 3, 'fake-account', 'kill switch — cierre de mercado', 100)

    assert result['status'] == 'OK'
    assert result['posicion_plana'] is True
    assert result['fill_price_verified'] is True
    assert len(b.posts) == 1
    assert b.posts[0]['orderType'] == 'MARKET'
    assert b.posts[0]['orderLegCollection'][0]['instruction'] == 'SELL'
    assert b.posts[0]['orderLegCollection'][0]['quantity'] == 3
    assert b.engine.pending.get(b.key()) is None
    assert any(event == 'STALE_EXIT_RETIRED_FOR_EMERGENCY' for event, _ in b.audit)


def test_emergency_uses_current_broker_side_not_stale_local_side(tmp_path):
    b = RecoveryBroker(tmp_path, side='SELL', qty=3)
    b.seed_pending(side='BUY', qty=3)

    result = b.engine.close('META', 'BUY', 3, 'fake-account', 'kill switch — cierre de mercado', 100)

    assert result['status'] == 'OK'
    assert b.posts[0]['orderLegCollection'][0]['instruction'] == 'BUY_TO_COVER'
    assert result['broker_side'] == 'SELL'


def test_stale_exit_broker_already_flat_is_cleared_without_duplicate_and_quarantines_price(tmp_path):
    b = RecoveryBroker(tmp_path, side='BUY', qty=0)
    b.seed_pending(side='BUY', qty=3)

    result = b.engine.close('META', 'BUY', 3, 'fake-account', 'kill switch — cierre de mercado', 100)

    assert result['status'] == 'OK'
    assert result['posicion_plana'] is True
    assert result['fill_price_verified'] is False
    assert b.posts == []
    assert b.engine.pending.get(b.key()) is None
    assert any(event == 'EXIT_RECOVERED_FLAT' for event, _ in b.audit)


def test_normal_exit_retires_stale_nonworking_pending_after_authoritative_proof(tmp_path):
    b = RecoveryBroker(tmp_path, side='BUY', qty=3)
    b.seed_pending(side='BUY', qty=3)

    result = b.engine.close('META', 'BUY', 3, 'fake-account', 'normal reversal', 100)

    assert result['status'] == 'OK'
    assert result['posicion_plana'] is True
    assert len(b.posts) == 1
    assert b.engine.pending.get(b.key()) is None
    assert any(event == 'STALE_EXIT_RETIRED_FOR_NORMAL_CLOSE' for event, _ in b.audit)


def test_normal_exit_stale_recovery_fails_closed_when_broker_side_differs(tmp_path):
    b = RecoveryBroker(tmp_path, side='SELL', qty=3)
    b.seed_pending(side='BUY', qty=3)

    result = b.engine.close('META', 'BUY', 3, 'fake-account', 'normal reversal', 100)

    assert result['status'] == 'RECONCILIAR'
    assert 'Broker side differs' in result['mensaje']
    assert b.posts == []


def test_working_old_order_still_blocks_duplicate_emergency_close(tmp_path):
    b = RecoveryBroker(tmp_path, side='BUY', qty=3)
    b.seed_pending(side='BUY', qty=3)
    b.old_working = True

    result = b.engine.close('META', 'BUY', 3, 'fake-account', 'kill switch', 100)

    assert result['status'] == 'RECONCILIAR'
    assert 'still WORKING' in result['mensaje']
    assert b.posts == []


def test_unreadable_working_snapshot_fails_closed(tmp_path):
    b = RecoveryBroker(tmp_path, side='BUY', qty=3)
    b.seed_pending(side='BUY', qty=3)
    b.working_readable = False

    result = b.engine.close('META', 'BUY', 3, 'fake-account', 'kill switch', 100)

    assert result['status'] == 'RECONCILIAR'
    assert b.posts == []


def test_other_working_order_on_symbol_blocks_stale_replacement(tmp_path):
    b = RecoveryBroker(tmp_path, side='BUY', qty=3)
    b.seed_pending(side='BUY', qty=3)
    b.ticker_working = ['protective-1']

    result = b.engine.close('META', 'BUY', 3, 'fake-account', 'kill switch', 100)

    assert result['status'] == 'RECONCILIAR'
    assert 'WORKING broker order' in result['mensaje']
    assert b.posts == []

def test_terminal_filled_hint_with_missing_fill_details_uses_broker_flat_not_stale_expected_qty(tmp_path):
    b = RecoveryBroker(tmp_path, side='BUY', qty=0)
    b.seed_pending(side='BUY', qty=3, kind='LIMIT')
    b.old_working = True

    def cancel(*args, **kwargs):
        b.old_working = False
        return {
            'confirmado': False,
            'broker_status': 'FILLED',
            'filled': 0.0,
            'terminal_by_broker_error': True,
        }

    b.cancelar_orden_confirmada = cancel
    result = b.engine.close('META', 'BUY', 3, 'fake-account', 'kill switch', 100)

    assert result['status'] == 'OK'
    assert result['posicion_plana'] is True
    assert result['fill_price_verified'] is False
    assert b.posts == []
    assert b.engine.pending.get(b.key()) is None
