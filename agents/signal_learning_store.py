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
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

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


class SignalLearningStore:
    """Thread-safe ledger for non-executed GALA10 signal outcomes."""

    VERSION = "3.3.11"

    def __init__(
        self,
        path: str = "data/signal_learning_observations.csv",
        *,
        enabled: bool = True,
        max_bars: int = 120,
    ):
        self.path = Path(path)
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
        if not all(math.isfinite(x) for x in (entry, sl, tp)) or entry <= 0:
            return None
        if sl == entry or tp == entry:
            return None

        features = construir_features(decision)
        signal_id = str(getattr(decision, "signal_id", "") or "").strip()
        ts = _timestamp(getattr(decision, "timestamp", None))
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
                "last_bar_ts": "",
                "dedupe_key": dedupe_key,
            }
            for name in LEARNING_FEATURES:
                row[name] = _fmt(features.get(name))
            self._active[oid] = dict(row)
            self._seen_keys.add(dedupe_key)
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
        if not self.enabled:
            return []
        ticker = str(ticker or "").strip().upper()
        try:
            high, low, close = float(high), float(low), float(close)
        except (TypeError, ValueError):
            return []
        if not ticker or not all(math.isfinite(x) for x in (high, low, close)):
            return []
        bar_ts = _timestamp(bar_timestamp)
        resolved: List[dict] = []
        with self._lock:
            ids = [oid for oid, row in self._active.items() if row.get("ticker") == ticker]
            changed = False
            for oid in ids:
                row = self._active.get(oid)
                if not row or row.get("last_bar_ts") == bar_ts:
                    continue
                row["last_bar_ts"] = bar_ts
                row["bars_seen"] = int(row.get("bars_seen", 0) or 0) + 1
                changed = True
                tipo = row["tipo"]
                entry = float(row["precio_entrada"])
                sl = float(row["stop_loss"])
                tp = float(row["take_profit"])
                hit_sl = low <= sl if tipo == "BUY" else high >= sl
                hit_tp = high >= tp if tipo == "BUY" else low <= tp
                if hit_sl and hit_tp:
                    final = self._resolve(row, "AMBIGUOUS_SAME_BAR", None, None)
                elif hit_tp:
                    final = self._resolve(row, "TP", tp, 1)
                elif hit_sl:
                    final = self._resolve(row, "SL", sl, 0)
                elif int(row["bars_seen"]) >= self.max_bars:
                    final = self._resolve(row, "TIMEOUT", close, None)
                else:
                    continue
                self._append(final)
                self._active.pop(oid, None)
                resolved.append(final)
            if changed:
                self._persist_state_locked()
        return resolved

    def _resolve(self, row: dict, resolution: str, exit_price, winner) -> dict:
        final = dict(row)
        final.update({
            "event": "RESOLVED",
            "resolved_utc": datetime.now(timezone.utc).isoformat(),
            "resolution": resolution,
        })
        if exit_price is None:
            final.update({
                "precio_salida": "", "r_multiple": "", "ganadora": "",
                "training_eligible": 0, "label_quality": "AMBIGUOUS",
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
        def empty_stats(tk: str) -> dict:
            return {
                "ticker": tk, "learning_total": 0, "learning_open": 0,
                "learning_resolved": 0, "learning_trainable": 0,
                "learning_winners": 0, "learning_losers": 0,
                "learning_ai_veto": 0, "learning_execution_missed": 0,
                "learning_blocked": 0, "learning_missed": 0,
                "learning_tp": 0, "learning_sl": 0,
                "learning_ambiguous": 0, "learning_timeout": 0,
                "learning_excluded": 0, "learning_avoided_losses": 0,
                "learning_blocked_winners": 0, "learning_real": 0,
                "learning_paper": 0, "learning_usefulness_pct": None,
                "learning_last_update": "", "learning_last_resolution": "",
            }

        stats: Dict[str, dict] = {}
        for row in by_id.values():
            tk = str(row.get("ticker", "") or "").upper()
            item = stats.setdefault(tk, empty_stats(tk))
            item["learning_total"] += 1
            event = str(row.get("event", "") or "").upper()
            src = str(row.get("source_kind", "") or "").upper()
            disposition = str(row.get("disposition", "") or "").upper()
            resolution = str(row.get("resolution", "") or "").upper()
            mode = str(row.get("execution_mode", "") or "").upper()
            update_ts = str(row.get("resolved_utc", "") or row.get("timestamp", "") or "")
            if update_ts and update_ts > str(item.get("learning_last_update", "") or ""):
                item["learning_last_update"] = update_ts
            if mode == "REAL":
                item["learning_real"] += 1
            elif mode == "PAPER":
                item["learning_paper"] += 1
            if disposition == "BLOCKED":
                item["learning_blocked"] += 1
            if disposition == "MISSED":
                item["learning_missed"] += 1
            is_veto = ("VETO" in src or "MODEL" in src)
            if is_veto:
                item["learning_ai_veto"] += 1
            if "EXECUTION" in src or "MISSED" in src:
                item["learning_execution_missed"] += 1

            if event != "RESOLVED":
                item["learning_open"] += 1
                continue

            item["learning_resolved"] += 1
            item["learning_last_resolution"] = max(
                str(item.get("learning_last_resolution", "") or ""),
                str(row.get("resolved_utc", "") or ""),
            )
            if resolution == "TP":
                item["learning_tp"] += 1
            elif resolution == "SL":
                item["learning_sl"] += 1
            elif resolution == "AMBIGUOUS_SAME_BAR":
                item["learning_ambiguous"] += 1
            elif resolution == "TIMEOUT":
                item["learning_timeout"] += 1

            eligible = str(row.get("training_eligible", "")) == "1"
            if eligible:
                item["learning_trainable"] += 1
                if str(row.get("ganadora", "")) == "1":
                    item["learning_winners"] += 1
                    if is_veto:
                        item["learning_blocked_winners"] += 1
                elif str(row.get("ganadora", "")) == "0":
                    item["learning_losers"] += 1
                    if is_veto:
                        item["learning_avoided_losses"] += 1
            else:
                item["learning_excluded"] += 1

        for item in stats.values():
            denom = int(item.get("learning_avoided_losses", 0) or 0) + int(item.get("learning_blocked_winners", 0) or 0)
            item["learning_usefulness_pct"] = (
                round(100.0 * float(item.get("learning_avoided_losses", 0) or 0) / denom, 1)
                if denom else None
            )
        if requested is not None:
            for tk in requested:
                stats.setdefault(tk, empty_stats(tk))
        return [stats[tk] for tk in sorted(stats)]

    def recent_observations(self, ticker: Optional[str] = None, limit: int = 50) -> List[dict]:
        """Return the latest state of recent learning observations for dashboard visibility.

        This is read-only observability.  It intentionally omits the full feature
        vector and exposes only signal/outcome fields that are safe and useful to
        an operator.
        """
        wanted = str(ticker or "").strip().upper() or None
        try:
            limit = max(1, min(200, int(limit or 50)))
        except (TypeError, ValueError):
            limit = 50
        by_id: Dict[str, dict] = {}
        if self.path.exists():
            try:
                with self.path.open("r", newline="", encoding="utf-8-sig") as fh:
                    for row in csv.DictReader(fh):
                        tk = str(row.get("ticker", "") or "").upper()
                        if wanted and tk != wanted:
                            continue
                        oid = str(row.get("observation_id", "") or "")
                        if not oid:
                            continue
                        state = by_id.setdefault(oid, dict(row))
                        if str(row.get("event", "") or "").upper() == "RESOLVED":
                            state.update(row)
            except Exception:
                return []
        fields = [
            "observation_id", "signal_id", "timestamp", "resolved_utc",
            "ticker", "execution_mode", "tipo", "source_kind", "disposition",
            "reason", "ai_probability", "ai_threshold", "market_regime",
            "market_regime_confidence", "market_regime_would_block",
            "precio_entrada", "stop_loss", "take_profit", "bars_seen",
            "resolution", "precio_salida", "r_multiple", "ganadora",
            "training_eligible", "label_quality", "notes",
        ]
        rows = []
        for row in by_id.values():
            item = {key: row.get(key, "") for key in fields}
            item["status"] = "RESOLVED" if str(row.get("event", "") or "").upper() == "RESOLVED" else "OBSERVING"
            item["last_update"] = str(row.get("resolved_utc", "") or row.get("timestamp", "") or "")
            rows.append(item)
        rows.sort(key=lambda x: str(x.get("last_update", "") or ""), reverse=True)
        return rows[:limit]

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
        payload = {"version": self.VERSION, "updated_utc": datetime.now(timezone.utc).isoformat(), "active": self._active}
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
        # Preserve dedupe across restarts/resolutions. A resolved signal should
        # never reappear as a fresh counterfactual sample after a UI/worker retry.
        if self.path.exists():
            try:
                with self.path.open("r", newline="", encoding="utf-8-sig") as fh:
                    for row in csv.DictReader(fh):
                        signal_id = str(row.get("signal_id", "") or "").strip()
                        key = signal_id or "|".join((
                            str(row.get("ticker", "") or "").upper(),
                            str(row.get("tipo", "") or "").upper(),
                            str(row.get("timestamp", "") or ""),
                        ))
                        if key.strip("|"):
                            self._seen_keys.add(key)
            except Exception:
                pass
        if not self.enabled or not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
            active = raw.get("active", {}) if isinstance(raw, dict) else {}
            self._active = {
                str(oid): dict(row) for oid, row in active.items()
                if isinstance(row, dict) and row.get("ticker") and row.get("tipo") in {"BUY", "SELL"}
            }
            for row in self._active.values():
                key = str(row.get("signal_id", "") or "").strip() or str(row.get("dedupe_key", "") or "")
                if key:
                    self._seen_keys.add(key)
        except Exception:
            self._active = {}

