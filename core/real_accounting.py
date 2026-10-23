"""Durable entry block for incomplete REAL execution accounting.

No order actions, automatic reset, evidence repair or model promotion. Existing
positions remain managed by the engine's broker reconciliation and protections.
"""
import csv
import threading
from pathlib import Path

from core.dashboard_evidence import annotate_history, mode_of, session_date
from core.runtime_store import JsonState, utc_now


class RealAccountingGuard:
    def __init__(self, path):
        self.store = JsonState(path)
        self._lock = threading.RLock()
        self._failure = ""

    def require_reconciliation(self, key, **evidence):
        with self._lock:
            try:
                rows = self.store.get("unresolved") or {}
                if not isinstance(rows, dict):
                    raise ValueError("Invalid accounting state")
                rows[str(key)] = {"recorded_utc": utc_now(), **evidence}
                self.store.put("unresolved", rows)
            except Exception:
                # Disk failure cannot allow the next entry in this process.
                self._failure = "Accounting state could not be persisted"

    def inspect_session(self, history_path, audit_path, date_ny):
        """Check the current risk baseline on startup without altering old ledgers."""
        try:
            history = Path(history_path)
            if not history.exists():
                return
            with history.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                fields = set(reader.fieldnames or [])
                if not {"modo", "ticker", "pnl_usd"} <= fields or not fields & {"fecha", "timestamp_utc"}:
                    raise ValueError("Unknown history schema")
                rows = [{**r, "evento": r.get("evento") or "POSITION_CLOSE"}
                        for r in reader if mode_of(r) == "REAL"
                        and session_date(r) == date_ny
                        and (r.get("evento") or "POSITION_CLOSE") == "POSITION_CLOSE"]
            if not rows:
                return
            audit = Path(audit_path)
            if audit.exists():
                with audit.open(encoding="utf-8-sig", newline="") as handle:
                    checked = annotate_history(rows, csv.DictReader(handle))
            else:
                checked = annotate_history(rows)
            for i, row in enumerate(checked):
                if not row["performance_verified"]:
                    self.require_reconciliation(
                        "history:" + date_ny + ":" + str(row.get("history_id") or row.get("position_id") or i),
                        ticker=row.get("ticker"), session_ny=date_ny,
                        reason=row["performance_exclusion_reason"],
                    )
        except Exception:
            self._failure = "Current-session REAL accounting could not be checked"

    def complete_exit_progress(self, key, *, position_id, execution):
        """Resolve only our own partial-exit intent, with complete exact fills.

        This is deliberately NOT an operator reset. Historical/quarantined
        accounting records, different positions, or incomplete legs stay blocked.
        The engine calls this only after recording the final verified P/L.
        """
        from core.execution_v2 import SmartExit
        with self._lock:
            try:
                rows = self.store.get('unresolved') or {}
                row = rows.get(str(key))
                if row is None:
                    return False
                if (row.get('kind') != 'NORMAL_EXIT_PROGRESS'
                        or not position_id or row.get('position_id') != position_id
                        or execution.get('position_id') != position_id
                        or not row.get('exit_execution_id')
                        or str(key) != 'pending_exit:' + str(row.get('exit_execution_id'))
                        or row.get('exit_execution_id') != execution.get('execution_id')
                        or row.get('quantity') != execution.get('quantity')
                        or SmartExit._verified_fill_legs(execution) is None):
                    return False
                final = {leg['order_id']: leg for leg in execution['legs']}
                for old in row.get('legs', []):
                    leg = final.get(old.get('order_id'))
                    if (leg is None or leg.get('filled') != old.get('filled')
                            or leg.get('avg_price') != old.get('avg_price')):
                        return False
                # Preserve the proof before removing this single transient key.
                resolved = self.store.get('resolved_exit_progress') or {}
                resolved[str(key)] = {**row, 'completed_utc': utc_now(),
                                     'complete_execution': execution}
                self.store.put('resolved_exit_progress', resolved)
                del rows[str(key)]
                self.store.put('unresolved', rows)
                return True
            except Exception:
                self._failure = 'Partial-exit reconciliation could not be persisted'
                return False

    def snapshot(self):
        with self._lock:
            try:
                rows = self.store.get("unresolved") or {}
                if not isinstance(rows, dict) or any(not isinstance(r, dict) for r in rows.values()):
                    raise ValueError("Invalid accounting state")
                reasons = sorted({str(r.get("reason") or "Execution accounting incomplete") for r in rows.values()})
                tickers = sorted({str(r.get("ticker") or "") for r in rows.values()} - {""})
            except Exception:
                rows, reasons, tickers = {}, [], []
                self._failure = "Accounting state unavailable or invalid"
            if self._failure:
                reasons.append(self._failure)
            return {"blocked": bool(rows or self._failure), "unresolved": len(rows),
                    "tickers": tickers, "reasons": reasons,
                    "automatic_reset": False}
