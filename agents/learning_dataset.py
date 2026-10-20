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
    """Use the original deduplication policy without allocating wide row dicts.

    A 100 MB / 60+ column pandas CSV can become millions of Python objects
    when expanded via df.to_dict("records"), exhausting an 8 GB VPS. Only
    identity and eligibility columns are inspected row by row. The last row for
    each identity wins before validation, exactly as in eligible_closed_rows.
    """
    needed = (
        "modo", "ticker", "position_id", "execution_id", "timestamp",
        "tipo", "precio_entrada", "precio_salida", "execution_valid",
    )
    columns = [column for column in needed if column in df.columns]
    latest = {}
    unlinked_valid = []
    for index, values in enumerate(df[columns].itertuples(index=False, name=None)):
        row = dict(zip(columns, values))
        identity = observation_identity(row)
        eligible = closed_row_is_eligible(
            row, fieldnames=df.columns, allow_legacy=allow_legacy
        )
        if identity is not None:
            latest[identity] = (index, eligible)
        elif eligible:
            unlinked_valid.append(index)

    indices = sorted(
        unlinked_valid + [
            index for index, eligible in latest.values() if eligible
        ]
    )
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
    """Count verified closes in one pass without retaining every wide CSV row.

    Latest duplicate identity wins, INCLUDING when the latest row is invalid.
    The small latest-state map retains only ticker/mode/eligibility, not entire
    feedback records. A DictReader with fieldnames therefore stays streaming.
    If callers omit fieldnames, preserve the historic union-of-columns contract.
    """
    if fieldnames is None:
        rows = list(rows)
        fields = {field for row in rows for field in row}
    else:
        fields = set(fieldnames)

    summary = {}
    latest = {}

    def account(ticker, mode, eligible):
        record = summary[ticker]
        if not eligible:
            record["quarantined_observations"] += 1
            return
        record["eligible_observations"] += 1
        record[
            f"eligible_closed_{mode if mode in ('real', 'paper') else 'legacy'}"
        ] += 1

    for row in rows:
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
        mode = _text(row.get("modo")).lower()
        eligible = closed_row_is_eligible(
            row, fieldnames=fields, allow_legacy=allow_legacy
        )
        identity = observation_identity(row)
        if identity is None:
            # Sparse legacy records have no safe identity; each remains distinct.
            account(ticker, mode, eligible)
        else:
            if identity in latest:
                record["duplicate_closed_rows"] += 1
            latest[identity] = (ticker, mode, eligible)

    for ticker, mode, eligible in latest.values():
        account(ticker, mode, eligible)
    return summary
