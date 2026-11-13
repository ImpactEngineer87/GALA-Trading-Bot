"""Recover a full-position, audited GALA STOP already filled at Schwab.

Preview performs broker GETs only. Apply requires inactive service, a fresh
flat account and the exact durable pending position. No order actions or guard
reset; original entry features and exact fill prices supply learning evidence.
"""
import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.manual_close_reconciliation import verified_manual_close
from scripts.recover_ba_manual_incident import replace_bytes


def prove_pending_stop(row, audit, orders, day):
    pid, ticker = row.get('position_id'), row.get('ticker')
    if not pid or row.get('session_ny') != day:
        raise ValueError('EXACT_PENDING_SESSION_REQUIRED')
    owned = [r for r in audit if r.get('modo') == 'REAL'
             and r.get('position_id') == pid and r.get('ticker') == ticker]
    entries = [r for r in owned if r.get('evento') == 'ENTRY_FILLED'
               and r.get('order_id') == row.get('entry_order_id')]
    if len(entries) != 1:
        raise ValueError('EXACT_ENTRY_AUDIT_REQUIRED')
    entry = entries[0]
    if (not all(entry.get(k) for k in ['signal_id', 'execution_id', 'correlation_id'])
            or json.loads(entry.get('detalle') or '{}').get('broker_price_verified') is not True
            or float(entry['cantidad']) != float(row['expected_quantity'])
            or float(entry['precio']) != float(row['entry_price'])
            or entry.get('lado') != row.get('side')):
        raise ValueError('COMPLETE_UNCHANGED_ENTRY_REQUIRED')
    # The strict fill matcher verifies the sole complete close and all canceled
    # siblings. Ownership is independently established by this position's audit.
    close = verified_manual_close(orders, ticker=ticker, side=row['side'],
        quantity=row['expected_quantity'], total_quantity=float(entry['cantidad']),
        entry_order_id=row['entry_order_id'], entry_price=row['entry_price'],
        managed_order_ids={row['entry_order_id']}, session_ny=day)
    submissions = {r['order_id'] for r in owned if r.get('evento') == 'STOP_SUBMITTED'
                   and float(r.get('cantidad') or 0) == float(row['expected_quantity'])}
    if not close or close['order_type'] != 'STOP' or close['order_id'] not in submissions:
        raise ValueError('EXACT_AUDITED_STOP_FILL_REQUIRED')
    gross = round((close['avg_price'] - row['entry_price']) * close['quantity']
                  * (1 if row['side'] == 'BUY' else -1), 6)
    return {'pending': row, 'entry': entry, 'exit': close, 'gross_pnl': gross}


def commit_proof(root, proof, day):
    """Idempotent local journal recovery; no broker/network dependencies."""
    from agents.feedback_store import FeedbackStore
    from core.order_audit import OrderAuditLedger
    from core.trade_history import TradeHistoryLedger

    row, entry, close = proof['pending'], proof['entry'], proof['exit']
    pid, ticker = row['position_id'], row['ticker']
    history = TradeHistoryLedger(root/'data/historial_operaciones.csv')
    audit = OrderAuditLedger(root/'data/auditoria_ordenes.csv')
    common = {k: entry.get(k, '') for k in
              ['ticker', 'position_id', 'execution_id', 'correlation_id', 'signal_id', 'position_side']}
    common.update(modo='REAL', order_id=close['order_id'], parent_order_id=row['entry_order_id'],
        cantidad=close['quantity'], state_before=entry['position_side'], state_after='OUT',
        order_action='SELL' if row['side'] == 'BUY' else 'BUY_TO_COVER',
        motivo='Exact audited GALA replacement STOP fill; recovered pending close')
    closing = {**common, 'exit_order_id': close['order_id'],
        'exit_action': common['order_action'], 'exit_confirmed': True,
        'fill_price_verified': True, 'audit_linked': True, 'history_linked': True,
        'closed_utc': close['filled_at']}
    # Validate original features, risk session and queue before the first write.
    feedback = FeedbackStore(str(root/'data/agentes_feedback.csv'))
    opened = feedback._abiertas.get(ticker)
    if opened is not None:
        if (opened.get('lifecycle') or {}).get('position_id') != pid or opened.get('closing_row'):
            raise ValueError('FEEDBACK_POSITION_CONFLICT')
        candidate = feedback._construir_cierre(ticker, close['avg_price'], opened, closing)
        if not candidate or int(candidate['execution_valid']) != 1:
            raise ValueError('COMPLETE_ORIGINAL_FEATURE_EVIDENCE_REQUIRED')
    else:
        rows = list(csv.DictReader((root/'data/agentes_feedback.csv').open(encoding='utf-8-sig')))
        found = [r for r in rows if r.get('position_id') == pid]
        if (len(found) != 1 or found[0]['execution_valid'] != '1'
                or found[0]['exit_order_id'] != close['order_id']):
            raise ValueError('ORIGINAL_OPEN_FEATURES_REQUIRED')
    risk_path = root/'data/daily_kill_switch_state.json'
    risk = json.loads(risk_path.read_text())
    if risk.get('session_date') != day:
        raise ValueError('CURRENT_RISK_SESSION_REQUIRED')
    markers = risk.setdefault('verified_pending_closes', {})
    marker = {'order_id': close['order_id'], 'gross_pnl': proof['gross_pnl']}
    if pid in markers and markers[pid] != marker:
        raise ValueError('RISK_PROOF_CONFLICT')
    queue_path = root/'data/manual_close_pending.json'
    queue = json.loads(queue_path.read_text())
    pending = queue.get('pending') or {}
    if pid in pending and pending[pid] != row:
        raise ValueError('PENDING_RECORD_CHANGED')
    prior = [r for r in history.read() if r.get('position_id') == pid
             and r.get('evento') == 'POSITION_CLOSE']
    if not prior:
        stamp = datetime.fromisoformat(close['filled_at'])
        history.record_close(**common, timestamp_utc=stamp.isoformat(), fecha=day,
            hora_ny=stamp.astimezone(ZoneInfo('America/New_York')).strftime('%H:%M:%S'),
            entrada=row['entry_price'], salida=close['avg_price'], pnl_usd=proof['gross_pnl'])
    elif (len(prior) != 1 or prior[0]['order_id'] != close['order_id']
            or abs(float(prior[0]['pnl_usd']) - proof['gross_pnl']) > 1e-6):
        raise ValueError('CONFLICTING_CLOSE_HISTORY')
    recorded = [r for r in audit.read(limit=None) if r.get('evento') == 'POSITION_CLOSED'
                and r.get('position_id') == pid]
    if not recorded:
        audit.record('POSITION_CLOSED', **common, lado=row['side'], estado='CLOSED',
            precio=close['avg_price'], pnl_usd=proof['gross_pnl'], detalle={
                'broker_confirmed': True, 'fill_price_verified': True, 'accounting_verified': True,
                'salida_real': close['avg_price'], 'final_quantity': close['quantity'],
                'tp1_ejecutado': False, 'external_manual_exit': False,
                'broker_filled_at': close['filled_at'], 'recovered_pending_stop': True})
    elif len(recorded) != 1 or recorded[0]['order_id'] != close['order_id']:
        raise ValueError('CONFLICTING_CLOSE_AUDIT')
    if pid not in markers:
        risk['realized_pnl']['REAL'] = round(float(risk['realized_pnl']['REAL']) + proof['gross_pnl'], 6)
        markers[pid] = marker
        info = risk_path.stat()
        replace_bytes(risk_path, json.dumps(risk, indent=2).encode(), info.st_uid, info.st_gid, info.st_mode & 0o777)
    if opened is not None:
        result = feedback.cerrar(ticker, close['avg_price'], lifecycle=closing)
        if not result or int(result['execution_valid']) != 1:
            raise ValueError('COMPLETE_ORIGINAL_FEATURE_EVIDENCE_REQUIRED')
    pending.pop(pid, None); queue['pending'] = pending
    info = queue_path.stat()
    replace_bytes(queue_path, json.dumps(queue, indent=2).encode(), info.st_uid, info.st_gid, info.st_mode & 0o777)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ticker', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from dotenv import load_dotenv
    import yaml
    from api.schwab_client import SchwabClient, TRADER_URL
    from scripts.authorize_archived_accounting_resume import service_inactive, verify_broker_flat
    if os.geteuid() != 0:
        raise ValueError('ROOT_REQUIRED')
    load_dotenv(ROOT/'.env'); cfg = yaml.safe_load((ROOT/'config.yaml').read_text())
    day = datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    queue_path = ROOT/'data/manual_close_pending.json'
    pending = json.loads(queue_path.read_text()).get('pending') or {}
    if any(k != r.get('position_id') for k,r in pending.items()):
        raise ValueError('PENDING_KEY_IDENTITY_MISMATCH')
    targets = [r for r in pending.values() if r.get('ticker') == args.ticker.upper()]
    if not targets:
        print(json.dumps({'pending': 0, 'applied': False})); return
    audit = list(csv.DictReader((ROOT/'data/auditoria_ordenes.csv').open(encoding='utf-8-sig')))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        client = SchwabClient(cfg); client.TRADER_URL = TRADER_URL
        broker = verify_broker_flat(client, os.environ['SCHWAB_ACCOUNT_NUMBER'])
        account = client.obtener_account_hash(os.environ['SCHWAB_ACCOUNT_NUMBER'])
        orders = client.obtener_ordenes_sesion(account)
    proofs = [prove_pending_stop(r, audit, orders, day) for r in targets]
    summary = [{'ticker': p['pending']['ticker'], 'quantity': p['exit']['quantity'],
                'exit_price': p['exit']['avg_price'], 'gross_pnl': p['gross_pnl']} for p in proofs]
    if not args.apply:
        print(json.dumps({'pending': len(proofs), 'proofs': summary, 'applied': False})); return
    service_inactive()
    paths = [ROOT/'data'/name for name in ['manual_close_pending.json', 'auditoria_ordenes.csv',
        'historial_operaciones.csv', 'daily_kill_switch_state.json', 'agentes_feedback.csv',
        'agentes_feedback.csv.open.json', 'real_accounting_reconciliation.json']]
    folder = Path(tempfile.mkdtemp(prefix='managed-stop-recovery-', dir='/opt/gala/backups'))
    folder.chmod(0o700); ownership = {}
    for path in paths:
        info = path.stat(); ownership[path] = (info.st_uid, info.st_gid, info.st_mode & 0o777)
        shutil.copy2(path, folder/path.name); (folder/path.name).chmod(0o600)
    for name, value in [('proofs.json', proofs), ('broker_orders.json', orders)]:
        (folder/name).write_text(json.dumps(value, indent=2)); (folder/name).chmod(0o600)
    guard_hash = hashlib.sha256(paths[-1].read_bytes()).hexdigest()
    try:
        for proof in proofs:
            commit_proof(ROOT, proof, day)
        if hashlib.sha256(paths[-1].read_bytes()).hexdigest() != guard_hash:
            raise ValueError('HISTORICAL_GUARD_CHANGED')
    finally:
        for path, info in ownership.items():
            os.chown(path, info[0], info[1]); os.chmod(path, info[2])
    print(json.dumps({'applied': True, 'proofs': summary, 'backup': str(folder),
        'broker': broker, 'historical_guard_unchanged': True, 'engine_started': False}))


if __name__ == '__main__':
    main()
