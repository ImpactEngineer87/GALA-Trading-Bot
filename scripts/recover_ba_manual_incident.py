"""Reconcile the exact Nov 4 BA duplicate-engine/manual-close incident.

Preview performs broker GETs only. Apply requires an inactive service and fresh
flat account. Original ledgers, learning labels and the Nov 2 review stay intact.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.archived_accounting_resume import _trusted_approval_file, read_json
from core.manual_close_reconciliation import verified_manual_close
from scripts.authorize_archived_accounting_resume import require, service_inactive, verify_broker_flat

DAY = '2026-11-04'
ENTRY = '1008547387723'
EXIT = '1008548503483'
REJECTED = '1008548502852'
MANAGED = 'aa50b1a4b5994f6d8750ab23498f9f99'
ORPHAN = '3264e147c73c4b14a3d550d42aba0e41'
INCIDENT = 'BA_MANUAL_CLOSE_2026_11_04'


def proof_for(state, baseline, audit, history, orders):
    """Reject any ambiguity or discrepancy beyond this already diagnosed scope."""
    closed = [r for r in history if r.get('fecha') == DAY and r.get('modo') == 'REAL'
              and r.get('ticker') == 'BA' and r.get('evento') == 'POSITION_CLOSE']
    require(len(closed) == 2 and {r['position_id'] for r in closed} == {MANAGED, ORPHAN},
            'EXACT_DUPLICATE_CLOSES_REQUIRED')
    require(all(float(r['cantidad']) == 5 and float(r['entrada']) == 176.59
                and r['position_side'] == 'SHORT' and not r.get('pnl_usd')
                and r.get('order_id') == REJECTED for r in closed), 'HISTORY_SCOPE_CHANGED')
    keys = {MANAGED, ORPHAN} | {'history:' + DAY + ':' + r['history_id'] for r in closed}
    require(keys <= set(state['unresolved']), 'INCIDENT_GUARD_KEYS_REQUIRED')
    expected = copy.deepcopy(state)
    for key in keys:
        row = expected['unresolved'].pop(key)
        require(row.get('ticker') == 'BA', 'GUARD_SYMBOL_CHANGED')
        if key in {MANAGED, ORPHAN}:
            require(row.get('entry_order_id') == (ENTRY if key == MANAGED else '')
                    and row.get('exit_order_id') == REJECTED
                    and row.get('broker_flat') is True
                    and row.get('broker_exit_price') is None
                    and row.get('requested_exit_price') == 176.59,
                    'RUNTIME_GUARD_EVIDENCE_CHANGED')
    require(expected == baseline, 'OTHER_ACCOUNTING_CHANGED')
    events = [r for r in audit if r.get('fecha') == DAY and r.get('modo') == 'REAL'
              and r.get('ticker') == 'BA']
    entries = [r for r in events if r['evento'] == 'ENTRY_FILLED']
    require(len(entries) == 1 and entries[0].get('position_id') == MANAGED
            and entries[0].get('order_id') == ENTRY, 'EXACT_MANAGED_ENTRY_REQUIRED')
    require(not any(r['evento'] == 'TP1_FILLED_70_PERCENT' for r in events),
            'PARTIAL_EXITS_REQUIRE_SEPARATE_REVIEW')
    known = {r['order_id'] for r in events if r.get('order_id') and (
        r['evento'].endswith('_SUBMITTED') or r['evento'] == 'ENTRY_FILLED')}
    proof = verified_manual_close(orders, ticker='BA', side='SELL', quantity=5,
        total_quantity=5, entry_order_id=ENTRY, entry_price=176.59,
        managed_order_ids=known, session_ny=DAY)
    require(proof.get('order_id') == EXIT and proof.get('avg_price') == 177.0419,
            'EXACT_MANUAL_FILL_REQUIRED')
    return {'session_ny': DAY, 'ticker': 'BA', 'entry_order_id': ENTRY,
            'exit_order_id': EXIT, 'quantity': 5, 'entry_fill': 176.59,
            'exit_fill': proof['avg_price'], 'gross_pnl': -2.2595,
            'manual_fill_verified': True, 'training_eligible': False,
            'performance_eligible': False, 'original_history_rows': closed,
            'resolved_guard_keys': sorted(keys)}


def replace_bytes(path, raw, uid, gid, mode):
    fd, temporary = tempfile.mkstemp(prefix='.incident-', dir=path.parent)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'ROOT_REQUIRED')
    import yaml
    from api.schwab_client import SchwabClient, TRADER_URL
    cfg = yaml.safe_load((ROOT / 'config.yaml').read_text())
    grant_path = ROOT / 'data/reviewed_manual_tsla_resume.json'
    require(_trusted_approval_file(grant_path), 'ORIGINAL_REVIEW_UNTRUSTED')
    _, grant = read_json(grant_path)
    baseline_path = Path(grant['review_backup']) / 'guard_before.json'
    baseline_raw, baseline = read_json(baseline_path)
    require(hashlib.sha256(baseline_raw).hexdigest() == grant['guard_sha256'],
            'ORIGINAL_REVIEW_BASELINE_CHANGED')
    guard = ROOT / 'data/real_accounting_reconciliation.json'
    guard_raw, state = read_json(guard)
    paths = [ROOT / 'data/historial_operaciones.csv', ROOT / 'data/auditoria_ordenes.csv',
             ROOT / 'data/daily_kill_switch_state.json', ROOT / 'config.yaml', grant_path]
    snapshots = {p: p.read_bytes() for p in paths}
    history, audit = [list(csv.DictReader(p.open(encoding='utf-8-sig'))) for p in paths[:2]]
    client = SchwabClient(cfg)
    client.TRADER_URL = TRADER_URL
    account = os.getenv('SCHWAB_ACCOUNT_NUMBER', '')
    broker = verify_broker_flat(client, account)
    hashed = client.obtener_account_hash(account)
    response = client._get_autorizado(f'{TRADER_URL}/accounts/{hashed}/orders', params={
        'fromEnteredTime': DAY + 'T05:00:00.000Z',
        'toEnteredTime': '2026-11-05T04:59:59.999Z', 'maxResults': 500}, timeout=20)
    require(response is not None and response.status_code == 200, 'BROKER_SESSION_GET_FAILED')
    orders = response.json()
    require(isinstance(orders, list) and len(orders) < 500 and all(
        str(order.get('accountNumber')) == account for order in orders), 'BROKER_ORDER_SCOPE_INVALID')
    evidence = proof_for(state, baseline, audit, history, orders)
    print(json.dumps({k:v for k,v in evidence.items() if k not in
                     {'original_history_rows','entry_order_id','exit_order_id','resolved_guard_keys'}}))
    if not args.apply:
        return
    service_inactive()
    verify_broker_flat(client, account)
    require(guard.read_bytes() == guard_raw and all(p.read_bytes() == raw for p,raw in snapshots.items()),
            'INPUTS_CHANGED_DURING_REVIEW')
    resolution_path = ROOT / 'data/manual_accounting_resolutions.json'
    require(not resolution_path.exists(), 'ALREADY_RESOLVED_OR_SEPARATE_REVIEW_REQUIRED')
    folder = Path(tempfile.mkdtemp(prefix='ba-manual-verified-', dir='/opt/gala/backups'))
    os.chmod(folder, 0o700)
    for name, raw in {**{p.name:v for p,v in snapshots.items()}, 'guard_before.json':guard_raw,
                      'broker_orders.json':json.dumps(orders).encode(),
                      'verified_proof.json':json.dumps(evidence).encode()}.items():
        target = folder / name
        target.write_bytes(raw)
        target.chmod(0o600)
    evidence.update(reviewed_utc=datetime.now(timezone.utc).isoformat(), review_backup=str(folder),
                    broker_preflight=broker)
    data = {'schema_version': 1, 'incidents': {INCIDENT: evidence}}
    import grp
    replace_bytes(resolution_path, json.dumps(data, indent=2).encode(), 0, grp.getgrnam('gala').gr_gid, 0o640)
    # Apply risk exactly once, only when that same session is still current.
    risk_path = paths[2]
    risk = json.loads(snapshots[risk_path])
    if risk.get('session_date') == DAY:
        risk['realized_pnl']['REAL'] = round(float(risk['realized_pnl']['REAL']) + evidence['gross_pnl'], 6)
        info = risk_path.stat()
        replace_bytes(risk_path, json.dumps(risk, indent=2).encode(), info.st_uid, info.st_gid, info.st_mode & 0o777)
    info = guard.stat()
    replace_bytes(guard, baseline_raw, info.st_uid, info.st_gid, info.st_mode & 0o777)
    print(json.dumps({'applied': True, 'remaining_historical_records': len(baseline['unresolved']),
                      'backup':str(folder), 'engine_started':False}))


if __name__ == '__main__':
    main()
