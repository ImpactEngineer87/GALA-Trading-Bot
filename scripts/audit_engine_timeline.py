#!/usr/bin/env python3
"""Read-only NY-session lifecycle evidence. No broker/auth/training imports.

Only fixed event codes and aware timestamps are exported, never arbitrary log
messages, credentials, account/order IDs, request URLs or trading state edits.
"""
import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
NY = ZoneInfo('America/New_York')


def classify(message):
    patterns = (
        ('START_REQUESTED', r'\[Motor\] SOLICITUD DE ARRANQUE'),
        ('START_ACCEPTED', r'\[Motor\] ARRANCADO desde'),
        ('ENGINE_INITIALIZATION_LOG', r'^Motor iniciado en modo (?:REAL|PAPER|MIXED)\b'),
        ('THREAD_STARTED', r'\[Motor\] HILO INICIADO'),
        ('THREAD_FAILED', r'\[Motor\] Terminó con error|\[Motor\] HILO TERMINADO: FAILED'),
        ('THREAD_STOPPED', r'\[Motor\] HILO TERMINADO: STOPPED'),
        ('STOP_REQUESTED', r'\[Motor\] SOLICITUD DE PARADA'),
        ('STOP_CONFIRMED_FLAT', r'\[Motor\] DETENIDO desde .*broker plano confirmado'),
        ('STOP_EXPOSURE_UNRESOLVED', r'\[Motor\] DETENIDO desde .*QUEDA EXPOSICIÓN REAL'),
        ('START_BLOCKED', r'\[Motor\] ARRANQUE BLOQUEADO|\[LiveGuard\] MODO REAL BLOQUEADO'),
        ('REAL_ACCOUNTING_BLOCK', r'Contabilidad REAL pendiente:|REAL_ACCOUNTING_UNRESOLVED'),
        ('SOURCE_PERMISSION_ERROR', r"can't open file .*server\.py.*Permission denied"),
        ('SERVICE_PROCESS_EXIT', r'gala\.service: Main process exited'),
        ('SERVICE_RESTART_SCHEDULED', r'gala\.service: Scheduled restart job'),
        ('SERVICE_STARTED', r'Started gala\.service'),
        ('SERVICE_STOPPED', r'Stopped gala\.service'),
        ('SERVICE_OOM_REPORTED', r'gala\.service:.*oom-kill'),
    )
    for event, pattern in patterns:
        if re.search(pattern, message):
            mode_match = re.search(r'modo[: ]+\s*(REAL|PAPER|MIXED)\b', message, re.I)
            return event, mode_match.group(1).upper() if mode_match else None
    return None, None


def _record(timestamp, message, source, session):
    if timestamp.tzinfo is None or timestamp.astimezone(NY).date() != session:
        return None
    event, mode = classify(message)
    if not event:
        return None
    return {'timestamp_utc': timestamp.astimezone(timezone.utc).isoformat(),
            'timestamp_ny': timestamp.astimezone(NY).isoformat(),
            'source': source, 'event': event, 'mode': mode}


def read_server_log(path, session):
    result = []
    if not path.exists():
        return result, False
    with path.open(encoding='utf-8', errors='replace') as handle:
        for line in handle:
            match = re.match(r'^\[([^\]]+)\] \[NY [^\]]+\] \w+: (.*)', line)
            if not match:
                continue
            try:
                timestamp = datetime.fromisoformat(match.group(1).replace('Z', '+00:00'))
            except ValueError:
                continue
            row = _record(timestamp, match.group(2), 'dashboard_server.log', session)
            if row:
                result.append(row)
    return result, True


def read_journal(lines, session):
    result = []
    for line in lines:
        try:
            row = json.loads(line)
            timestamp = datetime.fromtimestamp(int(row['__REALTIME_TIMESTAMP']) / 1_000_000,
                                               timezone.utc)
            message = str(row.get('MESSAGE') or '')
        except (TypeError, ValueError, KeyError, OverflowError, OSError):
            continue
        # Printed dashboard logs have an [HH:MM:SS] level prefix.
        message = re.sub(r'^\[\d\d:\d\d:\d\d\] \w+: ', '', message)
        record = _record(timestamp, message, 'gala.service journal', session)
        if record:
            result.append(record)
    return result


def journal_command(session):
    start = datetime.combine(session, time.min, NY)
    end = datetime.combine(session + timedelta(days=1), time.min, NY)
    fmt = '%Y-%m-%d %H:%M:%S UTC'
    return ['journalctl', '-u', 'gala', '--since', start.astimezone(timezone.utc).strftime(fmt),
            '--until', end.astimezone(timezone.utc).strftime(fmt), '--output=json', '--no-pager']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', required=True, type=date.fromisoformat, help='New York session date')
    parser.add_argument('--log', type=Path, default=ROOT/'logs/dashboard_server.log')
    parser.add_argument('--journal', action='store_true', help='Also read gala.service journalctl JSON')
    args = parser.parse_args(argv)
    errors = []
    try:
        records, log_available = read_server_log(args.log, args.date)
    except OSError:
        records, log_available = [], False
        errors.append('SERVER_LOG_UNREADABLE')
    journal_available = False
    if args.journal:
        try:
            response = subprocess.run(journal_command(args.date), capture_output=True,
                                      text=True, timeout=30, check=False)
            journal_available = response.returncode == 0
            if journal_available:
                records += read_journal(response.stdout.splitlines(), args.date)
            else:
                errors.append('JOURNAL_UNAVAILABLE')
        except (OSError, subprocess.TimeoutExpired):
            errors.append('JOURNAL_UNAVAILABLE')
    records.sort(key=lambda r: (r['timestamp_utc'], r['source']))
    shown = records if len(records) <= 40 else records[:10] + records[-30:]
    def first(event):
        return next((r['timestamp_ny'] for r in records if r['event'] == event), None)
    version = None
    try:
        value = (ROOT/'VERSION').read_text().strip()
        version = value if re.fullmatch(r'\d+\.\d+\.\d+', value) else None
    except OSError:
        pass
    print(json.dumps({
        'read_only': True, 'broker_requests': 0, 'session_ny': args.date.isoformat(),
        'checked_out_version': version, 'server_log_available': log_available,
        'journal_available': journal_available, 'source_errors': errors,
        'first_start_request_observed_ny': first('START_REQUESTED'),
        'first_start_acceptance_observed_ny': first('START_ACCEPTED'),
        'first_engine_initialization_log_observed_ny': first('ENGINE_INITIALIZATION_LOG'),
        'first_thread_failure_observed_ny': first('THREAD_FAILED'),
        'matching_records': len(records), 'omitted_records': len(records)-len(shown),
        'observed_record_counts': dict(Counter(r['event'] for r in records)),
        'events': shown,
        'scope': 'Observed retained logs only; acceptance is not initialization, readiness, or a fill. '
                 'Missing/rotated logs do not prove no start, crash, stop, or trade. '
                 'Checked-out version does not prove the running Python version.',
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
