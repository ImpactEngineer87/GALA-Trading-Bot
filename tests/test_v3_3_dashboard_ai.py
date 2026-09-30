from pathlib import Path
import json
import pickle
import shutil
import subprocess
from types import SimpleNamespace
import numpy as np
import pandas as pd
import pytest
import yaml
from core.model_identity import ManualRealAuthorization
from core.runtime_store import EventHistory
from agents.training import EntrenadorAgentes, FEATURES
from MIGRATE_CONFIG_V3_3_0 import migrate


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    import server
    cfg=migrate({'live_trading':{'max_tickers':2,'max_shares_per_order':3,'per_ticker_execution':True,'ticker_execution_path':str(tmp_path/'routing.json'),
        'authorization_audit_path':str(tmp_path/'mode-audit.jsonl')},'market':{'tickers':['META','NVDA','TSLA']}})
    config_path=tmp_path/'config.yaml';config_path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(server,'CONFIG_PATH',str(config_path))
    monkeypatch.setattr(server,'_ENGINE',None)
    monkeypatch.setitem(server.BOT_STATE,'running',False)
    monkeypatch.setitem(server.BOT_STATE,'tickers',['META','NVDA','TSLA'])
    auth=ManualRealAuthorization({'enabled':True,'state_path':str(tmp_path/'auth.json'),'audit_path':str(tmp_path/'auth-audit.jsonl')})
    model={'loaded':True,'model_version':'exact-v1','balanced_accuracy':65,'threshold':.6}
    trainer=SimpleNamespace(manual_real_authorization=auth,production_snapshot=lambda t:dict(model),modelo_disponible=lambda t:True,
        retraining=SimpleNamespace(history=EventHistory(str(tmp_path/'history.jsonl'))))
    monkeypatch.setattr(server,'_ai_trainer_for_status',lambda:trainer)
    client=server.app.test_client()
    with client.session_transaction() as sess:
        sess['logged_in']=True;sess['username']='operator';sess['csrf_token']='test-csrf'
    return server,client,config_path,trainer


def test_live_settings_require_csrf_and_persist_explicit_limit(dashboard):
    server,client,path,_=dashboard
    assert client.post('/api/live-settings',json={'max_real_tickers':3}).status_code==403
    response=client.post('/api/live-settings',json={'max_real_tickers':3},headers={'X-CSRF-Token':'test-csrf'})
    assert response.status_code==200
    assert yaml.safe_load(path.read_text())['live_trading']['max_real_tickers']==3
    server.BOT_STATE['running']=True
    assert client.post('/api/live-settings',json={'max_real_tickers':1},headers={'X-CSRF-Token':'test-csrf'}).status_code==409


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is required for dashboard-to-Flask transport checks')
@pytest.mark.parametrize('action',['max_real_tickers','max_shares_per_order','authorization'])
def test_actual_dashboard_request_is_accepted_by_flask(dashboard,action):
    """Execute shipped callbacks + api(), then replay raw fetch bytes/headers."""
    script = r"""
const fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(process.argv[1],'utf8');
const extract=(start,end)=>html.slice(html.indexOf(start),html.indexOf(end));
const requests=[];
const values={realTickerLimit:'3',realShareLimit:'20',v330Reason:'Operator approved',v330Confirm:'AUTHORIZE REAL META'};
const context=vm.createContext({Headers,csrfToken:'test-csrf',document:{activeElement:null},renderTickerExecution:()=>{},
  S:{botRunning:false,tickerExecutionMeta:{},v330Authorization:{ticker:'META',model:{model_version:'exact-v1'}}},
  el:id=>({value:values[id]||'',checked:true,dataset:{},reportValidity:()=>true}),
  toast:()=>{},showLogin:()=>{},
  fetch:async(url,opts)=>{
    requests.push({url,method:opts.method,headers:Object.fromEntries(opts.headers),body:opts.body});
    return {status:200,json:async()=>({status:'error',message:'Captured for replay'})};
  }});
vm.runInContext(extract('async function api(', 'function showLogin(')+
  extract('function realLimitControls(', 'function showV330Dialog(')+
  extract('async function saveModelAuthorization(', 'async function revokeModelAuthorization('),context);
(async()=>{
  if(process.argv[2]==='authorization')await context.saveModelAuthorization();
  else await context.saveRealLimit(process.argv[2]);
  process.stdout.write(JSON.stringify(requests));
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    html=Path(__file__).resolve().parents[1]/'dashboard'/'index.html'
    result=subprocess.run(['node','-e',script,str(html),action],capture_output=True,text=True,check=True)
    requests=json.loads(result.stdout)
    assert len(requests)==1
    request=requests[0]
    _,client,path,trainer=dashboard
    # No json= convenience argument: preserve exactly what the dashboard sends.
    response=client.open(request['url'],method=request['method'],data=request['body'],headers=request['headers'])
    assert response.status_code==200, response.json
    assert request['headers']['content-type']=='application/json'
    assert request['headers']['x-csrf-token']=='test-csrf'
    if action=='authorization':
        assert trainer.manual_real_authorization.active('META','exact-v1')['actor']=='operator'
    else:
        expected=3 if action=='max_real_tickers' else 20
        assert yaml.safe_load(path.read_text())['live_trading'][action]==expected


def test_live_limit_cannot_drop_below_selected_real_tickers(dashboard):
    server,client,path,_=dashboard
    server._ticker_execution_registry().update('META',mode='REAL')
    assert client.post('/api/live-settings',json={'max_real_tickers':0},headers={'X-CSRF-Token':'test-csrf'}).status_code==400


def test_limit_can_be_saved_before_watchlist_expansion(dashboard):
    server, client, path, _ = dashboard
    before = server._ticker_execution_registry().snapshot()
    response = client.post('/api/live-settings', json={'max_real_tickers':25},
                           headers={'X-CSRF-Token':'test-csrf'})
    assert response.status_code == 200, response.json
    cfg = yaml.safe_load(path.read_text())
    assert cfg['live_trading']['max_real_tickers'] == cfg['live_trading']['max_tickers'] == 25
    assert cfg['live_trading']['max_shares_per_order'] == 3
    assert server._ticker_execution_registry().snapshot() == before
    assert client.get('/api/ticker-execution').json['max_real_tickers'] == 25


@pytest.mark.parametrize('invalid', [-1, 1.5, True, None, 'ten', float('inf')])
def test_invalid_ticker_limits_leave_saved_config_unchanged(dashboard, invalid):
    _, client, path, _ = dashboard
    before = path.read_bytes()
    response = client.post('/api/live-settings', json={'max_real_tickers':invalid},
                           headers={'X-CSRF-Token':'test-csrf'})
    assert response.status_code == 400
    assert path.read_bytes() == before


def test_saved_limit_controls_real_selection_and_fresh_engine_capacity(dashboard, monkeypatch, tmp_path):
    """Exercise 3 -> 10 -> 3 -> 0 through the authenticated save/routing API.

    Rebuild the actual engine from saved YAML/registry without starting it.
    External clients and model loading are test doubles; no orders are sent.
    """
    import engine.bot_engine as engine_module
    from unittest.mock import MagicMock
    from core.live_guard import validate_live_start
    server, client, path, trainer = dashboard
    tickers = ['META','NVDA','TSLA','UAL','RCL','PINS','AMD','AAPL','BA','NKE','MSFT']
    monkeypatch.setitem(server.BOT_STATE, 'tickers', tickers)
    monkeypatch.setenv('GALA10_LIVE_ARMED', 'YES')
    monkeypatch.setattr('agents.training.EntrenadorAgentes', lambda cfg:trainer)
    monkeypatch.setattr(server, '_broker_symbol_flat_for_mode_change', lambda cfg,ticker:(True,''))
    monkeypatch.setattr(server, '_log', lambda *args,**kwargs:None)
    headers = {'X-CSRF-Token':'test-csrf'}
    def limit(count):
        return client.post('/api/live-settings', json={'max_real_tickers':count}, headers=headers)
    def select(ticker, mode='REAL'):
        return client.post('/api/ticker-execution/'+ticker, json={'mode':mode,'shares':3}, headers=headers)
    assert limit(3).status_code == 200
    for ticker in tickers[:3]:
        assert select(ticker).status_code == 200
    assert select(tickers[3]).status_code == 409
    assert limit(10).status_code == 200
    for ticker in tickers[3:10]:
        response = select(ticker)
        assert response.status_code == 200, response.json
    assert select(tickers[10]).status_code == 409
    cfg = yaml.safe_load(path.read_text())
    fresh = server._ticker_execution_registry(cfg)
    assert fresh.real_tickers(tickers) == tickers[:10]
    assert server._real_tickers_runtime(cfg) == tickers[:10]
    assert client.get('/api/ticker-execution').json['max_real_tickers'] == 10
    checked = []
    ok, errors, active = validate_live_start(cfg, get_env=lambda key,default:'YES',
        account_number='TEST-ONLY', model_checker=lambda symbols:[],
        broker_checker=lambda account,ticker:(checked.append(ticker) or {'ok':True}),
        real_tickers=fresh.real_tickers(tickers),
        ticker_settings={row['ticker']:row for row in fresh.snapshot(tickers)})
    assert ok, errors
    assert active == checked == tickers[:10]
    monkeypatch.chdir(tmp_path)
    for name in ('SchwabClient','SheetsClient','TelegramBot'):
        monkeypatch.setattr(engine_module, name, MagicMock())
    monkeypatch.setattr(engine_module, 'OrquestadorAgentes', lambda cfg:SimpleNamespace(entrenador=trainer))
    monkeypatch.setattr(engine_module, 'log_info', lambda *args,**kwargs:None)
    engine = engine_module.ChocoletraBot(str(path))
    assert engine.live_max_tickers == 10
    assert engine.real_capacity.config['max_positions'] == 10
    assert engine._real_tickers(tickers) == tickers[:10]
    assert all(engine.shares_for_ticker(t) == 3 for t in tickers[:10])
    assert not engine._activo  # construction never starts trading
    assert limit(3).status_code == 400
    for ticker in tickers[3:10]:
        assert select(ticker, 'PAPER').status_code == 200
    assert limit(3).status_code == 200
    for ticker in tickers[:3]:
        assert select(ticker, 'PAPER').status_code == 200
    assert limit(0).status_code == 200
    assert select(tickers[0]).status_code == 409
    assert server._ticker_execution_registry().real_tickers(tickers) == []


def test_more_ticker_slots_never_bypass_model_or_broker_checks(dashboard, monkeypatch):
    server, client, _, trainer = dashboard
    headers = {'X-CSRF-Token':'test-csrf'}
    assert client.post('/api/live-settings', json={'max_real_tickers':10}, headers=headers).status_code == 200
    monkeypatch.setenv('GALA10_LIVE_ARMED', 'YES')
    monkeypatch.setattr('agents.training.EntrenadorAgentes', lambda cfg:trainer)
    trainer.modelo_disponible = lambda ticker:False
    response = client.post('/api/ticker-execution/META', json={'mode':'REAL','shares':3}, headers=headers)
    assert response.status_code == 409 and response.json['code'] == 'NO_PRODUCTION_MODEL'
    trainer.modelo_disponible = lambda ticker:True
    monkeypatch.setattr(server, '_broker_symbol_flat_for_mode_change', lambda cfg,ticker:(False,'Broker state unavailable'))
    assert client.post('/api/ticker-execution/META', json={'mode':'REAL','shares':3}, headers=headers).status_code == 409
    assert server._ticker_execution_registry().mode('META') == 'PAPER'


def test_share_limit_persists_independently_and_keeps_ticker_quantities(dashboard):
    server,client,path,_=dashboard
    registry=server._ticker_execution_registry()
    registry.update('META',mode='REAL',shares=3)
    registry.update('NVDA',mode='PAPER',shares=100)
    before=registry.snapshot()
    response=client.post('/api/live-settings',json={'max_shares_per_order':20},headers={'X-CSRF-Token':'test-csrf'})
    assert response.status_code==200, response.json
    cfg=yaml.safe_load(path.read_text())
    assert cfg['live_trading']['max_shares_per_order']==20
    assert cfg['live_trading']['max_real_tickers']==2
    assert server._ticker_execution_registry().snapshot()==before
    assert client.get('/api/live-settings').json['max_shares_per_order']==20
    assert client.get('/api/ticker-execution').json['max_real_shares']==20
    audit=EventHistory(cfg['live_trading']['authorization_audit_path']).recent()[-1]
    assert audit['actor']=='operator'
    assert audit['previous']['max_shares_per_order']==3
    assert audit['requested']['max_shares_per_order']==20


@pytest.mark.parametrize('invalid',[0,-1,1.5,True,None,'ten',float('inf')])
def test_invalid_share_limits_never_change_config(dashboard,invalid):
    _,client,path,_=dashboard
    before=path.read_bytes()
    response=client.post('/api/live-settings',json={'max_shares_per_order':invalid},headers={'X-CSRF-Token':'test-csrf'})
    assert response.status_code==400
    assert path.read_bytes()==before


def test_combined_limit_change_is_atomic_when_existing_real_shares_exceed_limit(dashboard):
    server,client,path,_=dashboard
    server._ticker_execution_registry().update('META',mode='REAL',shares=3)
    before=path.read_bytes()
    response=client.post('/api/live-settings',json={'max_real_tickers':3,'max_shares_per_order':2},headers={'X-CSRF-Token':'test-csrf'})
    assert response.status_code==400 and 'META' in response.json['message']
    assert path.read_bytes()==before
    server._ticker_execution_registry().update('META',mode='REAL',shares=2)
    assert client.post('/api/live-settings',json={'max_shares_per_order':2},headers={'X-CSRF-Token':'test-csrf'}).status_code==200


def test_share_limit_requires_csrf_stopped_engine_and_durable_audit(dashboard,monkeypatch):
    server,client,path,_=dashboard
    before=path.read_bytes()
    payload={'max_shares_per_order':20}
    assert client.post('/api/live-settings',json=payload).status_code==403
    server.BOT_STATE['running']=True
    assert client.post('/api/live-settings',json=payload,headers={'X-CSRF-Token':'test-csrf'}).status_code==409
    server.BOT_STATE['running']=False
    def fail(*args,**kwargs):raise OSError('audit disk unavailable')
    monkeypatch.setattr(EventHistory,'append',fail)
    assert client.post('/api/live-settings',json=payload,headers={'X-CSRF-Token':'test-csrf'}).status_code==400
    assert path.read_bytes()==before


def test_dashboard_can_set_ten_real_shares_after_raising_cap(dashboard,monkeypatch):
    from core.risk_manager import GestorDeRiesgo
    from engine.bot_engine import ChocoletraBot
    server,client,path,trainer=dashboard
    monkeypatch.setenv('GALA10_LIVE_ARMED','YES')
    monkeypatch.setattr('agents.training.EntrenadorAgentes',lambda cfg:trainer)
    monkeypatch.setattr(server,'_broker_symbol_flat_for_mode_change',lambda cfg,ticker:(True,''))
    headers={'X-CSRF-Token':'test-csrf'}
    payload={'mode':'REAL','shares':10}
    assert client.post('/api/ticker-execution/META',json=payload,headers=headers).status_code==409
    assert client.post('/api/live-settings',json={'max_shares_per_order':20},headers=headers).status_code==200
    response=client.post('/api/ticker-execution/META',json=payload,headers=headers)
    assert response.status_code==200, response.json
    cfg=yaml.safe_load(path.read_text())
    engine=ChocoletraBot.__new__(ChocoletraBot)
    engine.per_ticker_execution=True
    engine.ticker_execution=server._ticker_execution_registry(cfg)
    engine.live_max_shares_per_order=cfg['live_trading']['max_shares_per_order']
    assert engine.shares_for_ticker('META')==10
    order=GestorDeRiesgo(cfg).calcular_orden('META','BUY',100,1,10000,cantidad_override=engine.shares_for_ticker('META'))
    assert order.cantidad==10
    assert client.post('/api/ticker-execution/META',json={'mode':'REAL','shares':21},headers=headers).status_code==409
    assert engine.shares_for_ticker('META')==10
    monkeypatch.setattr(server,'_broker_symbol_flat_for_mode_change',lambda cfg,ticker:(False,'Position still open'))
    assert client.post('/api/ticker-execution/META',json={'mode':'REAL','shares':15},headers=headers).status_code==409
    assert engine.shares_for_ticker('META')==10


@pytest.mark.parametrize('invalid',[2.5,True,0])
def test_per_ticker_quantity_rejects_fractional_or_invalid_values(dashboard,invalid):
    server,client,_,_=dashboard
    before=server._ticker_execution_registry().get('META')
    response=client.post('/api/ticker-execution/META',json={'mode':'PAPER','shares':invalid},headers={'X-CSRF-Token':'test-csrf'})
    assert response.status_code==400
    assert server._ticker_execution_registry().get('META')==before


def test_manual_authorization_rejects_stale_model_and_logs_valid_request(dashboard):
    _,client,_,trainer=dashboard
    body={'confirmation':'AUTHORIZE REAL META','model_version':'old-v0','reason':'Approved by owner','bypass_probability':True}
    assert client.post('/api/live-authorization/META',json=body,headers={'X-CSRF-Token':'test-csrf'}).status_code==400
    body['model_version']='exact-v1'
    assert client.post('/api/live-authorization/META',json=body,headers={'X-CSRF-Token':'test-csrf'}).status_code==200
    assert trainer.manual_real_authorization.active('META','exact-v1')['actor']=='operator'


def test_new_private_endpoints_are_not_public(dashboard):
    server,_,_,_=dashboard
    client=server.app.test_client()
    for url in ['/api/live-settings','/api/live-authorization/META','/api/ai/history/META','/api/execution-diagnostics']:
        assert client.get(url).status_code==401


class FitModel:
    def __init__(self, wrong=False):
        self.n_features_in_=len(FEATURES)
        self.fit_rows=[]
        self.wrong=wrong
    def fit(self,x,y,**kw):
        self.fit_rows.append(len(x))
    def predict_proba(self,x):
        p=np.where(x[:,0]>.5,.9,.1)
        if self.wrong:p=1-p
        return np.column_stack([1-p,p])


@pytest.mark.parametrize('wrong',[False,True])
def test_retraining_preserves_exact_evaluated_model_and_archives_rejected_candidate(tmp_path,wrong):
    data=pd.DataFrame([{**{f:i%2 for f in FEATURES},'ticker':'META','ganadora':i%2,'tipo':'BUY' if i%2 else 'SELL',
        'timestamp':f'2026-09-01T14:{i//60:02d}:{i%60:02d}', 'execution_valid':1} for i in range(100)])
    feedback=tmp_path/'feedback.csv';data.to_csv(feedback,index=False)
    cfg={'agents':{'entrenamiento':{'activo':False,'feedback_path':str(feedback),'min_muestras':25,
          'modelos_dir':str(tmp_path/'production'),'reportes_dir':str(tmp_path/'reports'),
          'automatic_retraining':{'state_path':str(tmp_path/'state.json')}},
        'shadow_mode':{'enabled':True,'tickers':['META'],'modelos_dir':str(tmp_path/'shadow'),
                       'min_balanced_accuracy':.55,'save_all_candidates':True}}}
    trainer=EntrenadorAgentes(cfg)
    model=FitModel(wrong)
    trainer._crear_modelo=lambda *a,**kw:model
    metrics=trainer.entrenar_ticker('META',allow_production_activation=False,automatic=True)
    assert metrics is not None
    assert model.fit_rows==[60]  # Neither validation nor test was used for refit.
    archive=Path(metrics['candidate_archive'])
    assert (archive/'model.pkl').is_file() and (archive/'model.meta.json').is_file()
    with (archive/'model.pkl').open('rb') as handle:
        assert pickle.load(handle).fit_rows==[60]
    assert not trainer.modelo_disponible('META')
    assert metrics['shadow_candidate_accepted'] is (not wrong)
    if not wrong:
        assert trainer._modelos_shadow['META'] is model


def test_authorized_probability_override_does_not_allow_missing_or_broken_model(tmp_path,monkeypatch):
    import agents.training as training
    auth_cfg={'enabled':True,'state_path':str(tmp_path/'auth.json'),'audit_path':str(tmp_path/'audit.jsonl')}
    trainer=EntrenadorAgentes({'live_trading':{'manual_authorization':auth_cfg},'agents':{'entrenamiento':{
        'feedback_path':str(tmp_path/'feedback.csv'),'modelos_dir':str(tmp_path/'models'),
        'automatic_retraining':{'state_path':str(tmp_path/'state.json')}}}})
    trainer.manual_real_authorization.authorize('META','v1','Client approved threshold exception','operator',True)
    decision=SimpleNamespace(ticker='META')
    assert trainer.evaluar(decision,requerir_modelo=True)==(None,False)
    trainer._modelos['META']=SimpleNamespace(predict_proba=lambda x:np.array([[.8,.2]]))
    trainer._umbrales['META']=.6;trainer._production_snapshots['META']={'model_version':'v1'}
    monkeypatch.setattr(training,'construir_features',lambda d:{f:0 for f in FEATURES})
    assert trainer.evaluar(decision,requerir_modelo=True)==(.2,True)
    trainer._modelos['META']=SimpleNamespace(predict_proba=lambda x:np.array([[0,float('nan')]]))
    assert trainer.evaluar(decision,requerir_modelo=True)==(None,False)


def test_fill_diagnostics_keep_missing_prices_unknown_and_match_roundtrip(tmp_path):
    from core.execution_diagnostics import ExecutionDiagnostics
    d=ExecutionDiagnostics(str(tmp_path/'diagnostics.jsonl'))
    base={'modo':'REAL','ticker':'META','lado':'BUY','position_id':'pos1','cantidad':2}
    d.record('ENTRY_FILLED',{**base,'order_id':'entry','precio':101,'detalle':{'signal_price':100}})
    d.record('EXIT_FILLED',{**base,'order_id':'exit','precio':109,'detalle':{'signal_price':110,'spread':.1}})
    report=d.report()
    assert report['summary']['strategy_pnl_usd']==20
    assert report['summary']['actual_pnl_matched_usd']==16
    assert report['summary']['execution_loss_usd']==4
    assert report['summary']['average_spread']==.1


def test_editor_rejects_stale_ticker_drafts_without_overwriting(dashboard):
    server,client,_,_=dashboard
    registry=server._ticker_execution_registry()
    old=registry.update('META',mode='PAPER',shares=20)
    newer=registry.update('META',mode='PAPER',shares=25)
    response=client.post('/api/ticker-execution/META',json={'mode':'PAPER','shares':30,'expected_updated_utc':old['updated_utc']},headers={'X-CSRF-Token':'test-csrf'})
    assert response.status_code==409 and response.json['code']=='EXECUTION_CHANGED'
    assert registry.get('META')==newer
    response=client.post('/api/ticker-execution/META',json={'mode':'PAPER','shares':30,'expected_updated_utc':newer['updated_utc']},headers={'X-CSRF-Token':'test-csrf'})
    assert response.status_code==200 and registry.shares('META')==30


def test_editor_html_disables_caching_even_with_old_conditional_headers(dashboard):
    _,client,_,_=dashboard
    first=client.get('/')
    assert first.status_code==200
    second=client.get('/',headers={'If-None-Match':first.headers.get('ETag','')})
    assert second.status_code==200
    assert second.headers['Cache-Control']=='no-store'
    assert b'v3.3.1-ui1' in second.data
