"""Shared closed-trade eligibility and identity rules for learning counters.

Rows in an integrity-aware file must explicitly pass execution validation.
Legacy external datasets can opt into compatibility when integrity columns are
absent; an empty value in a present column never grants that compatibility.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping, Optional


LEARNING_POLICY_VERSION = 2


def _text(value) -> str:
    if value is None:
        return ""
    try:
        if math.isnan(value):
            return ""
    except (TypeError, ValueError):
        pass
    result = str(value).strip()
    return "" if result.lower() in ("nan", "nat", "<na>") else result


def _number_key(value) -> str:
    text = _text(value)
    try:
        number = Decimal(text)
        return str(number.normalize()) if number.is_finite() else ""
    except InvalidOperation:
        return text


def observation_identity(row: Mapping) -> Optional[tuple]:
    """Identify one closed position independently of callback/retry timestamps.

    Distinct PAPER and REAL positions remain distinct even when their broker or
    simulated IDs coincide. Legacy rows use their historical price/time tuple;
    sparse fixtures without any trustworthy identity remain separate rows.
    """
    mode = _text(row.get("modo")).upper()
    ticker = _text(row.get("ticker")).upper()
    for field in ("position_id", "execution_id"):
        identifier = _text(row.get(field))
        if identifier and ticker:
            return (field, mode, ticker, identifier)
    timestamp = _text(row.get("timestamp"))
    side = _text(row.get("tipo")).upper()
    entry = _number_key(row.get("precio_entrada"))
    exit_price = _number_key(row.get("precio_salida"))
    if ticker and timestamp and side and entry and exit_price:
        return ("legacy", mode, ticker, timestamp, side, entry, exit_price)
    return None


def closed_row_is_eligible(
    row: Mapping, *, fieldnames: Iterable[str], allow_legacy: bool = True
) -> bool:
    fields = set(fieldnames)
    if "execution_valid" in fields:
        try:
            value = float(row.get("execution_valid"))
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value) or value != 1.0:
            return False
    elif not allow_legacy:
        return False
    if "modo" in fields:
        if _text(row.get("modo")).upper() not in ("REAL", "PAPER"):
            return False
    elif not allow_legacy:
        return False
    return True


def _unique_indices(rows: list) -> list[int]:
    # Keep the latest ledger result. A later quarantine must supersede an older
    # eligible duplicate instead of silently reviving its training label.
    latest = {}
    identities = []
    for index, row in enumerate(rows):
        identity = observation_identity(row)
        identities.append(identity)
        if identity is not None:
            latest[identity] = index
    return [
        index for index, identity in enumerate(identities)
        if identity is None or latest[identity] == index
    ]


def eligible_closed_rows(
    rows: Iterable[Mapping], *, fieldnames: Optional[Iterable[str]] = None,
    allow_legacy: bool = True,
) -> list[dict]:
    rows = list(rows)
    fields = set(fieldnames) if fieldnames is not None else {
        field for row in rows for field in row
    }
    return [dict(rows[index]) for index in _unique_indices(rows) if closed_row_is_eligible(
        rows[index], fieldnames=fields, allow_legacy=allow_legacy
    )]


def eligible_closed_trades(df, *, allow_legacy: bool = True):
    """Return a DataFrame using the same filtering/deduplication as x/50."""
    rows = df.to_dict("records")
    indices = [index for index in _unique_indices(rows) if closed_row_is_eligible(
        rows[index], fieldnames=df.columns, allow_legacy=allow_legacy
    )]
    result = df.iloc[indices].copy()
    if "ticker" in result.columns:
        result["ticker"] = result["ticker"].astype(str).str.strip().str.upper()
    if "modo" in result.columns:
        result["modo"] = result["modo"].astype(str).str.strip().str.upper()
    return result


def summarize_closed_trades(
    rows: Iterable[Mapping], *, fieldnames: Optional[Iterable[str]] = None,
    allow_legacy: bool = True,
) -> dict[str, dict]:
    """Count unique eligible closes, modes, quarantines and duplicate ledger rows."""
    rows = list(rows)
    fields = set(fieldnames) if fieldnames is not None else {
        field for row in rows for field in row
    }
    unique = set(_unique_indices(rows))
    summary = {}
    for index, row in enumerate(rows):
        ticker = _text(row.get("ticker")).upper()
        if not ticker:
            continue
        record = summary.setdefault(ticker, {
            "eligible_observations": 0, "eligible_closed_real": 0,
            "eligible_closed_paper": 0, "eligible_closed_legacy": 0,
            "quarantined_observations": 0, "duplicate_closed_rows": 0,
            "lifetime_observations": 0,
        })
        record["lifetime_observations"] += 1
        if index not in unique:
            record["duplicate_closed_rows"] += 1
            continue
        if not closed_row_is_eligible(row, fieldnames=fields, allow_legacy=allow_legacy):
            record["quarantined_observations"] += 1
            continue
        record["eligible_observations"] += 1
        mode = _text(row.get("modo")).lower()
        record[f"eligible_closed_{mode if mode in ('real', 'paper') else 'legacy'}"] += 1
    return summary
