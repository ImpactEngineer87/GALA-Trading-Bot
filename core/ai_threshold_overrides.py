"""Per-ticker AI decision thresholds set by the operator (v3.3.19).

A trained model stores its own decision threshold. The operator may replace it
for a ticker from the dashboard. The replacement applies to that ticker's
PAPER and REAL AI decisions and survives restarts. It only moves the
probability cut-off: hard vetoes, market regime, strategy, capital, daily loss
and broker checks still apply.
"""
import json
import math
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

MIN_THRESHOLD = 0.30
MAX_THRESHOLD = 0.95
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "ai_threshold_overrides.json"


def validate_threshold(value) -> float:
    """Return a threshold in [MIN_THRESHOLD, MAX_THRESHOLD] or raise ValueError."""
    if isinstance(value, bool):
        raise ValueError("El umbral debe ser un número")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("El umbral debe ser un número") from None
    if not math.isfinite(number) or not MIN_THRESHOLD <= number <= MAX_THRESHOLD:
        raise ValueError(
            f"El umbral debe estar entre {MIN_THRESHOLD:.0%} y {MAX_THRESHOLD:.0%}")
    return round(number, 4)


class AIThresholdOverrides:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.load_error = ""
        self._rows = self._load()

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # Unreadable file: fall back to the model thresholds (the stricter,
            # trained values) and report it instead of guessing.
            self.load_error = f"No se pudo leer {self.path.name}: {type(exc).__name__}"
            return {}
        rows = {}
        for ticker, row in (raw.items() if isinstance(raw, dict) else []):
            try:
                rows[str(ticker).upper()] = dict(row, threshold=validate_threshold(row["threshold"]))
            except (KeyError, TypeError, ValueError):
                continue
        return rows

    def _save(self, rows: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                         prefix=".ai-threshold-", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            json.dump(rows, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(temporary, self.path)
        except OSError:
            os.unlink(temporary)
            raise

    def get(self, ticker) -> Optional[float]:
        with self._lock:
            row = self._rows.get(str(ticker or "").upper())
            return None if row is None else float(row["threshold"])

    def row(self, ticker) -> Optional[dict]:
        with self._lock:
            row = self._rows.get(str(ticker or "").upper())
            return None if row is None else dict(row)

    def set(self, ticker, threshold, *, actor="", reason="", model_threshold=None) -> dict:
        ticker = str(ticker or "").strip().upper()
        reason = str(reason or "").strip()
        if not ticker:
            raise ValueError("Ticker requerido")
        if not reason:
            raise ValueError("Indique el motivo del cambio de umbral")
        row = {
            "threshold": validate_threshold(threshold),
            "model_threshold": None if model_threshold is None else round(float(model_threshold), 4),
            "reason": reason[:300],
            "updated_by": str(actor or "")[:80],
            "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with self._lock:
            rows = {**self._rows, ticker: row}
            self._save(rows)          # persist first; memory changes only on success
            self._rows = rows
        return dict(row)

    def clear(self, ticker) -> Optional[dict]:
        ticker = str(ticker or "").strip().upper()
        with self._lock:
            if ticker not in self._rows:
                return None
            rows = dict(self._rows)
            removed = rows.pop(ticker)
            self._save(rows)
            self._rows = rows
            return dict(removed)


_SHARED = None
_SHARED_LOCK = threading.Lock()


def shared_threshold_overrides() -> AIThresholdOverrides:
    """One store per process, so a dashboard change reaches the running engine."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = AIThresholdOverrides()
        return _SHARED
