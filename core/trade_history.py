"""Unambiguous, append-only position lifecycle history.

v2.8 stored only a closed-trade summary whose ``lado`` column meant the side of
the position being closed.  v2.9 keeps that legacy column for compatibility but
adds explicit event, state, signal, position and broker-order fields.  Existing
rows are migrated in place as POSITION_CLOSE records and are never discarded.
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

from core.position_state import (
    PositionState,
    entry_action_for_state,
    exit_action_for_state,
)


LEGACY_HISTORY_FIELDS = [
    "fecha", "hora_ny", "ticker", "lado", "entrada", "salida",
    "cantidad", "pnl_usd", "resultado", "motivo", "modo",
]

HISTORY_FIELDS = [
    "history_id", "timestamp_utc", "fecha", "hora_ny", "modo", "ticker",
    "evento", "correlation_id", "reversal_id", "signal_id", "execution_id",
    "position_id", "state_before", "state_after", "position_side",
    "signal_side", "order_action", "order_id", "parent_order_id", "lado",
    "entrada", "salida", "cantidad", "pnl_usd", "resultado", "motivo",
]


def _now_new_york() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now()


def _text(value: Any, limit: int = 500) -> str:
    return str(value if value is not None else "").replace("\r", " ").replace(
        "\n", " "
    ).strip()[:limit]


def _number(value: Any) -> Any:
    if value in (None, ""):
        return ""
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return ""


def _legacy_state(lado: str) -> PositionState:
    return PositionState.LONG if str(lado).upper() == "BUY" else PositionState.SHORT


class TradeHistoryLedger:
    """Thread-safe history with a lossless v2.8 schema migration."""

    def __init__(self, path: str = "data/historial_operaciones.csv") -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _backup_path(self) -> Path:
        index = 1
        while True:
            candidate = self.path.with_name(f"{self.path.name}.before_v2_9_{index}")
            if not candidate.exists():
                return candidate
            index += 1

    def _ensure_schema(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = list(reader.fieldnames or [])
            rows = list(reader)
        if header == HISTORY_FIELDS:
            return
        if header != LEGACY_HISTORY_FIELDS:
            raise ValueError("El historial de operaciones tiene un esquema desconocido")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.path, self._backup_path())
        temp = self.path.with_suffix(self.path.suffix + ".v2_9.tmp")
        with temp.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=HISTORY_FIELDS)
            writer.writeheader()
            for old in rows:
                state = _legacy_state(old.get("lado", ""))
                migrated = {key: "" for key in HISTORY_FIELDS}
                migrated.update(old)
                migrated.update({
                    "history_id": uuid.uuid4().hex,
                    "modo": str(old.get("modo", "") or "").upper(),
                    "ticker": str(old.get("ticker", "") or "").upper(),
                    "evento": "POSITION_CLOSE",
                    "state_before": state.value,
                    "state_after": PositionState.OUT.value,
                    "position_side": state.value,
                    "order_action": exit_action_for_state(state),
                    "motivo": str(old.get("motivo", "") or "")
                    + " [LEGACY_V2_8_UNLINKED]",
                })
                writer.writerow(migrated)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temp, self.path)

    def record(self, evento: str, **values: Any) -> Dict[str, str]:
        event = str(evento or "").strip().upper()
        if event not in {"POSITION_OPEN", "POSITION_CLOSE"}:
            raise ValueError(f"Evento de historial inválido: {evento}")
        now_ny = _now_new_york()
        position_side = str(values.get("position_side", "") or "").upper()
        legacy_side = "BUY" if position_side == "LONG" else (
            "SELL" if position_side == "SHORT" else ""
        )
        row: Dict[str, Any] = {key: "" for key in HISTORY_FIELDS}
        row.update({
            "history_id": _text(values.get("history_id") or uuid.uuid4().hex, 64),
            "timestamp_utc": _text(
                values.get("timestamp_utc")
                or datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                64,
            ),
            "fecha": _text(values.get("fecha") or now_ny.strftime("%Y-%m-%d"), 16),
            "hora_ny": _text(values.get("hora_ny") or now_ny.strftime("%H:%M:%S.%f")[:-3], 16),
            "modo": _text(values.get("modo"), 16).upper(),
            "ticker": _text(values.get("ticker"), 16).upper(),
            "evento": event,
            "correlation_id": _text(values.get("correlation_id"), 64),
            "reversal_id": _text(values.get("reversal_id"), 64),
            "signal_id": _text(values.get("signal_id"), 64),
            "execution_id": _text(values.get("execution_id"), 64),
            "position_id": _text(values.get("position_id"), 64),
            "state_before": _text(values.get("state_before"), 16).upper(),
            "state_after": _text(values.get("state_after"), 16).upper(),
            "position_side": position_side,
            "signal_side": _text(values.get("signal_side"), 16).upper(),
            "order_action": _text(values.get("order_action"), 24).upper(),
            "order_id": _text(values.get("order_id"), 128),
            "parent_order_id": _text(values.get("parent_order_id"), 128),
            "lado": legacy_side,
            "entrada": _number(values.get("entrada")),
            "salida": _number(values.get("salida")),
            "cantidad": int(float(values.get("cantidad", 0) or 0)),
            "pnl_usd": _number(values.get("pnl_usd")),
            "resultado": _text(values.get("resultado"), 24).upper(),
            "motivo": _text(values.get("motivo"), 500),
        })
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_schema()
            new_file = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=HISTORY_FIELDS)
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
        return {key: str(row.get(key, "")) for key in HISTORY_FIELDS}

    def record_open(self, **values: Any) -> Dict[str, str]:
        values.setdefault("salida", "")
        values.setdefault("pnl_usd", "")
        values.setdefault("resultado", "")
        return self.record("POSITION_OPEN", **values)

    def record_close(self, **values: Any) -> Dict[str, str]:
        pnl = float(values.get("pnl_usd", 0.0) or 0.0)
        values.setdefault(
            "resultado", "GANADA" if pnl > 0 else ("PERDIDA" if pnl < 0 else "NEUTRA")
        )
        return self.record("POSITION_CLOSE", **values)

    def read(
        self, limit: Optional[int] = None, newest_first: bool = True
    ) -> List[Dict[str, str]]:
        with self._lock:
            if not self.path.exists():
                return []
            self._ensure_schema()
            with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        if newest_first:
            rows.reverse()
        if limit is not None:
            rows = rows[:max(0, int(limit))]
        return rows


def read_trade_history(
    path: str = "data/historial_operaciones.csv",
    *,
    limit: Optional[int] = None,
    newest_first: bool = True,
) -> List[Dict[str, str]]:
    return TradeHistoryLedger(path).read(limit=limit, newest_first=newest_first)

