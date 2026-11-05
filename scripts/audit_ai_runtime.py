"""Read-only per-ticker artifact, routing and training evidence. No broker calls."""
import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def artifact(path):
    if not path.is_file():return {'exists':False}
    st=path.stat()
    born=None
    if sys.platform.startswith('linux'):
        raw=subprocess.check_output(['stat','-c','%w',str(path)],text=True).strip()
        born=None if raw=='-' else raw
    return {'exists':True,'size_bytes':st.st_size,
        'modified_utc':datetime.fromtimestamp(st.st_mtime,timezone.utc).isoformat(),
        'created_filesystem':born,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def inventory(root,tickers):
    import yaml
    from core.ticker_execution import TickerExecutionRegistry
    from core.runtime_store import EventHistory
    root=Path(root)
    cfg=yaml.safe_load((root/'config.yaml').read_text()) or {}
    train=((cfg.get('agents') or {}).get('entrenamiento') or {})
    auto=train.get('automatic_retraining') or {}
    live=cfg.get('live_trading') or {}
    def resolve(value):
        path=Path(value)
        return path if path.is_absolute() else root/path
    registry=TickerExecutionRegistry(resolve(live.get('ticker_execution_path','data/ticker_execution.json')))
    state_path=resolve(auto.get('state_path','data/retraining_state.json'))
    events=EventHistory(state_path.with_suffix('.history.jsonl'))
    report={'checked_at_utc':datetime.now(timezone.utc).isoformat(),
        'training_policy':{'target':'SHADOW','automatic_real_activation':False,
            'not_before_ny':auto.get('not_before_ny','16:15'),
            'new_closes_required':auto.get('retrain_samples',train.get('reentrenar_cada_n',50))},
        'routing_file':artifact(registry.path),'tickers':{}}
    for ticker in tickers:
        row={'saved_execution':registry.get(ticker),'artifacts':{}}
        for tier,directory in [('PRODUCTION',train.get('modelos_dir','data/modelos')),
            ('SHADOW',((cfg.get('agents') or {}).get('shadow_mode') or {}).get('modelos_dir','data/modelos_shadow'))]:
            p=resolve(directory)/f'modelo_{ticker}.pkl'
            m=p.with_suffix('.meta.json')
            row['artifacts'][tier]={'model':artifact(p),'metadata_file':artifact(m),
                'metadata':json.loads(m.read_text()) if m.is_file() else {}}
        history=events.recent(ticker,limit=20)
        row['recent_training_events']=[{k:v for k,v in event.items() if k!='metrics'}
            | {'evaluation':{k:(event.get('metrics') or {}).get(k) for k in [
                'evaluation_id','evaluation_utc','muestras','training_dataset_sha256',
                'shadow_candidate_accepted','production_gate_passed','production_gate_reasons',
                'temporal_integrity_passed','cost_label_integrity_passed']}} for event in history]
        report['tickers'][ticker]=row
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tickers',nargs='+',default=['META','AMD','PINS'])
    parser.add_argument('--output');args=parser.parse_args()
    result=json.dumps(inventory(ROOT,args.tickers),indent=2,default=str)+'\n'
    if args.output:Path(args.output).write_text(result,encoding='utf-8')
    else:print(result,end='')


if __name__=='__main__':main()
