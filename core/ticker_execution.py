"""Persistent per-ticker execution routing for GALA10 v2.9.2.

The registry deliberately lives outside ``config.yaml`` so switching a ticker
between PAPER and REAL never touches model files, feedback history, or
``data/retraining_state.json``.  Writes are atomic and reads reload from disk,
so the dashboard and engine cannot overwrite each other with stale state.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional


VALID_MODES = {"PAPER", "REAL"}


def normalize_share_count(value) -> int:
    """Accept whole shares without silently truncating fractions or booleans."""
    try:
        if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
            raise ValueError
        shares = int(value)
        if shares < 1:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError("shares debe ser un entero positivo (>= 1)") from None
    return shares


def _ticker(value: str) -> str:
    return str(value or "").strip().upper()


class TickerExecutionRegistry:
    VERSION = "2.9.2"

    def __init__(
        self,
        path: str = "data/ticker_execution.json",
        *,
        default_mode: str = "PAPER",
        default_shares: int = 1,
        initial_real_tickers=None,
    ):
        self.path = Path(path)
        self.default_mode = self._normalize_mode(default_mode)
        self.default_shares = max(1, int(default_shares or 1))
        from core.runtime_store import path_lock
        self._lock = path_lock(path)
        self.initial_real_tickers = {_ticker(t) for t in (initial_real_tickers or [])}

    @staticmethod
    def _normalize_mode(mode: str) -> str:
        value = str(mode or "PAPER").strip().upper()
        if value not in VALID_MODES:
            raise ValueError("mode debe ser PAPER o REAL")
        return value

    @staticmethod
    def _normalize_shares(shares: int) -> int:
        return normalize_share_count(shares)

    def _empty(self) -> dict:
        return {
            "version": self.VERSION,
            "updated_utc": "",
            "tickers": {},
        }

    def _load_locked(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            with self.path.open("r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
        except Exception:
            return self._empty()
        if not isinstance(raw, dict):
            return self._empty()
        tickers = raw.get("tickers")
        if not isinstance(tickers, dict):
            # Accept the earliest flat mapping used during development.
            tickers = raw if all(isinstance(v, dict) for v in raw.values()) else {}
        clean = self._empty()
        clean["updated_utc"] = str(raw.get("updated_utc", "") or "")
        for key, value in tickers.items():
            tk = _ticker(key)
            if not tk or not isinstance(value, dict):
                continue
            try:
                mode = self._normalize_mode(value.get("mode", self.default_mode))
                shares = self._normalize_shares(
                    value.get("shares", self.default_shares)
                )
            except (TypeError, ValueError):
                continue
            clean["tickers"][tk] = {
                "mode": mode,
                "shares": shares,
                "updated_utc": str(value.get("updated_utc", "") or ""),
            }
        return clean

    def _save_locked(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state = dict(state)
        state["version"] = self.VERSION
        state["updated_utc"] = datetime.now(timezone.utc).isoformat()
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, self.path)

    def ensure_tickers(
        self,
        tickers: Iterable[str],
        *,
        persist: bool = True,
    ) -> List[dict]:
        with self._lock:
            state = self._load_locked()
            changed = False
            for value in tickers or []:
                tk = _ticker(value)
                if not tk:
                    continue
                if tk not in state["tickers"]:
                    state["tickers"][tk] = {
                        "mode": "REAL" if tk in self.initial_real_tickers else self.default_mode,
                        "shares": self.default_shares,
                        "updated_utc": "",
                    }
                    changed = True
            if changed and persist:
                self._save_locked(state)
            return self._rows_from_state(state, tickers)

    def _rows_from_state(self, state: dict, tickers: Optional[Iterable[str]]) -> List[dict]:
        if tickers is None:
            ordered = sorted(state.get("tickers", {}).keys())
        else:
            ordered = []
            seen = set()
            for value in tickers:
                tk = _ticker(value)
                if tk and tk not in seen:
                    ordered.append(tk)
                    seen.add(tk)
        rows = []
        mapping = state.get("tickers", {})
        for tk in ordered:
            rec = mapping.get(tk) or {
                "mode": "REAL" if tk in self.initial_real_tickers else self.default_mode,
                "shares": self.default_shares,
                "updated_utc": "",
            }
            rows.append({
                "ticker": tk,
                "mode": self._normalize_mode(rec.get("mode", self.default_mode)),
                "shares": self._normalize_shares(rec.get("shares", self.default_shares)),
                "updated_utc": str(rec.get("updated_utc", "") or ""),
            })
        return rows

    def snapshot(self, tickers: Optional[Iterable[str]] = None) -> List[dict]:
        """Read-only fresh snapshot.  Never writes merely because the UI polled."""
        with self._lock:
            state = self._load_locked()
            return self._rows_from_state(state, tickers)

    def get(self, ticker: str) -> dict:
        tk = _ticker(ticker)
        if not tk:
            raise ValueError("ticker vacío")
        rows = self.snapshot([tk])
        return rows[0]

    def mode(self, ticker: str) -> str:
        return self.get(ticker)["mode"]

    def shares(self, ticker: str) -> int:
        return int(self.get(ticker)["shares"])

    def real_tickers(self, tickers: Iterable[str]) -> List[str]:
        return [row["ticker"] for row in self.snapshot(tickers) if row["mode"] == "REAL"]

    def update(
        self,
        ticker: str,
        *,
        mode: Optional[str] = None,
        shares: Optional[int] = None,
    ) -> dict:
        tk = _ticker(ticker)
        if not tk:
            raise ValueError("ticker vacío")
        with self._lock:
            state = self._load_locked()
            current = dict(state["tickers"].get(tk) or {
                "mode": self.default_mode,
                "shares": self.default_shares,
            })
            if mode is not None:
                current["mode"] = self._normalize_mode(mode)
            else:
                current["mode"] = self._normalize_mode(
                    current.get("mode", self.default_mode)
                )
            if shares is not None:
                current["shares"] = self._normalize_shares(shares)
            else:
                current["shares"] = self._normalize_shares(
                    current.get("shares", self.default_shares)
                )
            current["updated_utc"] = datetime.now(timezone.utc).isoformat()
            state["tickers"][tk] = current
            self._save_locked(state)
            return {
                "ticker": tk,
                "mode": current["mode"],
                "shares": current["shares"],
                "updated_utc": current["updated_utc"],
            }
