# -*- coding: utf-8 -*-
"""Guarded Shadow -> PRODUCTION promotion helpers for explicit REAL switches.

v3.0.7 adds an explicit *manual override* path.  The normal promotion path is
unchanged and still requires both ``shadow_candidate_accepted`` and
``production_gate_passed``.  The override path may ignore only those model-gate
flags; it never ignores runtime compatibility, schema, feature, threshold, or
model-load validation.
"""
from __future__ import annotations

import json
import os
import pickle
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional


class ModelPromotionError(RuntimeError):
    """Raised when a Shadow candidate cannot be safely promoted."""


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ModelPromotionError("metadata Shadow no es un objeto JSON válido")
    return payload


def _public(info: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in info.items() if not str(key).startswith("_")}


def production_artifact_state(trainer, ticker: str) -> Dict[str, Any]:
    ticker = str(ticker or "").strip().upper()
    model_path = str(trainer._ruta_modelo(ticker))
    meta_path = str(trainer._ruta_meta(ticker))
    model_exists = os.path.exists(model_path)
    meta_exists = os.path.exists(meta_path)
    loaded = bool(trainer.modelo_disponible(ticker))
    return {
        "ticker": ticker,
        "production_model_path": model_path,
        "production_meta_path": meta_path,
        "production_model_exists": model_exists,
        "production_meta_exists": meta_exists,
        "production_artifacts_present": bool(model_exists or meta_exists),
        "production_pair_complete": bool(model_exists and meta_exists),
        "production_loaded": loaded,
    }


def _inspect_shadow_base(
    trainer,
    ticker: str,
    *,
    expected_features: Iterable[str],
    feature_version: int,
    training_policy_version: int,
    load_model: bool = True,
) -> Dict[str, Any]:
    """Validate the immutable runtime contract separately from model gates."""
    ticker = str(ticker or "").strip().upper()
    expected_features = list(expected_features)
    structural_errors = []
    gate_errors = []
    model_path = str(trainer._ruta_shadow_modelo(ticker))
    meta_path = str(trainer._ruta_shadow_meta(ticker))

    if not ticker:
        structural_errors.append("ticker vacío")
    if not os.path.exists(model_path):
        structural_errors.append("falta modelo Shadow")
    if not os.path.exists(meta_path):
        structural_errors.append("falta metadata Shadow")

    meta: Dict[str, Any] = {}
    if not structural_errors:
        try:
            meta = _read_json(meta_path)
        except Exception as exc:
            structural_errors.append(
                f"metadata Shadow ilegible: {type(exc).__name__}: {exc}"
            )

    threshold = None
    if meta:
        if str(meta.get("ticker", "")).strip().upper() != ticker:
            structural_errors.append("ticker de metadata no coincide")
        if meta.get("shadow_candidate_accepted") is not True:
            gate_errors.append("shadow_candidate_accepted no es true")
        if meta.get("production_gate_passed") is not True:
            reasons = meta.get("production_gate_reasons")
            if isinstance(reasons, (list, tuple)) and reasons:
                gate_errors.extend(
                    f"production_gate: {str(reason)}" for reason in reasons
                )
            else:
                gate_errors.append("production_gate_passed no es true")
        try:
            meta_feature_version = int(meta.get("feature_version", -1))
        except (TypeError, ValueError):
            meta_feature_version = -1
        if meta_feature_version != int(feature_version):
            structural_errors.append("feature_version no coincide con el runtime")
        try:
            meta_policy_version = int(meta.get("training_policy_version", -1))
        except (TypeError, ValueError):
            meta_policy_version = -1
        if meta_policy_version != int(training_policy_version):
            structural_errors.append("training_policy_version no coincide con el runtime")
        if meta.get("features") != expected_features:
            structural_errors.append("lista de features no coincide con el runtime")
        try:
            threshold = float(meta.get("decision_threshold"))
            if not (0.0 < threshold < 1.0):
                raise ValueError
        except (TypeError, ValueError):
            threshold = None
            structural_errors.append("decision_threshold inválido")

    model = None
    if load_model and not structural_errors:
        try:
            with open(model_path, "rb") as handle:
                model = pickle.load(handle)
        except Exception as exc:
            structural_errors.append(
                f"modelo Shadow ilegible: {type(exc).__name__}: {exc}"
            )
        if model is not None:
            feature_count = int(getattr(model, "n_features_in_", -1))
            if feature_count != len(expected_features):
                structural_errors.append(
                    f"n_features_in_={feature_count} != runtime {len(expected_features)}"
                )

    all_errors = structural_errors + gate_errors
    return {
        "ticker": ticker,
        "eligible": not all_errors,
        "override_eligible": not structural_errors,
        "errors": all_errors,
        "structural_errors": structural_errors,
        "gate_errors": gate_errors,
        "shadow_model_path": model_path,
        "shadow_meta_path": meta_path,
        "balanced_accuracy": meta.get("balanced_accuracy") if meta else None,
        "shadow_incumbent_balanced_accuracy": (
            meta.get("shadow_incumbent_balanced_accuracy") if meta else None
        ),
        "production_incumbent_balanced_accuracy": (
            meta.get("production_incumbent_balanced_accuracy",
                     meta.get("incumbent_balanced_accuracy")) if meta else None
        ),
        "production_gate_reasons": (
            list(meta.get("production_gate_reasons") or []) if meta else []
        ),
        "production_gate_policy": (
            dict(meta.get("production_gate_policy") or {}) if meta else {}
        ),
        "evaluation_id": meta.get("evaluation_id") if meta else None,
        "evaluation_utc": (
            meta.get("evaluation_utc") or meta.get("entrenado_utc")
            if meta else None
        ),
        "evaluation_test_start": (
            meta.get("evaluation_test_start") if meta else None
        ),
        "evaluation_test_end": (
            meta.get("evaluation_test_end") if meta else None
        ),
        "decision_threshold": threshold,
        "trained_utc": (
            (meta.get("entrenado_utc") or meta.get("trained_utc")) if meta else None
        ),
        "shadow_candidate_accepted": (
            bool(meta.get("shadow_candidate_accepted")) if meta else False
        ),
        "production_gate_passed": (
            bool(meta.get("production_gate_passed")) if meta else False
        ),
        "feature_version": meta.get("feature_version") if meta else None,
        "training_policy_version": (
            meta.get("training_policy_version") if meta else None
        ),
        "_model": model,
        "_meta": meta,
    }


def inspect_shadow_candidate(
    trainer,
    ticker: str,
    *,
    expected_features: Iterable[str],
    feature_version: int,
    training_policy_version: int,
    load_model: bool = True,
) -> Dict[str, Any]:
    """Normal validation: runtime contract + all production gates must pass."""
    return _inspect_shadow_base(
        trainer,
        ticker,
        expected_features=expected_features,
        feature_version=feature_version,
        training_policy_version=training_policy_version,
        load_model=load_model,
    )


def inspect_shadow_candidate_for_override(
    trainer,
    ticker: str,
    *,
    expected_features: Iterable[str],
    feature_version: int,
    training_policy_version: int,
    load_model: bool = True,
) -> Dict[str, Any]:
    """Manual override validation.

    Only the *decision* gates may be overridden.  Missing/corrupt/incompatible
    artifacts remain blocked.
    """
    info = _inspect_shadow_base(
        trainer,
        ticker,
        expected_features=expected_features,
        feature_version=feature_version,
        training_policy_version=training_policy_version,
        load_model=load_model,
    )
    return info


def append_promotion_audit(path: str, payload: Dict[str, Any]) -> None:
    if not path:
        return
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    row = dict(payload)
    row.setdefault("timestamp_utc", datetime.now(timezone.utc).isoformat())
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _persist_production(
    trainer,
    info: Dict[str, Any],
    *,
    production_meta: Dict[str, Any],
) -> Optional[str]:
    return trainer._persist_model_pair(
        ticker=info["ticker"],
        modelo=info["_model"],
        metricas=production_meta,
        models_dir=trainer.modelos_dir,
        model_path=trainer._ruta_modelo(info["ticker"]),
        meta_path=trainer._ruta_meta(info["ticker"]),
        tier="PRODUCTION",
    )


def promote_shadow_candidate(
    trainer,
    ticker: str,
    *,
    expected_features: Iterable[str],
    feature_version: int,
    training_policy_version: int,
    audit_path: Optional[str] = None,
    reason: str = "explicit_paper_to_real",
    app_version: str = "3.0.0",
) -> Dict[str, Any]:
    """Atomically promote a normally eligible Shadow pair."""
    info = inspect_shadow_candidate(
        trainer,
        ticker,
        expected_features=expected_features,
        feature_version=feature_version,
        training_policy_version=training_policy_version,
        load_model=True,
    )
    if not info["eligible"]:
        raise ModelPromotionError("; ".join(info["errors"]) or "Shadow no elegible")

    ticker = info["ticker"]
    production_meta = dict(info["_meta"])
    promoted_at = datetime.now(timezone.utc).isoformat()
    production_meta.update({
        "ticker": ticker,
        "production_approved": True,
        "production_gate_passed": True,
        "activado": True,
        "modelo_cargado": True,
        "production_activation_allowed": True,
        "manual_promotion": True,
        "manual_promotion_source": "accepted_shadow_candidate",
        "manual_promotion_reason": str(reason),
        "manual_promotion_utc": promoted_at,
        "promotion_app_version": str(app_version),
        "manual_override": False,
        "production_gate_override": False,
        "modelo_path": trainer._ruta_modelo(ticker),
    })
    archive_dir = _persist_production(trainer, info, production_meta=production_meta)

    result = _public(info)
    result.update({
        "promoted": True,
        "manual_override": False,
        "promoted_utc": promoted_at,
        "promotion_reason": str(reason),
        "production_model_path": trainer._ruta_modelo(ticker),
        "production_meta_path": trainer._ruta_meta(ticker),
        "previous_model_archive": archive_dir,
    })
    if audit_path:
        append_promotion_audit(audit_path, {
            "event": "SHADOW_TO_PRODUCTION",
            "result": "PROMOTED",
            "ticker": ticker,
            "reason": str(reason),
            "app_version": str(app_version),
            "balanced_accuracy": result.get("balanced_accuracy"),
            "decision_threshold": result.get("decision_threshold"),
            "trained_utc": result.get("trained_utc"),
            "shadow_candidate_accepted": True,
            "production_gate_passed": True,
            "manual_override": False,
            "production_model_path": result.get("production_model_path"),
            "previous_model_archive": archive_dir,
        })
    return result


def promote_shadow_candidate_override(
    trainer,
    ticker: str,
    *,
    expected_features: Iterable[str],
    feature_version: int,
    training_policy_version: int,
    audit_path: Optional[str] = None,
    reason: str = "explicit_failed_gate_real_override",
    app_version: str = "3.2.0",
    acknowledged_by: str = "dashboard_user",
) -> Dict[str, Any]:
    """Promote a structurally compatible SHADOW despite failed decision gates.

    This is intentionally *not* a way around broken/incompatible artifacts.  It
    only permits a human-authorized override of the two training decision flags.
    The original gate values are preserved in production metadata and audit.
    """
    info = inspect_shadow_candidate_for_override(
        trainer,
        ticker,
        expected_features=expected_features,
        feature_version=feature_version,
        training_policy_version=training_policy_version,
        load_model=True,
    )
    if not info.get("override_eligible"):
        raise ModelPromotionError(
            "; ".join(info.get("structural_errors") or [])
            or "Shadow incompatible; override no permitido"
        )

    ticker = info["ticker"]
    original_accepted = bool(info.get("shadow_candidate_accepted"))
    original_gate = bool(info.get("production_gate_passed"))
    promoted_at = datetime.now(timezone.utc).isoformat()
    production_meta = dict(info["_meta"])
    production_meta.update({
        "ticker": ticker,
        # 'approved' here means explicit human approval, not gate success.
        "production_approved": True,
        "production_gate_passed": original_gate,
        "activado": True,
        "modelo_cargado": True,
        "production_activation_allowed": True,
        "manual_promotion": True,
        "manual_promotion_source": "failed_shadow_manual_override",
        "manual_promotion_reason": str(reason),
        "manual_promotion_utc": promoted_at,
        "promotion_app_version": str(app_version),
        "manual_override": True,
        "production_gate_override": True,
        "manual_override_acknowledged_by": str(acknowledged_by or "dashboard_user"),
        "manual_override_original_shadow_candidate_accepted": original_accepted,
        "manual_override_original_production_gate_passed": original_gate,
        "manual_override_gate_errors": list(info.get("gate_errors") or []),
        "production_approval_source": "CLIENT_MANUAL_OVERRIDE",
        "modelo_path": trainer._ruta_modelo(ticker),
    })
    archive_dir = _persist_production(trainer, info, production_meta=production_meta)

    result = _public(info)
    result.update({
        "promoted": True,
        "manual_override": True,
        "production_gate_override": True,
        "promoted_utc": promoted_at,
        "promotion_reason": str(reason),
        "production_model_path": trainer._ruta_modelo(ticker),
        "production_meta_path": trainer._ruta_meta(ticker),
        "previous_model_archive": archive_dir,
    })
    if audit_path:
        append_promotion_audit(audit_path, {
            "event": "SHADOW_TO_PRODUCTION_OVERRIDE",
            "result": "FORCED_PROMOTED",
            "ticker": ticker,
            "reason": str(reason),
            "app_version": str(app_version),
            "acknowledged_by": str(acknowledged_by or "dashboard_user"),
            "balanced_accuracy": result.get("balanced_accuracy"),
            "decision_threshold": result.get("decision_threshold"),
            "trained_utc": result.get("trained_utc"),
            "shadow_candidate_accepted": original_accepted,
            "production_gate_passed": original_gate,
            "gate_errors": list(info.get("gate_errors") or []),
            "manual_override": True,
            "production_model_path": result.get("production_model_path"),
            "previous_model_archive": archive_dir,
        })
    return result
