"""Read-only signal attribution. An AI score is never broker fill evidence."""
from collections import defaultdict
import csv
import re
from pathlib import Path
from core.dashboard_evidence import session_date


def _key(row, *, signal=False):
    mode = str(row.get('execution_mode' if signal else 'modo') or '').upper()
    ticker = str(row.get('ticker') or '').upper()
    sid = str(row.get('signal_id') or '').strip()
    day = session_date(row)
    return (mode, ticker, sid, day) if mode in {'REAL', 'PAPER'} and ticker and sid and day else None


def _code(value):
    text = str(value or '').strip()
    # Never return arbitrary broker exceptions/URLs or account identifiers.
    known = {'REAL_ACCOUNTING_UNRESOLVED', 'DAILY_LOSS_BLOCK', 'ATR_INVALID',
             'RISK_ORDER_REJECTED', 'BROKER_ACCOUNT_NOT_READY',
             'BROKER_OAUTH_UNAVAILABLE', 'BROKER_RECONCILIATION_REQUIRED',
             'BROKER_SYMBOL_NOT_CLEAN', 'REAL_CAPACITY_BLOCK',
             'AI_THRESHOLD', 'NO_SIGNAL', 'POSITION_ALREADY_OPEN',
             'REVERSAL_CLOSE_FAILED', 'UNKNOWN', 'ENGINE_STOPPED',
             'TICKER_ENTRY_RESERVED', 'ALREADY_SAME_SIDE', 'REVERSAL_DISABLED',
             'POSITION_STATE_MISMATCH', 'PENDING_ENTRY_NOT_CLEARED',
             'ENTRY_SLOT_NOT_AVAILABLE', 'ENTRY_NOT_EXECUTED',
             'REAL_LONG_ONLY_CLOSE_ONLY', 'REVERSAL_PRIOR_CLOSE_FAILED'}
    if text in known or re.fullmatch(r'(BROKER|PAPER)_ENTRY_(ERROR|REJECTED|RECONCILIAR|CANCELED|CANCELLED)', text):
        return text
    return ''


def annotate_signals(signals, audit_rows=(), *, audit_available=True):
    """Exact mode/ticker/signal identities only; never infer by time or price.

    These are local audit observations, not a live order query. Missing IDs
    stay unlinked. Original signals, prices and AI decisions are unchanged.
    """
    index = defaultdict(list)
    for event in audit_rows:
        key = _key(event)
        if key:
            index[key].append(event)
    output = []
    for source in signals:
        row = dict(source)
        named = {str(e.get('evento') or '').upper(): e
                 for e in index.get(_key(row, signal=True), [])}
        reason = ''
        state, label = 'UNLINKED', 'Sin envío broker enlazado'
        if not audit_available:
            state, label = 'UNAVAILABLE', 'Auditoría no disponible'
        elif 'REVERSAL_ABORTED' in named or 'POSITION_CLOSE_FAILED' in named:
            state, label = 'CLOSE_UNRESOLVED', 'Cierre/reversión sin confirmar'
        elif ('ENTRY_FILLED' in named and named['ENTRY_FILLED'].get('order_id')
                and str(named['ENTRY_FILLED'].get('estado') or '').upper() in (
                    {'FILLED', 'SIMULATED_FILLED'} if row.get('execution_mode') == 'PAPER' else {'FILLED'})):
            state, label = 'FILL_RECORDED', 'Fill de entrada registrado'
        elif 'ENTRY_FILLED' in named:
            state, label = 'UNCERTAIN', 'Registro de fill sin confirmación/identidad suficiente'
        elif any(k in named for k in ('ENTRY_TIMEOUT_CANCELED', 'PENDING_ENTRY_CANCELED', 'ENTRY_TERMINATED')):
            state, label = 'TERMINATED', 'Entrada cancelada/terminada; revisar fills'
        elif any(k in named for k in ('ENTRY_RECONCILE_REQUIRED', 'EXIT_RECONCILE_REQUIRED')):
            state, label = 'UNCERTAIN', 'Orden pendiente de reconciliar'
        elif 'ENTRY_REJECTED' in named:
            state, label = 'REJECTED', 'Rechazo de entrada registrado'
        elif any(named.get(k, {}).get('order_id') for k in ('ENTRY_SUBMITTED', 'ENTRY_ACKNOWLEDGED')):
            state, label = 'SUBMITTED', 'Envío registrado; fill no confirmado'
        elif any(k in named for k in ('ENTRY_BLOCKED', 'SIGNAL_EXECUTION_SKIPPED')):
            state, label = 'BLOCKED', 'Entrada bloqueada/no enviada'
        elif 'REVERSAL_CLOSE_ONLY' in named or row.get('ai_decision') == 'VETO_CLOSE_ONLY':
            state, label = 'CLOSE_ONLY', 'Sólo cierre; nueva entrada vetada'
        elif str(row.get('execution_mode') or '').upper() == 'PAPER':
            state, label = 'PAPER', 'Simulación'
        for name in ('SIGNAL_EXECUTION_SKIPPED', 'ENTRY_BLOCKED', 'ENTRY_REJECTED', 'REVERSAL_ABORTED'):
            reason = _code(named.get(name, {}).get('motivo'))
            if reason:
                break
        if str(row.get('execution_mode') or '').upper() == 'PAPER' and state != 'UNAVAILABLE':
            label = 'PAPER · simulación · ' + label
        row['execution_evidence'] = {
            'state': state, 'label': label, 'reason_code': reason,
            'source': 'LOCAL_ORDER_AUDIT', 'is_live_broker_query': False,
            'is_price_or_quantity_verification': False,
        }
        row['signal_price_source'] = 'STRATEGY_SIGNAL_REFERENCE'
        output.append(row)
    return output


def read_signal_audit(path, signals):
    """Do not construct a ledger: its legacy schema migration can write files."""
    path = Path(path)
    if not path.exists():
        return [], False
    wanted = {_key(s, signal=True) for s in signals} - {None}
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not {'modo', 'ticker', 'signal_id', 'evento', 'order_id'} <= set(reader.fieldnames or []):
            return [], False
        return [row for row in reader if _key(row) in wanted], True
