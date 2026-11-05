"""Read-only proof for a manual close of a GALA-managed REAL position.

The broker's exact equity execution legs supply price and quantity. An external
order is accepted only when it is the sole external fill after the audited
GALA entry and Schwab has already confirmed the symbol flat.
"""
from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.audit_schwab_execution_prices import fill_evidence

NY = ZoneInfo("America/New_York")


def _fill_time(order):
    stamps = []
    for activity in order.get("orderActivityCollection") or []:
        if not isinstance(activity, dict):
            raise ValueError("Invalid execution activity")
        for leg in activity.get("executionLegs") or []:
            if not isinstance(leg, dict):
                raise ValueError("Invalid execution leg")
            stamp = datetime.fromisoformat(str(leg.get("time") or "").replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("Execution time has no timezone")
            stamps.append(stamp)
    if not stamps:
        raise ValueError("Execution time missing")
    return max(stamps)


def _nodes(orders):
    pending = list(orders)
    while pending:
        order = pending.pop()
        if not isinstance(order, dict):
            raise ValueError("Invalid broker order")
        yield order
        children = order.get("childOrderStrategies") or []
        if not isinstance(children, list):
            raise ValueError("Invalid child orders")
        pending.extend(children)


def _equity_leg(order, ticker):
    legs = order.get("orderLegCollection") or []
    matching = [leg for leg in legs if isinstance(leg, dict)
                and (leg.get("instrument") or {}).get("symbol") == ticker]
    if not matching:
        return None
    if len(legs) != 1 or len(matching) != 1 or (
            (matching[0].get("instrument") or {}).get("assetType") != "EQUITY"):
        raise ValueError("Ambiguous equity order legs")
    return matching[0]


def _unpriced_execution(order):
    """A claimed fill with no execution legs makes the order window incomplete."""
    try:
        reported = float(order.get("filledQuantity", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return True
    return (reported > 0 or str(order.get("status") or "").upper() == "FILLED")


def _zero_fill_cancellation(order):
    """Schwab cancellation legs are not trades despite activityType EXECUTION.

    Ignore only a terminal, explicitly zero-filled order whose every activity
    is a zero-price cancellation. A claimed or malformed FILL is never hidden.
    """
    try:
        if str(order.get('status') or '').upper() not in {'CANCELED','CANCELLED','REPLACED','EXPIRED'}:
            return False
        reported=float(order['filledQuantity']);ordered=float(order['quantity'])
        if not math.isfinite(reported) or reported != 0 or not math.isfinite(ordered) or ordered <= 0:
            return False
        activities=order.get('orderActivityCollection')
        if not isinstance(activities,list) or not activities:
            return False
        for activity in activities:
            if (not isinstance(activity,dict) or activity.get('activityType') != 'EXECUTION'
                    or activity.get('executionType') not in {'CANCELED','CANCELLED'}):
                return False
            legs=activity.get('executionLegs')
            if not isinstance(legs,list) or not legs:
                return False
            for leg in legs:
                qty,price=float(leg['quantity']),float(leg['price'])
                if not math.isfinite(qty) or not 0 < qty <= ordered or not math.isfinite(price) or price != 0:
                    return False
        return True
    except (ValueError,TypeError,KeyError,OverflowError):
        return False


def verified_manual_close(orders, *, ticker, side, quantity, total_quantity,
                          entry_order_id, entry_price, managed_order_ids,
                          session_ny):
    """Return exact manual exit evidence, or an empty dict on any uncertainty.

    ``managed_order_ids`` includes the tracked entry, stop, and partial exit.
    This proof never infers an exit from a limit price or account flatness alone.
    A broker tag cannot establish who placed the order and is not fill proof.
    """
    try:
        if not isinstance(orders, list) or len(orders) >= 500:
            return {}
        ticker = str(ticker or "").upper()
        instruction = {"BUY": "SELL", "SELL": "BUY_TO_COVER"}.get(side)
        if not ticker or not instruction or not entry_order_id:
            return {}
        quantity, total_quantity, entry_price = (
            float(quantity), float(total_quantity), float(entry_price))
        if (not all(math.isfinite(v) and v > 0 for v in
                    (quantity, total_quantity, entry_price))
                or quantity > total_quantity):
            return {}
        known = {str(value) for value in managed_order_ids if value}
        known.add(str(entry_order_id))
        by_id = {}
        relevant = []
        for order in _nodes(orders):
            oid = str(order.get("orderId") or "")
            if not oid or oid in by_id:
                return {}
            by_id[oid] = order
            leg = _equity_leg(order, ticker)
            if leg is not None:
                relevant.append((oid, order, leg))
        entry = by_id.get(str(entry_order_id))
        if entry is None or _equity_leg(entry, ticker) is None:
            return {}
        entry_action = "BUY" if side == "BUY" else "SELL_SHORT"
        entry_fill = fill_evidence(entry, str(entry_order_id), ticker, entry_action)
        if (entry_fill.get("verified") is not True
                or entry_fill.get("status") != "FILLED"
                or abs(float(entry_fill["quantity"]) - total_quantity) > 1e-6
                or abs(float(entry_fill["average_price"]) - entry_price) > 1e-5):
            return {}
        opened_at = _fill_time(entry)
        if opened_at.astimezone(NY).date().isoformat() != session_ny:
            return {}
        external = []
        for oid, order, leg in relevant:
            if oid == str(entry_order_id):
                continue
            if _zero_fill_cancellation(order):
                continue
            # A filled, partly filled, or malformed same-symbol order after the
            # entry makes attribution uncertain unless it is already tracked.
            if not order.get("orderActivityCollection"):
                if _unpriced_execution(order):
                    return {}
                continue
            filled_at = _fill_time(order)
            if filled_at <= opened_at:
                continue
            if filled_at.astimezone(NY).date().isoformat() != session_ny:
                return {}
            action = str(leg.get("instruction") or "")
            evidence = fill_evidence(order, oid, ticker, action)
            if evidence.get("verified") is not True:
                return {}
            if oid in known:
                # Previously executed partial exits must account exactly for
                # the difference between original and currently tracked size.
                if action != instruction:
                    return {}
                continue
            external.append((oid, order, action, evidence))
        if len(external) != 1:
            return {}
        oid, order, action, fill = external[0]
        if (action != instruction or fill.get("status") != "FILLED"
                or abs(float(fill["quantity"]) - quantity) > 1e-6):
            return {}
        known_closed = 0.0
        for known_id, known_order, known_leg in relevant:
            if known_id not in known or known_id == str(entry_order_id) or not known_order.get('orderActivityCollection'):
                continue
            if _zero_fill_cancellation(known_order):
                continue
            if _fill_time(known_order) <= opened_at:
                continue
            evidence = fill_evidence(known_order, known_id, ticker, instruction)
            if evidence.get('verified') is not True:
                return {}
            known_closed += float(evidence['quantity'])
        if abs(known_closed + quantity - total_quantity) > 1e-6:
            return {}
        return {"order_id": oid, "avg_price": float(fill["average_price"]),
                "quantity": quantity, "order_type": fill.get("order_type"),
                "tag": str(order.get("tag") or ""), "filled_at": _fill_time(order).isoformat()}
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return {}


def verified_gala_orphan_entry(orders, *, ticker, side, quantity, broker_price,
                               submitted_entry_ids, submitted_order_ids,
                               session_ny):
    """Permit orphan protection only for an audited GALA entry and exits.

    A manual or unknown latest fill leaves the ticker quarantined for review.
    Previously audited partial exits may reduce the entry to broker quantity.
    """
    try:
        if not isinstance(orders, list) or len(orders) >= 500:
            return {}
        ticker = str(ticker or "").upper()
        action = {"BUY": "BUY", "SELL": "SELL_SHORT"}.get(side)
        qty, price = float(quantity), float(broker_price)
        if not ticker or not action or not (math.isfinite(qty) and qty > 0):
            return {}
        entry_ids = {str(value) for value in submitted_entry_ids if value}
        submitted_ids = {str(value) for value in submitted_order_ids if value}
        seen, fills = set(), []
        for order in _nodes(orders):
            oid = str(order.get("orderId") or "")
            if not oid or oid in seen:
                return {}
            seen.add(oid)
            leg = _equity_leg(order, ticker)
            if leg is None:
                continue
            if _zero_fill_cancellation(order):
                continue
            if not order.get("orderActivityCollection"):
                if _unpriced_execution(order):
                    return {}
                continue
            filled_at = _fill_time(order)
            if filled_at.astimezone(NY).date().isoformat() != session_ny:
                continue
            if oid not in submitted_ids:
                return {}
            instruction = str(leg.get("instruction") or "")
            evidence = fill_evidence(order, oid, ticker, instruction)
            if evidence.get("verified") is not True:
                return {}
            fills.append((filled_at, oid, order, leg, evidence))
        if not fills:
            return {}
        fills.sort(key=lambda item: item[0])
        entries = [item for item in fills
                   if item[1] in entry_ids and item[3].get("instruction") == action
                   and item[4].get("status") == "FILLED"]
        if not entries:
            return {}
        opened_at, oid, order, leg, evidence = entries[-1]
        if (float(evidence["quantity"]) < qty
                or not math.isfinite(price) or price <= 0
                or abs(float(evidence["average_price"]) - price) > 0.02):
            return {}
        remaining = float(evidence["quantity"])
        close_action = "SELL" if side == "BUY" else "BUY_TO_COVER"
        for filled_at, later_id, _, later_leg, later_fill in fills:
            if filled_at <= opened_at:
                continue
            if later_id == oid or later_leg.get("instruction") != close_action:
                return {}
            remaining -= float(later_fill["quantity"])
            if remaining <= 0:
                return {}
        if abs(remaining - qty) > 1e-6:
            return {}
        return {"order_id": oid, "quantity": qty,
                "avg_price": float(evidence["average_price"])}
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return {}
