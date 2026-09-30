"""Execution attribution from broker-linked fills; missing prices stay unknown."""
from core.runtime_store import EventHistory


class ExecutionDiagnostics:
    EVENTS = {'ENTRY_REQUESTED', 'ENTRY_SUBMITTED', 'ENTRY_FILLED', 'TP1_SUBMITTED',
              'TP1_FILLED_70_PERCENT', 'STOP_SUBMITTED', 'EXIT_SUBMITTED', 'EXIT_FILLED',
              'POSITION_CLOSED', 'STOP_FILL_RECOVERED'}

    def __init__(self, path='data/execution_diagnostics.jsonl'):
        self.history = EventHistory(path)

    def record(self, event, fields):
        if event in self.EVENTS:
            self.history.append({'event': event, **fields})

    def report(self, limit=2000):
        events = self.history.recent(limit=limit)
        submitted, rows, seen = {}, [], set()
        for e in events:
            detail = e.get('detalle') if isinstance(e.get('detalle'), dict) else {}
            key = (e.get('modo'), e.get('ticker'), e.get('order_id'))
            if e['event'].endswith('SUBMITTED') and e.get('order_id'):
                submitted[key] = e
                continue
            if e['event'] not in {'ENTRY_FILLED', 'TP1_FILLED_70_PERCENT', 'EXIT_FILLED', 'STOP_FILL_RECOVERED'} or not e.get('order_id') or key in seen:
                continue
            seen.add(key)
            sub = submitted.get(key, {})
            sd = sub.get('detalle') if isinstance(sub.get('detalle'), dict) else {}
            exit_fill = e['event'] != 'ENTRY_FILLED'
            order_buy = (e.get('lado') == 'SELL') if exit_fill else (e.get('lado') == 'BUY')
            actual = float(e.get('precio') or 0) or None
            reference = detail.get('signal_price', sd.get('signal_price'))
            limit_price = detail.get('limit_price', detail.get('submitted_limit_price', sd.get('limit_price', sd.get('submitted_limit_price'))))
            if e['event'] == 'TP1_FILLED_70_PERCENT':
                reference = detail.get('target_price', sub.get('precio'))
                limit_price = reference
            bid, ask = sd.get('bid'), sd.get('ask')
            spread = detail.get('spread')
            if spread is None and bid and ask:
                spread = ask - bid
            qty = float(e.get('cantidad') or 0)
            slippage = (actual - float(reference)) * (1 if order_buy else -1) * qty if actual and reference else None
            rows.append({'timestamp_utc': e['timestamp_utc'], 'ticker': e['ticker'],
                'mode': e.get('modo'), 'order_id': e['order_id'], 'role': 'EXIT' if exit_fill else 'ENTRY',
                'signal_price': reference, 'requested_price': detail.get('requested_price', sd.get('requested_price', reference)),
                'limit_price': limit_price, 'fill_price': actual, 'quantity': qty,
                'spread': spread, 'slippage_usd': slippage, 'exit_reason': e.get('motivo') if exit_fill else '',
                'position_id': e.get('position_id'), 'side': e.get('lado')})
        real = [r for r in rows if r['mode'] == 'REAL']
        measured = [r['slippage_usd'] for r in real if r['slippage_usd'] is not None]
        spreads = [r['spread'] for r in real if r['spread'] is not None]
        books, strategy_pnl, actual_pnl, matched_qty = {}, 0., 0., 0.
        for r in real:
            key = (r['ticker'], r['position_id'])
            if not r['position_id']:
                continue
            if r['role'] == 'ENTRY':
                books[key] = {**r, 'remaining': r['quantity']}
            elif key in books:
                entry = books[key]
                qty = min(entry['remaining'], r['quantity'])
                entry['remaining'] -= qty
                if qty and all(x for x in (entry['signal_price'], entry['fill_price'], r['signal_price'], r['fill_price'])):
                    sign = 1 if entry['side'] == 'BUY' else -1
                    strategy_pnl += sign * (float(r['signal_price']) - float(entry['signal_price'])) * qty
                    actual_pnl += sign * (r['fill_price'] - entry['fill_price']) * qty
                    matched_qty += qty
        return {'orders': rows[-200:], 'summary': {
            'real_fills': len(real), 'measured_fills': len(measured),
            'execution_loss_usd': round(sum(measured), 4) if measured else None,
            'strategy_pnl_usd': round(strategy_pnl, 4) if matched_qty else None,
            'actual_pnl_matched_usd': round(actual_pnl, 4) if matched_qty else None,
            'matched_shares': matched_qty, 'average_spread': sum(spreads) / len(spreads) if spreads else None},
            'scope': f'Last {len(events)} events; matched fills only; excludes commissions',
            'attribution_note': 'Measured slippage includes spread, latency, and market movement; broker fault is not inferred.'}
