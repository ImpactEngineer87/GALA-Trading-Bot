"""Append-only Shadow Mode decision ledger.

A shadow model may recommend APPROVE/VETO, but this module never controls order
execution.  It only records the recommendation at the real PAPER entry and
joins it with the eventual trade result so each ticker can be evaluated safely.
"""

from __future__ import annotations

import csv
import math
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from agents.contracts import DecisionFinal


COLUMNS = [
    "event",
    "signal_id",
    "timestamp",
    "ticker",
    "tipo",
    "execution_mode",
    "shadow_prob",
    "shadow_threshold",
    "shadow_would_approve",
    "shadow_model_source",
    "market_regime",
    "market_regime_policy",
    "market_regime_confidence",
    "market_regime_would_block",
    "market_regime_reason",
    "precio_entrada",
    "stop_loss",
    "precio_salida",
    "r_multiple",
    "ganadora",
    "shadow_correct",
    "veto_would_help",
    "regime_correct",
    "regime_veto_would_help",
    "regime_false_block",
]


def _fmt(value):
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return value


class ShadowDecisionStore:
    """Thread-safe append-only OPEN/CLOSE ledger, one active trade per ticker."""

    def __init__(self, path: str = "data/shadow_decisions.csv"):
        self.path = path
        self._lock = threading.RLock()
        self._open: Dict[str, dict] = {}
        self._ensure_schema()
        self._recover_open()

    def abrir(
        self,
        decision: DecisionFinal,
        precio_entrada: float,
        stop_loss: Optional[float] = None,
    ) -> Optional[dict]:
        shadow_available = bool(
            getattr(decision, "shadow_enabled", False)
            and getattr(decision, "shadow_model_loaded", False)
        )
        policy = str(getattr(decision, "market_regime_policy", "") or "").lower()
        if policy not in {"off", "observe", "enforce"}:
            legacy = str(getattr(decision, "market_regime_enforcement", "off") or "off").lower()
            policy = "enforce" if legacy == "real" else ("observe" if legacy == "shadow" else "off")
        regime_available = bool(
            getattr(decision, "market_regime_enabled", False)
            and policy in {"observe", "enforce"}
        )
        if not shadow_available and not regime_available:
            return None
        shadow_prob = getattr(decision, "shadow_prob", None)
        shadow_threshold = getattr(decision, "shadow_threshold", None)
        if shadow_available and (shadow_prob is None or shadow_threshold is None):
            return None
        ticker = str(getattr(decision, "ticker", "") or "").upper()
        tipo = str(getattr(decision, "tipo", "") or "").upper()
        if not ticker or tipo not in ("BUY", "SELL"):
            return None

        row = {
            "event": "OPEN",
            "signal_id": uuid.uuid4().hex,
            "timestamp": self._timestamp(getattr(decision, "timestamp", None)),
            "ticker": ticker,
            "tipo": tipo,
            "execution_mode": str(getattr(decision, "execution_mode", "") or "").upper(),
            "shadow_prob": round(float(shadow_prob), 6) if shadow_available else "",
            "shadow_threshold": round(float(shadow_threshold), 6) if shadow_available else "",
            "shadow_would_approve": (
                int(bool(getattr(decision, "shadow_would_approve", False)))
                if shadow_available else ""
            ),
            "shadow_model_source": (
                str(getattr(decision, "shadow_model_source", "") or "")
                if shadow_available else ""
            ),
            "market_regime": str(getattr(decision, "market_regime", "") or ""),
            "market_regime_policy": policy if regime_available else "",
            "market_regime_confidence": _fmt(
                getattr(decision, "market_regime_confidence", "")
            ),
            "market_regime_would_block": (
                int(bool(getattr(decision, "market_regime_would_block", False)))
                if regime_available else ""
            ),
            "market_regime_reason": str(
                getattr(decision, "market_regime_reason", "") or ""
            ),
            "precio_entrada": round(float(precio_entrada), 6),
            "stop_loss": _fmt(float(stop_loss)) if stop_loss is not None else "",
            "precio_salida": "",
            "r_multiple": "",
            "ganadora": "",
            "shadow_correct": "",
            "veto_would_help": "",
            "regime_correct": "",
            "regime_veto_would_help": "",
            "regime_false_block": "",
        }
        with self._lock:
            self._open[ticker] = dict(row)
            self._append(row)
        return row

    def cerrar(self, ticker: str, precio_salida: float) -> Optional[dict]:
        ticker = str(ticker or "").upper()
        with self._lock:
            opened = self._open.pop(ticker, None)
            if opened is None:
                return None

            if precio_salida is None:
                # Broker-flat with unknown execution price is no win/loss label,
                # including in SHADOW evaluation. Close the observation honestly.
                row = {**opened, "event": "CLOSE", "timestamp": self._timestamp(None)}
                for key in ("precio_salida", "r_multiple", "ganadora", "shadow_correct",
                            "veto_would_help", "regime_correct", "regime_veto_would_help",
                            "regime_false_block"):
                    row[key] = ""
                self._append(row)
                return row

            entrada = float(opened["precio_entrada"])
            salida = float(precio_salida)
            stop_raw = opened.get("stop_loss")
            stop = float(stop_raw) if stop_raw not in (None, "") else None
            riesgo = abs(entrada - stop) if stop is not None else 0.0
            if riesgo > 0:
                r_multiple = (
                    (salida - entrada) / riesgo
                    if opened["tipo"] == "BUY"
                    else (entrada - salida) / riesgo
                )
            else:
                r_multiple = 0.0
            ganadora = int(r_multiple > 0)
            shadow_raw = opened.get("shadow_would_approve", "")
            if shadow_raw in (None, ""):
                shadow_correct = ""
                veto_would_help = ""
            else:
                would_approve = bool(int(float(shadow_raw)))
                shadow_correct = int(
                    (would_approve and ganadora)
                    or ((not would_approve) and not ganadora)
                )
                veto_would_help = int((not would_approve) and not ganadora)

            regime_raw = opened.get("market_regime_would_block", "")
            if regime_raw in (None, ""):
                regime_correct = ""
                regime_veto_would_help = ""
                regime_false_block = ""
            else:
                regime_would_block = bool(int(float(regime_raw)))
                regime_correct = int(
                    (regime_would_block and not ganadora)
                    or ((not regime_would_block) and ganadora)
                )
                regime_veto_would_help = int(regime_would_block and not ganadora)
                regime_false_block = int(regime_would_block and ganadora)

            row = dict(opened)
            row.update(
                {
                    "event": "CLOSE",
                    "timestamp": self._timestamp(None),
                    "precio_salida": round(salida, 6),
                    "r_multiple": round(float(r_multiple), 6),
                    "ganadora": ganadora,
                    "shadow_correct": shadow_correct,
                    "veto_would_help": veto_would_help,
                    "regime_correct": regime_correct,
                    "regime_veto_would_help": regime_veto_would_help,
                    "regime_false_block": regime_false_block,
                }
            )
            self._append(row)
            return row

    def _ensure_schema(self) -> None:
        """Migrate the append-only CSV when regime audit columns evolve."""
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            return
        try:
            with open(self.path, "r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                existing = list(reader.fieldnames or [])
                rows = list(reader)
            if existing == COLUMNS:
                return

            backup = self.path + ".before_v3_1_1"
            suffix = 1
            while os.path.exists(backup):
                backup = self.path + f".before_v3_1_1_{suffix}"
                suffix += 1
            shutil.copy2(self.path, backup)

            temporary = self.path + ".v3_1_1.tmp"
            with open(temporary, "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({name: row.get(name, "") for name in COLUMNS})
            os.replace(temporary, self.path)
        except Exception:
            # Audit migration must never interrupt trading. If migration cannot
            # be completed, the next append/restart will retry.
            return

    def _append(self, row: dict) -> None:
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        new_file = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        with open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerow({name: _fmt(row.get(name, "")) for name in COLUMNS})

    def _recover_open(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            by_id: Dict[str, dict] = {}
            with open(self.path, "r", newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    signal_id = row.get("signal_id", "")
                    if not signal_id:
                        continue
                    if row.get("event") == "OPEN":
                        by_id[signal_id] = row
                    elif row.get("event") == "CLOSE":
                        by_id.pop(signal_id, None)
            for row in by_id.values():
                ticker = str(row.get("ticker", "")).upper()
                if ticker:
                    self._open[ticker] = row
        except Exception:
            self._open = {}

    @staticmethod
    def _timestamp(value) -> str:
        if isinstance(value, datetime):
            dt = value
            if dt.tzinfo is None:
                return dt.isoformat(sep=" ")
            return dt.astimezone(timezone.utc).isoformat()
        return datetime.now(timezone.utc).isoformat()

