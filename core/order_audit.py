"""
Persistent, append-only audit ledger for the complete order lifecycle.

The closed-trade history is intentionally kept separate: it is a PnL summary.
This ledger records every material broker transition so an operator can explain
why a trade succeeded, failed, bounced, or was postponed.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


LEGACY_AUDIT_FIELDS = [
    "event_id",
    "timestamp_utc",
    "fecha",
    "hora_ny",
    "modo",
    "ticker",
    "lado",
    "evento",
    "estado",
    "order_id",
    "parent_order_id",
    "cantidad",
    "precio",
    "pnl_usd",
    "latency_ms",
    "motivo",
    "detalle",
]

AUDIT_FIELDS = [
    "event_id",
    "timestamp_utc",
    "fecha",
    "hora_ny",
    "modo",
    "ticker",
    "correlation_id",
    "reversal_id",
    "signal_id",
    "execution_id",
    "position_id",
    "state_before",
    "state_after",
    "position_side",
    "signal_side",
    "order_action",
    "lado",
    "evento",
    "estado",
    "order_id",
    "parent_order_id",
    "cantidad",
    "precio",
    "pnl_usd",
    "latency_ms",
    "motivo",
    "detalle",
]


def _now_new_york() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now()


def _safe_text(value: Any, max_len: int = 2000) -> str:
    text = str(value if value is not None else "")
    text = text.replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len]


def _safe_number(value: Any) -> Any:
    if value in (None, ""):
        return ""
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return ""


class OrderAuditLedger:
    """Thread-safe CSV ledger that survives process and VPS restarts."""

    def __init__(self, path: str = "data/auditoria_ordenes.csv"):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _archive_incompatible_schema(self) -> Optional[Path]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        try:
            with self.path.open("r", encoding="utf-8-sig", newline="") as fh:
                header = next(csv.reader(fh), [])
        except Exception:
            header = []
        if header == AUDIT_FIELDS:
            return None

        if header == LEGACY_AUDIT_FIELDS:
            with self.path.open("r", encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
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
                writer = csv.DictWriter(fh, fieldnames=AUDIT_FIELDS)
                writer.writeheader()
                for old in rows:
                    row = {key: "" for key in AUDIT_FIELDS}
                    row.update(old)
                    writer.writerow(row)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            os.replace(temporary, self.path)
            return backup

        index = 1
        while True:
            archived = self.path.with_name(f"{self.path.name}.old{index}")
            if not archived.exists():
                self.path.replace(archived)
                return archived
            index += 1

    def record(
        self,
        evento: str,
        *,
        modo: str,
        ticker: str = "",
        lado: str = "",
        estado: str = "",
        order_id: str = "",
        parent_order_id: str = "",
        correlation_id: str = "",
        reversal_id: str = "",
        signal_id: str = "",
        execution_id: str = "",
        position_id: str = "",
        state_before: str = "",
        state_after: str = "",
        position_side: str = "",
        signal_side: str = "",
        order_action: str = "",
        cantidad: Any = "",
        precio: Any = "",
        pnl_usd: Any = "",
        latency_ms: Any = "",
        motivo: str = "",
        detalle: Any = "",
    ) -> Dict[str, str]:
        now_ny = _now_new_york()
        if isinstance(detalle, (dict, list, tuple)):
            try:
                detalle = json.dumps(
                    detalle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
            except Exception:
                detalle = str(detalle)

        row: Dict[str, Any] = {
            "event_id": uuid.uuid4().hex,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "fecha": now_ny.strftime("%Y-%m-%d"),
            "hora_ny": now_ny.strftime("%H:%M:%S.%f")[:-3],
            "modo": _safe_text(modo, 16).upper(),
            "ticker": _safe_text(ticker, 16).upper(),
            "correlation_id": _safe_text(correlation_id, 64),
            "reversal_id": _safe_text(reversal_id, 64),
            "signal_id": _safe_text(signal_id, 64),
            "execution_id": _safe_text(execution_id, 64),
            "position_id": _safe_text(position_id, 64),
            "state_before": _safe_text(state_before, 16).upper(),
            "state_after": _safe_text(state_after, 16).upper(),
            "position_side": _safe_text(position_side, 16).upper(),
            "signal_side": _safe_text(signal_side, 16).upper(),
            "order_action": _safe_text(order_action, 24).upper(),
            "lado": _safe_text(lado, 16).upper(),
            "evento": _safe_text(evento, 64).upper(),
            "estado": _safe_text(estado, 48).upper(),
            "order_id": _safe_text(order_id, 128),
            "parent_order_id": _safe_text(parent_order_id, 128),
            "cantidad": _safe_number(cantidad),
            "precio": _safe_number(precio),
            "pnl_usd": _safe_number(pnl_usd),
            "latency_ms": _safe_number(latency_ms),
            "motivo": _safe_text(motivo, 500),
            "detalle": _safe_text(detalle, 2000),
        }

        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._archive_incompatible_schema()
            new_file = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=AUDIT_FIELDS)
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
        return {key: str(row.get(key, "")) for key in AUDIT_FIELDS}

    def read(self, limit: Optional[int] = 500, newest_first: bool = True) -> List[Dict[str, str]]:
        with self._lock:
            if not self.path.exists():
                return []
            self._archive_incompatible_schema()
            if not self.path.exists():
                return []
            with self.path.open("r", encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
        if newest_first:
            rows.reverse()
        if limit is not None:
            rows = rows[: max(0, int(limit))]
        return rows


def read_order_audit(
    path: str = "data/auditoria_ordenes.csv",
    limit: Optional[int] = 500,
    newest_first: bool = True,
) -> List[Dict[str, str]]:
    """Read helper used by the dashboard without constructing the trading bot."""

    return OrderAuditLedger(path).read(limit=limit, newest_first=newest_first)
