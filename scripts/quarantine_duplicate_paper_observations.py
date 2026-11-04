"""Exclude ambiguous Nov 4 PAPER examples created by concurrent engines.

Append superseding eligibility records; preserve original fills/outcomes. No
broker calls, REAL changes, model promotion, risk-limit change or order actions.
"""
import argparse
import csv
import json
import os
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.learning_dataset import eligible_closed_rows
from core.order_audit import OrderAuditLedger

DAY = '2026-11-04'
REASON = 'DUPLICATE_ENGINE_BAR_AMBIGUOUS'


def ambiguous_groups(feedback, signals):
    signals = {r.get('signal_id'): r for r in signals if r.get('date') == DAY}
    grouped = defaultdict(list)
    for row in eligible_closed_rows(feedback):
        if row.get('modo') != 'PAPER' or not row.get('timestamp', '').startswith(DAY):
            continue
        signal = signals.get(row.get('entry_signal_id') or row.get('signal_id'))
        if (not signal or signal.get('execution_mode') != 'PAPER'
                or signal.get('tipo') != row.get('tipo')
                or signal.get('ticker') != row.get('ticker')
                or not isinstance(signal.get('signal_bar_epoch_ms'), int)
                or not row.get('position_id') or not row.get('execution_id')):
            continue
        key = (row['ticker'], row['tipo'], signal['signal_bar_epoch_ms'])
        grouped[key].append(row)
    return {key: rows for key, rows in grouped.items()
            if len({r['position_id'] for r in rows}) > 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    feedback_path = ROOT / 'data/agentes_feedback.csv'
    signal_path = ROOT / 'data/historial_senales.jsonl'
    with feedback_path.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        rows = [r for r in reader if r.get('timestamp', '').startswith(DAY)]
    with signal_path.open() as handle:
        signals = [json.loads(line) for line in handle]
    groups = ambiguous_groups(rows, signals)
    count = sum(len(rows) for rows in groups.values())
    print(json.dumps({'duplicate_signal_bars': len(groups), 'ambiguous_paper_positions': count,
        'by_ticker': dict(Counter({ticker: sum(len(rows) for key, rows in groups.items()
            if key[0] == ticker) for ticker, _, _ in groups}))}))
    if not args.apply or not groups:
        return
    from scripts.authorize_archived_accounting_resume import service_inactive
    if os.geteuid() != 0:
        raise ValueError('ROOT_REQUIRED')
    service_inactive()
    folder = Path(tempfile.mkdtemp(prefix='paper-duplicate-quarantine-', dir='/opt/gala/backups'))
    folder.chmod(0o700)
    for path in [feedback_path, signal_path, ROOT/'data/auditoria_ordenes.csv', ROOT/'data/retraining_state.json']:
        shutil.copy2(path, folder/path.name)
        (folder/path.name).chmod(0o600)
    (folder/'scope.json').write_text(json.dumps([{'ticker':k[0], 'side':k[1], 'bar_epoch_ms':k[2],
        'original_rows':rows} for k,rows in groups.items()], indent=2))
    audit = OrderAuditLedger(ROOT/'data/auditoria_ordenes.csv')
    with feedback_path.open('a', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        for (ticker, side, bar), rows in groups.items():
            for row in rows:
                writer.writerow({**row, 'execution_valid':'0', 'exclusion_reason':REASON})
                audit.record('PAPER_OBSERVATION_QUARANTINED', modo='PAPER', ticker=ticker,
                    position_id=row['position_id'], execution_id=row['execution_id'],
                    signal_id=row.get('entry_signal_id') or row.get('signal_id'),
                    estado='EXCLUDED', motivo=REASON,
                    detalle={'source_session':DAY, 'entry_bar_epoch_ms':bar,
                             'training_eligible':False, 'performance_eligible':False,
                             'review_backup':str(folder)})
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps({'applied':True, 'excluded_positions':count, 'backup':str(folder)}))


if __name__ == '__main__':
    main()
