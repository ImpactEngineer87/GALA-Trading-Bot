"""Persistent per-ticker retraining lifecycle for Shadow AI models.

The lifecycle intentionally separates *collecting observations* from *running
training*.  A closed trade updates the ticker counter immediately, but automatic
training can be restricted to the end of the market session.  State is persisted
atomically so a VPS restart does not lose progress.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
import os
import re
import stat
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional

from agents.learning_dataset import summarize_closed_trades


STATE_SCHEMA_VERSION = 4
_PROCESS_TOKEN = uuid.uuid4().hex


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RetrainingLifecycle:
    """Track 30/50-style observation thresholds independently per ticker."""

    def __init__(
        self,
        *,
        feedback_path: str,
        state_path: str,
        shadow_models_dir: str,
        warning_samples: int = 30,
        retrain_samples: int = 50,
    ) -> None:
        self.feedback_path = feedback_path
        self.state_path = state_path
        self.shadow_models_dir = shadow_models_dir
        self.warning_samples = max(1, int(warning_samples))
        self.retrain_samples = max(self.warning_samples, int(retrain_samples))
        self._lock = threading.RLock()
        self._feedback_cache_signature = None
        self._feedback_cache_counts: Dict[str, int] = {}
        self._feedback_cache_quarantined: Dict[str, int] = {}
        self._feedback_cache_lifetime: Dict[str, int] = {}
        self._feedback_cache_sources: Dict[str, dict] = {}
        self._feedback_available = False
        self._feedback_integrity_schema = False
        self._loaded_state_schema_version = 1
        self._state: Dict[str, dict] = self._load_state()
        from core.runtime_store import EventHistory
        self.history = EventHistory(str(Path(state_path).with_suffix('.history.jsonl')))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def refresh(
        self,
        tickers: Optional[Iterable[str]] = None,
        *,
        persist: bool = True,
    ) -> Dict[str, dict]:
        """Refresh observation counters.

        Mutating lifecycle paths keep ``persist=True``. Dashboard/status callers
        can use ``persist=False`` so a stale in-memory reader never overwrites a
        newer retraining result written by another process.
        """
        counts = self._count_feedback_rows()
        names = {str(t).upper() for t in (tickers or []) if str(t).strip()}
        names.update(counts)
        with self._lock:
            for ticker in sorted(names):
                if self._feedback_available:
                    total_count = counts.get(ticker, 0)
                else:
                    total_count = int(
                        (self._state.get(ticker) or {}).get(
                            "total_observations", 0
                        )
                    )
                self._ensure_record(ticker, total_count)
            if persist:
                self._save_state_locked()
            return {ticker: dict(record) for ticker, record in self._state.items()}

    def register_closed(self, ticker: str, total_count: Optional[int] = None) -> dict:
        ticker = str(ticker or "").upper()
        if not ticker:
            return {}
        if total_count is None:
            counts = self._count_feedback_rows()
            if self._feedback_available:
                total_count = counts.get(ticker, 0)
            else:
                total_count = int(
                    (self._state.get(ticker) or {}).get(
                        "total_observations", 0
                    )
                )
        with self._lock:
            record = self._ensure_record(ticker, int(total_count))
            self._recompute_status(record)
            self._save_state_locked()
            return dict(record)

    def eligible_tickers(self, tickers: Iterable[str]) -> List[str]:
        self.refresh(tickers)
        with self._lock:
            return [
                ticker
                for ticker in dict.fromkeys(str(t).upper() for t in tickers or [])
                if ticker in self._state
                and int(self._state[ticker].get("new_observations", 0))
                >= self.retrain_samples
                and self._state[ticker].get("status") != "TRAINING"
            ]

    def mark_training(self, ticker: str, total_count: Optional[int] = None) -> dict:
        ticker = str(ticker or "").upper()
        if not ticker:
            return {}
        if total_count is None:
            counts = self._count_feedback_rows()
            total_count = (
                counts.get(ticker, 0) if self._feedback_available else
                int((self._state.get(ticker) or {}).get("total_observations", 0))
            )
        with self._lock:
            record = self._ensure_record(ticker, int(total_count))
            record["status"] = "TRAINING"
            record["pending"] = True
            record["training_started_utc"] = _utc_now()
            record["training_owner_pid"] = os.getpid()
            record["training_owner_token"] = _PROCESS_TOKEN
            self.history.append({'event': 'CANDIDATE_TRAINING', 'ticker': ticker,
                                 'total_observations': int(total_count)})
            self._save_state_locked()
            return dict(record)

    def mark_result(
        self,
        ticker: str,
        *,
        total_count: Optional[int] = None,
        accepted: bool,
        reason: str = "",
        metrics: Optional[dict] = None,
        error: str = "",
        consume_batch: bool = True,
        deferred: bool = False,
    ) -> dict:
        ticker = str(ticker or "").upper()
        if not ticker:
            return {}
        if total_count is None:
            counts = self._count_feedback_rows()
            total_count = (
                counts.get(ticker, 0) if self._feedback_available else
                int((self._state.get(ticker) or {}).get("total_observations", 0))
            )
        total_count = int(total_count)
        metrics = metrics or {}
        with self._lock:
            record = self._ensure_record(ticker, total_count)
            # A completed evaluation consumes the current batch so the exact same
            # 50 observations are not retrained every session. Infrastructure or
            # runtime errors deliberately keep the batch pending for a retry at
            # the next safe post-session window.
            if consume_batch and not deferred:
                record["last_attempt_count"] = total_count
            record["last_attempt_utc"] = _utc_now()
            record["last_result"] = (
                "WAITING_DATA" if deferred else
                ("ACCEPTED" if accepted else ("ERROR" if error else "REJECTED"))
            )
            record["last_reason"] = error or reason or ""
            record["training_started_utc"] = ""
            record["training_owner_pid"] = None
            record["training_owner_token"] = ""
            record["pending"] = False
            record["status"] = "COLLECTING"
            if accepted:
                record["last_success_count"] = total_count
                record["last_success_utc"] = record["last_attempt_utc"]
            # v3.2: keep all comparison values from ONE evaluation snapshot.
            # The old lifecycle only overwrote an incumbent when the new run had
            # one.  If a later run had no incumbent, the dashboard could combine
            # the new candidate BA with an older incumbent BA, producing a false
            # delta.  A real evaluation now atomically replaces the whole metric
            # snapshot (including explicit None values).
            has_evaluation = any(
                key in metrics for key in (
                    "evaluation_id", "balanced_accuracy", "decision_threshold",
                    "production_gate_passed", "shadow_candidate_accepted",
                    "economic_test", "economic_gate_passed",
                )
            )
            if has_evaluation:
                def _float_or_none(value):
                    try:
                        if value is None or value == "":
                            return None
                        return float(value)
                    except (TypeError, ValueError):
                        return None

                candidate = _float_or_none(metrics.get("balanced_accuracy"))
                shadow_incumbent = _float_or_none(
                    metrics.get("shadow_incumbent_balanced_accuracy")
                )
                production_incumbent = _float_or_none(
                    metrics.get(
                        "production_incumbent_balanced_accuracy",
                        metrics.get("incumbent_balanced_accuracy"),
                    )
                )

                record["last_evaluation_id"] = str(
                    metrics.get("evaluation_id", "") or ""
                )
                record["last_evaluation_utc"] = str(
                    metrics.get("evaluation_utc")
                    or metrics.get("entrenado_utc")
                    or record["last_attempt_utc"]
                    or ""
                )
                record["last_evaluation_test_start"] = str(
                    metrics.get("evaluation_test_start", "") or ""
                )
                record["last_evaluation_test_end"] = str(
                    metrics.get("evaluation_test_end", "") or ""
                )
                record["last_balanced_accuracy"] = candidate
                record["last_shadow_incumbent_balanced_accuracy"] = shadow_incumbent
                record["last_production_incumbent_balanced_accuracy"] = production_incumbent

                # Legacy fields remain available to older dashboards, but now
                # they are always sourced from this same evaluation.
                legacy_incumbent = (
                    shadow_incumbent
                    if shadow_incumbent is not None
                    else production_incumbent
                )
                record["last_incumbent_balanced_accuracy"] = legacy_incumbent

                record["last_shadow_delta_pp"] = (
                    round(candidate - shadow_incumbent, 2)
                    if candidate is not None and shadow_incumbent is not None
                    else None
                )
                record["last_production_delta_pp"] = (
                    round(candidate - production_incumbent, 2)
                    if candidate is not None and production_incumbent is not None
                    else None
                )
                record["last_balanced_delta_pp"] = (
                    record["last_shadow_delta_pp"]
                    if record["last_shadow_delta_pp"] is not None
                    else record["last_production_delta_pp"]
                )
                record["last_decision_threshold"] = _float_or_none(
                    metrics.get("decision_threshold")
                )
                record["last_validation_balanced_accuracy"] = _float_or_none(
                    metrics.get("validation_balanced_accuracy")
                )
                record["last_test_samples"] = int(metrics.get("muestras_test", 0) or 0)
                record["last_validation_samples"] = int(
                    metrics.get("muestras_validation", 0) or 0
                )
                record["last_fit_samples"] = int(metrics.get("muestras_fit", 0) or 0)
                record["last_shadow_candidate_accepted"] = (
                    bool(metrics.get("shadow_candidate_accepted"))
                    if "shadow_candidate_accepted" in metrics else None
                )
                record["last_production_gate_passed"] = (
                    bool(metrics.get("production_gate_passed"))
                    if "production_gate_passed" in metrics else None
                )
                gate_reasons = metrics.get("production_gate_reasons")
                record["last_production_gate_reasons"] = (
                    [str(item) for item in gate_reasons]
                    if isinstance(gate_reasons, (list, tuple)) else []
                )
                policy = metrics.get("production_gate_policy")
                record["last_production_gate_policy"] = (
                    dict(policy) if isinstance(policy, dict) else {}
                )
                # Keep source mix and economic evidence from this exact run.
                # Replacing the snapshot clears missing fields instead of mixing
                # a new classification score with an older profitability result.
                evaluation_fields = (
                    "evaluation_id", "evaluation_utc", "muestras_fit",
                    "muestras_fit_reales", "muestras_fit_suplementarias",
                    "validation_test_actual_only", "economic_test",
                    "economic_gate_passed", "economic_reasons",
                    "cost_label_integrity_passed",
                    "temporal_integrity_passed",
                )
                record["last_evaluation"] = {
                    key: metrics.get(key) for key in evaluation_fields
                }
                record["economic_test"] = (
                    dict(metrics["economic_test"])
                    if isinstance(metrics.get("economic_test"), dict) else {}
                )
                record["economic_gate_passed"] = metrics.get("economic_gate_passed")
                record["economic_reasons"] = list(metrics.get("economic_reasons") or [])
                record["temporal_integrity_passed"] = metrics.get("temporal_integrity_passed")
                record["muestras_fit_reales"] = int(metrics.get(
                    "muestras_fit_reales", metrics.get("muestras_fit", 0)
                ) or 0)
                record["muestras_fit_suplementarias"] = int(
                    metrics.get("muestras_fit_suplementarias", 0) or 0
                )
                record["validation_test_actual_only"] = metrics.get("validation_test_actual_only")

            if not deferred:
                record["last_model_saved"] = bool(
                    metrics.get("shadow_model_saved", accepted)
                )
            self._recompute_status(record)
            self.history.append({'event': 'CANDIDATE_EVALUATED', 'ticker': ticker,
                'result': record['last_result'], 'reason': record['last_reason'],
                'total_observations': total_count, 'metrics': metrics,
                'production_action': 'PENDING_MANUAL_REVIEW' if accepted else 'UNCHANGED'})
            self._save_state_locked()
            return dict(record)

    @staticmethod
    def _metric_percent(value) -> Optional[float]:
        """Normalize stored/reason metrics to dashboard percentage units."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        # Rejection reasons store balanced accuracy as a 0..1 fraction while
        # lifecycle/dashboard fields use 0..100 percentages.
        if -1.000001 <= number <= 1.000001:
            number *= 100.0
        return round(number, 2)

    @classmethod
    def _backfill_comparison_from_reason(cls, record: dict) -> None:
        """Populate legacy incumbent/delta fields for dashboard display only.

        Older lifecycle records persisted the candidate score and rejection text
        but not the incumbent score as separate fields.  The rejection text still
        contains the exact same-block comparison (for example,
        ``candidate 0.800 no mejora incumbente 0.900``).  Parse that historical
        value without rewriting the state file.  New retraining runs persist the
        explicit fields directly in :meth:`mark_result`.
        """
        incumbent = record.get("last_incumbent_balanced_accuracy")
        candidate = record.get("last_balanced_accuracy")

        if incumbent is None:
            reason = str(record.get("last_reason", "") or "")
            match = re.search(
                r"candidate\s+([0-9]+(?:\.[0-9]+)?)"
                r".*?incumb(?:ent|ente)\s+([0-9]+(?:\.[0-9]+)?)",
                reason,
                flags=re.IGNORECASE,
            )
            if match:
                if candidate is None:
                    candidate = cls._metric_percent(match.group(1))
                    record["last_balanced_accuracy"] = candidate
                incumbent = cls._metric_percent(match.group(2))
                record["last_incumbent_balanced_accuracy"] = incumbent

        candidate_pct = cls._metric_percent(candidate)
        incumbent_pct = cls._metric_percent(incumbent)
        if candidate_pct is not None and incumbent_pct is not None:
            record["last_balanced_delta_pp"] = round(
                candidate_pct - incumbent_pct, 2
            )

    def snapshot(
        self,
        tickers: Iterable[str],
        *,
        production_model_loaded: Optional[Callable[[str], bool]] = None,
        shadow_model_loaded: Optional[Callable[[str], bool]] = None,
    ) -> List[dict]:
        # Dashboard/status is a read-only path. Reload the newest persisted
        # lifecycle state first so a cached trainer instance cannot restore an
        # older PENDING_RETRAIN snapshot after another process completes training.
        with self._lock:
            if os.path.exists(self.state_path):
                self._state = self._load_state()

        # Recalculate display counters from the feedback CSV without persisting
        # them. Only register_closed/mark_training/mark_result (and explicit
        # mutating refresh callers) are allowed to write lifecycle state.
        self.refresh(tickers, persist=False)
        rows: List[dict] = []
        with self._lock:
            for ticker in dict.fromkeys(str(t).upper() for t in tickers or []):
                record = dict(self._state.get(ticker) or self._ensure_record(ticker, 0))
                self._backfill_comparison_from_reason(record)
                prod_loaded = bool(production_model_loaded(ticker)) if production_model_loaded else False
                shadow_loaded = bool(shadow_model_loaded(ticker)) if shadow_model_loaded else False
                record.update({
                    "ticker": ticker,
                    "warning_samples": self.warning_samples,
                    "retrain_samples": self.retrain_samples,
                    "production_model_loaded": prod_loaded,
                    "shadow_model_loaded": shadow_loaded,
                    "dashboard_model_status": (
                        "READY" if (shadow_loaded or prod_loaded) else "N/A"
                    ),
                    "eligible_observations": int(
                        record.get("total_observations", 0)
                    ),
                    "quarantined_observations": int(
                        self._feedback_cache_quarantined.get(
                            ticker,
                            record.get("quarantined_observations", 0),
                        )
                    ),
                    "lifetime_observations": int(
                        self._feedback_cache_lifetime.get(
                            ticker,
                            record.get("lifetime_observations", 0),
                        )
                    ),
                    "feedback_available": bool(self._feedback_available),
                    **{
                        key: int(self._feedback_cache_sources.get(ticker, {}).get(
                            key, record.get(key, 0)
                        )) for key in (
                            "eligible_closed_real", "eligible_closed_paper",
                            "eligible_closed_legacy", "duplicate_closed_rows",
                        )
                    },
                })
                rows.append(record)
        return rows

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def _load_state(self) -> Dict[str, dict]:
        if not os.path.exists(self.state_path):
            return {}
        try:
            with open(self.state_path, "r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                try:
                    self._loaded_state_schema_version = int(
                        payload.get("schema_version", 1)
                    )
                except (TypeError, ValueError):
                    self._loaded_state_schema_version = 1
            raw = payload.get("tickers", payload) if isinstance(payload, dict) else {}
            return {
                str(ticker).upper(): dict(record)
                for ticker, record in raw.items()
                if isinstance(record, dict)
            }
        except Exception:
            return {}

    def _save_state_locked(self) -> None:
        folder = os.path.dirname(self.state_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "updated_utc": _utc_now(),
            "warning_samples": self.warning_samples,
            "retrain_samples": self.retrain_samples,
            "tickers": self._state,
        }
        try:
            existing_mode = stat.S_IMODE(os.stat(self.state_path).st_mode)
        except FileNotFoundError:
            existing_mode = None
        # Each trainer has its own lifecycle lock. A shared .tmp filename lets
        # another instance replace (and consume) our file before we can use it.
        # A unique sibling keeps replacement atomic on the same filesystem.
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=folder or ".",
                prefix=f".{os.path.basename(self.state_path)}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp = handle.name
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            if existing_mode is not None:
                os.chmod(tmp, existing_mode)
            os.replace(tmp, self.state_path)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass

    def _ensure_record(self, ticker: str, total_count: int) -> dict:
        ticker = str(ticker).upper()
        total_count = max(0, int(total_count))
        record = self._state.get(ticker)
        if record is None:
            metadata_samples = self._shadow_metadata_samples(ticker)
            # An integrity-aware v2.9 CSV already excludes every legacy row, so
            # all execution_valid=1 rows are genuine eligible observations and
            # must survive a deleted/missing state file.  Legacy CSVs without the
            # integrity column keep the original installation-baseline behavior.
            baseline = 0 if self._feedback_integrity_schema else total_count
            record = {
                "total_observations": total_count,
                "last_attempt_count": baseline,
                "last_success_count": baseline,
                "new_observations": max(0, total_count - baseline),
                "pending": False,
                "status": "COLLECTING",
                "last_attempt_utc": "",
                "last_success_utc": "",
                "training_started_utc": "",
                "last_result": "",
                "last_reason": "",
                "last_balanced_accuracy": None,
                "last_incumbent_balanced_accuracy": None,
                "last_balanced_delta_pp": None,
                "last_evaluation_id": "",
                "last_evaluation_utc": "",
                "last_evaluation_test_start": "",
                "last_evaluation_test_end": "",
                "last_shadow_incumbent_balanced_accuracy": None,
                "last_production_incumbent_balanced_accuracy": None,
                "last_shadow_delta_pp": None,
                "last_production_delta_pp": None,
                "last_decision_threshold": None,
                "last_validation_balanced_accuracy": None,
                "last_test_samples": 0,
                "last_validation_samples": 0,
                "last_fit_samples": 0,
                "last_shadow_candidate_accepted": None,
                "last_production_gate_passed": None,
                "last_production_gate_reasons": [],
                "last_production_gate_policy": {},
                "last_evaluation": {},
                "economic_test": {},
                "economic_gate_passed": None,
                "economic_reasons": [],
                "temporal_integrity_passed": None,
                "muestras_fit_reales": 0,
                "muestras_fit_suplementarias": 0,
                "validation_test_actual_only": None,
                "training_owner_pid": None,
                "training_owner_token": "",
                "last_model_saved": bool(metadata_samples is not None),
                "counting_basis": (
                    "execution_valid" if self._feedback_integrity_schema
                    else "legacy_all_rows"
                ),
            }
            self._state[ticker] = record
        else:
            prior_basis = str(record.get("counting_basis", "") or "")
            last_attempt = max(0, int(record.get("last_attempt_count", 0)))
            last_attempt_utc = str(record.get("last_attempt_utc", "") or "")
            # v2.9 quarantined the old 13k-19k per-ticker rows but retained the
            # v2.1 counters.  That mixed two incompatible counting bases and held
            # progress at 0/50.  Rebase exactly once to the eligible-row epoch.
            # If a post-v2.9 attempt is already plausible (timestamp present and
            # its count fits inside the eligible total), retain that valid epoch.
            needs_integrity_rebase = (
                self._feedback_integrity_schema
                and prior_basis != "execution_valid"
                and (last_attempt > total_count or not last_attempt_utc)
            )
            if needs_integrity_rebase:
                record["legacy_last_attempt_count"] = last_attempt
                record["legacy_last_success_count"] = max(
                    0, int(record.get("last_success_count", 0))
                )
                record["last_attempt_count"] = 0
                record["last_success_count"] = 0
                record["counter_rebased_utc"] = _utc_now()
                record["counter_rebase_reason"] = (
                    "V2_9_EXECUTION_VALID_EPOCH"
                )
            elif self._feedback_available and last_attempt > total_count:
                # Identity deduplication can reduce an old raw-row counter.
                # Keep completed work consumed while preventing progress from
                # being stuck below a counter that no longer exists.
                record["legacy_last_attempt_count"] = last_attempt
                record["last_attempt_count"] = total_count
                record["last_success_count"] = min(
                    total_count, max(0, int(record.get("last_success_count", 0)))
                )
                record["counter_rebased_utc"] = _utc_now()
                record["counter_rebase_reason"] = "UNIQUE_ELIGIBLE_CLOSES"
            record["total_observations"] = total_count
            record.setdefault("last_attempt_count", total_count)
            record.setdefault("last_success_count", record["last_attempt_count"])
            record.setdefault("last_attempt_utc", "")
            record.setdefault("last_success_utc", "")
            record.setdefault("training_started_utc", "")
            record.setdefault("last_result", "")
            record.setdefault("last_reason", "")
            record.setdefault("last_balanced_accuracy", None)
            record.setdefault("last_incumbent_balanced_accuracy", None)
            record.setdefault("last_balanced_delta_pp", None)
            record.setdefault("last_evaluation_id", "")
            record.setdefault("last_evaluation_utc", "")
            record.setdefault("last_evaluation_test_start", "")
            record.setdefault("last_evaluation_test_end", "")
            record.setdefault("last_shadow_incumbent_balanced_accuracy", None)
            record.setdefault("last_production_incumbent_balanced_accuracy", None)
            record.setdefault("last_shadow_delta_pp", None)
            record.setdefault("last_production_delta_pp", None)
            record.setdefault("last_decision_threshold", None)
            record.setdefault("last_validation_balanced_accuracy", None)
            record.setdefault("last_test_samples", 0)
            record.setdefault("last_validation_samples", 0)
            record.setdefault("last_fit_samples", 0)
            record.setdefault("last_shadow_candidate_accepted", None)
            record.setdefault("last_production_gate_passed", None)
            record.setdefault("last_production_gate_reasons", [])
            record.setdefault("last_production_gate_policy", {})
            for key, default in (
                ("last_evaluation", {}), ("economic_test", {}),
                ("economic_gate_passed", None), ("economic_reasons", []),
                ("temporal_integrity_passed", None), ("muestras_fit_reales", 0),
                ("muestras_fit_suplementarias", 0), ("validation_test_actual_only", None),
                ("training_owner_pid", None), ("training_owner_token", ""),
            ):
                record.setdefault(key, default)
            record.setdefault("last_model_saved", False)
            record["counting_basis"] = (
                "execution_valid" if self._feedback_integrity_schema
                else (prior_basis or "legacy_all_rows")
            )
        record["quarantined_observations"] = int(
            self._feedback_cache_quarantined.get(
                ticker, record.get("quarantined_observations", 0)
            )
        )
        record["lifetime_observations"] = int(
            self._feedback_cache_lifetime.get(
                ticker,
                max(
                    total_count,
                    int(record.get("lifetime_observations", 0)),
                ),
            )
        )
        for key in (
            "eligible_closed_real", "eligible_closed_paper",
            "eligible_closed_legacy", "duplicate_closed_rows",
        ):
            record[key] = int(self._feedback_cache_sources.get(ticker, {}).get(
                key, record.get(key, 0)
            ))
        self._recover_interrupted_training(record)
        self._recompute_status(record)
        return record

    @staticmethod
    def _recover_interrupted_training(record: dict) -> None:
        """Release a persisted job only when its owner is demonstrably gone.

        Dashboard reads never write this recovery. A live training process keeps
        ownership even if its job takes longer than expected.
        """
        if record.get("status") != "TRAINING":
            return
        try:
            owner_pid = int(record.get("training_owner_pid") or 0)
        except (TypeError, ValueError):
            owner_pid = 0
        owner_token = str(record.get("training_owner_token", "") or "")
        interrupted = False
        if owner_pid == os.getpid() and owner_token:
            interrupted = owner_token != _PROCESS_TOKEN
        elif owner_pid > 0 and os.name == "posix":
            try:
                os.kill(owner_pid, 0)
            except ProcessLookupError:
                interrupted = True
            except PermissionError:
                return
            except OSError:
                return
        elif owner_pid > 0:
            # On Windows os.kill(pid, 0) can terminate the target process.
            # Recover conservatively only from explicit process evidence.
            return
        else:
            # Older builds never recorded ownership. Wait six hours before
            # treating an unowned legacy job as abandoned.
            try:
                started = datetime.fromisoformat(str(record.get("training_started_utc", "")))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                interrupted = (datetime.now(timezone.utc) - started).total_seconds() >= 6 * 3600
            except (TypeError, ValueError):
                interrupted = False
        if interrupted:
            record["status"] = "COLLECTING"
            record["pending"] = False
            record["training_started_utc"] = ""
            record["training_owner_pid"] = None
            record["training_owner_token"] = ""
            record["last_result"] = "INTERRUPTED"
            record["last_reason"] = "Previous training process exited; observations remain pending"
            record["training_recovered_utc"] = _utc_now()

    def _recompute_status(self, record: dict) -> None:
        total = max(0, int(record.get("total_observations", 0)))
        last_attempt = max(0, int(record.get("last_attempt_count", 0)))
        new_count = max(0, total - last_attempt)
        record["new_observations"] = new_count
        if record.get("status") == "TRAINING":
            record["pending"] = True
            return
        if new_count >= self.retrain_samples:
            record["status"] = "PENDING_RETRAIN"
            record["pending"] = True
        elif new_count >= self.warning_samples:
            record["status"] = "READY_REVIEW"
            record["pending"] = False
        else:
            record["status"] = "COLLECTING"
            record["pending"] = False

    def _shadow_metadata_samples(self, ticker: str) -> Optional[int]:
        path = os.path.join(self.shadow_models_dir, f"modelo_{ticker}.meta.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                meta = json.load(handle)
            return int(meta.get("muestras", 0))
        except Exception:
            return None

    def _count_feedback_rows(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        quarantined: Dict[str, int] = {}
        lifetime: Dict[str, int] = {}
        if not os.path.exists(self.feedback_path):
            self._feedback_available = False
            return counts
        try:
            stat = os.stat(self.feedback_path)
            signature = (stat.st_mtime_ns, stat.st_size)
            with self._lock:
                if signature == self._feedback_cache_signature:
                    self._feedback_available = True
                    return dict(self._feedback_cache_counts)
            with open(self.feedback_path, "r", newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                self._feedback_integrity_schema = bool(
                    reader.fieldnames and "execution_valid" in reader.fieldnames
                )
                sources = summarize_closed_trades(list(reader), fieldnames=reader.fieldnames)
                counts = {ticker: row["eligible_observations"] for ticker, row in sources.items()}
                quarantined = {ticker: row["quarantined_observations"] for ticker, row in sources.items()}
                lifetime = {ticker: row["lifetime_observations"] for ticker, row in sources.items()}
            with self._lock:
                self._feedback_cache_signature = signature
                self._feedback_cache_counts = dict(counts)
                self._feedback_cache_quarantined = dict(quarantined)
                self._feedback_cache_lifetime = dict(lifetime)
                self._feedback_cache_sources = dict(sources)
                self._feedback_available = True
        except Exception:
            self._feedback_available = False
            return dict(self._feedback_cache_counts)
        return counts
