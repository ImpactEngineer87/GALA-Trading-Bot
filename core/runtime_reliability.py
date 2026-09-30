"""Small pure helpers for runtime/UI consistency.

These functions intentionally have no Flask, broker, or model dependencies so
critical timestamp/AI-display rules can be regression-tested in isolation.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

ONE_MINUTE_MS = 60_000


def marker_within_1m_candle_span(
    marker_ms: int,
    first_candle_ms: int,
    last_candle_ms: int,
) -> bool:
    """Return True when *marker_ms* belongs to the loaded 1-minute candle span.

    Candle timestamps identify the *start* of their minute.  Therefore the final
    candle at 13:34:00 covers events through 13:34:59.999.  Comparing a signal
    timestamp directly with ``last_candle_ms`` drops valid events such as
    13:34:04 from the chart.
    """

    try:
        marker = int(marker_ms)
        first = int(first_candle_ms)
        last = int(last_candle_ms)
    except (TypeError, ValueError):
        return False
    if last < first:
        return False
    return first <= marker < (last + ONE_MINUTE_MS)


def infer_closed_bar_epoch_ms(signal_epoch_ms: Any) -> Optional[int]:
    """Infer the source 1-minute bar for legacy signals lacking bar metadata.

    The live 1m loop evaluates the just-closed bar shortly after the next minute
    boundary.  A signal emitted at 13:34:04 therefore belongs to the 13:33:00
    source candle.  v3.3.1 persists the exact bar for new signals; this helper
    repairs display-only alignment for older ledgers.
    """

    try:
        epoch = int(signal_epoch_ms)
    except (TypeError, ValueError):
        return None
    minute_start = (epoch // ONE_MINUTE_MS) * ONE_MINUTE_MS
    return minute_start - ONE_MINUTE_MS


def effective_ai_threshold(
    snapshot: Optional[Mapping[str, Any]],
    fallback: Any = None,
) -> Optional[float]:
    """Return the threshold that actually governed the displayed AI decision.

    PAPER may be audited by a SHADOW model while REAL uses PRODUCTION.  Reading
    only the production trainer threshold can therefore show ``0%`` beside a
    SHADOW decision that really used, for example, ``41%``.  The decision
    snapshot is authoritative; the trainer value is only a compatibility
    fallback.
    """

    snap = snapshot or {}
    for key in ("threshold", "decision_threshold"):
        value = snap.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    try:
        return None if fallback in (None, "") else float(fallback)
    except (TypeError, ValueError):
        return None
