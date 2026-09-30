"""Loopback-only dashboard test server with temporary data and no broker network.

Used by test_dashboard_browser.cjs. Never use this fixture for trading.
"""
import hmac
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import requests
    def block_network(*args, **kwargs):
        raise RuntimeError('Broker/network access is disabled in the browser fixture')
    requests.sessions.Session.request = block_network
    import yaml
    import server
    import agents.training
    from flask import jsonify, request
    from werkzeug.serving import make_server
    from core.model_identity import ManualRealAuthorization
    from core.runtime_store import EventHistory
    from MIGRATE_CONFIG_V3_3_0 import migrate

    with tempfile.TemporaryDirectory(prefix='gala-ui-test-') as folder:
        root = Path(folder)
        tickers = ['AAPL','AMD','AMZN','BA','META','NVDA','RCL','UAL','TSLA','UNH','PINS','NCLH','MSFT','NKE','PYPL','NFLX','ADBE']
        real = {'META','NVDA','TSLA','NKE','PYPL'}
        production = real | {'AAPL','AMD','AMZN','BA','RCL'}
        cfg = migrate({'risk':{'fixed_shares':20,'paper_trading':True},
            'live_trading':{'per_ticker_execution':True,'max_tickers':15,'max_shares_per_order':3,
                'ticker_execution_path':str(root/'routing.json'), 'authorization_audit_path':str(root/'audit.jsonl')},
            'market':{'tickers':tickers}})
        path = root/'config.yaml';path.write_text(yaml.safe_dump(cfg))
        server.CONFIG_PATH = str(path)
        server.ENV_PATH = str(root/'unused.env')
        server.TICKERS_PATH = str(root/'tickers.json')
        server._ENGINE = None
        server.BOT_STATE.update(running=False,tickers=tickers,account_number='TEST-ONLY')
        server.app.secret_key = 'temporary-browser-test-session-key'
        server.app.config['SESSION_COOKIE_SECURE'] = False
        server.get_dashboard_user = lambda:'ui-test'
        server.get_dashboard_password = lambda:'ui-test-password'
        server.get_dashboard_password_hash = lambda:''
        server._dashboard_password_ok = lambda candidate:hmac.compare_digest(candidate,'ui-test-password')
        server.get_env = lambda key,default='': 'YES' if key=='GALA10_LIVE_ARMED' else default
        state = {'flat':True,'reject_limits':False}
        registry = server._ticker_execution_registry(cfg)
        for ticker in tickers:
            registry.update(ticker,mode='REAL' if ticker in real else 'PAPER',shares=3 if ticker in real else 20)
        auth = ManualRealAuthorization({'enabled':True,'state_path':str(root/'auth.json'),'audit_path':str(root/'auth-audit.jsonl')})
        trainer = SimpleNamespace(manual_real_authorization=auth,modelo_disponible=lambda t:t in production,
            production_snapshot=lambda t:{'loaded':t in production,'model_version':'test-model-v1','balanced_accuracy':65,'threshold':.62,'production_status':'PRODUCTION' if t in production else 'SHADOW','training_date':'2026-09-10','validation_date':'2026-09-12'},
            retraining=SimpleNamespace(history=EventHistory(str(root/'history.jsonl'))))
        server._ai_trainer_for_status = lambda:trainer
        agents.training.EntrenadorAgentes = lambda config:trainer
        server._broker_symbol_flat_for_mode_change = lambda cfg,ticker:(state['flat'],'' if state['flat'] else 'El ticker todavía tiene una posición REAL abierta en Schwab.')
        server._log = lambda *args,**kwargs:None

        @server.app.before_request
        def fixture_routes():
            if request.path=='/api/test-fixture-state':
                data=request.get_json() or {}
                for key in ('flat','reject_limits'):
                    if key in data:state[key]=bool(data[key])
                if 'running' in data:server.BOT_STATE['running']=bool(data['running'])
                if 'external_shares' in data:registry.update('META',shares=int(data['external_shares']))
                return jsonify(status='ok')
            if request.path=='/api/live-settings' and request.method=='POST' and state['reject_limits']:
                return jsonify(status='error',message='No se pudo escribir la configuración de prueba.'),400
            actual = ('/api/login','/api/logout','/api/auth/status','/api/tickers','/api/ticker-execution','/api/live-settings')
            if request.path in actual or request.path.startswith(('/api/ticker-execution/','/api/live-authorization/','/api/ai/history/')):
                return None
            if request.path=='/api/ai/status':
                return jsonify(status='ok',tickers=[{'ticker':t,'new_observations':43 if t in real else 19,'retrain_samples':50,
                    'production_model_loaded':t in production,'shadow_model_loaded':t not in production,'production_threshold':.62 if t in production else None} for t in tickers])
            if request.path=='/api/status':
                return jsonify(status='ok',bot={'running':server.BOT_STATE['running']},stats={},daily_risk={'enabled':True,'limit_usd':700,'remaining_to_limit_usd':700},scheduler={})
            if request.path in ('/api/bot/start','/api/bot/stop'):
                return jsonify(status='error',message='Trading is disabled in this fixture'),403
            if request.path.startswith('/api/'):
                return jsonify(status='ok',tickers=[],signals=[],trades=[],logs=[],data=[],history=[],summary={})
            return None
        httpd = make_server('127.0.0.1',0,server.app,threaded=True)
        print(json.dumps({'url':f'http://127.0.0.1:{httpd.server_port}'}),flush=True)
        httpd.serve_forever()


if __name__=='__main__':
    main()
