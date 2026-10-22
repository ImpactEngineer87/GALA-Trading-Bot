"""Recoverable smart exits. A cancel request is never proof of cancellation.

v3.3.2 adds emergency recovery for stale durable exit records.  During a kill
switch the broker's *current* position and WORKING-order snapshot are the final
authority; an old ``execution_pending.json`` row may never permanently block a
verified emergency flatten.
"""
import hashlib
import math
import time
import uuid
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from core.runtime_store import JsonState, EventHistory, utc_now

TERMINAL = {'FILLED', 'CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED'}
EMERGENCY_WORDS = ('stop', 'kill', 'emergen', 'seguridad', 'huérfana', 'huerfana',
                   'apagar', 'apagado', 'shutdown', 'daily', 'diari', 'cierre mercado',
                   'fin de sesión', 'fin_sesion')


def marketable_price(side, bid, ask, buffer_bps=5):
    bid, ask, buffer_bps = float(bid), float(ask), float(buffer_bps)
    if not all(math.isfinite(x) for x in (bid, ask, buffer_bps)) or not 0 < bid <= ask or buffer_bps < 0:
        raise ValueError('Invalid bid/ask or price buffer')
    buy = side == 'SELL'  # closing a short = BUY_TO_COVER
    price = ask * (1 + buffer_bps / 10000) if buy else bid * (1 - buffer_bps / 10000)
    value = Decimal(str(price)).quantize(Decimal('0.01'), rounding=ROUND_CEILING if buy else ROUND_FLOOR)
    if value <= 0:
        raise ValueError('Invalid marketable limit price')
    return float(value)


class SmartExit:
    def __init__(self, executor, config):
        self.executor = executor
        self.config = config
        self.pending = JsonState(config.get('pending_path', 'data/execution_pending.json'))
        self.history = EventHistory(config.get('history_path', 'data/execution_v2.jsonl'))

    @staticmethod
    def _is_emergency(reason):
        text = str(reason or '')
        lower = text.lower()
        return (
            text.upper().startswith('SL')
            or any(word in lower for word in EMERGENCY_WORDS)
            or 'sin proteccion' in lower
            or 'sin protección' in lower
        )

    def _quote(self, ticker):
        provider = getattr(self.executor, '_quote_provider', None)
        try:
            q = provider(ticker) if provider else {}
            age = time.time() - float(q.get('quote_time', 0))
            if q.get('delayed') or not 0 <= age <= float(self.config.get('max_quote_age_sec', 5)):
                return {}
            marketable_price('BUY', q['bid'], q['ask'], 0)
            return q
        except Exception:
            return {}

    def _pending_result(self, state, reason):
        last = (state.get('legs') or [{}])[-1]
        return {'status': 'RECONCILIAR', 'order_id': last.get('order_id', ''),
                'posicion_plana': False, 'mensaje': reason,
                'cantidad': state.get('quantity', 0), 'broker_side': state.get('side', '')}

    def _refresh(self, account, leg):
        if not leg.get('order_id'):
            return False
        d = self.executor.detalle_orden(account, leg['order_id'])
        leg.update(status=str(d.get('status', '')).upper(), filled=float(d.get('filled', 0) or 0),
                   avg_price=float(d.get('avg_price', 0) or 0))
        return leg['status'] in TERMINAL

    def _snapshot_exposure(self, account, ticker):
        """Return (snapshot, side, qty); ``None`` snapshot means broker unknown."""
        snap = self.executor._position_snapshot(account, ticker)
        if snap is None:
            return None, '', 0
        long_q = int(snap.get('long', 0) or 0)
        short_q = int(snap.get('short', 0) or 0)
        if long_q > 0 and short_q > 0:
            return snap, 'AMBIGUOUS', long_q + short_q
        if long_q > 0:
            return snap, 'BUY', long_q
        if short_q > 0:
            return snap, 'SELL', short_q
        return snap, '', 0

    def _working_order(self, account, order_id):
        fn = getattr(self.executor, 'orden_trabajando', None)
        if not callable(fn) or not order_id:
            return None
        try:
            return fn(account, order_id)
        except Exception:
            return None

    def _working_orders_for_ticker(self, account, ticker):
        fn = getattr(self.executor, 'ordenes_trabajando_ticker', None)
        if not callable(fn):
            return None
        try:
            return fn(account, ticker)
        except Exception:
            return None

    def _new_state(self, ticker, side, quantity, reason, reference_price=None,
                   recovery_from=None, position_id=None):
        q = self._quote(ticker)
        emergency = self._is_emergency(reason)
        limit = marketable_price(
            side, q['bid'], q['ask'], self.config.get('exit_buffer_bps', 5)
        ) if q and not emergency else None
        state = {
            'execution_id': uuid.uuid4().hex,
            'ticker': ticker,
            'side': side,
            'quantity': int(quantity),
            'reason': reason,
            'position_id': str(position_id or ''),
            'created_utc': utc_now(),
            'reference_price': reference_price,
            'quote': q,
            'limit': limit,
            'route_reason': (
                'EMERGENCY' if emergency
                else ('MARKETABLE_LIMIT' if limit else 'QUOTE_UNAVAILABLE')
            ),
            'legs': [],
        }
        if recovery_from:
            state['recovery_from'] = recovery_from
        return state

    @staticmethod
    def _verified_fill_legs(state):
        """Return actual broker-weighted exit fill price and order id, or None.

        A FLAT position, quoted price, limit price, incomplete order list, or
        duplicate order identity is never sufficient for training evidence.
        Require a terminal broker order detail and a real execution price for
        *every* filled share, with total shares exactly equal to the original
        exit intent. Partial fills in canceled LIMIT legs are allowed.
        """
        try:
            target = int(state.get('quantity', 0))
        except (TypeError, ValueError, OverflowError):
            return None
        if target <= 0 or not state.get('legs'):
            return None
        seen = set()
        qty = 0.0
        gross = 0.0
        last_filled_id = ''
        for leg in state['legs']:
            if not isinstance(leg, dict):
                return None
            oid = str(leg.get('order_id', '') or '').strip()
            status = str(leg.get('status', '') or '').upper()
            try:
                filled = float(leg.get('filled', 0) or 0)
                price = float(leg.get('avg_price', 0) or 0)
            except (TypeError, ValueError, OverflowError):
                return None
            if (not oid or oid in seen or status not in TERMINAL
                    or not math.isfinite(filled) or filled < 0):
                return None
            seen.add(oid)
            if filled:
                if (status not in {'FILLED', 'CANCELED', 'CANCELLED', 'EXPIRED'}
                        or not math.isfinite(price) or price <= 0):
                    return None
                qty += filled
                gross += filled * price
                last_filled_id = oid
        if not last_filled_id or not math.isclose(qty, target, abs_tol=1e-6, rel_tol=0):
            return None
        avg = gross / qty
        return (avg, last_filled_id) if math.isfinite(avg) and avg > 0 else None

    def _recover_flat(self, key, state, reason, *, account=None,
                      requested_side=None, requested_quantity=None,
                      requested_position_id=None):
        """Broker is flat but historical fill evidence is incomplete.

        Safety and model-learning are intentionally separated: the position is
        allowed to close locally because Schwab is authoritatively flat, while
        ``fill_price_verified=False`` tells the engine to quarantine this close
        from the verified x/50 learning set.
        """
        # The pending journal may belong to a previous trade in the same
        # ticker. Do not attribute its executions to a later position, even if
        # Schwab is flat. Match the requesting side and expected share count.
        try:
            matches_request = (
                account is not None
                and str(state.get('side', '') or '').upper()
                    == str(requested_side or '').upper()
                and int(state.get('quantity', 0)) == int(requested_quantity)
                and bool(state.get('position_id'))
                and str(state.get('position_id')) == str(requested_position_id or '')
            )
        except (TypeError, ValueError, OverflowError):
            matches_request = False
        if matches_request:
            # Read-only broker details: no new order, cancel, or fill is
            # generated. Never trust stale cached fields after a GET failure.
            for leg in state.get('legs', []):
                try:
                    if not leg.get('order_id'):
                        break
                    self._refresh(account, leg)
                except Exception:
                    leg['status'] = 'UNKNOWN'
                    leg['filled'] = 0
                    leg['avg_price'] = 0
            evidence = self._verified_fill_legs(state)
            if evidence is not None:
                state['recovered_from_broker_flat'] = True
                state['fill_evidence_source'] = 'BROKER_ORDER_EXECUTION_LEGS'
                return self._complete(key, state)
        last = (state.get('legs') or [{}])[-1]
        last_id = str(last.get('order_id', '') or '')
        event = {
            **state,
            'event': 'EXIT_RECOVERED_FLAT',
            'recovery_reason': reason,
            'fill_price': None,
            'fill_price_verified': False,
        }
        self.history.append(event)
        self.executor._audit(
            'EXIT_RECOVERED_FLAT', ticker=state.get('ticker', ''),
            lado=state.get('side', ''), order_id=last_id,
            cantidad=state.get('quantity', 0), estado='BROKER_FLAT',
            motivo=reason,
            detalle={
                'execution_id': state.get('execution_id', ''),
                'order_ids': [str(x.get('order_id', '') or '') for x in state.get('legs', [])],
                'fill_price_verified': False,
            },
        )
        self.pending.put(key, None)
        return {
            'status': 'OK', 'order_id': last_id,
            'cantidad': int(state.get('quantity', 0) or 0),
            'avg_price': 0.0, 'broker_status': 'FLAT',
            'posicion_plana': True, 'broker_side': state.get('side', ''),
            'fill_price_verified': False, 'execution_details': event,
            'mensaje': reason,
        }

    def retire_pending_after_authoritative_flat(self, account, ticker, reason="broker confirmed flat"):
        """Retire a durable exit intent only after an authoritative flat snapshot.

        The caller must already have confirmed that the ticker has neither a
        broker position nor a WORKING order.  This method performs no broker
        mutation and is therefore safe for the dashboard's read-only
        reconciliation path.
        """
        key = hashlib.sha256(str(account).encode()).hexdigest()[:16] + ':' + str(ticker).upper()
        state = self.pending.get(key)
        if state is None:
            return False
        event = {
            **state,
            'event': 'STALE_EXIT_RETIRED_BROKER_FLAT',
            'recovery_reason': str(reason or 'broker confirmed flat'),
            'fill_price': None,
            'fill_price_verified': False,
        }
        self.history.append(event)
        last = (state.get('legs') or [{}])[-1]
        self.executor._audit(
            'STALE_EXIT_RETIRED_BROKER_FLAT',
            ticker=str(ticker).upper(),
            lado=state.get('side', ''),
            order_id=str(last.get('order_id', '') or ''),
            cantidad=state.get('quantity', 0),
            estado='BROKER_FLAT',
            motivo=str(reason or 'broker confirmed flat'),
            detalle={
                'execution_id': state.get('execution_id', ''),
                'order_ids': [str(x.get('order_id', '') or '') for x in state.get('legs', [])],
                'fill_price_verified': False,
            },
        )
        self.pending.put(key, None)
        return True

    def _replace_stale_emergency_state(self, key, state, ticker, side, quantity,
                                       reason, reference_price, position_id=None):
        old_ids = [str(x.get('order_id', '') or '') for x in state.get('legs', [])]
        recovery = {
            'old_execution_id': state.get('execution_id', ''),
            'old_order_ids': old_ids,
            'old_created_utc': state.get('created_utc', ''),
            'old_reason': state.get('reason', ''),
        }
        new_state = self._new_state(
            ticker, side, quantity, reason, reference_price,
            recovery_from=recovery, position_id=position_id,
        )
        # One atomic replacement: there is never a window where a crash loses the
        # durable intent while broker exposure still exists.
        self.pending.put(key, new_state)
        self.history.append({
            **recovery,
            'event': 'STALE_EXIT_RETIRED_FOR_EMERGENCY',
            'ticker': ticker,
            'broker_side': side,
            'broker_quantity': int(quantity),
            'new_execution_id': new_state['execution_id'],
            'reason': reason,
        })
        self.executor._audit(
            'STALE_EXIT_RETIRED_FOR_EMERGENCY', ticker=ticker, lado=side,
            estado='RECOVERED_FROM_BROKER', cantidad=int(quantity),
            motivo='Old pending exit is not WORKING; emergency close rebuilt from broker exposure',
            detalle={
                'old_execution_id': recovery['old_execution_id'],
                'old_order_ids': old_ids,
                'new_execution_id': new_state['execution_id'],
            },
        )
        return new_state

    def _replace_stale_normal_state(self, key, state, ticker, side, quantity,
                                    reason, reference_price, position_id=None):
        """Rebuild a proven-stale normal exit from current broker exposure.

        v3.3.5 extends stale-journal recovery to reversals/TP/opposite-signal
        closes, but only after the caller has already proved: the historical
        order is not WORKING, the ticker has no other WORKING order, and the
        current broker side matches the position the engine intends to close.
        """
        old_ids = [str(x.get('order_id', '') or '') for x in state.get('legs', [])]
        recovery = {
            'old_execution_id': state.get('execution_id', ''),
            'old_order_ids': old_ids,
            'old_created_utc': state.get('created_utc', ''),
            'old_reason': state.get('reason', ''),
        }
        new_state = self._new_state(
            ticker, side, quantity, reason, reference_price,
            recovery_from=recovery, position_id=position_id,
        )
        self.pending.put(key, new_state)
        self.history.append({
            **recovery,
            'event': 'STALE_EXIT_RETIRED_FOR_NORMAL_CLOSE',
            'ticker': ticker,
            'broker_side': side,
            'broker_quantity': int(quantity),
            'new_execution_id': new_state['execution_id'],
            'reason': reason,
        })
        self.executor._audit(
            'STALE_EXIT_RETIRED_FOR_NORMAL_CLOSE', ticker=ticker, lado=side,
            estado='RECOVERED_FROM_BROKER', cantidad=int(quantity),
            motivo='Old pending exit is not WORKING; normal close rebuilt from broker exposure',
            detalle={
                'old_execution_id': recovery['old_execution_id'],
                'old_order_ids': old_ids,
                'new_execution_id': new_state['execution_id'],
            },
        )
        return new_state

    def close(self, ticker, side, quantity, account, reason, reference_price=None,
              position_id=None):
        key = hashlib.sha256(str(account).encode()).hexdigest()[:16] + ':' + ticker
        emergency = self._is_emergency(reason)
        # This method is called within the executor's symbol lock.
        state = self.pending.get(key)
        if state is None:
            snap, broker_side, broker_qty = self._snapshot_exposure(account, ticker)
            if snap is None:
                return self._pending_result({}, 'Broker position unavailable')
            if broker_side == 'AMBIGUOUS':
                return self._pending_result({}, 'Ambiguous broker side')
            if broker_qty == 0:
                return {'status': 'OK', 'posicion_plana': True, 'cantidad': 0,
                        'broker_status': 'FLAT', 'order_id': '', 'avg_price': 0,
                        'fill_price_verified': False}
            state = self._new_state(
                ticker, broker_side, broker_qty, reason, reference_price,
                position_id=position_id,
            )
            self.pending.put(key, state)

        # Resume accepted orders on retries/restarts.  v3.3.2 recovery rule:
        # an old journal row may block a replacement only while its broker order
        # is actually WORKING (or broker state cannot be read).  If it is no longer
        # WORKING, reconcile current exposure before deciding what to do next.
        stale_nonworking_seen = False
        for leg in state.get('legs', []):
            terminal = self._refresh(account, leg)
            self.pending.put(key, state)
            if terminal:
                continue

            cancel_result = None
            working = self._working_order(account, leg.get('order_id'))
            if working is True and leg.get('order_id') and leg.get('type') == 'LIMIT':
                cancel_result = self.executor.cancelar_orden_confirmada(
                    account, leg['order_id'], ticker=ticker
                )
                hinted = str((cancel_result or {}).get('broker_status', '') or '').upper()
                if hinted in TERMINAL:
                    leg['status'] = hinted
                    leg['filled'] = float((cancel_result or {}).get('filled', 0) or 0)
                    terminal = True
                else:
                    terminal = self._refresh(account, leg)
                self.pending.put(key, state)
                working = self._working_order(account, leg.get('order_id'))

            if terminal:
                continue
            if working is True:
                return self._pending_result(
                    state, 'Prior exit is still WORKING; no duplicate order sent'
                )
            if working is None:
                # Preserve fail-closed behavior when the broker cannot answer the
                # current WORKING-order question.
                return self._pending_result(
                    state, 'Prior exit state cannot be verified against WORKING orders'
                )
            stale_nonworking_seen = True

        if stale_nonworking_seen:
            # Before retiring anything, prove that there is no other live order for
            # this symbol.  This prevents a replacement close from colliding with a
            # protective/child order that was not the one stored in the journal.
            working_ids = self._working_orders_for_ticker(account, ticker)
            if working_ids is None:
                return self._pending_result(
                    state, 'Ticker WORKING-order snapshot unavailable; stale exit retained'
                )
            if working_ids:
                return self._pending_result(
                    state, 'Ticker still has WORKING broker order(s); stale exit retained'
                )

            snap, broker_side, broker_qty = self._snapshot_exposure(account, ticker)
            if snap is None:
                return self._pending_result(
                    state, 'Broker position unavailable while resolving stale exit'
                )
            if broker_side == 'AMBIGUOUS':
                return self._pending_result(
                    state, 'Ambiguous broker side while resolving stale exit'
                )
            if broker_qty == 0:
                return self._recover_flat(
                    key, state,
                    'Prior exit is no longer WORKING and Schwab confirms the position is flat',
                    account=account, requested_side=side,
                    requested_quantity=quantity, requested_position_id=position_id,
                )
            if not emergency:
                requested_side = str(side or '').upper()
                if requested_side not in ('BUY', 'SELL') or broker_side != requested_side:
                    return self._pending_result(
                        state,
                        'Broker side differs from local position during stale normal-exit recovery',
                    )
                # v3.3.5: a historical journal row is not allowed to permanently
                # block an ordinary reversal/TP/opposite-signal close once Schwab
                # proves that the old order is no longer WORKING, that no other
                # order for the ticker is WORKING, and that current exposure still
                # matches the side the engine intends to close. Rebuild from the
                # broker-confirmed remaining quantity and continue with the normal
                # marketable-LIMIT policy.
                state = self._replace_stale_normal_state(
                    key, state, ticker, broker_side, broker_qty, reason, reference_price,
                    position_id=position_id,
                )
            else:
                # Emergency/KILL path: safety takes precedence over stale historical
                # accounting. Rebuild from *current* broker side/quantity and submit a
                # fresh MARKET close only after proving there are no live orders.
                state = self._replace_stale_emergency_state(
                    key, state, ticker, broker_side, broker_qty, reason, reference_price,
                    position_id=position_id,
                )

        # At most one LIMIT attempt and one MARKET remainder per operation.
        while True:
            snap = self.executor._position_snapshot(account, ticker)
            if snap is None:
                return self._pending_result(state, 'Broker position unavailable after exit')
            expected = state['quantity'] - sum(leg.get('filled', 0) for leg in state['legs'])
            actual = int(snap.get('long', 0)) if state['side'] == 'BUY' else int(snap.get('short', 0))
            opposite = int(snap.get('short', 0)) if state['side'] == 'BUY' else int(snap.get('long', 0))
            if opposite or actual != expected or expected < 0:
                # A protective STOP, a partially filled LIMIT, or a prior exit
                # can flatten Schwab without leaving a fully readable order
                # journal. If the broker is authoritatively FLAT and there are
                # no outstanding WORKING orders, do not leave this ticker stuck
                # forever in RECONCILIAR merely because fills are incomplete.
                # Reconcile local exposure, but keep the close excluded from
                # AI training unless exact broker fill evidence is recovered.
                if actual == 0 and opposite == 0:
                    working_ids = self._working_orders_for_ticker(account, ticker)
                    if working_ids is None:
                        return self._pending_result(
                            state, 'Schwab is flat but WORKING-order snapshot unavailable'
                        )
                    if working_ids:
                        return self._pending_result(
                            state, 'Schwab is flat but WORKING orders remain'
                        )
                    return self._recover_flat(
                        key, state,
                        'Schwab confirms FLAT with no WORKING orders; recorded exit fills disagree',
                        account=account, requested_side=side,
                        requested_quantity=quantity, requested_position_id=position_id,
                    )
                if emergency:
                    working_ids = self._working_orders_for_ticker(account, ticker)
                    if working_ids is None:
                        return self._pending_result(
                            state,
                            'Position/fill mismatch and WORKING-order snapshot is unavailable',
                        )
                    if working_ids:
                        return self._pending_result(
                            state,
                            'Position/fill mismatch while broker order(s) are still WORKING',
                        )
                    snap_now, broker_side, broker_qty = self._snapshot_exposure(account, ticker)
                    if snap_now is None:
                        return self._pending_result(
                            state, 'Broker position unavailable during emergency reconciliation'
                        )
                    if broker_side == 'AMBIGUOUS':
                        return self._pending_result(
                            state, 'Ambiguous broker side during emergency reconciliation'
                        )
                    if broker_qty == 0:
                        return self._recover_flat(
                            key, state,
                            'Schwab is flat after a terminal/unknown prior exit; fill accounting was incomplete',
                            account=account, requested_side=side,
                            requested_quantity=quantity, requested_position_id=position_id,
                        )
                    state = self._replace_stale_emergency_state(
                        key, state, ticker, broker_side, broker_qty, reason, reference_price,
                        position_id=position_id,
                    )
                    continue
                return self._pending_result(state, 'Position and confirmed fills disagree; reconcile before retry')
            if actual == 0:
                if self._verified_fill_legs(state) is not None:
                    return self._complete(key, state)
                # Some terminal broker orders expose FILLED and quantity but
                # not the executionLegs price. Remaining in RECONCILIAR does
                # not protect an already-flat account; it only strands local
                # state and prevents normal learning/next signals. Quarantine
                # the label, but release the stale exit after the no-WORKING
                # broker safety check.
                working_ids = self._working_orders_for_ticker(account, ticker)
                if working_ids is None:
                    return self._pending_result(
                        state, 'Schwab is flat but WORKING-order snapshot unavailable'
                    )
                if working_ids:
                    return self._pending_result(
                        state, 'Schwab is flat but WORKING orders remain'
                    )
                return self._recover_flat(
                    key, state,
                    'Schwab confirms FLAT with no WORKING orders; exit price unverified',
                    account=account, requested_side=side,
                    requested_quantity=quantity, requested_position_id=position_id,
                )
            if len(state['legs']) >= (2 if state['limit'] is not None else 1):
                return self._pending_result(state, 'Exit ended with residual exposure; operator reconciliation required')
            is_limit = not state['legs'] and state['limit'] is not None
            if state['legs'] and not self.config.get('market_fallback', True):
                return self._pending_result(state, 'Limit canceled; market fallback disabled')
            leg = {'order_id': '', 'type': 'LIMIT' if is_limit else 'MARKET',
                   'quantity': actual, 'limit_price': state['limit'] if is_limit else None,
                   'filled': 0, 'avg_price': 0, 'status': 'SUBMITTING'}
            state['legs'].append(leg)
            # Durable intent BEFORE POST. Timeout or missing Location is ambiguous.
            self.pending.put(key, state)
            body = {'session': 'NORMAL', 'duration': 'DAY', 'orderStrategyType': 'SINGLE',
                    'orderType': leg['type'], 'orderLegCollection': [{
                        'orderLegType': 'EQUITY', 'legId': 1, 'positionEffect': 'CLOSING',
                        'instruction': 'SELL' if state['side'] == 'BUY' else 'BUY_TO_COVER',
                        'quantity': actual, 'instrument': {'symbol': ticker, 'assetType': 'EQUITY'}}]}
            if is_limit:
                body['price'] = format(state['limit'], '.2f')
            try:
                response = self.executor._submit_order_request(
                    'post', f'{self.executor.TRADER_URL}/accounts/{account}/orders', json=body,
                    timeout=self.executor.request_timeout_s)
                response.raise_for_status()
                leg['order_id'] = response.headers.get('Location', '').rstrip('/').split('/')[-1]
                leg['status'] = 'SUBMITTED'
                self.pending.put(key, state)
                self.executor._audit('EXIT_SUBMITTED', ticker=ticker, lado=state['side'],
                    order_id=leg['order_id'], cantidad=actual, precio=leg['limit_price'],
                    motivo=reason, estado='SUBMITTED', detalle={'tipo_orden': leg['type'],
                    'signal_price': state['reference_price'], 'limit_price': leg['limit_price'],
                    'bid': state['quote'].get('bid'), 'ask': state['quote'].get('ask'),
                    'recovered_stale_exit': bool(state.get('recovery_from'))})
            except Exception as exc:
                return self._pending_result(state, f'Exit submission uncertain: {type(exc).__name__}')
            if not leg['order_id']:
                return self._pending_result(state, 'Accepted exit has no order ID; manual reconciliation required')
            self.executor.esperar_estado_orden(account, leg['order_id'], deseados={'FILLED'},
                fallos=TERMINAL - {'FILLED'}, timeout_s=float(self.config.get('limit_wait_sec', 1.5)) if is_limit else self.executor.fill_timeout_s)
            terminal = self._refresh(account, leg)
            if not terminal and is_limit:
                cancel_result = self.executor.cancelar_orden_confirmada(account, leg['order_id'], ticker=ticker)
                hinted = str((cancel_result or {}).get('broker_status', '') or '').upper()
                if hinted in TERMINAL:
                    leg['status'] = hinted
                    leg['filled'] = float((cancel_result or {}).get('filled', 0) or 0)
                    terminal = True
                else:
                    terminal = self._refresh(account, leg)
            self.pending.put(key, state)
            if not terminal:
                return self._pending_result(state, 'Exit still working or cancellation unconfirmed')
            # A fresh authoritative snapshot follows every terminal status.
            if leg['status'] == 'FILLED':
                self.executor.esperar_posicion_plana(account, ticker, timeout_s=self.executor.flat_timeout_s)

    def _complete(self, key, state):
        verified = self._verified_fill_legs(state)
        if verified is None:
            return self._pending_result(
                state, 'Position flat, but complete broker fill evidence is unavailable'
            )
        avg, last_id = verified
        q = state['quote']
        reference = state.get('reference_price')
        benchmark = q.get('bid') if state['side'] == 'BUY' else q.get('ask')
        sign = -1 if state['side'] == 'BUY' else 1  # positive = adverse execution
        event = {**state, 'event': 'EXIT_COMPLETED', 'fill_price': avg,
                 'fill_price_verified': True,
                 'requested_price': benchmark, 'limit_price': state['limit'],
                 'signal_price': reference, 'spread': q['ask'] - q['bid'] if q else None,
                 'slippage_usd': sign * (avg - reference) * state['quantity'] if reference else None,
                 'quote_slippage_usd': sign * (avg - benchmark) * state['quantity'] if benchmark else None}
        self.history.append(event)
        self.executor._audit('EXIT_FILLED', ticker=state['ticker'], lado=state['side'],
            order_id=last_id, cantidad=state['quantity'], precio=avg, estado='FILLED',
            motivo=state['reason'], detalle={'execution_id': state['execution_id'],
                'order_ids': [leg['order_id'] for leg in state['legs']], 'fill_price': avg,
                'fill_price_verified': True,
                'signal_price': reference, 'requested_price': benchmark, 'limit_price': state['limit'],
                'spread': event['spread'], 'slippage_usd': event['slippage_usd']})
        self.pending.put(key, None)
        return {'status': 'OK', 'order_id': last_id, 'cantidad': state['quantity'],
                'avg_price': avg, 'broker_status': 'FILLED', 'posicion_plana': True,
                'broker_side': state['side'], 'fill_price_verified': True,
                'execution_details': event}
