"""Persistent entry-fill quality ledger kept separate from training labels.

The model should learn from the market setup and trade outcome, not from an
unexplained execution artifact.  This append-only CSV records the signal,
submitted limit, and broker fill as separate values so slippage can be audited
or excluded during a later training-data cleaning pass.
"""

from __future__ import annotations

import csv
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


LEGACY_EXECUTION_QUALITY_FIELDS = [
    "execution_id",
    "timestamp_utc",
    "fecha",
    "hora_ny",
    "modo",
    "ticker",
    "lado",
    "order_id",
    "order_type",
    "cantidad",
    "signal_price",
    "submitted_limit_price",
    "fill_price",
    "slippage_vs_signal_per_share",
    "slippage_vs_signal_usd",
    "slippage_vs_limit_per_share",
    "slippage_vs_limit_usd",
]

EXECUTION_QUALITY_FIELDS = [
    "execution_id",
    "correlation_id",
    "reversal_id",
    "signal_id",
    "position_id",
    "timestamp_utc",
    "fecha",
    "hora_ny",
    "modo",
    "ticker",
    "state_before",
    "state_after",
    "position_side",
    "signal_side",
    "order_action",
    "lado",
    "order_id",
    "order_type",
    "cantidad",
    "signal_price",
    "submitted_limit_price",
    "fill_price",
    "slippage_vs_signal_per_share",
    "slippage_vs_signal_usd",
    "slippage_vs_limit_per_share",
    "slippage_vs_limit_usd",
]


def _now_new_york() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now()


def _safe_number(value: Any) -> Any:
    if value in (None, ""):
        return ""
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return ""


class ExecutionQualityLedger:
    """Thread-safe, append-only CSV for entry execution quality."""

    def __init__(self, path: str = "data/slippage_entradas.csv"):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _ensure_schema(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with self.path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            header = list(reader.fieldnames or [])
            rows = list(reader)
        if header == EXECUTION_QUALITY_FIELDS:
            return
        if header != LEGACY_EXECUTION_QUALITY_FIELDS:
            raise ValueError("El CSV de slippage tiene un esquema incompatible")

        index = 1
        while True:
            backup = self.path.with_name(
                f"{self.path.name}.before_v2_9_{index}"
            )
            if not backup.exists():
                break
            index += 1
        shutil.copy2(self.path, backup)
        temporary = self.path.with_suffix(self.path.suffix + ".v2_9.tmp")
        with temporary.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=EXECUTION_QUALITY_FIELDS)
            writer.writeheader()
            for old in rows:
                row = {key: "" for key in EXECUTION_QUALITY_FIELDS}
                row.update(old)
                writer.writerow(row)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(temporary, self.path)

    @staticmethod
    def _adverse_delta(side: str, actual: float, reference: float) -> float:
        """Positive means adverse slippage; negative means price improvement."""

        return actual - reference if str(side).upper() == "BUY" else reference - actual

    def record_entry(
        self,
        *,
        modo: str,
        ticker: str,
        lado: str,
        cantidad: Any,
        signal_price: Any,
        submitted_limit_price: Any,
        fill_price: Any,
        order_id: str = "",
        order_type: str = "LIMIT",
        execution_id: str = "",
        correlation_id: str = "",
        reversal_id: str = "",
        signal_id: str = "",
        position_id: str = "",
        state_before: str = "OUT",
        state_after: str = "",
        position_side: str = "",
        signal_side: str = "",
        order_action: str = "",
    ) -> Dict[str, str]:
        quantity = max(0, int(float(cantidad or 0)))
        signal = float(signal_price or 0.0)
        submitted = float(submitted_limit_price or signal or 0.0)
        fill = float(fill_price or submitted or signal or 0.0)
        if signal <= 0 or submitted <= 0 or fill <= 0 or quantity <= 0:
            raise ValueError("Precios y cantidad de ejecución deben ser positivos")

        slip_signal = self._adverse_delta(lado, fill, signal)
        slip_limit = self._adverse_delta(lado, fill, submitted)
        now_ny = _now_new_york()
        row: Dict[str, Any] = {
            "execution_id": str(execution_id or uuid.uuid4().hex)[:64],
            "correlation_id": str(correlation_id or "")[:64],
            "reversal_id": str(reversal_id or "")[:64],
            "signal_id": str(signal_id or "")[:64],
            "position_id": str(position_id or "")[:64],
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "fecha": now_ny.strftime("%Y-%m-%d"),
            "hora_ny": now_ny.strftime("%H:%M:%S.%f")[:-3],
            "modo": str(modo or "").strip().upper()[:16],
            "ticker": str(ticker or "").strip().upper()[:16],
            "state_before": str(state_before or "").strip().upper()[:16],
            "state_after": str(state_after or "").strip().upper()[:16],
            "position_side": str(position_side or "").strip().upper()[:16],
            "signal_side": str(signal_side or lado or "").strip().upper()[:16],
            "order_action": str(order_action or "").strip().upper()[:24],
            "lado": str(lado or "").strip().upper()[:16],
            "order_id": str(order_id or "").strip()[:128],
            "order_type": str(order_type or "LIMIT").strip().upper()[:24],
            "cantidad": quantity,
            "signal_price": _safe_number(signal),
            "submitted_limit_price": _safe_number(submitted),
            "fill_price": _safe_number(fill),
            "slippage_vs_signal_per_share": _safe_number(slip_signal),
            "slippage_vs_signal_usd": _safe_number(slip_signal * quantity),
            "slippage_vs_limit_per_share": _safe_number(slip_limit),
            "slippage_vs_limit_usd": _safe_number(slip_limit * quantity),
        }

        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_schema()
            new_file = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=EXECUTION_QUALITY_FIELDS)
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
        return {key: str(row.get(key, "")) for key in EXECUTION_QUALITY_FIELDS}

    def read(
        self, limit: Optional[int] = 500, newest_first: bool = True
    ) -> List[Dict[str, str]]:
        with self._lock:
            if not self.path.exists():
                return []
            self._ensure_schema()
            with self.path.open("r", encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
        if newest_first:
            rows.reverse()
        if limit is not None:
            rows = rows[: max(0, int(limit))]
        return rows


def read_execution_quality(
    path: str = "data/slippage_entradas.csv",
    limit: Optional[int] = 500,
    newest_first: bool = True,
) -> List[Dict[str, str]]:
    return ExecutionQualityLedger(path).read(limit=limit, newest_first=newest_first)
