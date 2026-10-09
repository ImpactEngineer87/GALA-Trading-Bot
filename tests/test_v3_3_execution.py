import time
from types import SimpleNamespace
import pytest
from core.execution_v2 import SmartExit, marketable_price
from api.schwab_transport import SchwabRequestGate


@pytest.fixture(autouse=True)
def isolated_request_gate(tmp_path, monkeypatch):
    # SmartExit now shares the transport gate. Keep test traffic/pause state
    # isolated from other tests and any runtime recovery file.
    monkeypatch.setattr('api.schwab_transport._GATE', SchwabRequestGate(
        tmp_path / 'request_pause.json', diagnostic_sink=lambda event: None))


class Broker:
    TRADER_URL = 'https://test.invalid'
    request_timeout_s = fill_timeout_s = flat_timeout_s = .01

    def __init__(self, tmp_path, qty=3, side='BUY', behavior='fill'):
        self.quantity = qty
        self.side = side
        self.behavior = behavior
        self.posts = []
        self.orders = {}
        self.cancelled = []
        self.audit = []
        self._quote_provider = lambda ticker: {'bid': 100., 'ask': 100.10, 'quote_time': time.time(), 'delayed': False}
        self.engine = SmartExit(self, {'pending_path': str(tmp_path/'pending.json'),
            'history_path': str(tmp_path/'history.jsonl'), 'limit_wait_sec': .01})

    def _position_snapshot(self, account, ticker):
        return {'long': self.quantity if self.side == 'BUY' else 0,
                'short': self.quantity if self.side == 'SELL' else 0}

    def _http_session(self):
        return self

    def post(self, url, json, timeout, allow_redirects=False):
        assert allow_redirects is False
        self.posts.append(json)
        oid = str(len(self.posts))
        qty = json['orderLegCollection'][0]['quantity']
        is_limit = json['orderType'] == 'LIMIT'
        filled = (1 if self.behavior == 'partial' else 0) if is_limit and self.behavior != 'fill' else qty
        if self.behavior == 'market_pending':
            filled = 0
        self.quantity -= filled
        self.orders[oid] = {'status': 'FILLED' if filled == qty else 'WORKING',
                            'filled': filled, 'avg_price': 100 if is_limit else 99.50}
        if self.behavior == 'timeout':
            raise TimeoutError('ambiguous')
        return SimpleNamespace(headers={} if self.behavior == 'missing_id' else {'Location': '/orders/'+oid},
                               raise_for_status=lambda: None)

    def detalle_orden(self, account, oid):
        return dict(self.orders[oid])

    def esperar_estado_orden(self, *args, **kwargs):
        return {}

    def esperar_posicion_plana(self, *args, **kwargs):
        return {'confirmado': self.quantity == 0}

    def cancelar_orden_confirmada(self, account, oid, **kwargs):
        self.cancelled.append(oid)
        if self.behavior == 'cancel_race':
            self.orders[oid].update(status='FILLED', filled=self.quantity, avg_price=100)
            self.quantity = 0
        elif self.behavior != 'cancel_pending':
            self.orders[oid]['status'] = 'CANCELED'
        return {'confirmado': True}  # Must also inspect authoritative terminal detail.

    def _audit(self, event, **fields):
        self.audit.append((event, fields))

    def close(self, reason='normal exit', price=100.):
        return self.engine.close('META', self.side, 3, 'fake-account', reason, price)


def test_long_limit_fill_has_no_market_fallback(tmp_path):
    b = Broker(tmp_path)
    result = b.close()
    assert result['status'] == 'OK'
    assert [p['orderType'] for p in b.posts] == ['LIMIT']
    assert b.posts[0]['price'] == '99.95'
    assert b.posts[0]['orderLegCollection'][0]['instruction'] == 'SELL'
    assert result['execution_details']['spread'] == pytest.approx(.1)


def test_partial_cancel_fallback_only_closes_remainder_and_uses_weighted_fill(tmp_path):
    b = Broker(tmp_path, behavior='partial')
    result = b.close()
    assert result['status'] == 'OK'
    assert [p['orderType'] for p in b.posts] == ['LIMIT', 'MARKET']
    assert b.posts[1]['orderLegCollection'][0]['quantity'] == 2
    assert result['avg_price'] == pytest.approx((100 + 2 * 99.5) / 3)
    assert result['execution_details']['slippage_usd'] == pytest.approx(1)


def test_cancel_fill_race_does_not_double_close(tmp_path):
    b = Broker(tmp_path, behavior='cancel_race')
    assert b.close()['status'] == 'OK'
    assert len(b.posts) == 1


def test_cancel_ack_without_terminal_status_never_allows_market(tmp_path):
    b = Broker(tmp_path, behavior='cancel_pending')
    assert b.close()['status'] == 'RECONCILIAR'
    assert b.close()['status'] == 'RECONCILIAR'
    assert len(b.posts) == 1


@pytest.mark.parametrize('behavior', ['missing_id', 'timeout'])
def test_ambiguous_submission_survives_restart_without_duplicate(tmp_path, behavior):
    b = Broker(tmp_path, behavior=behavior)
    assert b.close()['status'] == 'RECONCILIAR'
    b.engine = SmartExit(b, b.engine.config)
    assert b.close()['status'] == 'RECONCILIAR'
    assert len(b.posts) == 1


@pytest.mark.parametrize('reason', ['Emergency stop', 'SL', 'SL(breakeven)', 'kill switch', 'STOP no identificado', 'sin proteccion tras TP1'])
def test_emergency_routes_directly_to_market(tmp_path, reason):
    b = Broker(tmp_path)
    assert b.close(reason)['status'] == 'OK'
    assert b.posts[0]['orderType'] == 'MARKET'


def test_short_exit_uses_buy_to_cover_and_ask_limit(tmp_path):
    b = Broker(tmp_path, side='SELL')
    assert b.close('reversal')['status'] == 'OK'
    assert b.posts[0]['orderLegCollection'][0]['instruction'] == 'BUY_TO_COVER'
    assert b.posts[0]['price'] == '100.16'


@pytest.mark.parametrize('quote', [{}, {'bid':100,'ask':101,'quote_time':1},
    {'bid':100,'ask':101,'quote_time':time.time(),'delayed':True},
    {'bid':101,'ask':100,'quote_time':time.time()}])
def test_invalid_quote_uses_documented_market_recovery(tmp_path, quote):
    b = Broker(tmp_path)
    b._quote_provider = lambda t: quote
    assert b.close()['status'] == 'OK'
    assert b.posts[0]['orderType'] == 'MARKET'


def test_pending_market_is_not_submitted_twice(tmp_path):
    b = Broker(tmp_path, behavior='market_pending')
    assert b.close('kill switch')['status'] == 'RECONCILIAR'
    assert b.close('kill switch')['status'] == 'RECONCILIAR'
    assert len(b.posts) == 1


def test_fill_later_can_be_recovered_from_durable_journal(tmp_path):
    b = Broker(tmp_path, behavior='market_pending')
    b.close('kill switch')
    b.orders['1'].update(status='FILLED', filled=3, avg_price=99.5)
    b.quantity = 0
    b.engine = SmartExit(b, b.engine.config)
    assert b.close()['status'] == 'OK'
    assert len(b.posts) == 1


def test_fill_price_is_not_invented_from_the_limit(tmp_path):
    b = Broker(tmp_path)
    original = b.detalle_orden
    b.detalle_orden = lambda *a: {**original(*a), 'avg_price':0}
    assert b.close()['status'] == 'RECONCILIAR'
    assert len(b.posts) == 1


def test_disagreeing_broker_quantity_blocks_market_remainder(tmp_path):
    b = Broker(tmp_path, behavior='partial')
    orig = b.cancelar_orden_confirmada
    def cancel(*args, **kwargs):
        result = orig(*args, **kwargs)
        b.quantity += 2
        return result
    b.cancelar_orden_confirmada = cancel
    assert b.close()['status'] == 'RECONCILIAR'
    assert len(b.posts) == 1


def test_unavailable_position_never_submits(tmp_path):
    b = Broker(tmp_path)
    b._position_snapshot = lambda *a: None
    assert b.close()['status'] == 'RECONCILIAR'
    assert b.posts == []


def test_corrupt_recovery_state_never_submits(tmp_path):
    b = Broker(tmp_path)
    (tmp_path/'pending.json').write_text('bad json')
    with pytest.raises(ValueError):
        b.close()
    assert b.posts == []


@pytest.mark.parametrize('bid,ask', [(float('nan'),1), (1,float('inf')), (-1,1), (2,1)])
def test_nonfinite_or_crossed_prices_rejected(bid, ask):
    with pytest.raises(ValueError):
        marketable_price('BUY',bid,ask)


def test_public_executor_routes_v2_with_symbol_lock_and_audited_reference(tmp_path):
    from api.hybrid_order_executor import EjecutorHibridoDeOrdenes
    b = Broker(tmp_path, behavior='partial')
    ex = EjecutorHibridoDeOrdenes(False, config={'execution_engine':{'enabled':True,**b.engine.config}})
    ex._position_snapshot = b._position_snapshot
    ex._http_session = lambda:b
    ex.detalle_orden = b.detalle_orden
    ex.esperar_estado_orden = b.esperar_estado_orden
    ex.esperar_posicion_plana = b.esperar_posicion_plana
    ex.cancelar_orden_confirmada = b.cancelar_orden_confirmada
    ex.set_quote_provider(b._quote_provider)
    ex.set_audit_hook(lambda event,**kw:b.audit.append((event,kw)))
    with ex.lifecycle_context(exit_signal_price=100,position_id='original-position'):
        result=ex.liquidar_posicion('META','BUY',3,'fake-account','reversal')
    assert result['status']=='OK'
    assert result['execution_details']['signal_price']==100
    assert not ex.has_pending_exit('fake-account','META')
    assert any(e=='EXIT_FILLED' and row['position_id']=='original-position' for e,row in b.audit)


@pytest.mark.parametrize('shares',[3,10,20])
def test_v2_entry_keeps_reserved_quantity_price_and_limit_type(tmp_path,shares):
    from api.hybrid_order_executor import EjecutorHibridoDeOrdenes, ContextoMercado
    from core.risk_manager import OrdenTradingCalculada
    ex=EjecutorHibridoDeOrdenes(False,config={'execution_engine':{'enabled':True},'risk':{'sizing_mode':'atr'}})
    requests=[]
    def post(url,json,timeout,allow_redirects=False):
        assert allow_redirects is False
        requests.append(json)
        return SimpleNamespace(headers={'Location':'/orders/entry'},raise_for_status=lambda:None)
    ex._http_session=lambda:SimpleNamespace(post=post)
    ex.esperar_estado_orden=lambda *a,**kw:{'confirmado':True,'broker_status':'WORKING','latency_ms':1}
    order=OrdenTradingCalculada('META','BUY',100,99,1,shares,shares,1)
    result=ex.enviar_entrada(order,ContextoMercado(atr_relativo=2,es_rango_lateral=False,prob_ia=.99,es_senal_maestra=True),'fake')
    assert requests[0]['orderType']=='LIMIT'
    assert float(requests[0]['price'])==100
    assert requests[0]['orderLegCollection'][0]['quantity']==shares
    assert result['tipo_orden']=='LIMIT'
