"""Complete signal-learning ledger for GALA10 (v3.3.7).

The production model historically learned only from execution-valid CLOSED trades.
That remains the highest-trust dataset and remains the only source that advances
x/50.  This module adds a separate, lower-trust counterfactual dataset for
signals that were *not* executed (AI vetoes, broker/capacity misses, etc.).

Each non-executed signal stores the exact model features and follows confirmed
bars until TP, SL, ambiguous same-bar touch, or timeout.  Only unambiguous TP/SL
resolutions can be used as supplemental FIT samples.  They never enter
validation/test and never increment x/50.
"""
from __future__ import annotations

import csv
import json
import math
import os
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from core.project_paths import resolve_runtime_path
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from agents.feedback_store import construir_features
from agents.gala10_features import GALA10_FEATURE_NAMES
from agents.volume_features import VOLUME_FEATURE_NAMES


LEARNING_FEATURES = [
    "tipo_num", "confianza_macro", "atr_relativo",
    "fvg_tam_rel", "hueco_apertura", "confluencia", "es_rango_lateral",
    *VOLUME_FEATURE_NAMES,
    *GALA10_FEATURE_NAMES,
]

COLUMNS = [
    "event", "observation_id", "signal_id", "timestamp", "resolved_utc",
    "ticker", "execution_mode", "tipo", "source_kind", "disposition",
    "reason", "training_candidate", "ai_probability", "ai_threshold",
    "market_regime", "market_regime_confidence", "market_regime_would_block",
    "precio_entrada", "stop_loss", "take_profit", "bars_seen", "resolution",
    "precio_salida", "r_multiple", "ganadora", "training_eligible",
    "label_quality", "notes", *LEARNING_FEATURES,
    "signal_bar_utc", "last_bar_ts", "bar_duration_minutes",
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
            return str(value)
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


def _as_utc(value, *, naive_zone=timezone.utc) -> Optional[datetime]:
    """Candle indices are UTC; legacy naive decision times are New York."""
    if value is None or str(value).strip() in {"", "NaT", "nan", "None"}:
        return None
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=naive_zone)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _valid_bracket(tipo: str, entry, sl, tp) -> bool:
    try:
        entry, sl, tp = float(entry), float(sl), float(tp)
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(x) and x > 0 for x in (entry, sl, tp)):
        return False
    if tipo == "BUY":
        return sl < entry < tp
    return tipo == "SELL" and tp < entry < sl


def _intraday_gap(previous: datetime, current: datetime, duration_minutes: int) -> bool:
    """Censor unobserved regular-hours intervals, including across restarts.

    An ordinary overnight/weekend transition requires coverage through the
    prior session's close and from the next weekday's open. Without an exchange
    calendar, uncertain holidays and early closes are conservatively censored.
    """
    if current - previous <= timedelta(minutes=duration_minutes):
        return False
    ny = ZoneInfo("America/New_York")
    left, right = previous.astimezone(ny), current.astimezone(ny)
    previous_close = left + timedelta(minutes=duration_minutes)
    session_close = left.replace(hour=16, minute=0, second=0, microsecond=0)
    if left.date() == right.date():
        session_open = left.replace(hour=9, minute=30, second=0, microsecond=0)
        return left.weekday() < 5 and max(previous_close, session_open) < min(right, session_close)
    next_weekday = left.date() + timedelta(days=1)
    while next_weekday.weekday() >= 5:
        next_weekday += timedelta(days=1)
    next_open = right.replace(hour=9, minute=30, second=0, microsecond=0)
    return (
        left.weekday() >= 5 or right.date() != next_weekday
        or previous_close < session_close or right > next_open
    )


class SignalLearningStore:
    """Thread-safe ledger for non-executed GALA10 signal outcomes."""

    VERSION = "3.4.0"

    def __init__(
        self,
        path: str = "data/signal_learning_observations.csv",
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
        self._seen_keys = set()
        self._recover()

    def record_nonexecuted(
        self,
        decision,
        *,
        execution_mode: str,
        source_kind: str,
        disposition: str,
        reason: str = "",
        training_candidate: bool = True,
        signal_bar_timestamp=None,
        bar_duration_minutes=None,
    ) -> Optional[dict]:
        if not self.enabled or decision is None:
            return None
        ticker = str(getattr(decision, "ticker", "") or "").strip().upper()
        tipo = str(getattr(decision, "tipo", "") or "").strip().upper()
        propuesta = getattr(decision, "propuesta", None)
        if not ticker or tipo not in {"BUY", "SELL"} or propuesta is None:
            return None
        try:
            entry = float(getattr(propuesta, "precio_entrada", 0.0) or 0.0)
            sl = float(getattr(propuesta, "stop_loss", 0.0) or 0.0)
            tp = float(getattr(propuesta, "take_profit", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None
        if not _valid_bracket(tipo, entry, sl, tp):
            return None

        features = construir_features(decision)
        signal_id = str(getattr(decision, "signal_id", "") or "").strip()
        ts = _timestamp(getattr(decision, "timestamp", None))
        baseline_value = (
            signal_bar_timestamp if signal_bar_timestamp is not None
            else getattr(decision, "signal_bar_timestamp", None)
        )
        baseline = _as_utc(baseline_value)
        if baseline is None and baseline_value is not None:
            # An explicitly supplied but invalid bar cannot be labeled safely.
            return None
        if baseline is None:
            baseline = _as_utc(ts, naive_zone=ZoneInfo("America/New_York"))
        if baseline is None:
            return None
        try:
            duration = max(1, int(
                getattr(decision, "signal_bar_duration_minutes", 1)
                if bar_duration_minutes is None else bar_duration_minutes
            ))
        except (TypeError, ValueError, OverflowError):
            return None
        decision_utc = _as_utc(ts, naive_zone=ZoneInfo("America/New_York"))
        if baseline_value is not None and (
            decision_utc is None or baseline + timedelta(minutes=duration) > decision_utc
        ):
            # A bar's high/low and features are unavailable before its close.
            return None
        # A stable fallback key prevents a retry of the same signal/bar from
        # becoming several synthetic training examples.
        dedupe_key = signal_id or f"{ticker}|{tipo}|{ts}"
        with self._lock:
            if dedupe_key in self._seen_keys:
                for row in self._active.values():
                    existing = str(row.get("signal_id", "") or "") or str(row.get("dedupe_key", "") or "")
                    if existing == dedupe_key:
                        return dict(row)
                return None

            oid = uuid.uuid4().hex
            prob = getattr(decision, "prob_meta", None)
            threshold = None
            snapshot = dict(getattr(decision, "ai_model_snapshot", {}) or {})
            for key in ("threshold", "decision_threshold"):
                if snapshot.get(key) not in (None, ""):
                    try:
                        threshold = float(snapshot[key]); break
                    except (TypeError, ValueError):
                        pass
            row = {
                "event": "OPEN",
                "observation_id": oid,
                "signal_id": signal_id,
                "timestamp": ts,
                "resolved_utc": "",
                "ticker": ticker,
                "execution_mode": str(execution_mode or "").upper() or "UNKNOWN",
                "tipo": tipo,
                "source_kind": str(source_kind or "NON_EXECUTED").upper(),
                "disposition": str(disposition or "NON_EXECUTED").upper(),
                "reason": str(reason or ""),
                "training_candidate": int(bool(training_candidate)),
                "ai_probability": _fmt(float(prob)) if prob is not None else "",
                "ai_threshold": _fmt(threshold),
                "market_regime": str(getattr(decision, "market_regime", "") or ""),
                "market_regime_confidence": _fmt(getattr(decision, "market_regime_confidence", "")),
                "market_regime_would_block": int(bool(getattr(decision, "market_regime_would_block", False))),
                "precio_entrada": round(entry, 6),
                "stop_loss": round(sl, 6),
                "take_profit": round(tp, 6),
                "bars_seen": 0,
                "resolution": "OPEN",
                "precio_salida": "",
                "r_multiple": "",
                "ganadora": "",
                "training_eligible": 0,
                "label_quality": "",
                "notes": "supplemental_not_x50",
                "signal_bar_utc": baseline.isoformat(),
                "bar_duration_minutes": duration,
                "last_bar_ts": "",
                "dedupe_key": dedupe_key,
            }
            for name in LEARNING_FEATURES:
                row[name] = _fmt(features.get(name))
            self._append(row)
            self._active[oid] = dict(row)
            self._seen_keys.add(dedupe_key)
            self._persist_state_locked()
            return dict(row)

    def pending_after(self, ticker: str) -> Optional[datetime]:
        """Earliest UTC replay cursor among this ticker's open observations."""
        ticker = str(ticker or "").strip().upper()
        with self._lock:
            cursors = [
                _as_utc(row.get("last_bar_ts")) or _as_utc(row.get("signal_bar_utc"))
                for row in self._active.values() if row.get("ticker") == ticker
            ]
            return min((cursor for cursor in cursors if cursor is not None), default=None)

    def update_bar(
        self,
        ticker: str,
        *,
        high: float,
        low: float,
        close: float,
        bar_timestamp=None,
        bar_duration_minutes=None,
    ) -> List[dict]:
        if not self.enabled:
            return []
        ticker = str(ticker or "").strip().upper()
        try:
            high, low, close = float(high), float(low), float(close)
        except (TypeError, ValueError):
            return []
        if (
            not ticker or not all(math.isfinite(x) and x > 0 for x in (high, low, close))
            or not low <= close <= high
        ):
            return []
        bar_dt = _as_utc(bar_timestamp)
        if bar_dt is None:
            return []
        incoming_duration = None
        if bar_duration_minutes is not None:
            try:
                incoming_duration = max(1, int(bar_duration_minutes))
            except (TypeError, ValueError, OverflowError):
                return []
        bar_ts = bar_dt.isoformat()
        resolved: List[dict] = []
        with self._lock:
            ids = [oid for oid, row in self._active.items() if row.get("ticker") == ticker]
            changed = False
            for oid in ids:
                active = self._active.get(oid)
                if not active:
                    continue
                row = dict(active)
                baseline = _as_utc(row.get("signal_bar_utc"))
                last_bar = _as_utc(row.get("last_bar_ts"))
                original_duration = int(row.get("bar_duration_minutes") or 1)
                if (
                    baseline is None or bar_dt < baseline + timedelta(minutes=original_duration)
                    or (last_bar is not None and bar_dt <= last_bar)
                ):
                    continue
                previous_duration = int(row.get("last_bar_duration_minutes") or row.get("bar_duration_minutes") or 1)
                duration = incoming_duration or int(row.get("bar_duration_minutes") or 1)
                missing_candle = _intraday_gap(last_bar or baseline, bar_dt, previous_duration)
                row["last_bar_ts"] = bar_ts
                row["last_bar_duration_minutes"] = duration
                row["bars_seen"] = int(row.get("bars_seen", 0) or 0) + 1
                changed = True
                tipo = row["tipo"]
                entry = float(row["precio_entrada"])
                sl = float(row["stop_loss"])
                tp = float(row["take_profit"])
                hit_sl = low <= sl if tipo == "BUY" else high >= sl
                hit_tp = high >= tp if tipo == "BUY" else low <= tp
                if missing_candle:
                    final = self._resolve(row, "DATA_GAP", None, None)
                elif hit_sl and hit_tp:
                    final = self._resolve(row, "AMBIGUOUS_SAME_BAR", None, None)
                elif hit_tp:
                    final = self._resolve(row, "TP", tp, 1)
                elif hit_sl:
                    final = self._resolve(row, "SL", sl, 0)
                elif int(row["bars_seen"]) >= self.max_bars:
                    final = self._resolve(row, "TIMEOUT", close, None)
                else:
                    self._active[oid] = row
                    continue
                self._append(final)
                self._active.pop(oid, None)
                resolved.append(final)
            if changed:
                self._persist_state_locked()
        return resolved

    def _resolve(self, row: dict, resolution: str, exit_price, winner) -> dict:
        final = dict(row)
        known_at = datetime.now(timezone.utc)
        bar_start = _as_utc(row.get("last_bar_ts"))
        if bar_start is not None:
            duration = int(row.get("last_bar_duration_minutes") or row.get("bar_duration_minutes") or 1)
            known_at = max(known_at, bar_start + timedelta(minutes=duration))
        final.update({
            "event": "RESOLVED",
            "resolved_utc": known_at.isoformat(),
            "resolution": resolution,
        })
        if exit_price is None:
            final.update({
                "precio_salida": "", "r_multiple": "", "ganadora": "",
                "training_eligible": 0,
                "label_quality": "INCOMPLETE_CANDLE_HISTORY" if resolution == "DATA_GAP" else "AMBIGUOUS",
            })
            return final
        entry, sl = float(row["precio_entrada"]), float(row["stop_loss"])
        risk = abs(entry - sl)
        r_mult = 0.0
        if risk > 0:
            r_mult = ((float(exit_price) - entry) / risk if row["tipo"] == "BUY"
                      else (entry - float(exit_price)) / risk)
        if winner is None:
            winner = int(r_mult > 0)
        unambiguous = resolution in {"TP", "SL"}
        candidate = bool(int(row.get("training_candidate", 0) or 0))
        final.update({
            "precio_salida": round(float(exit_price), 6),
            "r_multiple": round(float(r_mult), 6),
            "ganadora": int(winner),
            "training_eligible": int(unambiguous and candidate),
            "label_quality": "COUNTERFACTUAL_TP_SL" if unambiguous else "TIMEOUT_MARK_TO_MARKET",
        })
        return final

    def summary_by_ticker(self, tickers: Optional[Iterable[str]] = None) -> List[dict]:
        requested = None if tickers is None else {str(t).upper() for t in tickers if str(t).strip()}
        by_id: Dict[str, dict] = {}
        if self.path.exists():
            try:
                with self.path.open("r", newline="", encoding="utf-8-sig") as fh:
                    for row in csv.DictReader(fh):
                        tk = str(row.get("ticker", "") or "").upper()
                        if not tk or (requested is not None and tk not in requested):
                            continue
                        oid = str(row.get("observation_id", "") or "")
                        if not oid:
                            continue
                        state = by_id.setdefault(oid, dict(row))
                        if str(row.get("event", "")).upper() == "RESOLVED":
                            state.update(row)
            except Exception:
                by_id = {}
        stats: Dict[str, dict] = {}
        for row in by_id.values():
            tk = str(row.get("ticker", "") or "").upper()
            item = stats.setdefault(tk, {
                "ticker": tk, "learning_total": 0, "learning_open": 0,
                "learning_resolved": 0, "learning_trainable": 0,
                "learning_winners": 0, "learning_losers": 0,
                "learning_ai_veto": 0, "learning_execution_missed": 0,
            })
            item["learning_total"] += 1
            event = str(row.get("event", "") or "").upper()
            if event != "RESOLVED":
                item["learning_open"] += 1
            else:
                item["learning_resolved"] += 1
                if str(row.get("training_eligible", "")) == "1":
                    item["learning_trainable"] += 1
                    if str(row.get("ganadora", "")) == "1":
                        item["learning_winners"] += 1
                    elif str(row.get("ganadora", "")) == "0":
                        item["learning_losers"] += 1
            src = str(row.get("source_kind", "") or "").upper()
            if "VETO" in src or "MODEL" in src:
                item["learning_ai_veto"] += 1
            if "EXECUTION" in src or "MISSED" in src:
                item["learning_execution_missed"] += 1
        if requested is not None:
            for tk in requested:
                stats.setdefault(tk, {
                    "ticker": tk, "learning_total": 0, "learning_open": 0,
                    "learning_resolved": 0, "learning_trainable": 0,
                    "learning_winners": 0, "learning_losers": 0,
                    "learning_ai_veto": 0, "learning_execution_missed": 0,
                })
        return [stats[tk] for tk in sorted(stats)]

    def _append(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        columns = list(COLUMNS)
        if not new_file:
            with self.path.open("r", newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                previous_columns = list(reader.fieldnames or [])
                if not previous_columns or "observation_id" not in previous_columns:
                    raise ValueError("Signal-learning CSV has no observation_id column")
                if set(COLUMNS).issubset(previous_columns):
                    columns = previous_columns
                else:
                    # Preserve the append-only evidence when introducing causal
                    # cursor columns. Appending wider rows to an old header
                    # would otherwise silently corrupt subsequent CSV reads.
                    columns.extend(name for name in previous_columns if name not in columns)
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(
                            mode="w", newline="", encoding="utf-8",
                            dir=str(self.path.parent), prefix=self.path.name + ".",
                            suffix=".tmp", delete=False,
                        ) as output:
                            temporary = Path(output.name)
                            writer = csv.DictWriter(output, fieldnames=columns)
                            writer.writeheader()
                            writer.writerows(reader)
                            output.flush()
                            os.fsync(output.fileno())
                        # Close the old CSV handle before replacing it on Windows.
                    except Exception:
                        if temporary is not None:
                            temporary.unlink(missing_ok=True)
                        raise
            if not set(COLUMNS).issubset(previous_columns):
                try:
                    os.replace(temporary, self.path)
                finally:
                    temporary.unlink(missing_ok=True)
        original_size = self.path.stat().st_size if self.path.exists() else 0
        try:
            with self.path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
                if new_file:
                    writer.writeheader()
                writer.writerow({name: _fmt(row.get(name, "")) for name in columns})
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            # A handled partial write must not become an apparent committed
            # event. Keep the OPEN/cursor unchanged so the same signal or
            # decisive candle can retry without duplicate training samples.
            try:
                with self.path.open("r+b") as fh:
                    fh.truncate(original_size)
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError:
                pass
            raise

    def _persist_state_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": self.VERSION, "updated_utc": datetime.now(timezone.utc).isoformat(), "active": self._active}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(self.state_path.parent),
                prefix=self.state_path.name + ".", suffix=".tmp", delete=False,
            ) as fh:
                temporary = Path(fh.name)
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _recover(self) -> None:
        # CSV events are durable before their sidecar snapshot. A crash between
        # the two writes must neither resurrect a resolution nor lose an OPEN.
        by_id: Dict[str, dict] = {}
        if self.path.exists():
            try:
                with self.path.open("r", newline="", encoding="utf-8-sig") as fh:
                    for row in csv.DictReader(fh):
                        key = self._dedupe_key(row)
                        if key:
                            self._seen_keys.add(key)
                        oid = str(row.get("observation_id", "") or "").strip()
                        if not oid:
                            continue
                        prior = by_id.get(oid, {})
                        if str(prior.get("event", "")).upper() != "RESOLVED":
                            by_id[oid] = dict(row)
            except Exception:
                # Unreadable evidence cannot safely be replaced with a stale
                # sidecar that might still contain already-resolved examples.
                return
        if not self.enabled:
            return
        saved_active = {}
        try:
            if self.state_path.exists():
                raw = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
                saved_active = raw.get("active", {}) if isinstance(raw, dict) else {}
                if not isinstance(saved_active, dict):
                    saved_active = {}
        except Exception:
            saved_active = {}
        # Recover a sidecar-only ledger only when no CSV survives; otherwise the
        # event log decides which identities are still open.
        if not self.path.exists():
            by_id = {
                str(oid): dict(row) for oid, row in saved_active.items()
                if isinstance(row, dict)
            }
        for oid, evidence in by_id.items():
            if str(evidence.get("event", "OPEN")).upper() != "OPEN":
                continue
            row = dict(evidence)
            ticker = str(row.get("ticker", "") or "").strip().upper()
            tipo = str(row.get("tipo", "") or "").strip().upper()
            if not ticker or not _valid_bracket(
                tipo, row.get("precio_entrada"), row.get("stop_loss"), row.get("take_profit")
            ):
                continue
            baseline = _as_utc(row.get("signal_bar_utc"))
            if baseline is None:
                # Old rows stored only NY decision wall-clock time. This
                # conservative baseline excludes every pre-decision candle.
                baseline = _as_utc(row.get("timestamp"), naive_zone=ZoneInfo("America/New_York"))
            if baseline is None:
                continue
            row.update(ticker=ticker, tipo=tipo, observation_id=oid,
                       signal_bar_utc=baseline.isoformat(), event="OPEN", resolution="OPEN")
            try:
                row["bar_duration_minutes"] = max(1, int(row.get("bar_duration_minutes") or 1))
            except (TypeError, ValueError, OverflowError):
                continue
            saved = saved_active.get(oid, {})
            if isinstance(saved, dict):
                saved_cursor = _as_utc(saved.get("last_bar_ts"))
                if saved_cursor is not None and saved_cursor > baseline:
                    row["last_bar_ts"] = saved_cursor.isoformat()
                    try:
                        row["bars_seen"] = max(0, int(saved.get("bars_seen", 0) or 0))
                        row["last_bar_duration_minutes"] = max(1, int(
                            saved.get("last_bar_duration_minutes") or row["bar_duration_minutes"]
                        ))
                    except (TypeError, ValueError):
                        row["bars_seen"] = 0
            try:
                row["bars_seen"] = max(0, int(row.get("bars_seen", 0) or 0))
            except (TypeError, ValueError):
                row["bars_seen"] = 0
            row["dedupe_key"] = self._dedupe_key(row)
            self._active[oid] = row
            self._seen_keys.add(row["dedupe_key"])

    @staticmethod
    def _dedupe_key(row: dict) -> str:
        key = str(row.get("signal_id", "") or "").strip()
        if key:
            return key
        ticker = str(row.get("ticker", "") or "").strip().upper()
        tipo = str(row.get("tipo", "") or "").strip().upper()
        return f"{ticker}|{tipo}|{row.get('timestamp', '')}" if ticker and tipo else ""

