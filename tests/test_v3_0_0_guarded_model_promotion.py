from __future__ import annotations

import json
import pickle
from pathlib import Path

import pytest

from agents.training import (
    EntrenadorAgentes,
    FEATURE_VERSION,
    FEATURES,
    TRAINING_POLICY_VERSION,
)
from core.model_promotion import (
    ModelPromotionError,
    inspect_shadow_candidate,
    promote_shadow_candidate,
)


class DummyModel:
    def __init__(self, n_features):
        self.n_features_in_ = int(n_features)


def _config(root: Path):
    return {
        "agents": {
            "shadow_mode": {
                "enabled": True,
                "tickers": ["TSLA"],
                "modelos_dir": str(root / "shadow"),
                "default_threshold": 0.56,
            },
            "entrenamiento": {
                "activo": False,
                "modelos_dir": str(root / "production"),
                "feedback_path": str(root / "feedback.csv"),
                "automatic_retraining": {
                    "enabled": True,
                    "only_after_session": True,
                    "retrain_samples": 50,
                    "state_path": str(root / "retraining_state.json"),
                    "auto_activate_production": False,
                },
            },
        }
    }


def _write_shadow(root: Path, *, accepted=True, gate=True, threshold=0.61):
    shadow = root / "shadow"
    shadow.mkdir(parents=True, exist_ok=True)
    model = DummyModel(len(FEATURES))
    with (shadow / "modelo_TSLA.pkl").open("wb") as handle:
        pickle.dump(model, handle)
    meta = {
        "ticker": "TSLA",
        "balanced_accuracy": 0.63,
        "decision_threshold": threshold,
        "entrenado_utc": "2026-08-22T19:45:00+00:00",
        "shadow_candidate_accepted": accepted,
        "production_gate_passed": gate,
        "feature_version": FEATURE_VERSION,
        "training_policy_version": TRAINING_POLICY_VERSION,
        "features": FEATURES,
        "production_approved": False,
    }
    (shadow / "modelo_TSLA.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def test_eligible_shadow_can_be_promoted_and_reloaded(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_shadow(tmp_path)
    trainer = EntrenadorAgentes(cfg)
    assert trainer.modelo_disponible("TSLA") is False

    preview = inspect_shadow_candidate(
        trainer,
        "TSLA",
        expected_features=FEATURES,
        feature_version=FEATURE_VERSION,
        training_policy_version=TRAINING_POLICY_VERSION,
    )
    assert preview["eligible"] is True
    assert preview["shadow_candidate_accepted"] is True
    assert preview["production_gate_passed"] is True

    audit = tmp_path / "promotion_audit.jsonl"
    result = promote_shadow_candidate(
        trainer,
        "TSLA",
        expected_features=FEATURES,
        feature_version=FEATURE_VERSION,
        training_policy_version=TRAINING_POLICY_VERSION,
        audit_path=str(audit),
        reason="explicit_paper_to_real",
        app_version="3.0.0",
    )
    assert result["promoted"] is True
    assert (tmp_path / "production" / "modelo_TSLA.pkl").exists()
    production_meta = json.loads(
        (tmp_path / "production" / "modelo_TSLA.meta.json").read_text(encoding="utf-8")
    )
    assert production_meta["production_approved"] is True
    assert production_meta["manual_promotion_source"] == "accepted_shadow_candidate"
    assert production_meta["manual_promotion_reason"] == "explicit_paper_to_real"
    assert production_meta["promotion_app_version"] == "3.0.0"
    assert audit.exists()

    fresh = EntrenadorAgentes(cfg)
    assert fresh.modelo_disponible("TSLA") is True
    assert fresh._umbrales["TSLA"] == pytest.approx(0.61)


def test_rejected_shadow_is_never_promoted(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_shadow(tmp_path, accepted=False)
    trainer = EntrenadorAgentes(cfg)
    preview = inspect_shadow_candidate(
        trainer,
        "TSLA",
        expected_features=FEATURES,
        feature_version=FEATURE_VERSION,
        training_policy_version=TRAINING_POLICY_VERSION,
    )
    assert preview["eligible"] is False
    assert any("shadow_candidate_accepted" in x for x in preview["errors"])
    with pytest.raises(ModelPromotionError):
        promote_shadow_candidate(
            trainer,
            "TSLA",
            expected_features=FEATURES,
            feature_version=FEATURE_VERSION,
            training_policy_version=TRAINING_POLICY_VERSION,
        )
    assert not (tmp_path / "production" / "modelo_TSLA.pkl").exists()


def test_v300_server_requires_explicit_second_confirmation_and_keeps_retrain_auto_promotion_off():
    root = Path(__file__).resolve().parents[1]
    server = (root / "server.py").read_text(encoding="utf-8")
    html = (root / "dashboard" / "index.html").read_text(encoding="utf-8")
    version = (root / "VERSION").read_text(encoding="utf-8").strip()

    assert version in {"3.0.0", "3.0.1", "3.0.7", "3.1.0", "3.1.1", "3.2.0", "3.3.0", "3.3.1"}
    assert "PROMOTION_REQUIRED" in server
    assert "confirm_shadow_promotion" in server
    assert "NO_ELIGIBLE_SHADOW_MODEL" in server
    assert "PRODUCTION_ARTIFACT_INVALID" in server
    assert "explicit_paper_to_real" in server
    assert "confirm_shadow_promotion" in html
    assert "SHADOW → PRODUCTION → REAL" in html
    assert "Esto NO activa promociones automáticas" in html
    assert 'globals()["_AI_STATUS_TRAINER"] = None' in server
    # Actual promotion happens only after the broker-flat check in the route.
    route = server[server.index("def update_ticker_execution"):server.index("# MARKET DATA PROXY")]
    assert route.index("_broker_symbol_flat_for_mode_change") < route.index("promote_shadow_candidate")


def test_model_promotion_module_has_no_silent_background_switch():
    root = Path(__file__).resolve().parents[1]
    module = (root / "core" / "model_promotion.py").read_text(encoding="utf-8")
    assert "registry.update" not in module
    assert "auto_activate_production" not in module
    assert "explicit_paper_to_real" in module
