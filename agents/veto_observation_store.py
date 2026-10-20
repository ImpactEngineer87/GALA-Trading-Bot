"""Audit-only ledger for AI-vetoed GALA10 signals (v2.9.4).

This dataset is deliberately separate from ``agentes_feedback.csv``. A signal
that the production AI vetoes was never executed, so it must NOT be counted as
one of the verified closed operations in the ticker's x/50 retraining cycle.

Instead, this store records the hypothetical entry/SL/TP and follows subsequent
closed bars until TP, SL, an ambiguous same-bar touch, or a bounded timeout.
That gives us evidence about whether the veto was useful without contaminating
execution-trained labels or pretending that an unfilled order was a real trade.
"""

from __future__ import annotations

import csv
import json
import math
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from core.project_paths import resolve_runtime_path
from typing import Dict, Iterable, List, Optional


COLUMNS = [
    "event",
    "observation_id",
    "signal_id",
    "timestamp",
    "resolved_utc",
    "ticker",
    "execution_mode",
    "tipo",
    "ai_probability",
    "ai_threshold",
    "veto_reason",
    "precio_entrada",
    "stop_loss",
    "take_profit",
    "bars_seen",
    "resolution",
    "precio_salida",
    "r_multiple",
    "ganadora",
    "notes",
]


def _fmt(value):
    if value is None:
        return ""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return ""
    return value


def _timestamp(value=None) -> str:
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return dt.isoformat(sep=" ")
        return dt.astimezone(timezone.utc).isoformat()
    if value is not None:
        try:
            # pandas.Timestamp and similar values stringify safely here; this is
            # audit metadata only and never a model feature.
            return str(value)
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


class VetoObservationStore:
    """Thread-safe, append-only audit of non-executed AI vetoes.

    Active hypothetical observations are persisted to ``<csv>.open.json`` so a
    server restart does not lose the audit.  The store never imports or calls the
    retraining lifecycle and therefore cannot increment ``x/50``.
    """

    VERSION = "2.9.6"

    def __init__(
        self,
        path: str = "data/ai_veto_observations.csv",
        *,
        enabled: bool = True,
        max_bars: int = 120,
    ):
        self.path = Path(resolve_runtime_path(path))
        self.enabled = bool(enabled)
        self.max_bars = max(1, int(max_bars or 120))
        self.state_path = Path(str(self.path) + ".open.json")
        self._lock = threading.RLock()
        self._active: Dict[str, dict] = {}
        self._recover()

    def record_veto(
        self,
        *,
        ticker: str,
        tipo: str,
        probability: float,
        threshold: Optional[float],
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        execution_mode: str,
        veto_reason: str,
        signal_id: str = "",
        timestamp=None,
    ) -> Optional[dict]:
        if not self.enabled:
            return None
        ticker = str(ticker or "").strip().upper()
        tipo = str(tipo or "").strip().upper()
        mode = str(execution_mode or "").strip().upper() or "UNKNOWN"
        if not ticker or tipo not in ("BUY", "SELL"):
            return None
        try:
            prob = float(probability)
            entry = float(entry_price)
            sl = float(stop_loss)
            tp = float(take_profit)
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(x) for x in (prob, entry, sl, tp)) or entry <= 0:
            return None
        if sl == entry or tp == entry:
            return None
        threshold_value = None
        try:
            if threshold is not None:
                threshold_value = float(threshold)
                if not math.isfinite(threshold_value):
                    threshold_value = None
        except (TypeError, ValueError):
            threshold_value = None

        ts = _timestamp(timestamp)
        # Do not duplicate the exact same rejected signal if a UI/worker retry
        # reaches this function twice.
        dedupe = (ticker, tipo, ts)
        with self._lock:
            for active in self._active.values():
                active_key = (
                    active.get("ticker"), active.get("tipo"),
                    active.get("timestamp", ""),
                )
                if active_key == dedupe:
                    return dict(active)

            observation_id = uuid.uuid4().hex
            row = {
                "event": "VETO",
                "observation_id": observation_id,
                "signal_id": str(signal_id or ""),
                "timestamp": ts,
                "resolved_utc": "",
                "ticker": ticker,
                "execution_mode": mode,
                "tipo": tipo,
                "ai_probability": round(prob, 6),
                "ai_threshold": (
                    round(threshold_value, 6)
                    if threshold_value is not None else ""
                ),
                "veto_reason": str(veto_reason or "AI_VETO"),
                "precio_entrada": round(entry, 6),
                "stop_loss": round(sl, 6),
                "take_profit": round(tp, 6),
                "bars_seen": 0,
                "resolution": "OPEN",
                "precio_salida": "",
                "r_multiple": "",
                "ganadora": "",
                "notes": "audit_only_not_x50",
                "last_bar_ts": "",
            }
            self._active[observation_id] = dict(row)
            self._append(row)
            self._persist_state_locked()
            return dict(row)

    def update_bar(
        self,
        ticker: str,
        *,
        high: float,
        low: float,
        close: float,
        bar_timestamp=None,
    ) -> List[dict]:
        """Advance hypothetical veto observations with one confirmed bar."""
        if not self.enabled:
            return []
        ticker = str(ticker or "").strip().upper()
        try:
            high = float(high)
            low = float(low)
            close = float(close)
        except (TypeError, ValueError):
            return []
        if not ticker or not all(math.isfinite(x) for x in (high, low, close)):
            return []
        bar_ts = _timestamp(bar_timestamp)
        resolved: List[dict] = []
        with self._lock:
            ids = [
                oid for oid, row in self._active.items()
                if row.get("ticker") == ticker
            ]
            changed = False
            for oid in ids:
                row = self._active.get(oid)
                if not row:
                    continue
                if row.get("last_bar_ts") == bar_ts:
                    continue
                row["last_bar_ts"] = bar_ts
                row["bars_seen"] = int(row.get("bars_seen", 0) or 0) + 1
                changed = True

                tipo = row["tipo"]
                entry = float(row["precio_entrada"])
                sl = float(row["stop_loss"])
                tp = float(row["take_profit"])
                if tipo == "BUY":
                    hit_sl = low <= sl
                    hit_tp = high >= tp
                else:
                    hit_sl = high >= sl
                    hit_tp = low <= tp

                if hit_sl and hit_tp:
                    final = self._resolve_row(
                        row, resolution="AMBIGUOUS_SAME_BAR",
                        exit_price=None, winner=None,
                        notes="SL_and_TP_touched_same_confirmed_bar",
                    )
                elif hit_tp:
                    final = self._resolve_row(
                        row, resolution="TP", exit_price=tp, winner=1,
                    )
                elif hit_sl:
                    final = self._resolve_row(
                        row, resolution="SL", exit_price=sl, winner=0,
                    )
                elif int(row["bars_seen"]) >= self.max_bars:
                    final = self._resolve_row(
                        row, resolution="TIMEOUT", exit_price=close,
                        winner=None,
                    )
                else:
                    continue

                self._append(final)
                self._active.pop(oid, None)
                resolved.append(final)

            if changed:
                self._persist_state_locked()
        return resolved

    def active_count(self, ticker: Optional[str] = None) -> int:
        with self._lock:
            if ticker is None:
                return len(self._active)
            tk = str(ticker or "").strip().upper()
            return sum(1 for row in self._active.values() if row.get("ticker") == tk)

    def summary_by_ticker(self, tickers: Optional[Iterable[str]] = None) -> List[dict]:
        """Return one audit summary per ticker without touching x/50.

        ``VETO`` and ``RESOLVED`` are two append-only events for the same
        observation_id, so this method deduplicates by id.  The resulting
        counters are dashboard/audit information only and never feed the
        verified execution retraining lifecycle.
        """
        requested = None
        if tickers is not None:
            requested = {
                str(t or "").strip().upper() for t in tickers
                if str(t or "").strip()
            }

        by_id: Dict[str, dict] = {}
        if self.path.exists():
            try:
                with self.path.open("r", newline="", encoding="utf-8-sig") as fh:
                    for row in csv.DictReader(fh):
                        ticker = str(row.get("ticker", "") or "").strip().upper()
                        if not ticker or (requested is not None and ticker not in requested):
                            continue
                        oid = str(row.get("observation_id", "") or "").strip()
                        if not oid:
                            # Compatibility with a manually created/legacy audit row.
                            oid = "|".join((ticker, str(row.get("tipo", "")), str(row.get("timestamp", ""))))
                        state = by_id.setdefault(oid, {
                            "ticker": ticker, "event": "", "resolution": "",
                            "ganadora": "",
                        })
                        state["ticker"] = ticker
                        event = str(row.get("event", "") or "").upper()
                        if event == "RESOLVED":
                            state["event"] = "RESOLVED"
                            state["resolution"] = str(row.get("resolution", "") or "").upper()
                            state["ganadora"] = str(row.get("ganadora", "") or "").strip()
                        elif not state.get("event"):
                            state["event"] = event or "VETO"
            except Exception:
                # Dashboard summaries are non-critical; the live engine must not
                # fail because an audit CSV is temporarily unreadable.
                by_id = {}

        with self._lock:
            active_ids = set(self._active)
            active_rows = {oid: dict(row) for oid, row in self._active.items()}
        for oid, row in active_rows.items():
            ticker = str(row.get("ticker", "") or "").strip().upper()
            if not ticker or (requested is not None and ticker not in requested):
                continue
            by_id.setdefault(oid, {
                "ticker": ticker, "event": "VETO",
                "resolution": "OPEN", "ganadora": "",
            })

        stats: Dict[str, dict] = {}
        for oid, row in by_id.items():
            ticker = row["ticker"]
            item = stats.setdefault(ticker, {
                "ticker": ticker, "veto_total": 0, "veto_open": 0,
                "veto_resolved": 0, "veto_good": 0, "veto_bad": 0,
                "veto_ambiguous": 0, "veto_timeout": 0,
            })
            item["veto_total"] += 1
            if oid in active_ids or str(row.get("resolution", "")).upper() == "OPEN":
                item["veto_open"] += 1
                continue
            if str(row.get("event", "")).upper() == "RESOLVED":
                item["veto_resolved"] += 1
                resolution = str(row.get("resolution", "") or "").upper()
                winner = str(row.get("ganadora", "") or "").strip()
                # From the point of view of the veto: a hypothetical losing trade
                # is a GOOD veto; a hypothetical winner is a BAD/missed veto.
                if winner == "0":
                    item["veto_good"] += 1
                elif winner == "1":
                    item["veto_bad"] += 1
                if resolution == "AMBIGUOUS_SAME_BAR":
                    item["veto_ambiguous"] += 1
                elif resolution == "TIMEOUT":
                    item["veto_timeout"] += 1

        if requested is not None:
            for ticker in requested:
                stats.setdefault(ticker, {
                    "ticker": ticker, "veto_total": 0, "veto_open": 0,
                    "veto_resolved": 0, "veto_good": 0, "veto_bad": 0,
                    "veto_ambiguous": 0, "veto_timeout": 0,
                })

        # v3.2: make the client-facing meaning explicit. Only resolved TP/SL
        # outcomes with a winner label enter these rates; open, timeout and
        # same-bar ambiguous observations are not silently counted as success.
        for item in stats.values():
            comparable = int(item.get("veto_good", 0) or 0) + int(
                item.get("veto_bad", 0) or 0
            )
            item["veto_comparable"] = comparable
            item["veto_usefulness_pct"] = (
                round(100.0 * int(item.get("veto_good", 0) or 0) / comparable, 2)
                if comparable else None
            )
            item["veto_missed_winner_pct"] = (
                round(100.0 * int(item.get("veto_bad", 0) or 0) / comparable, 2)
                if comparable else None
            )
        return [stats[t] for t in sorted(stats)]

    def _resolve_row(
        self,
        row: dict,
        *,
        resolution: str,
        exit_price: Optional[float],
        winner: Optional[int],
        notes: str = "",
    ) -> dict:
        final = dict(row)
        final["event"] = "RESOLVED"
        final["resolved_utc"] = datetime.now(timezone.utc).isoformat()
        final["resolution"] = resolution
        final["notes"] = notes or "audit_only_not_x50"
        if exit_price is None:
            final["precio_salida"] = ""
            final["r_multiple"] = ""
            final["ganadora"] = ""
            return final

        entry = float(row["precio_entrada"])
        sl = float(row["stop_loss"])
        risk = abs(entry - sl)
        r_mult = 0.0
        if risk > 0:
            if row["tipo"] == "BUY":
                r_mult = (float(exit_price) - entry) / risk
            else:
                r_mult = (entry - float(exit_price)) / risk
        final["precio_salida"] = round(float(exit_price), 6)
        final["r_multiple"] = round(float(r_mult), 6)
        if winner is None:
            winner = int(r_mult > 0)
        final["ganadora"] = int(winner)
        return final

    def _append(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerow({name: _fmt(row.get(name, "")) for name in COLUMNS})
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass

    def _persist_state_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.VERSION,
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "active": self._active,
        }
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, self.state_path)

    def _recover(self) -> None:
        if not self.enabled or not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
            active = raw.get("active", {}) if isinstance(raw, dict) else {}
            if not isinstance(active, dict):
                return
            clean = {}
            for oid, row in active.items():
                if not isinstance(row, dict):
                    continue
                ticker = str(row.get("ticker", "") or "").strip().upper()
                tipo = str(row.get("tipo", "") or "").strip().upper()
                if ticker and tipo in ("BUY", "SELL"):
                    clean[str(oid)] = dict(row)
            self._active = clean
            if clean:
                print(
                    "[AI-VETO-AUDIT] Observaciones hipotéticas recuperadas: "
                    f"{len(clean)}"
                )
        except Exception as exc:
            print(f"[AI-VETO-AUDIT] No se pudo recuperar estado: {exc}")
            self._active = {}
