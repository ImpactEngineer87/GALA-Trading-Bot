from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from agents.signal_learning_store import SignalLearningStore, LEARNING_FEATURES
from agents.training import EntrenadorAgentes, FEATURES, TRAINING_POLICY_VERSION


def _decision(signal_id="sig-1", ticker="META", tipo="BUY"):
    dd = SimpleNamespace(confianza=0.72, hueco_apertura_pct=0.1)
    ver = SimpleNamespace(atr_relativo=0.4, es_rango_lateral=False)
    prop = SimpleNamespace(
        precio_entrada=100.0,
        stop_loss=98.0,
        take_profit=104.0,
        ratio_rb=2.0,
        riesgo_por_accion=2.0,
        fvg=None,
    )
    return SimpleNamespace(
        signal_id=signal_id,
        ticker=ticker,
        tipo=tipo,
        timestamp=datetime(2026, 9, 27, 14, 0, tzinfo=timezone.utc),
        execution_mode="PAPER",
        propuesta=prop,
        direccion_dia=dd,
        veredicto=ver,
        confluencia_estrategia=True,
        fvg_contexto_tam_rel=0.0,
        prob_meta=0.42,
        ai_model_snapshot={"threshold": 0.50},
        market_regime="RANGE",
        market_regime_confidence=0.8,
        market_regime_would_block=True,
    )


def test_learning_feature_schema_matches_runtime_training_features():
    assert LEARNING_FEATURES == FEATURES
    # Existing v3.3.x production models must remain structurally loadable.
    assert TRAINING_POLICY_VERSION == 4


def test_veto_counterfactual_tp_is_trainable_but_not_x50(tmp_path):
    path = tmp_path / "signal_learning.csv"
    store = SignalLearningStore(str(path), max_bars=10)
    row = store.record_nonexecuted(
        _decision(), execution_mode="PAPER", source_kind="AI_VETO",
        disposition="BLOCKED", reason="AI_THRESHOLD", training_candidate=True,
    )
    assert row and row["notes"] == "supplemental_not_x50"
    resolved = store.update_bar(
        "META", high=104.2, low=99.5, close=103.9,
        bar_timestamp="2026-09-27T14:01:00Z",
    )
    assert len(resolved) == 1
    assert resolved[0]["resolution"] == "TP"
    assert resolved[0]["ganadora"] == 1
    assert resolved[0]["training_eligible"] == 1
    summary = store.summary_by_ticker(["META"])[0]
    assert summary["learning_trainable"] == 1
    assert summary["learning_winners"] == 1


def test_same_bar_ambiguous_is_never_training_label(tmp_path):
    path = tmp_path / "signal_learning.csv"
    store = SignalLearningStore(str(path), max_bars=10)
    store.record_nonexecuted(
        _decision("sig-amb"), execution_mode="REAL", source_kind="EXECUTION_MISSED",
        disposition="MISSED", reason="BROKER_ENTRY_ERROR", training_candidate=True,
    )
    resolved = store.update_bar(
        "META", high=104.5, low=97.5, close=100.0,
        bar_timestamp="2026-09-27T14:01:00Z",
    )
    assert resolved[0]["resolution"] == "AMBIGUOUS_SAME_BAR"
    assert resolved[0]["training_eligible"] == 0


def test_policy_only_signal_is_audited_but_not_trainable(tmp_path):
    path = tmp_path / "signal_learning.csv"
    store = SignalLearningStore(str(path), max_bars=10)
    store.record_nonexecuted(
        _decision("sig-policy"), execution_mode="REAL", source_kind="POLICY_BLOCK",
        disposition="MISSED", reason="DAILY_LOSS_BLOCK", training_candidate=False,
    )
    resolved = store.update_bar(
        "META", high=104.2, low=99.0, close=104.0,
        bar_timestamp="2026-09-27T14:01:00Z",
    )
    assert resolved[0]["resolution"] == "TP"
    assert resolved[0]["training_eligible"] == 0


def test_dedupe_survives_restart(tmp_path):
    path = tmp_path / "signal_learning.csv"
    store = SignalLearningStore(str(path), max_bars=10)
    d = _decision("sig-dedupe")
    assert store.record_nonexecuted(
        d, execution_mode="PAPER", source_kind="AI_VETO",
        disposition="BLOCKED", reason="AI_THRESHOLD", training_candidate=True,
    )
    store.update_bar(
        "META", high=104.2, low=99.0, close=104.0,
        bar_timestamp="2026-09-27T14:01:00Z",
    )
    restarted = SignalLearningStore(str(path), max_bars=10)
    assert restarted.record_nonexecuted(
        d, execution_mode="PAPER", source_kind="AI_VETO",
        disposition="BLOCKED", reason="AI_THRESHOLD", training_candidate=True,
    ) is None


def test_supplemental_fit_loader_respects_resolution_cutoff_and_cap(tmp_path):
    learning = tmp_path / "learning.csv"
    rows = []
    for i in range(6):
        row = {
            "observation_id": f"o{i}", "ticker": "META", "event": "RESOLVED",
            "training_eligible": 1, "ganadora": i % 2, "resolution": "TP" if i % 2 else "SL",
            "resolved_utc": f"2026-09-27T14:0{i}:00+00:00", "timestamp": f"2026-09-27 09:5{i}:00",
            "tipo_num": i % 2,
        }
        for name in FEATURES:
            row.setdefault(name, 1.0)
        rows.append(row)
    pd.DataFrame(rows).to_csv(learning, index=False)
    cfg = {
        "agents": {
            "entrenamiento": {
                "modelos_dir": str(tmp_path / "models"),
                "feedback_path": str(tmp_path / "feedback.csv"),
                "automatic_retraining": {"state_path": str(tmp_path / "retrain.json")},
            },
            "shadow_mode": {"modelos_dir": str(tmp_path / "shadow")},
            "signal_learning": {
                "enabled": True, "use_for_training": True,
                "path": str(learning), "counterfactual_weight": 0.35,
                "max_counterfactual_to_actual_fit_ratio": 0.5,
            },
        },
        "live_trading": {},
    }
    trainer = EntrenadorAgentes(cfg)
    frame = trainer._supplemental_fit_rows(
        "META", fit_cutoff=pd.Timestamp("2026-09-27 10:03:30"), actual_fit_count=6,
    )
    # max ratio 0.5 caps 6 possible rows to 3, and future resolutions are excluded.
    assert len(frame) == 3
    assert set(frame["observation_id"]) <= {"o0", "o1", "o2", "o3"}
