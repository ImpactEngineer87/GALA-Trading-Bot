"""Recover missing training close times/planned risk from exact saved ledgers.

Preserves original rows and eligibility. Never estimates a time, infers risk
from a profit label, revives quarantined observations, or contacts the broker.
"""
import argparse
import csv
import json
import math
import os
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.learning_dataset import eligible_closed_rows
from core.dashboard_evidence import annotate_history
from core.order_audit import OrderAuditLedger


def identity(row):
    return tuple(row.get(k) for k in ('modo','ticker','position_id','execution_id'))


def stamp(value, *, decision=False):
    result = datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if result.tzinfo is None:
        if not decision:
            raise ValueError('Unzoned source timestamp')
        result = result.replace(tzinfo=ZoneInfo('America/New_York'))
    return result


def recover_metadata(row, audit, history):
    try:
        if row.get('execution_valid') != '1' or (row.get('closed_utc') and row.get('risk_per_share')):
            return {}
        key = identity(row)
        if not all(key) or row.get('tipo') not in ('BUY','SELL'):
            return {}
        audit = [a for a in audit if identity(a) == key]
        history = [h for h in history if identity(h) == key and h.get('evento') == 'POSITION_CLOSE']
        entries = [a for a in audit if a.get('evento') == 'ENTRY_FILLED'
                   and a.get('order_id') == row.get('entry_order_id')]
        closes = [a for a in audit if a.get('evento') == 'POSITION_CLOSED'
                  and a.get('order_id') == row.get('exit_order_id')]
        if len(history) != 1 or len(entries) != 1 or len(closes) != 1:
            return {}
        entry, close, h = entries[0], closes[0], history[0]
        if not annotate_history([h], audit)[0]['performance_verified']:
            return {}
        if h.get('parent_order_id') != row.get('entry_order_id') or h.get('order_id') != row.get('exit_order_id'):
            return {}
        price, exit_price, quantity = (float(row['precio_entrada']), float(row['precio_salida']), float(h['cantidad']))
        if not all(math.isfinite(v) and v > 0 for v in (price, exit_price, quantity)):
            return {}
        h_entry,h_exit,a_entry,a_quantity,pnl = (
            float(h['entrada']),float(h['salida']),float(entry['precio']),
            float(entry['cantidad']),float(h['pnl_usd']))
        if not all(math.isfinite(v) for v in (h_entry,h_exit,a_entry,a_quantity,pnl)):
            return {}
        if (h.get('tipo') != row['tipo'] or entry.get('tipo') != row['tipo']
                or abs(h_entry-price)>1e-5 or abs(h_exit-exit_price)>1e-5
                or abs(a_entry-price)>1e-5 or a_quantity!=quantity):
            return {}
        direction = 1 if row['tipo']=='BUY' else -1
        if abs((exit_price-price)*quantity*direction-pnl)>0.02:
            return {}
        details = json.loads(entry.get('detalle') or '{}')
        stop = float(details['stop_loss'])
        risk = float(abs(Decimal(str(price))-Decimal(str(stop))))
        if not math.isfinite(stop) or stop <= 0 or risk <= 0 or (price-stop)*direction <= 0:
            return {}
        closed = max(stamp(h['timestamp_utc']), stamp(close['timestamp_utc']))
        if abs((stamp(h['timestamp_utc'])-stamp(close['timestamp_utc'])).total_seconds())>60:
            return {}
        if closed < stamp(row['timestamp'], decision=True) or closed < stamp(entry['timestamp_utc']):
            return {}
        if row.get('closed_utc') and stamp(row['closed_utc']) != closed:
            return {}
        if row.get('risk_per_share') and abs(float(row['risk_per_share'])-risk)>1e-8:
            return {}
        return {'closed_utc':closed.isoformat(), 'risk_per_share':risk}
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return {}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    fp=ROOT/'data/agentes_feedback.csv';latest={}
    with fp.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f);fields=reader.fieldnames
        for row in reader:
            key=identity(row)
            if all(key):latest[key]=row
    eligible=eligible_closed_rows(latest.values(),fieldnames=fields)
    pending={identity(r):r for r in eligible if not r.get('closed_utc') or not r.get('risk_per_share')}
    audit,history=defaultdict(list),defaultdict(list)
    for path,destination in [(ROOT/'data/auditoria_ordenes.csv',audit),(ROOT/'data/historial_operaciones.csv',history)]:
        with path.open(encoding='utf-8-sig',newline='') as f:
            for row in csv.DictReader(f):
                key=identity(row)
                if key in pending:destination[key].append(row)
    repairs=[]
    for key,row in pending.items():
        recovered=recover_metadata(row,audit[key],history[key])
        if recovered:repairs.append((row,recovered))
    print(json.dumps({'eligible_observations':len(eligible),'missing_metadata':len(pending),
        'provable_repairs':len(repairs),'by_ticker':dict(Counter(row['ticker'] for row,_ in repairs))}))
    if not args.apply or not repairs:return
    from scripts.authorize_archived_accounting_resume import service_inactive
    if os.geteuid()!=0:raise ValueError('ROOT_REQUIRED')
    service_inactive()
    folder=Path(tempfile.mkdtemp(prefix='learning-metadata-proof-',dir='/opt/gala/backups'));folder.chmod(0o700)
    for path in [fp,ROOT/'data/auditoria_ordenes.csv',ROOT/'data/historial_operaciones.csv']:
        shutil.copy2(path,folder/path.name);(folder/path.name).chmod(0o600)
    (folder/'repairs.json').write_text(json.dumps([{'original_row':row,'recovered':repair} for row,repair in repairs]))
    ledger=OrderAuditLedger(ROOT/'data/auditoria_ordenes.csv')
    with fp.open('a',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields)
        for row,repair in repairs:
            writer.writerow({**row,**repair})
            ledger.record('LEARNING_METADATA_RECOVERED',modo=row['modo'],ticker=row['ticker'],
                position_id=row['position_id'],execution_id=row['execution_id'],
                estado='EXISTING_EVIDENCE',motivo='Exact recorded close time and entry STOP',
                detalle={**repair,'review_backup':str(folder)})
        f.flush();os.fsync(f.fileno())
    for row,repair in repairs:
        latest[identity(row)]={**row,**repair}
    assert len(eligible_closed_rows(latest.values(),fieldnames=fields))==len(eligible)
    print(json.dumps({'applied':True,'repairs':len(repairs),'eligible_count_unchanged':True,'backup':str(folder)}))


if __name__=='__main__':main()
