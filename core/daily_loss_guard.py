"""Persistent daily net-loss protection for the trading engine.

The guard stores session state in JSON so a triggered daily block survives a
process/VPS restart.  Limits are mode-specific, but a trigger blocks the whole
engine until the next New York calendar session.
"""

from __future__ import annotations

import csv
import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Optional


def _now_new_york() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now()


def _mode_name(paper_trading: bool) -> str:
    return "PAPER" if bool(paper_trading) else "REAL"


class DailyLossGuard:
    """Mode-aware, restart-persistent daily loss guard."""

    STATE_VERSION = 2

    def __init__(
        self,
        config: dict,
        history_path: str = "data/historial_operaciones.csv",
        now_provider: Optional[Callable[[], datetime]] = None,
    ):
        ks = (config.get("kill_switch", {}) or {}).get("daily_loss", {}) or {}
        self.enabled = bool(ks.get("enabled", True))
        self.paper_limit = -abs(float(ks.get("paper_limit_usd", 250.0)))
        self.live_limit = -abs(float(ks.get("live_limit_usd", 200.0)))
        self.include_unrealized = bool(ks.get("include_unrealized", True))
        self.state_path = Path(
            ks.get("state_file", "data/daily_kill_switch_state.json")
        )
        self.history_path = Path(history_path)
        self._now = now_provider or _now_new_york
        self._lock = threading.RLock()
        self._state: Dict[str, object] = {}
        self._load_or_initialize()

    @staticmethod
    def mode_name(paper_trading: bool) -> str:
        return _mode_name(paper_trading)

    def _session_date(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    @staticmethod
    def _is_trading_day(value: datetime) -> bool:
        # Schwab's calendar is authoritative for order routing.  The persistent
        # guard only needs the conservative rule here: never clear a Friday lock
        # just because the VPS crossed midnight into Saturday/Sunday.
        return value.weekday() < 5

    def _should_reset(self, stored_date: str) -> bool:
        now = self._now()
        return bool(stored_date != now.strftime("%Y-%m-%d") and self._is_trading_day(now))

    def next_reset_at_ny(self) -> str:
        now = self._now()
        candidate = now + timedelta(days=1)
        while not self._is_trading_day(candidate):
            candidate += timedelta(days=1)
        return candidate.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    def _default_state(self, session_date: str) -> Dict[str, object]:
        realized = self._history_realized_for_date(session_date)
        return {
            "version": self.STATE_VERSION,
            "session_date": session_date,
            "blocked": False,
            "triggered_at_ny": "",
            "trigger_mode": "",
            "trigger_net_pnl": 0.0,
            "trigger_unrealized_pnl": 0.0,
            "trigger_limit_usd": 0.0,
            "reason": "",
            "realized_pnl": {
                "PAPER": round(float(realized.get("PAPER", 0.0)), 6),
                "REAL": round(float(realized.get("REAL", 0.0)), 6),
            },
        }

    def _history_realized_for_date(self, session_date: str) -> Dict[str, float]:
        totals = {"PAPER": 0.0, "REAL": 0.0}
        if not self.history_path.exists():
            return totals
        try:
            with self.history_path.open(
                "r", encoding="utf-8-sig", newline=""
            ) as fh:
                for row in csv.DictReader(fh):
                    if str(row.get("fecha", "")) != session_date:
                        continue
                    if str(
                        row.get("evento", "POSITION_CLOSE") or "POSITION_CLOSE"
                    ).upper() != "POSITION_CLOSE":
                        continue
                    mode = str(row.get("modo", "PAPER") or "PAPER").upper()
                    if mode not in totals:
                        continue
                    try:
                        totals[mode] += float(row.get("pnl_usd", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        continue
        except Exception:
            # A damaged history must not disable the guard.  It starts from zero
            # and new realized events will continue to be persisted in state.
            return {"PAPER": 0.0, "REAL": 0.0}
        return totals

    def _load_or_initialize(self) -> None:
        today = self._session_date()
        loaded: Dict[str, object] = {}
        try:
            if self.state_path.exists():
                with self.state_path.open("r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    loaded = raw
        except Exception:
            loaded = {}

        stored_date = str(loaded.get("session_date", "") or "")
        if not stored_date or self._should_reset(stored_date):
            self._state = self._default_state(today)
            self._persist_locked()
            return

        realized = loaded.get("realized_pnl")
        if not isinstance(realized, dict):
            realized = {"PAPER": 0.0, "REAL": 0.0}
        self._state = {
            "version": self.STATE_VERSION,
            "session_date": stored_date,
            "blocked": bool(loaded.get("blocked", False)),
            "triggered_at_ny": str(loaded.get("triggered_at_ny", "") or ""),
            "trigger_mode": str(loaded.get("trigger_mode", "") or ""),
            "trigger_net_pnl": float(loaded.get("trigger_net_pnl", 0.0) or 0.0),
            "trigger_unrealized_pnl": float(
                loaded.get("trigger_unrealized_pnl", 0.0) or 0.0
            ),
            "trigger_limit_usd": float(
                loaded.get("trigger_limit_usd", 0.0) or 0.0
            ),
            "reason": str(loaded.get("reason", "") or ""),
            "realized_pnl": {
                "PAPER": float(realized.get("PAPER", 0.0) or 0.0),
                "REAL": float(realized.get("REAL", 0.0) or 0.0),
            },
        }

    def _persist_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self._state, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, self.state_path)

    def ensure_session(self) -> bool:
        """Reset state when the New York session date changes.

        Returns True when a reset occurred.
        """
        with self._lock:
            today = self._session_date()
            if not self._should_reset(str(self._state.get("session_date", "") or "")):
                return False
            self._state = self._default_state(today)
            self._persist_locked()
            return True

    def limit_for(self, paper_trading: bool) -> float:
        return self.paper_limit if bool(paper_trading) else self.live_limit

    def is_blocked(self) -> bool:
        with self._lock:
            self.ensure_session()
            return bool(self._state.get("blocked", False))

    def realized_pnl(self, paper_trading: bool) -> float:
        mode = _mode_name(paper_trading)
        with self._lock:
            self.ensure_session()
            realized = self._state.get("realized_pnl") or {}
            return float(realized.get(mode, 0.0) or 0.0)

    def add_realized(self, paper_trading: bool, amount: float) -> float:
        mode = _mode_name(paper_trading)
        with self._lock:
            self.ensure_session()
            realized = self._state.setdefault(
                "realized_pnl", {"PAPER": 0.0, "REAL": 0.0}
            )
            total = float(realized.get(mode, 0.0) or 0.0) + float(amount or 0.0)
            realized[mode] = round(total, 6)
            self._persist_locked()
            return float(realized[mode])

    def mark_triggered(
        self,
        paper_trading: bool,
        net_pnl: float,
        unrealized_pnl: float,
        reason: str,
    ) -> bool:
        """Persist the block before liquidation starts.

        Returns True only for the first trigger in the session.
        """
        with self._lock:
            self.ensure_session()
            if bool(self._state.get("blocked", False)):
                return False
            now = self._now()
            self._state.update(
                {
                    "blocked": True,
                    "triggered_at_ny": now.isoformat(timespec="seconds"),
                    "trigger_mode": _mode_name(paper_trading),
                    "trigger_net_pnl": round(float(net_pnl), 6),
                    "trigger_unrealized_pnl": round(float(unrealized_pnl), 6),
                    "trigger_limit_usd": round(self.limit_for(paper_trading), 6),
                    "reason": str(reason or "daily loss limit"),
                }
            )
            self._persist_locked()
            return True

    def book_status(
        self,
        paper_trading: bool,
        *,
        unrealized_pnl: float = 0.0,
        priced_positions: int = 0,
        unpriced_positions: int = 0,
    ) -> Dict[str, object]:
        """Live net PnL of one execution book against its own limit.

        Unlike ``snapshot`` this never freezes on the trigger values, so a
        MIXED session can evaluate the PAPER and REAL books independently.
        """
        with self._lock:
            self.ensure_session()
            mode = _mode_name(paper_trading)
            realized = float(
                (self._state.get("realized_pnl") or {}).get(mode, 0.0) or 0.0
            )
            unrealized = (
                float(unrealized_pnl or 0.0) if self.include_unrealized else 0.0
            )
            net = round(realized + unrealized, 2)
            limit = round(self.limit_for(paper_trading), 2)
            return {
                "mode": mode,
                "limit_usd": limit,
                "realized_pnl": round(realized, 2),
                "unrealized_pnl": round(unrealized, 2),
                "net_pnl": net,
                "remaining_to_limit_usd": round(max(0.0, net - limit), 2),
                "breached": bool(self.enabled and net <= limit),
                "priced_positions": int(priced_positions),
                "unpriced_positions": int(unpriced_positions),
            }

    def snapshot(
        self,
        paper_trading: bool,
        *,
        unrealized_pnl: float = 0.0,
        priced_positions: int = 0,
        unpriced_positions: int = 0,
    ) -> Dict[str, object]:
        with self._lock:
            self.ensure_session()
            mode = _mode_name(paper_trading)
            realized = float(
                (self._state.get("realized_pnl") or {}).get(mode, 0.0) or 0.0
            )
            unrealized = (
                float(unrealized_pnl or 0.0) if self.include_unrealized else 0.0
            )
            net = realized + unrealized
            limit = self.limit_for(paper_trading)
            blocked = bool(self._state.get("blocked", False))
            trigger_mode = str(self._state.get("trigger_mode", "") or "")
            trigger_net = float(self._state.get("trigger_net_pnl", 0.0) or 0.0)
            trigger_unrealized = float(
                self._state.get("trigger_unrealized_pnl", 0.0) or 0.0
            )
            trigger_limit = float(
                self._state.get("trigger_limit_usd", 0.0) or 0.0
            )
            if blocked and not trigger_limit:
                trigger_limit = self.paper_limit if trigger_mode == "PAPER" else self.live_limit

            # Once blocked, the primary fields describe the trigger itself.  The
            # old dashboard combined the current PAPER totals with a REAL trigger
            # reason, producing contradictory -$4,975 / -$700 / -$857 values.
            display_mode = trigger_mode or mode if blocked else mode
            display_net = trigger_net if blocked else net
            display_limit = trigger_limit if blocked else limit
            display_unrealized = trigger_unrealized if blocked else unrealized
            display_realized = display_net - display_unrealized if blocked else realized
            return {
                "enabled": self.enabled,
                "session_date": self._state.get("session_date", ""),
                "mode": display_mode,
                "limit_usd": round(display_limit, 2),
                "realized_pnl": round(display_realized, 2),
                "unrealized_pnl": round(display_unrealized, 2),
                "net_pnl": round(display_net, 2),
                "remaining_to_limit_usd": round(max(0.0, display_net - display_limit), 2),
                "blocked": blocked,
                "current_mode": mode,
                "current_limit_usd": round(limit, 2),
                "current_realized_pnl": round(realized, 2),
                "current_unrealized_pnl": round(unrealized, 2),
                "current_net_pnl": round(net, 2),
                "triggered_at_ny": self._state.get("triggered_at_ny", ""),
                "trigger_mode": trigger_mode,
                "trigger_net_pnl": round(trigger_net, 2),
                "trigger_unrealized_pnl": round(trigger_unrealized, 2),
                "trigger_limit_usd": round(trigger_limit, 2),
                "next_reset_at_ny": self.next_reset_at_ny() if blocked else "",
                "reason": self._state.get("reason", ""),
                "include_unrealized": self.include_unrealized,
                "priced_positions": int(priced_positions),
                "unpriced_positions": int(unpriced_positions),
            }
