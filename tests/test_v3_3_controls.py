import json
import threading
import time
from types import SimpleNamespace
import pytest
import yaml
from core.live_capacity import RealCapacityManager, max_real_tickers
from core.model_identity import ManualRealAuthorization, model_snapshot
from core.ticker_execution import TickerExecutionRegistry
from agents.retraining_lifecycle import RetrainingLifecycle
from MIGRATE_CONFIG_V3_3_0 import migrate


def balance(amount=1000):
    return {'source':'BROKER', 'as_of_epoch':time.time(), 'buying_power':amount, 'available_funds':amount}


@pytest.mark.parametrize('amount,daily,allowed', [(1000,100,True),(99,100,False),(1000,0,False),(0,100,False)])
def test_capacity_applies_capital_and_daily_risk(amount,daily,allowed):
    m=RealCapacityManager({'notional_multiplier':1, 'max_positions':10})
    assert m.reserve('META',1,100,99,balance(amount),daily,{})['allowed'] is allowed


@pytest.mark.parametrize('bad', [{}, {'source':'PAPER'}, {'source':'BROKER','as_of_epoch':0},
    {'source':'BROKER','as_of_epoch':time.time(),'buying_power':float('nan'),'available_funds':100}])
def test_capacity_fails_closed_on_missing_stale_or_simulated_balance(bad):
    assert not RealCapacityManager({}).reserve('META',1,100,99,bad,100,{})['allowed']


def test_concurrent_reservations_cannot_spend_same_capital_twice():
    m=RealCapacityManager({'notional_multiplier':1, 'max_positions':10})
    positions={'META':{'reservado':True,'execution_mode':'REAL'},'NVDA':{'reservado':True,'execution_mode':'REAL'}}
    results=[]
    def reserve(ticker):
        results.append(m.reserve(ticker,1,100,99,balance(100),100,positions))
    threads=[threading.Thread(target=reserve,args=(t,)) for t in positions]
    for t in threads:t.start()
    for t in threads:t.join()
    assert sum(r['allowed'] for r in results)==1


def test_existing_position_risk_and_cash_are_reserved():
    m=RealCapacityManager({'notional_multiplier':1})
    pos={'NVDA':{'execution_mode':'REAL','cantidad':1,'entrada':100,'sl':90}}
    r=m.reserve('META',1,100,99,balance(1000),10,pos)
    assert not r['allowed']
    assert r['remaining_daily_risk']==0


def test_paper_positions_do_not_consume_real_capacity():
    m=RealCapacityManager({'notional_multiplier':1})
    assert m.reserve('META',1,100,99,balance(100),1,{'NVDA':{'execution_mode':'PAPER','cantidad':1000}})['allowed']


def test_configurable_count_supports_zero_and_legacy_key():
    assert max_real_tickers({'max_tickers':15})==15
    assert max_real_tickers({'max_real_tickers':0,'max_tickers':15})==0
    with pytest.raises(ValueError):max_real_tickers({'max_real_tickers':1.5})


def test_registry_seeds_yaml_selection_without_overwriting_dashboard_changes(tmp_path):
    r=TickerExecutionRegistry(str(tmp_path/'routing.json'),initial_real_tickers=['META'])
    r.ensure_tickers(['META','NVDA'])
    assert r.mode('META')=='REAL' and r.mode('NVDA')=='PAPER'
    r.update('META',mode='PAPER')
    r.ensure_tickers(['META','NVDA'])
    assert r.mode('META')=='PAPER'


def auth(tmp_path):
    return ManualRealAuthorization({'enabled':True,'state_path':str(tmp_path/'auth.json'),'audit_path':str(tmp_path/'audit.jsonl')})


def test_manual_override_is_pinned_to_model_and_revocable(tmp_path):
    a=auth(tmp_path)
    a.authorize('META','v1','Client authorization','operator',True)
    assert a.approve_below_threshold('META','v1',.2,.6)
    assert not a.approve_below_threshold('META','v2',.2,.6)
    a.revoke('META','operator')
    assert not a.approve_below_threshold('META','v1',.2,.6)
    assert len(a.history.recent())==3


def test_manual_authorization_does_not_implicitly_bypass_probability(tmp_path):
    a=auth(tmp_path)
    a.authorize('META','v1','Approve routing','operator')
    assert not a.approve_below_threshold('META','v1',.2,.6)


def test_override_requires_successful_audit(tmp_path,monkeypatch):
    a=auth(tmp_path)
    a.authorize('META','v1','reason','operator',True)
    def failed(*args):raise OSError('disk full')
    monkeypatch.setattr(a.history,'append',failed)
    with pytest.raises(OSError):a.approve_below_threshold('META','v1',.2,.6)


def test_model_identity_changes_when_artifact_bytes_change(tmp_path):
    p=tmp_path/'model.pkl';p.write_bytes(b'first')
    one=model_snapshot({'evaluation_id':'unchanged'},str(p),True)
    p.write_bytes(b'second')
    two=model_snapshot({'evaluation_id':'unchanged'},str(p),True)
    assert one['model_version'] != two['model_version']


def test_retraining_history_retains_rejections_across_restart(tmp_path):
    kwargs={'feedback_path':str(tmp_path/'feedback.csv'),'state_path':str(tmp_path/'state.json'),'shadow_models_dir':str(tmp_path/'shadow')}
    r=RetrainingLifecycle(**kwargs)
    r.mark_result('META',total_count=50,accepted=False,reason='Candidate worse',metrics={'evaluation_id':'first','balanced_accuracy':51})
    r.mark_result('META',total_count=100,accepted=True,metrics={'evaluation_id':'second','balanced_accuracy':60})
    history=RetrainingLifecycle(**kwargs).history.recent('META')
    assert [e['result'] for e in history]==['REJECTED','ACCEPTED']
    assert [e['metrics']['evaluation_id'] for e in history]==['first','second']


def test_migration_is_idempotent_and_preserves_existing_risk_and_modes():
    original={'risk':{'paper_trading':True},'live_trading':{'max_tickers':3,'max_shares_per_order':1},'custom':{'value':42}}
    result=migrate(original)
    assert result==migrate(result)
    assert result['live_trading']['max_real_tickers']==3
    assert result['risk']==original['risk'] and result['custom']==original['custom']
    assert 'execution_engine' not in original


def test_capacity_guard_is_called_before_real_entry_submission():
    from engine.bot_engine import ChocoletraBot
    bot=ChocoletraBot.__new__(ChocoletraBot)
    bot.config={'live_trading':{'capacity':{'enabled':True}}}
    bot._lock=threading.RLock();bot.account_number='test'
    bot._posiciones={};bot.real_capacity=RealCapacityManager({})
    bot.schwab=SimpleNamespace(obtener_balance=lambda account:balance(10))
    bot.daily_risk_snapshot=lambda:{'enabled':True,'remaining_to_limit_usd':100}
    bot._audit_event=lambda *a,**kw:None
    order=SimpleNamespace(cantidad=1,precio_limite=100,stop_loss=99)
    assert bot._reserve_real_capacity('META',order) is False


def test_quarantine_keeps_open_real_position_live(tmp_path):
    from engine.bot_engine import ChocoletraBot
    bot=ChocoletraBot.__new__(ChocoletraBot)
    bot.config={'live_trading':{'runtime_isolate_ticker_failures':True}}
    bot._lock=threading.RLock();bot._ticker_quarantine={};bot.per_ticker_execution=True
    bot.ticker_execution=TickerExecutionRegistry(str(tmp_path/'routing.json'))
    bot.ticker_execution.update('META',mode='REAL');bot.ticker_execution.update('NVDA',mode='REAL')
    bot._posiciones={'META':{'execution_mode':'REAL','cantidad':1}}
    bot._audit_event=lambda *a,**kw:None
    bot._quarantine_real_ticker('META','model missing')
    assert bot.execution_mode_for('META')=='REAL' and bot.execution_mode_for('NVDA')=='REAL'
    assert 'META' in bot._ticker_quarantine
    bot._posiciones={};bot.account_number='test'
    bot.schwab=SimpleNamespace(obtener_posiciones=lambda a:[],obtener_ordenes_abiertas=lambda a:[])
    bot._apply_ticker_quarantine('META')
    assert bot.execution_mode_for('META')=='PAPER' and bot.execution_mode_for('NVDA')=='REAL'
