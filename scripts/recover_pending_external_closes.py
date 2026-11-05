"""Recover exact pending external closes during a controlled maintenance stop.

Preview uses broker GETs only. Apply requires root, inactive service and a fresh
flat/no-orders account. No new broker orders, model promotion or guard reset.
"""
import argparse
import csv
import hashlib
import io
import contextlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.manual_close_reconciliation import verified_manual_close
from scripts.recover_ba_manual_incident import replace_bytes


def prove_pending(row,audit,orders,day):
    if row.get('session_ny')!=day or not row.get('position_id'):
        raise ValueError('CURRENT_EXACT_PENDING_ID_REQUIRED')
    entries=[r for r in audit if r.get('evento')=='ENTRY_FILLED' and r.get('modo')=='REAL'
        and r.get('position_id')==row['position_id'] and r.get('ticker')==row.get('ticker')
        and r.get('order_id')==row.get('entry_order_id')]
    if len(entries)!=1:raise ValueError('EXACT_ENTRY_AUDIT_REQUIRED')
    entry=entries[0];detail=json.loads(entry.get('detalle') or '{}')
    if (detail.get('broker_price_verified') is not True
        or not all(entry.get(k) for k in ['position_id','execution_id','correlation_id'])
        or float(entry['cantidad'])!=float(row['expected_quantity'])
        or float(entry['precio'])!=float(row['entry_price'])
        or entry.get('lado')!=row.get('side')):
        raise ValueError('COMPLETE_UNCHANGED_ENTRY_REQUIRED')
    known={r['order_id'] for r in audit if r.get('ticker')==row['ticker']
        and r.get('modo')=='REAL' and r.get('order_id') and (r['evento'].endswith('_SUBMITTED')
        or r['evento'] in ['ENTRY_FILLED','STOP_MOVED_TO_BREAKEVEN'])}
    proof=verified_manual_close(orders,ticker=row['ticker'],side=row['side'],
        quantity=row['expected_quantity'],total_quantity=float(entry['cantidad']),
        entry_order_id=row['entry_order_id'],entry_price=row['entry_price'],
        managed_order_ids=known,session_ny=day)
    if not proof:raise ValueError('EXACT_EXTERNAL_FILL_REQUIRED')
    gross=round((proof['avg_price']-row['entry_price'])*proof['quantity']*(1 if row['side']=='BUY' else -1),6)
    return {'pending':row,'entry':entry,'exit':proof,'gross_pnl':gross}


def record_verified_close(history,ledger,audit,proof,day):
    row,entry,exit=proof['pending'],proof['entry'],proof['exit'];pid=row['position_id']
    common={k:entry.get(k,'') for k in ['ticker','position_id','execution_id','correlation_id','signal_id','position_side']}
    stamp=datetime.fromisoformat(exit['filled_at']);ny=stamp.astimezone(ZoneInfo('America/New_York'))
    common.update(modo='REAL',order_id=exit['order_id'],parent_order_id=row['entry_order_id'],
        cantidad=exit['quantity'],timestamp_utc=stamp.isoformat(),fecha=day,hora_ny=ny.strftime('%H:%M:%S'),
        state_before=entry['position_side'],state_after='OUT',
        order_action='SELL' if row['side']=='BUY' else 'BUY_TO_COVER',
        motivo='Verified external closing fill; maintenance reconciliation')
    closes=[r for r in history.read() if r.get('position_id')==pid and r.get('evento')=='POSITION_CLOSE']
    if not closes:
        history.record_close(**common,entrada=row['entry_price'],salida=exit['avg_price'],pnl_usd=proof['gross_pnl'])
    elif (len(closes)!=1 or closes[0].get('order_id')!=exit['order_id']
        or abs(float(closes[0]['pnl_usd'])-proof['gross_pnl'])>1e-6):
        raise ValueError('CONFLICTING_CLOSE_HISTORY')
    if not any(r.get('evento')=='POSITION_CLOSED' and r.get('position_id')==pid
               and r.get('order_id')==exit['order_id'] for r in audit):
        # Audit timestamps describe this recovery observation. The history and
        # explicit broker_filled_at retain the actual execution timestamp.
        audit_fields={k:v for k,v in common.items() if k not in {'timestamp_utc','fecha','hora_ny'}}
        ledger.record('POSITION_CLOSED',**audit_fields,lado=row['side'],precio=exit['avg_price'],pnl_usd=proof['gross_pnl'],
            estado='CLOSED',detalle={'broker_confirmed':True,'fill_price_verified':True,
            'salida_real':exit['avg_price'],'external_manual_exit':True,'manual_risk_verified':True,
            'tp1_ejecutado':False,'external_order_type':exit['order_type'],'broker_tag':exit['tag'],
            'broker_filled_at':exit['filled_at']})
    return common


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true');args=p.parse_args()
    from dotenv import load_dotenv
    import yaml
    from api.schwab_client import SchwabClient, TRADER_URL
    from scripts.authorize_archived_accounting_resume import service_inactive, verify_broker_flat
    if os.geteuid()!=0:raise ValueError('ROOT_REQUIRED')
    load_dotenv(ROOT/'.env');cfg=yaml.safe_load((ROOT/'config.yaml').read_text())
    day=datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    queue=ROOT/'data/manual_close_pending.json'
    state=json.loads(queue.read_text());pending=state.get('pending') or {}
    if not pending:print(json.dumps({'pending':0}));return
    if any(key!=row.get('position_id') for key,row in pending.items()):
        raise ValueError('PENDING_KEY_IDENTITY_MISMATCH')
    audit_path=ROOT/'data/auditoria_ordenes.csv';history_path=ROOT/'data/historial_operaciones.csv'
    audit=list(csv.DictReader(audit_path.open(encoding='utf-8-sig')))
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        client=SchwabClient(cfg);client.TRADER_URL=TRADER_URL
        verify_broker_flat(client,os.environ['SCHWAB_ACCOUNT_NUMBER'])
        account=client.obtener_account_hash(os.environ['SCHWAB_ACCOUNT_NUMBER'])
        orders=client.obtener_ordenes_sesion(account)
    proofs=[prove_pending(row,audit,orders,day) for row in pending.values()]
    print(json.dumps({'pending':len(proofs),'proofs':[{'ticker':r['pending']['ticker'],
        'quantity':r['exit']['quantity'],'entry':r['pending']['entry_price'],
        'exit':r['exit']['avg_price'],'gross_pnl':r['gross_pnl'],'exit_type':r['exit']['order_type']} for r in proofs]}))
    if not args.apply:return
    service_inactive()
    paths=[queue,audit_path,history_path,ROOT/'data/daily_kill_switch_state.json',
        ROOT/'data/agentes_feedback.csv',ROOT/'data/agentes_feedback.csv.open.json',
        ROOT/'data/real_accounting_reconciliation.json']
    folder=Path(tempfile.mkdtemp(prefix='pending-external-close-',dir='/opt/gala/backups'));folder.chmod(0o700)
    ownership={}
    for path in paths:
        if path.exists():
            info=path.stat();ownership[path]=(info.st_uid,info.st_gid,info.st_mode&0o777)
            shutil.copy2(path,folder/path.name);(folder/path.name).chmod(0o600)
    (folder/'proofs.json').write_text(json.dumps(proofs,indent=2))
    (folder/'broker_orders.json').write_text(json.dumps(orders));(folder/'broker_orders.json').chmod(0o600)
    guard_hash=hashlib.sha256(paths[-1].read_bytes()).hexdigest()
    from core.trade_history import TradeHistoryLedger
    from core.order_audit import OrderAuditLedger
    from agents.feedback_store import FeedbackStore
    history=TradeHistoryLedger(history_path);ledger=OrderAuditLedger(audit_path)
    try:
        feedback=FeedbackStore(str(paths[4]))
        for proof in proofs:
            row,entry,exit=proof['pending'],proof['entry'],proof['exit'];pid=row['position_id']
            common=record_verified_close(history,ledger,audit,proof,day)
            risk_path=paths[3];risk=json.loads(risk_path.read_text())
            if risk.get('session_date')!=day:raise ValueError('CURRENT_RISK_SESSION_REQUIRED')
            markers=risk.setdefault('verified_pending_closes',{})
            marker={'order_id':exit['order_id'],'gross_pnl':proof['gross_pnl']}
            if pid in markers and markers[pid]!=marker:raise ValueError('RISK_PROOF_CONFLICT')
            if pid not in markers:
                risk['realized_pnl']['REAL']=round(float(risk['realized_pnl']['REAL'])+proof['gross_pnl'],6)
                markers[pid]=marker
                replace_bytes(risk_path,json.dumps(risk,indent=2).encode(),*ownership[risk_path])
            opened=feedback._abiertas.get(row['ticker'])
            if opened:
                if (opened.get('lifecycle') or {}).get('position_id')!=pid:raise ValueError('FEEDBACK_POSITION_CONFLICT')
                result=feedback.cerrar(row['ticker'],None,lifecycle={**common,'exit_order_id':exit['order_id'],
                    'exit_confirmed':False,'audit_linked':True,'history_linked':True,
                    'exit_action':common['order_action']})
                if not result or int(result['execution_valid'])!=0:raise ValueError('EXTERNAL_CLOSE_MUST_NOT_TRAIN')
        state['pending']={k:v for k,v in pending.items() if k not in {p['pending']['position_id'] for p in proofs}}
        replace_bytes(queue,json.dumps(state,indent=2).encode(),*ownership[queue])
        if hashlib.sha256(paths[-1].read_bytes()).hexdigest()!=guard_hash:raise ValueError('HISTORICAL_GUARD_CHANGED')
    finally:
        for path,info in ownership.items():
            if path.exists():os.chown(path,info[0],info[1]);os.chmod(path,info[2])
    print(json.dumps({'applied':True,'backup':str(folder),'historical_guard_unchanged':True,'engine_started':False}))


if __name__=='__main__':main()
