import json
import pickle
from pathlib import Path

from agents.retraining_lifecycle import RetrainingLifecycle
from agents.training import FEATURE_VERSION, FEATURES, TRAINING_POLICY_VERSION
from core.live_guard import validate_live_start_partitioned
from core.model_promotion import inspect_shadow_candidate


class _DummyModel:
    def __init__(self):
        self.n_features_in_ = len(FEATURES)


def _lifecycle(tmp_path: Path):
    return RetrainingLifecycle(
        feedback_path=str(tmp_path / "feedback.csv"),
        state_path=str(tmp_path / "state.json"),
        shadow_models_dir=str(tmp_path / "shadow"),
        warning_samples=30,
        retrain_samples=50,
    )


def test_metric_snapshot_never_mixes_new_candidate_with_old_incumbent(tmp_path: Path):
    lifecycle = _lifecycle(tmp_path)
    lifecycle.mark_result(
        "UAL",
        total_count=50,
        accepted=False,
        metrics={
            "evaluation_id": "run-1",
            "evaluation_utc": "2026-08-18T20:00:00+00:00",
            "balanced_accuracy": 60.0,
            "shadow_incumbent_balanced_accuracy": 70.0,
            "production_incumbent_balanced_accuracy": 68.0,
            "decision_threshold": 0.50,
            "production_gate_passed": False,
            "production_gate_reasons": ["recall_ganadoras 0.100 < mínimo 0.200"],
            "shadow_candidate_accepted": False,
            "muestras_test": 20,
        },
    )
    row1 = lifecycle.snapshot(["UAL"])[0]
    assert row1["last_balanced_delta_pp"] == -10.0

    # The second snapshot intentionally has no incumbent. v3.1 could retain the
    # 70% from run-1 and display a false -5 pp comparison for run-2.
    lifecycle.mark_result(
        "UAL",
        total_count=100,
        accepted=True,
        metrics={
            "evaluation_id": "run-2",
            "evaluation_utc": "2026-09-09T20:00:00+00:00",
            "balanced_accuracy": 65.0,
            "shadow_incumbent_balanced_accuracy": None,
            "production_incumbent_balanced_accuracy": None,
            "decision_threshold": 0.43,
            "production_gate_passed": True,
            "production_gate_reasons": [],
            "shadow_candidate_accepted": True,
            "muestras_test": 25,
        },
    )
    row2 = lifecycle.snapshot(["UAL"])[0]
    assert row2["last_evaluation_id"] == "run-2"
    assert row2["last_balanced_accuracy"] == 65.0
    assert row2["last_shadow_incumbent_balanced_accuracy"] is None
    assert row2["last_production_incumbent_balanced_accuracy"] is None
    assert row2["last_incumbent_balanced_accuracy"] is None
    assert row2["last_balanced_delta_pp"] is None


def test_snapshot_separates_shadow_acceptance_from_production_gate(tmp_path: Path):
    lifecycle = _lifecycle(tmp_path)
    lifecycle.mark_result(
        "UAL",
        total_count=75,
        accepted=True,
        reason="candidato Shadow aceptado",
        metrics={
            "evaluation_id": "ual-sept",
            "balanced_accuracy": 60.88,
            "shadow_incumbent_balanced_accuracy": 57.36,
            "production_incumbent_balanced_accuracy": 64.0,
            "decision_threshold": 0.43,
            "shadow_candidate_accepted": True,
            "production_gate_passed": False,
            "production_gate_reasons": [
                "precision_lift 0.020 < mínimo 0.050"
            ],
            "muestras_fit": 180,
            "muestras_validation": 60,
            "muestras_test": 60,
        },
    )
    row = lifecycle.snapshot(["UAL"])[0]
    assert row["last_shadow_candidate_accepted"] is True
    assert row["last_production_gate_passed"] is False
    assert row["last_shadow_delta_pp"] == 3.52
    assert row["last_production_delta_pp"] == -3.12
    assert row["last_decision_threshold"] == 0.43
    assert "precision_lift" in row["last_production_gate_reasons"][0]


def test_promotion_preview_exposes_exact_failed_production_gate(tmp_path: Path):
    shadow = tmp_path / "shadow"
    shadow.mkdir(parents=True)
    with (shadow / "modelo_UAL.pkl").open("wb") as fh:
        pickle.dump(_DummyModel(), fh)
    meta = {
        "ticker": "UAL",
        "balanced_accuracy": 60.88,
        "shadow_incumbent_balanced_accuracy": 57.36,
        "production_incumbent_balanced_accuracy": 64.0,
        "decision_threshold": 0.43,
        "shadow_candidate_accepted": True,
        "production_gate_passed": False,
        "production_gate_reasons": [
            "precision_lift 0.020 < mínimo 0.050"
        ],
        "feature_version": FEATURE_VERSION,
        "training_policy_version": TRAINING_POLICY_VERSION,
        "features": FEATURES,
    }
    (shadow / "modelo_UAL.meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )

    class _Trainer:
        def _ruta_shadow_modelo(self, ticker):
            return str(shadow / f"modelo_{ticker}.pkl")
        def _ruta_shadow_meta(self, ticker):
            return str(shadow / f"modelo_{ticker}.meta.json")

    info = inspect_shadow_candidate(
        _Trainer(),
        "UAL",
        expected_features=FEATURES,
        feature_version=FEATURE_VERSION,
        training_policy_version=TRAINING_POLICY_VERSION,
    )
    assert info["eligible"] is False
    assert info["production_gate_reasons"] == [
        "precision_lift 0.020 < mínimo 0.050"
    ]
    assert any("precision_lift" in err for err in info["gate_errors"])
    assert info["shadow_incumbent_balanced_accuracy"] == 57.36
    assert info["production_incumbent_balanced_accuracy"] == 64.0


def _live_cfg():
    return {
        "risk": {
            "paper_trading": True,
            "sizing_mode": "fixed",
            "fixed_shares": 20,
            "max_acciones_por_orden": 20,
        },
        "scheduler": {"modo": "manual"},
        "agents": {
            "entrenamiento": {
                "requerir_modelo_aprobado_en_real": True,
                "automatic_retraining": {"auto_activate_production": False},
            }
        },
        "kill_switch": {
            "liquidar_al_apagar": True,
            "liquidar_al_cierre_mercado": True,
            "daily_loss": {"enabled": True},
        },
        "live_trading": {
            "per_ticker_execution": True,
            "allowed_tickers": ["META", "NVDA", "TSLA"],
            "max_tickers": 3,
            "max_shares_per_order": 3,
            "require_manual_start": True,
            "require_approved_models": True,
            "require_broker_preflight": True,
            "arm_env": "GALA10_LIVE_ARMED",
            "arm_value": "YES",
        },
    }


def test_partitioned_preflight_blocks_only_failing_real_ticker_and_retries():
    cfg = _live_cfg()
    calls = []

    def broker(account, ticker):
        calls.append(ticker)
        if ticker == "META":
            return {"ok": False, "errors": ["sin velas reales de 1m para META"]}
        return {"ok": True, "errors": []}

    result = validate_live_start_partitioned(
        cfg,
        get_env=lambda key, default="": "YES",
        account_number="acct",
        model_checker=lambda tickers: [],
        broker_checker=broker,
        real_tickers=["META", "NVDA"],
        ticker_settings={
            "META": {"shares": 3},
            "NVDA": {"shares": 3},
        },
        broker_retry_attempts=3,
        broker_retry_delay_sec=0,
    )
    assert result["start_allowed"] is True
    assert result["approved_real"] == ["NVDA"]
    assert result["blocked_real"] == ["META"]
    assert "sin velas reales de 1m" in result["ticker_errors"]["META"][0]
    assert calls.count("META") == 3
    assert calls.count("NVDA") == 1


def test_partitioned_preflight_keeps_global_arm_fail_closed():
    cfg = _live_cfg()
    calls = []
    result = validate_live_start_partitioned(
        cfg,
        get_env=lambda key, default="": "",
        account_number="acct",
        model_checker=lambda tickers: [],
        broker_checker=lambda account, ticker: calls.append(ticker) or {"ok": True},
        real_tickers=["META", "NVDA"],
        ticker_settings={
            "META": {"shares": 3},
            "NVDA": {"shares": 3},
        },
    )
    assert result["start_allowed"] is False
    assert any("GALA10_LIVE_ARMED=YES" in e for e in result["global_errors"])
    assert calls == []


def test_veto_summary_reports_useful_vs_missed_winner_rates(tmp_path: Path):
    from agents.veto_observation_store import VetoObservationStore

    store = VetoObservationStore(
        str(tmp_path / "veto.csv"), enabled=True, max_bars=5
    )
    # Hypothetical BUY that later hits SL: veto was useful.
    store.record_veto(
        ticker="UAL", tipo="BUY", probability=0.40, threshold=0.43,
        entry_price=100.0, stop_loss=99.0, take_profit=102.0,
        execution_mode="REAL", veto_reason="AI_THRESHOLD",
        timestamp="2026-09-09T14:00:00Z",
    )
    store.update_bar(
        "UAL", high=100.5, low=98.9, close=99.2,
        bar_timestamp="2026-09-09T14:01:00Z",
    )
    # Hypothetical BUY that later hits TP: veto missed a winner.
    store.record_veto(
        ticker="UAL", tipo="BUY", probability=0.41, threshold=0.43,
        entry_price=100.0, stop_loss=99.0, take_profit=102.0,
        execution_mode="REAL", veto_reason="AI_THRESHOLD",
        timestamp="2026-09-09T14:02:00Z",
    )
    store.update_bar(
        "UAL", high=102.1, low=99.5, close=101.8,
        bar_timestamp="2026-09-09T14:03:00Z",
    )

    row = store.summary_by_ticker(["UAL"])[0]
    assert row["veto_comparable"] == 2
    assert row["veto_good"] == 1
    assert row["veto_bad"] == 1
    assert row["veto_usefulness_pct"] == 50.0
    assert row["veto_missed_winner_pct"] == 50.0
