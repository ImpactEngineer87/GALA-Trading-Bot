"""Broker-linked, quantity-bounded profit targets for confirmed equity fills.

Each allocation owns one OCO with a same-quantity LIMIT and STOP. Parent ids
are journaled before acknowledgement reads; an ambiguous submission is never
retried. Callers serialize this controller with the executor's symbol lock.
"""
import copy
import hashlib
import math
from pathlib import Path

from core.runtime_store import JsonState

ACTIVE = {'WORKING', 'QUEUED', 'ACCEPTED', 'PENDING_ACTIVATION'}
TERMINAL = {'FILLED', 'CANCELED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'REPLACED'}


def _number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Non-finite exit value')
    return number


def oco_body(ticker, side, quantity, target, stop):
    quantity_value = _number(quantity)
    if quantity_value != int(quantity_value) or quantity_value <= 0:
        raise ValueError('Exit quantity must be a positive integer')
    if side not in {'BUY', 'SELL'}:
        raise ValueError('Unknown position side')
    target, stop = round(_number(target), 2), round(_number(stop), 2)
    if min(target, stop) <= 0 or (target <= stop if side == 'BUY' else target >= stop):
        raise ValueError('Invalid target/stop ordering')
    instruction = 'SELL' if side == 'BUY' else 'BUY_TO_COVER'
    children = []
    for kind, price in [('LIMIT', target), ('STOP', stop)]:
        child = {'session': 'NORMAL', 'duration': 'DAY', 'orderStrategyType': 'SINGLE',
            'orderType': kind, 'price' if kind == 'LIMIT' else 'stopPrice': f'{price:.2f}',
            'orderLegCollection': [{'instruction': instruction, 'positionEffect': 'CLOSING',
                'quantity': int(quantity_value), 'instrument': {'symbol': ticker, 'assetType': 'EQUITY'}}]}
        children.append(child)
    return {'orderStrategyType': 'OCO', 'childOrderStrategies': children}


def new_plan(order, entry_order_id):
    qty, partial, runner = (int(getattr(order, name)) for name in
                            ('cantidad', 'cantidad_parcial', 'cantidad_resto'))
    if not entry_order_id or min(partial, runner) <= 0 or partial + runner != qty:
        raise ValueError('Linked exits require an exact entry and complete share allocation')
    entry, first, final = map(_number, (order.precio_limite, order.take_profit_parcial, order.take_profit))
    if not (entry < first < final if order.tipo == 'BUY' else final < first < entry):
        raise ValueError('TP1 must be between entry and final target')
    groups = []
    for stage, amount, target in [('TP1', partial, first), ('FINAL', runner, final)]:
        oco_body(order.ticker, order.tipo, amount, target, order.stop_loss)
        groups.append({'stage': stage, 'allocation': amount, 'quantity': amount,
            'target': round(target, 2), 'stop': round(float(order.stop_loss), 2),
            'attempted': False, 'root_id': '', 'leaves': {}})
    return {'schema': 1, 'ticker': order.ticker, 'side': order.tipo, 'quantity': qty,
        'entry_order_id': str(entry_order_id), 'groups': groups, 'retired': [], 'status': 'NEW'}


def read_group(raw, group, ticker, side, detail_reader):
    """Validate exact root/leg identities before treating an OCO as protection."""
    if (not isinstance(raw, dict) or str(raw.get('orderId') or '') != group['root_id']
            or raw.get('orderStrategyType') != 'OCO'):
        raise ValueError('Exact OCO parent unavailable')
    children = raw.get('childOrderStrategies')
    if not isinstance(children, list) or len(children) != 2:
        raise ValueError('Expected exactly one target and one stop')
    leaves = {}
    for child in children:
        kind = child.get('orderType')
        legs = child.get('orderLegCollection') or []
        if kind not in {'LIMIT', 'STOP'} or kind in leaves or len(legs) != 1:
            raise ValueError('Unexpected linked exit leg')
        leg = legs[0]
        expected_instruction = 'SELL' if side == 'BUY' else 'BUY_TO_COVER'
        if (leg.get('instruction') != expected_instruction
                or (leg.get('instrument') or {}).get('symbol') != ticker
                or (leg.get('instrument') or {}).get('assetType') != 'EQUITY'
                or _number(leg.get('quantity')) != group['quantity']
                or round(_number(child.get('price' if kind == 'LIMIT' else 'stopPrice')), 2)
                   != group['target' if kind == 'LIMIT' else 'stop']):
            raise ValueError('Broker exit differs from the saved allocation')
        oid = str(child.get('orderId') or '')
        if not oid or oid == group['root_id']:
            raise ValueError('Missing child identity')
        detail = detail_reader(child)
        filled = _number(detail.get('filled', 0))
        if not 0 <= filled <= group['quantity']:
            raise ValueError('Invalid child execution quantity')
        if detail.get('status') == 'FILLED' and filled <= 0:
            raise ValueError('Filled child lacks an execution quantity')
        leaves[kind] = {'order_id': oid, **detail}
    if len({x['order_id'] for x in leaves.values()}) != 2:
        raise ValueError('Duplicate linked exit identity')
    if sum(x['filled'] for x in leaves.values()) > group['quantity']:
        raise ValueError('OCO executions exceed the allocation')
    return leaves


def execution_summary(plan, *, exclude_tp1=False):
    """Aggregate only actual, independently priced child executions."""
    fills = {}
    for group in plan.get('retired', []) + plan['groups']:
        if exclude_tp1 and group['stage'] == 'TP1':
            continue
        for kind, leaf in group.get('leaves', {}).items():
            qty = _number(leaf.get('filled', 0))
            if not qty:
                continue
            price = _number(leaf.get('avg_price', 0))
            if leaf.get('fill_price_verified') is not True or price <= 0:
                return None
            oid = leaf['order_id']
            item = {'order_id': oid, 'filled': qty, 'avg_price': price,
                'stage': group['stage'], 'kind': kind, 'status': leaf.get('status')}
            if oid in fills and fills[oid] != item:
                return None
            fills[oid] = item
    legs = list(fills.values())
    quantity = sum(x['filled'] for x in legs)
    return {'quantity': quantity, 'avg_price': sum(x['filled'] * x['avg_price'] for x in legs) / quantity
            if quantity else 0., 'legs': legs}


def plan_ids(plan):
    ids = []
    for group in plan.get('retired', []) + plan.get('groups', []):
        ids.append(group.get('root_id'))
        ids.extend(leaf.get('order_id') for leaf in group.get('leaves', {}).values())
    return list(dict.fromkeys(str(x) for x in ids if x))


class NativeExits:
    def __init__(self, executor, account, path):
        self.executor, self.account, self.path = executor, account, Path(path)

    def _key(self, entry_id):
        return hashlib.sha256(f'{self.account}:{entry_id}'.encode()).hexdigest()

    def save(self, plan):
        self.path.mkdir(parents=True, exist_ok=True)
        self.path.chmod(0o700)
        state = JsonState(self.path / (self._key(plan['entry_order_id']) + '.json'))
        if state.get('plan') != plan:
            state.put('plan', plan)
            state.path.chmod(0o600)

    def load(self, entry_id):
        return JsonState(self.path / (self._key(entry_id) + '.json')).get('plan')

    def submit_group(self, plan, group):
        if group['attempted']:
            return
        # Persist intent before POST. A timeout/missing Location must never
        # turn into another POST for the same allocation.
        group['attempted'] = True
        self.save(plan)
        body = oco_body(plan['ticker'], plan['side'], group['quantity'], group['target'], group['stop'])
        try:
            response = self.executor._submit_order_request('post',
                f'{self.executor.TRADER_URL}/accounts/{self.account}/orders',
                json=body, timeout=self.executor.request_timeout_s)
            response.raise_for_status()
            group['root_id'] = response.headers.get('Location', '').rstrip('/').split('/')[-1]
            group['submission'] = 'ACCEPTED' if group['root_id'] else 'UNKNOWN'
        except Exception as exc:
            response = getattr(exc, 'response', None)
            # A timeout/5xx may have accepted the request. Preserve its intent.
            group['submission'] = ('REJECTED' if response is not None
                and 400 <= response.status_code < 500 else 'UNKNOWN')
        self.save(plan)
        self.executor._audit('LINKED_EXIT_SUBMITTED', ticker=plan['ticker'], lado=plan['side'],
            estado=group['submission'], order_id=group['root_id'], cantidad=group['quantity'],
            precio=group['target'], parent_order_id=plan['entry_order_id'],
            detalle={'stage': group['stage'], 'stop': group['stop'], 'strategy': 'OCO'})

    def install(self, order, entry_id):
        plan = self.load(entry_id)
        if plan is not None:
            if (plan['ticker'], plan['side'], plan['quantity']) != (order.ticker, order.tipo, order.cantidad):
                raise ValueError('Entry identity already owns a different native exit plan')
            return self.refresh(plan)
        plan = new_plan(order, entry_id)
        self.save(plan)
        for group in plan['groups']:
            if not self._can_submit_group(plan, group):
                break
            self.submit_group(plan, group)
            if not group['root_id']:
                break
        return self.refresh(plan)

    def _can_submit_group(self, plan, candidate):
        """Recheck external fills/manual trades between allocation submissions."""
        try:
            reserved = 0
            for group in plan.get('retired', []) + plan['groups']:
                if not group.get('attempted'):
                    continue
                if not group.get('root_id'):
                    return False
                raw = self.executor._buscar_orden_en_listado(self.account, group['root_id'])
                leaves = read_group(raw, group, plan['ticker'], plan['side'],
                                    self.executor._detalle_desde_orden)
                group['leaves'] = leaves
                # A stop execution requires the normal close/reconciliation path.
                if leaves['STOP'].get('filled', 0):
                    return False
                reserved += max((group['quantity'] - leaf.get('filled', 0)
                    for leaf in leaves.values() if leaf.get('status') in ACTIVE), default=0)
            summary = execution_summary(plan)
            snap = self.executor._position_snapshot(self.account, plan['ticker'])
            wanted = 'long' if plan['side'] == 'BUY' else 'short'
            opposite = 'short' if wanted == 'long' else 'long'
            working = self.executor.ordenes_trabajando_ticker(self.account, plan['ticker'])
            return (summary is not None and snap is not None and working is not None
                and _number(snap.get(opposite, 0)) == 0
                and _number(snap.get(wanted, 0)) == plan['quantity'] - summary['quantity']
                and candidate['quantity'] + reserved <= _number(snap.get(wanted, 0))
                and set(map(str, working)).issubset(set(plan_ids(plan))))
        except (TypeError, ValueError, KeyError, AttributeError):
            return False

    def refresh(self, plan, *, force=False):
        plan['status'] = 'VERIFIED'
        seen = set()
        observed = getattr(self.executor, 'native_order_snapshot', None)
        snapshot = observed(self.account) if callable(observed) else None
        def nodes(orders):
            for node in orders or []:
                if isinstance(node, dict):
                    yield node
                    yield from nodes(node.get('childOrderStrategies') or [])
        current = {str(node.get('orderId')):node for node in nodes(snapshot if isinstance(snapshot,list) else [])}
        for group in plan.get('retired', []) + plan['groups']:
            if not group.get('root_id'):
                plan['status'] = 'RECONCILE'
                continue
            try:
                leaves = group.get('leaves', {})
                terminal = len(leaves) == 2 and all(x.get('status') in TERMINAL
                    and (not x.get('filled') or x.get('fill_price_verified') is True) for x in leaves.values())
                if force or not terminal:
                    raw = None if force else current.get(group['root_id'])
                    if raw is None:
                        raw = self.executor._buscar_orden_en_listado(self.account, group['root_id'])
                    leaves = read_group(raw, group, plan['ticker'], plan['side'],
                                        self.executor._detalle_desde_orden)
                for leaf in leaves.values():
                    if leaf['order_id'] in seen:
                        raise ValueError('Child identity belongs to two allocations')
                    seen.add(leaf['order_id'])
                group['leaves'] = leaves
            except (TypeError, ValueError, KeyError, AttributeError):
                plan['status'] = 'RECONCILE'
        self.save(plan)
        return plan

    def cancel(self, plan, groups=None):
        """Confirm each selected allocation's cancellation and current absence."""
        selected = plan.get('retired', []) + plan['groups'] if groups is None else groups
        if any(g.get('attempted') and not g.get('root_id') for g in selected):
            return False
        confirmed = True
        ids = plan_ids({'groups': selected})
        for oid in ids:
            result = self.executor.cancelar_orden_confirmada(self.account, oid, ticker=plan['ticker']) or {}
            confirmed = confirmed and result.get('confirmado') is True
        working = self.executor.ordenes_trabajando_ticker(self.account, plan['ticker'])
        return confirmed and working is not None and not set(ids).intersection(working)

    def repair(self, plan, stop=None):
        """Rebuild only unfilled shares after cancellation and exact reconciliation.

        Covers partial fills and moving the runner to breakeven. All previous
        child executions remain attached to their original order identities.
        """
        before = execution_summary(plan)
        if before is None:
            return False
        repair_key = [before['quantity'] if before is not None else None, stop]
        if plan.get('repair_key') == repair_key and plan.get('repair_attempts', 0) >= 2:
            return False
        selected = [g for g in plan['groups'] if (
            any(0 < x.get('filled', 0) < g['quantity'] for x in g['leaves'].values())
            or (sum(x.get('filled', 0) for x in g['leaves'].values()) < g['quantity']
                and (g['leaves'].get('STOP') or {}).get('status') not in ACTIVE)
            or (stop is not None and g['stage'] == 'FINAL' and g['stop'] != stop))]
        if not selected:
            return True
        if not self.cancel(plan, selected):
            return False
        self.refresh(plan, force=True)
        if (plan['status'] != 'VERIFIED'
                or any(x.get('status') not in TERMINAL for g in selected for x in g['leaves'].values())):
            return False
        summary = execution_summary(plan)
        snap = self.executor._position_snapshot(self.account, plan['ticker'])
        side = 'long' if plan['side'] == 'BUY' else 'short'
        other = 'short' if side == 'long' else 'long'
        if (summary is None or snap is None or _number(snap.get(other, 0)) != 0
                or _number(snap.get(side, 0)) != plan['quantity'] - summary['quantity']):
            return False
        current = selected
        replacements = []
        for group in current:
            filled = sum(leaf.get('filled', 0) for leaf in group['leaves'].values())
            remaining = group['quantity'] - filled
            if remaining < 0 or remaining != int(remaining):
                return False
            if remaining:
                replacements.append({**{k:group[k] for k in ('stage','allocation','target','stop')},
                    'quantity': int(remaining), 'stop': float(stop) if stop is not None and group['stage'] == 'FINAL' else group['stop'],
                    'attempted': False, 'root_id': '', 'leaves': {}})
        # Validate every replacement before changing the saved generation.
        for group in replacements:
            oco_body(plan['ticker'], plan['side'], group['quantity'], group['target'], group['stop'])
        plan['repair_attempts'] = plan.get('repair_attempts', 0) + 1 if plan.get('repair_key') == repair_key else 1
        plan['repair_key'] = repair_key
        plan['retired'].extend(copy.deepcopy(current))
        plan['groups'] = [g for g in plan['groups'] if g not in selected] + replacements
        plan['groups'].sort(key=lambda g: g['stage'] != 'TP1')
        self.save(plan)
        for group in replacements:
            if not self._can_submit_group(plan, group):
                plan['status'] = 'RECONCILE'
                self.save(plan)
                return False
            self.submit_group(plan, group)
            if not group['root_id']:
                return False
        self.refresh(plan)
        return plan['status'] == 'VERIFIED'
