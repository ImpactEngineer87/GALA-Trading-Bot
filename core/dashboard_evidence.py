"""Read-only provenance checks for dashboard results, never training or risk state.

Local fill-derived gross P/L is NOT a Schwab-reported account P/L. A mode badge
alone, a nonempty order ID, or a local POSITION_CLOSED event is not fill proof.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo


def number(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def mode_of(row):
    mode = str(row.get("modo") or row.get("mode") or "").strip().upper()
    return mode if mode in {"PAPER", "REAL"} else "UNKNOWN"


def session_date(row):
    raw = str(row.get("fecha") or row.get("date") or "").strip()
    if raw:
        return raw[:10]
    raw = str(row.get("timestamp_utc") or row.get("timestamp_iso") or "").strip()
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            return ""  # do not guess the timezone of old evidence
        return value.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except ValueError:
        return ""


def _identity(row):
    return (mode_of(row),) + tuple(str(row.get(k) or "").strip().upper() if k == "ticker"
                 else str(row.get(k) or "").strip()
                 for k in ("ticker", "position_id", "execution_id"))


def _detail(row):
    value = row.get("detalle") or {}
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _same(a, b, tolerance=0.00001):
    a, b = number(a), number(b)
    return a is not None and b is not None and abs(a - b) <= tolerance


def annotate_history(history_rows, audit_rows=()):
    """Return copies with display eligibility; never repair/modify source ledgers.

    REAL requires matching entry AND close audit confirmations, complete linked
    IDs, positive fill quantities/prices, and arithmetic matching recorded fills.
    Partial exits additionally require the linked partial fill event. Old or
    contradictory records stay visible as UNVERIFIED, excluded from performance.
    """
    rows = [dict(row) for row in history_rows]
    wanted = {_identity(r) for r in rows if mode_of(r) == "REAL"}
    index = defaultdict(dict)
    paper_quarantine = {}
    for event in audit_rows:
        key = _identity(event)
        kind = str(event.get("evento") or "").upper()
        if mode_of(event) == 'PAPER' and kind == 'PAPER_OBSERVATION_QUARANTINED' and all(key):
            reason = str(event.get('motivo') or 'Simulación ambigua')
            detail = _detail(event)
            known_maintenance = (reason.startswith('CONTROLLED_MAINTENANCE_CLOSE_')
                and detail.get('training_eligible') is False
                and detail.get('performance_eligible') is False)
            paper_quarantine[key] = (reason, known_maintenance)
            continue
        if (key not in wanted or mode_of(event) != "REAL"
                or kind not in {"ENTRY_FILLED", "POSITION_CLOSED", "TP1_FILLED_70_PERCENT"}):
            continue
        # Keep the latest event for the same order; never revive older evidence.
        index[key][(kind, str(event.get("order_id") or ""))] = event

    latest_close = {}
    for row in rows:
        if str(row.get("evento") or "").upper() == "POSITION_CLOSE":
            key = _identity(row)
            if all(key):
                prior = latest_close.get(key)
                if prior is None or str(row.get("timestamp_utc") or "") >= str(prior.get("timestamp_utc") or ""):
                    latest_close[key] = row

    for row in rows:
        mode = mode_of(row)
        reason = ""
        risk_reason = ""
        manual_exit = False
        pnl = number(row.get("pnl_usd"))
        is_close = str(row.get("evento") or "").upper() == "POSITION_CLOSE"
        if not is_close:
            reason = "No es un cierre"
        elif mode == "UNKNOWN":
            reason = "Modo histórico desconocido"
        elif pnl is None:
            reason = "P/L ausente o inválido"
        elif all(_identity(row)) and latest_close.get(_identity(row)) is not row:
            reason = "Cierre duplicado; se conserva sólo el último registro"
        elif mode == 'PAPER' and _identity(row) in paper_quarantine:
            reason = paper_quarantine[_identity(row)][0]
        elif mode == "REAL":
            events = index.get(_identity(row), {})
            risk_reason = _real_close_reason(row, events)
            close_id = str(row.get("order_id") or "")
            close = events.get(("POSITION_CLOSED", close_id)) if close_id else None
            detail = _detail(close or {})
            manual_exit = detail.get("external_manual_exit") is True
            reason = risk_reason or (
                "Cierre manual verificado; excluido del rendimiento GALA"
                if manual_exit else "")
            if manual_exit and detail.get("manual_risk_verified") is not True:
                risk_reason = risk_reason or "Riesgo de cierre manual no verificado"
        if mode == "REAL" and reason and reason != (
                "Cierre manual verificado; excluido del rendimiento GALA"):
            risk_reason = risk_reason or reason
        row["accounting_verified"] = not bool(risk_reason or (
            reason if mode != "REAL" else ""))
        row["performance_verified"] = not bool(reason)
        row["performance_exclusion_reason"] = reason
        exclusion = paper_quarantine.get(_identity(row))
        row['performance_intentionally_excluded'] = bool(
            mode == 'PAPER' and is_close and pnl is not None and exclusion
            and exclusion[1] and reason == exclusion[0])
        row["pnl_source"] = (
            "MANUAL_BROKER_FILL_GROSS" if manual_exit and row["accounting_verified"] else
            "UNVERIFIED" if reason else
            "PAPER_SIMULATION" if mode == "PAPER" else "LOCAL_RECORDED_FILLS_GROSS"
        )
        row["display_pnl_usd"] = pnl if not reason else None
    return rows


def _real_close_reason(row, events):
    if not all(_identity(row)):
        return "REAL sin identidad de posición/ejecución completa"
    entry_id = str(row.get("parent_order_id") or "")
    close_id = str(row.get("order_id") or "")
    entry = events.get(("ENTRY_FILLED", entry_id)) if entry_id else None
    close = events.get(("POSITION_CLOSED", close_id)) if close_id else None
    if entry is None or close is None:
        return "Faltan confirmaciones de entrada/cierre enlazadas"
    ed, cd = _detail(entry), _detail(close)
    if (str(entry.get("estado") or "").upper() != "FILLED"
            or ed.get("broker_price_verified") is not True
            or cd.get("broker_confirmed") is not True
            or cd.get("fill_price_verified") is not True):
        return "Precio/ejecución REAL no confirmados por broker"
    qty, price = number(row.get("cantidad")), number(row.get("entrada"))
    if qty is None or qty <= 0 or price is None or price <= 0:
        return "Cantidad/precio de entrada incompletos"
    if not (_same(entry.get("cantidad"), qty) and _same(entry.get("precio"), price)
            and _same(close.get("cantidad"), qty)
            and _same(close.get("precio"), row.get("salida"))
            and _same(close.get("pnl_usd"), row.get("pnl_usd"), 0.02)):
        return "Historial y confirmaciones de fills no coinciden"
    exit_price = number(cd.get("salida_real"))
    if exit_price is None or exit_price <= 0:
        return "Falta precio de fill final"
    partials = [e for (kind, _), e in events.items() if kind == "TP1_FILLED_70_PERCENT"]
    partial_qty, partial_value = 0.0, 0.0
    if cd.get("tp1_ejecutado"):
        if len(partials) != 1:
            return "Fill parcial ausente o ambiguo"
        part = partials[0]
        pq, pp = number(part.get("cantidad")), number(part.get("precio"))
        if (str(part.get("estado") or "").upper() != "FILLED"
                or _detail(part).get("fill_price_verified") is not True
                or not part.get("order_id") or pq is None or not 0 < pq < qty
                or pp is None or pp <= 0):
            return "Fill parcial no verificable"
        partial_qty, partial_value = pq, pq * pp
    elif partials:
        return "Evidencia parcial contradictoria"
    side = str(row.get("position_side") or "").upper()
    if side not in {"LONG", "SHORT"} or str(entry.get("position_side") or "").upper() != side:
        return "Dirección de posición no verificable"
    exit_value = partial_value + (qty - partial_qty) * exit_price
    gross = (exit_value - qty * price) * (1 if side == "LONG" else -1)
    if not _same(row.get("salida"), exit_value / qty) or not _same(row.get("pnl_usd"), gross, 0.02):
        return "P/L no coincide con cantidades/precios de fills"
    return ""
