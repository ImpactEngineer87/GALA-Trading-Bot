import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agents.signal_learning_store import SignalLearningStore


def _decision(signal_id, *, tipo="BUY", ticker="META"):
    return SimpleNamespace(
        signal_id=signal_id,
        ticker=ticker,
        tipo=tipo,
        timestamp=datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc),
        execution_mode="PAPER",
        propuesta=SimpleNamespace(
            precio_entrada=100.0,
            stop_loss=98.0,
            take_profit=104.0,
            ratio_rb=2.0,
            riesgo_por_accion=2.0,
            fvg=None,
        ),
        direccion_dia=SimpleNamespace(confianza=0.72, hueco_apertura_pct=0.1),
        veredicto=SimpleNamespace(atr_relativo=0.4, es_rango_lateral=False),
        confluencia_estrategia=True,
        fvg_contexto_tam_rel=0.0,
        prob_meta=0.42,
        ai_model_snapshot={"threshold": 0.50},
        market_regime="RANGE",
        market_regime_confidence=0.8,
        market_regime_would_block=True,
    )


def test_visibility_summary_distinguishes_avoided_loss_and_blocked_winner(tmp_path):
    path = tmp_path / "learning.csv"
    store = SignalLearningStore(str(path), max_bars=10)

    store.record_nonexecuted(
        _decision("loss"), execution_mode="REAL", source_kind="AI_VETO",
        disposition="BLOCKED", reason="AI_THRESHOLD", training_candidate=True,
    )
    # SL means the veto avoided a losing trade.
    store.update_bar("META", high=101.0, low=97.5, close=98.0,
                     bar_timestamp="2026-09-29T14:01:00Z")

    store.record_nonexecuted(
        _decision("winner"), execution_mode="PAPER", source_kind="AI_VETO",
        disposition="BLOCKED", reason="AI_THRESHOLD", training_candidate=True,
    )
    # TP means the veto incorrectly blocked a would-be winner.
    store.update_bar("META", high=104.5, low=99.0, close=104.0,
                     bar_timestamp="2026-09-29T14:02:00Z")

    row = store.summary_by_ticker(["META"])[0]
    assert row["learning_ai_veto"] == 2
    assert row["learning_avoided_losses"] == 1
    assert row["learning_blocked_winners"] == 1
    assert row["learning_usefulness_pct"] == 50.0
    assert row["learning_trainable"] == 2
    assert row["learning_tp"] == 1
    assert row["learning_sl"] == 1
    assert row["learning_real"] == 1
    assert row["learning_paper"] == 1
    assert row["learning_last_update"]


def test_visibility_summary_tracks_open_ambiguous_timeout_and_excluded(tmp_path):
    path = tmp_path / "learning.csv"
    store = SignalLearningStore(str(path), max_bars=1)

    store.record_nonexecuted(
        _decision("amb"), execution_mode="PAPER", source_kind="EXECUTION_MISSED",
        disposition="MISSED", reason="BROKER_ENTRY_ERROR", training_candidate=True,
    )
    store.update_bar("META", high=104.5, low=97.5, close=100.0,
                     bar_timestamp="2026-09-29T14:01:00Z")

    store.record_nonexecuted(
        _decision("timeout"), execution_mode="PAPER", source_kind="POLICY_BLOCK",
        disposition="MISSED", reason="CAPACITY", training_candidate=False,
    )
    store.update_bar("META", high=101.0, low=99.0, close=100.5,
                     bar_timestamp="2026-09-29T14:02:00Z")

    store.record_nonexecuted(
        _decision("open"), execution_mode="REAL", source_kind="AI_VETO",
        disposition="BLOCKED", reason="AI_THRESHOLD", training_candidate=True,
    )

    row = store.summary_by_ticker(["META"])[0]
    assert row["learning_open"] == 1
    assert row["learning_ambiguous"] == 1
    assert row["learning_timeout"] == 1
    assert row["learning_excluded"] == 2
    assert row["learning_execution_missed"] == 1
    assert row["learning_missed"] == 2


def test_recent_visibility_collapses_open_and_resolved_rows(tmp_path):
    path = tmp_path / "learning.csv"
    store = SignalLearningStore(str(path), max_bars=10)
    store.record_nonexecuted(
        _decision("sig-one"), execution_mode="PAPER", source_kind="AI_VETO",
        disposition="BLOCKED", reason="AI_THRESHOLD", training_candidate=True,
    )
    store.update_bar("META", high=104.5, low=99.0, close=104.0,
                     bar_timestamp="2026-09-29T14:01:00Z")
    rows = store.recent_observations("META", limit=10)
    assert len(rows) == 1
    assert rows[0]["signal_id"] == "sig-one"
    assert rows[0]["status"] == "RESOLVED"
    assert rows[0]["resolution"] == "TP"
    assert rows[0]["training_eligible"] in (1, "1")
    # Full feature vectors are intentionally not exposed by the visibility API.
    assert "atr_relativo" not in rows[0]


def test_signal_learning_visibility_api_and_dashboard_source():
    server_text = Path("server.py").read_text(encoding="utf-8")
    html = Path("dashboard/index.html").read_text(encoding="utf-8")
    version = Path("VERSION").read_text(encoding="utf-8").strip()
    assert "@app.route('/api/ai/signal-learning')" in server_text
    assert f'"version": "{version}"' in server_text
    assert 'learning_avoided_losses' in server_text
    assert 'learning_blocked_winners' in server_text
    assert "Visibilidad · Aprendizaje de señales" in html
    assert "/api/ai/signal-learning" in html
    assert "Pérdidas evitadas por veto" in html
    assert "Ganadoras bloqueadas por veto" in html
    assert f"Trading Algorítmico v{version}" in html
