"""Audited, model-pinned per-ticker manual AI decision thresholds.

An override changes only the probability veto for future decisions. It does not
change a trained model, a signal score, REAL/PAPER routing, risk controls, open
positions, or brokerage orders. Overrides become inactive when the model changes.
"""
import math
import re

from core.runtime_store import EventHistory, JsonState
from core.project_paths import resolve_runtime_path


_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
MIN_THRESHOLD = 0.30
MAX_THRESHOLD = 0.95


def validate_threshold(value):
    """Accept numeric fractions in the explicitly supported 30%-95% range."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("threshold must be a numeric fraction (e.g. 0.60)")
    number = float(value)
    if not math.isfinite(number) or not MIN_THRESHOLD <= number <= MAX_THRESHOLD:
        raise ValueError("threshold must be between 0.30 and 0.95")
    return round(number, 4)


def normalize_ticker(ticker):
    symbol = str(ticker or "").strip().upper()
    if not _TICKER_RE.fullmatch(symbol):
        raise ValueError("Invalid ticker symbol")
    return symbol


class AIThresholdOverrides:
    def __init__(self, config=None):
        config = config or {}
        self.state = JsonState(resolve_runtime_path(config.get("state_path", "data/ai_threshold_overrides.json")))
        self.history = EventHistory(resolve_runtime_path(config.get(
            "audit_path", "data/ai_threshold_overrides_audit.jsonl"
        )))

    def active(self, ticker, model_version):
        """Read persisted override on each decision, without engine restart."""
        ticker = normalize_ticker(ticker)
        if not model_version:
            return None
        row = self.state.get(ticker)
        if not row or row.get("model_version") != model_version:
            return None
        # Corrupt state must not silently weaken a live decision.
        validate_threshold(row.get("threshold"))
        return row

    def effective(self, ticker, model_version, model_threshold):
        row = self.active(ticker, model_version)
        return float(row["threshold"]) if row else float(model_threshold)

    def set(self, ticker, model_version, threshold, actor, reason=""):
        ticker = normalize_ticker(ticker)
        if not model_version or not str(actor or "").strip():
            raise ValueError("Loaded model version and logged-in operator required")
        threshold = validate_threshold(threshold)
        row = {
            "ticker": ticker,
            "model_version": str(model_version),
            "threshold": threshold,
            "actor": str(actor),
            "reason": str(reason or "").strip()[:500] or "Ajuste desde el dashboard",
        }
        # An audit failure prevents an unaudited policy change.
        self.history.append({"event": "AI_THRESHOLD_CHANGE_REQUESTED", **row})
        self.state.put(ticker, row)
        return row

    def reset(self, ticker, actor):
        ticker = normalize_ticker(ticker)
        if not str(actor or "").strip():
            raise ValueError("Logged-in operator required")
        self.history.append({
            "event": "AI_THRESHOLD_RESET_REQUESTED", "ticker": ticker, "actor": str(actor)
        })
        self.state.put(ticker, None)
