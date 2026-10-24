"""Learning evidence, temporal boundaries and cost-aware model acceptance."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

from agents.learning_economics import (
    economic_metrics, economic_rejection_reasons, net_trade_returns,
)
from agents.training import EntrenadorAgentes, FEATURES
from agents.learning_dataset import LEARNING_POLICY_VERSION
from core.model_promotion import inspect_shadow_candidate


class FixedClassifier:
    """Deterministic scores let integration tests isolate data boundaries."""
    instances = []

    def __init__(self, **kwargs):
        self.instances.append(self)
        self.n_features_in_ = len(FEATURES)

    def fit(self, x, y, sample_weight=None, **kwargs):
        self.fit_x, self.fit_y = np.asarray(x), np.asarray(y)
        self.weights = np.asarray(sample_weight)
        return self

    def predict_proba(self, x):
        p = np.asarray(x)[:, FEATURES.index("confianza_macro")]
        return np.column_stack([1 - p, p])


class LearningTrainingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = {"agents": {
            "entrenamiento": {
                "feedback_path": str(self.root / "feedback.csv"),
                "modelos_dir": str(self.root / "models"),
                "reportes_dir": str(self.root / "reports"),
                "automatic_retraining": {"state_path": str(self.root / "state.json")},
            },
            "shadow_mode": {"enabled": True, "modelos_dir": str(self.root / "shadow")},
            "signal_learning": {"path": str(self.root / "signals.csv")},
        }}
        self.trainer = EntrenadorAgentes(self.config)
        FixedClassifier.instances.clear()
        self.network = self.enterContext(patch("requests.sessions.Session.request",
            side_effect=AssertionError("Learning tests must not contact broker")))

    def tearDown(self):
        self.network.assert_not_called()

    def trades(self, total=120):
        rows = []
        for i in range(total):
            entry_time = pd.Timestamp("2026-01-05T14:30:00Z") + pd.Timedelta(minutes=10*i)
            winner = i % 2 == 0
            rows.append({**dict.fromkeys(FEATURES, 1.0),
                "confianza_macro": .8 if winner else .2,
                "ticker": "AAPL", "tipo": "BUY", "execution_valid": 1,
                "modo": "REAL" if i % 3 == 0 else "PAPER",
                "position_id": f"position-{i}", "execution_id": f"execution-{i}",
                "timestamp": entry_time.isoformat(),
                "closed_utc": (entry_time + pd.Timedelta(minutes=5)).isoformat(),
                "precio_entrada": 100.0, "precio_salida": 102.0 if winner else 99.0,
                "risk_per_share": 1.0, "r_multiple": 2.0 if winner else -1.0,
                "ganadora": int(winner)})
        return pd.DataFrame(rows)

    def train(self, rows=None):
        (self.trades() if rows is None else rows).to_csv(self.trainer.feedback_path, index=False)
        with patch.dict("sys.modules", {"xgboost": SimpleNamespace(XGBClassifier=FixedClassifier)}):
            return self.trainer.entrenar_ticker("AAPL", automatic=True,
                                              allow_production_activation=False)

    def signal(self, **updates):
        return {**dict.fromkeys(FEATURES, 1.0),
            "ticker": "AAPL", "event": "RESOLVED", "training_eligible": 1,
            "tipo": "BUY",
            "resolution": "TP", "ganadora": 1, "observation_id": "signal-1",
            "timestamp": "2026-01-05T14:30:00Z", "resolved_utc": "2026-01-05T15:00:00Z",
            "precio_entrada": 100., "precio_salida": 102., "stop_loss": 99.,
            "r_multiple": 2., **updates}

    def test_actual_fill_cost_reserve_is_subtracted_once_for_both_sides(self):
        rows = pd.DataFrame([
            {"tipo": "BUY", "r_multiple": 2., "risk_per_share": 1., "precio_entrada": 100., "precio_salida": 102.},
            {"tipo": "SELL", "r_multiple": 2., "risk_per_share": 1., "precio_entrada": 100., "precio_salida": 98.},
        ])
        np.testing.assert_allclose(net_trade_returns(rows, 2), [1.9798, 1.9802])

    def test_missing_risk_is_unknown_even_with_zero_cost(self):
        rows = self.trades(2).drop(columns="risk_per_share")
        self.assertTrue(np.isnan(net_trade_returns(rows, 0)).all())

    def test_drawdown_includes_loss_on_first_trade(self):
        m = economic_metrics([-1., 2., -2.], [.9]*3, .5, 2)
        self.assertEqual(m["max_drawdown_r"], 2.)
        self.assertEqual(m["net_r"], -1.)
        self.assertAlmostEqual(m["profit_factor"], 2/3, places=6)

    def test_missing_approved_return_fails_economic_gate(self):
        m = economic_metrics([2., np.nan], [.9, .9], .5, 2)
        self.assertFalse(m["evaluated"])
        self.assertEqual(m["missing_returns"], 1)
        self.assertTrue(economic_rejection_reasons(m, min_trades=1,
            min_expectancy_r=0., min_profit_factor=1.1, max_drawdown_r=6.))

    def test_high_win_rate_does_not_hide_unprofitable_returns(self):
        m = economic_metrics([.1]*8 + [-1.]*2, [.9]*10, .5, 2)
        self.assertEqual(m["win_rate"], .8)
        self.assertLess(m["expectancy_r"], 0)
        self.assertTrue(economic_rejection_reasons(m, min_trades=10,
            min_expectancy_r=0., min_profit_factor=1.1, max_drawdown_r=6.))

    def test_no_loss_evidence_is_json_safe_and_empty_approval_fails(self):
        m = economic_metrics([2., 1.], [.9, .9], .5, 2)
        self.assertIsNone(m["profit_factor"])
        self.assertTrue(m["no_loss_trades"])
        json.dumps(m, allow_nan=False)
        empty = economic_metrics([2., 1.], [.1, .1], .5, 2)
        self.assertFalse(empty["evaluated"])

    def test_positive_returns_alone_do_not_pass_if_filter_loses_baseline_profit(self):
        m = economic_metrics([3., 2., -1.], [.9, .1, .1], .5, 2)
        self.assertEqual(m["net_r"], 3.)
        self.assertEqual(m["baseline_net_r"], 4.)
        reasons = economic_rejection_reasons(m, min_trades=1,
            min_expectancy_r=0., min_profit_factor=1.1, max_drawdown_r=6.)
        self.assertTrue(any("degrada estrategia" in r for r in reasons))

    def test_net_labels_treat_fee_consumed_winner_as_loss(self):
        rows = self.trades(2)
        rows.loc[0, ["precio_salida", "r_multiple"]] = [100.01, .01]
        prepared = self.trainer._prepare_training_rows(rows)
        self.assertEqual(prepared.iloc[0].ganadora, 0)
        self.assertLess(prepared.iloc[0]._net_r, 0.)

    def test_net_returns_use_actual_fills_instead_of_stale_gross_r(self):
        rows = self.trades(2)
        rows.loc[1, "r_multiple"] = 99.
        result = net_trade_returns(rows, 2)
        self.assertLess(result[1], -1.)

    def test_stable_identity_counts_distinct_positions_with_identical_prices(self):
        rows = self.trades(2)
        rows.loc[1, ["timestamp", "precio_salida", "r_multiple", "ganadora"]] = rows.loc[
            0, ["timestamp", "precio_salida", "r_multiple", "ganadora"]].values
        self.assertEqual(len(self.trainer._prepare_training_rows(rows)), 2)

    def test_invalid_flags_labels_features_and_timestamps_are_excluded(self):
        rows = self.trades(6)
        rows.loc[0, "execution_valid"] = np.nan
        rows.loc[1, "ganadora"] = 3
        rows.loc[2, "volumen_relativo_20"] = np.inf
        rows.loc[3, "timestamp"] = "invalid"
        rows.loc[4, "modo"] = "UNKNOWN"
        self.assertEqual(len(self.trainer._prepare_training_rows(rows)), 1)

    def test_known_late_closures_are_purged_before_both_holdouts(self):
        rows = self.trades()
        rows.loc[0, "closed_utc"] = rows.loc[73, "closed_utc"]
        rows.loc[72, "closed_utc"] = rows.loc[97, "closed_utc"]
        fit, val, test, _, temporal = self.trainer._causal_blocks(
            self.trainer._prepare_training_rows(rows), automatic=True)
        self.assertEqual((len(fit), len(val), len(test)), (71, 23, 24))
        self.assertTrue(temporal["temporal_integrity_passed"])
        self.assertEqual(temporal["purged_fit_rows"], 1)
        self.assertEqual(temporal["purged_validation_rows"], 1)

    def test_supplement_requires_proven_cutoff_and_resolution(self):
        pd.DataFrame([self.signal()]).to_csv(self.trainer.signal_learning_path, index=False)
        self.assertTrue(self.trainer._supplemental_fit_rows("AAPL", fit_cutoff=None,
            actual_fit_count=10).empty)
        self.assertTrue(self.trainer._supplemental_fit_rows("AAPL", fit_cutoff="bad",
            actual_fit_count=10).empty)
        pd.DataFrame([self.signal()]).drop(columns="resolved_utc").to_csv(
            self.trainer.signal_learning_path, index=False)
        self.assertTrue(self.trainer._supplemental_fit_rows("AAPL",
            fit_cutoff="2026-01-06T00:00:00Z", actual_fit_count=10).empty)

    def test_supplement_is_capped_and_future_outcome_excluded(self):
        pd.DataFrame([self.signal(observation_id="old"), self.signal(observation_id="future",
            resolved_utc="2026-02-01T00:00:00Z")]).to_csv(self.trainer.signal_learning_path, index=False)
        sup = self.trainer._supplemental_fit_rows("AAPL", fit_cutoff="2026-01-06T00:00:00Z",
            actual_fit_count=1)
        self.assertEqual(sup.observation_id.tolist(), ["old"])

    def test_explicit_zero_disables_supplement_weight_or_ratio(self):
        for field in ("counterfactual_weight", "max_counterfactual_to_actual_fit_ratio"):
            self.config["agents"]["signal_learning"][field] = 0
            trainer = EntrenadorAgentes(self.config)
            actual = trainer.signal_learning_weight if field == "counterfactual_weight" else trainer.signal_learning_max_ratio
            self.assertEqual(actual, 0)

    def test_full_run_keeps_signals_in_fit_only_and_production_manual(self):
        pd.DataFrame([self.signal()]).to_csv(self.trainer.signal_learning_path, index=False)
        metrics = self.train()
        model = FixedClassifier.instances[-1]
        self.assertEqual((metrics["muestras_fit"], metrics["muestras_fit_suplementarias"]), (72, 1))
        self.assertEqual((metrics["muestras_validation"], metrics["muestras_test"]), (24, 24))
        self.assertEqual(len(model.fit_y), 73)
        self.assertEqual(model.weights[-1], .35)
        self.assertTrue(metrics["validation_test_actual_only"])
        self.assertTrue(metrics["economic_gate_passed"])
        self.assertTrue(metrics["production_gate_passed"])
        self.assertFalse(metrics["activado"])
        self.assertFalse(self.trainer.modelo_disponible("AAPL"))

    def test_unknown_old_close_time_blocks_production_without_fabrication(self):
        metrics = self.train(self.trades().drop(columns="closed_utc"))
        self.assertIsNotNone(metrics)
        self.assertFalse(metrics["temporal_integrity_passed"])
        self.assertFalse(metrics["production_gate_passed"])
        self.assertEqual(metrics["unknown_close_timestamps"], 120)

    def test_automatic_candidate_cannot_activate_even_with_explicit_legacy_permission(self):
        self.trades().to_csv(self.trainer.feedback_path, index=False)
        self.trainer.auto_activate_production = True
        with patch.dict("sys.modules", {"xgboost": SimpleNamespace(XGBClassifier=FixedClassifier)}):
            metrics = self.trainer.entrenar_ticker("AAPL", automatic=True,
                                                  allow_production_activation=True)
        self.assertTrue(metrics["production_gate_passed"])
        self.assertFalse(metrics["production_activation_allowed"])
        self.assertFalse(metrics["activado"])
        self.assertFalse(Path(self.trainer.modelos_dir).exists())
        self.assertTrue((Path(metrics["candidate_archive"]) / "model.pkl").is_file())
        self.assertTrue((Path(metrics["candidate_archive"]) / "model.meta.json").is_file())

    def test_small_test_remains_shadow_until_enough_approved_evidence(self):
        metrics = self.train(self.trades(50))
        self.assertIsNotNone(metrics)
        self.assertFalse(metrics["economic_gate_passed"])
        self.assertTrue(any("10 operaciones" in r for r in metrics["economic_reasons"]))

    def test_unknown_cost_fit_or_validation_can_never_pass_production(self):
        rows = self.trades()
        rows.loc[[0, 72], "risk_per_share"] = np.nan
        metrics = self.train(rows)
        self.assertTrue(metrics["economic_test"]["evaluated"])
        self.assertFalse(metrics["cost_label_integrity_passed"])
        self.assertFalse(metrics["economic_gate_passed"])
        self.assertFalse(metrics["production_gate_passed"])

    def test_threshold_uses_validation_net_returns(self):
        y, p = [1, 1, 0, 0], [.9, .7, .6, .2]
        # The medium-confidence gross winner is a large net loser in this
        # fixture; economic threshold selection must exclude it.
        threshold, _ = self.trainer._seleccionar_umbral(y, p, net_returns=[1., -5., -1., -1.])
        self.assertGreater(threshold, .7)
        self.assertLessEqual(threshold, .9)

    def test_test_returns_cannot_choose_threshold(self):
        first = self.train()
        rows = self.trades()
        rows.loc[96:, "precio_salida"] = np.where(rows.loc[96:, "ganadora"] == 1, 100.1, 95.)
        second = self.train(rows)
        self.assertEqual(first["decision_threshold"], second["decision_threshold"])
        self.assertNotEqual(first["economic_test"]["net_r"], second["economic_test"]["net_r"])

    def test_readiness_explains_missing_classes_and_real_paper_sources(self):
        rows = self.trades(50)
        rows.to_csv(self.trainer.feedback_path, index=False)
        status = self.trainer.retraining_status(["AAPL"])[0]
        self.assertTrue(status["training_readiness"]["ready"])
        self.assertEqual(status["eligible_closed_real"] + status["eligible_closed_paper"], 50)
        rows["ganadora"], rows["r_multiple"], rows["precio_salida"] = 1, 2., 102.
        rows.to_csv(self.trainer.feedback_path, index=False)
        status = self.trainer.retraining_status(["AAPL"])[0]
        self.assertFalse(status["training_readiness"]["ready"])
        self.assertIn("ganadoras y perdedoras", status["training_readiness"]["reason"])

    def inspect_candidate(self, **updates):
        from agents.training import FEATURE_VERSION, TRAINING_POLICY_VERSION
        path = Path(self.trainer._ruta_shadow_modelo("AAPL"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"inspection only; model loading disabled")
        meta = {"ticker": "AAPL", "features": FEATURES,
            "feature_version": FEATURE_VERSION, "training_policy_version": TRAINING_POLICY_VERSION,
            "decision_threshold": .5, "production_gate_passed": True,
            "shadow_candidate_accepted": True, "learning_policy_version": LEARNING_POLICY_VERSION,
            "economic_gate_passed": True, "temporal_integrity_passed": True,
            "cost_label_integrity_passed": True, "validation_test_actual_only": True, **updates}
        Path(self.trainer._ruta_shadow_meta("AAPL")).write_text(json.dumps(meta), encoding="utf-8")
        return inspect_shadow_candidate(self.trainer, "AAPL", expected_features=FEATURES,
            feature_version=FEATURE_VERSION, training_policy_version=TRAINING_POLICY_VERSION,
            load_model=False)

    def test_new_promotion_requires_current_economic_and_temporal_evidence(self):
        self.assertTrue(self.inspect_candidate()["eligible"])
        for key in ("economic_gate_passed", "temporal_integrity_passed", "cost_label_integrity_passed"):
            with self.subTest(key=key):
                info = self.inspect_candidate(**{key: False})
                self.assertFalse(info["eligible"])
                self.assertTrue(info["override_eligible"])
                self.assertTrue(any(key in reason for reason in info["gate_errors"]))

    def test_old_shadow_pass_requires_retraining_before_normal_promotion(self):
        info = self.inspect_candidate(learning_policy_version=None, economic_gate_passed=None)
        self.assertFalse(info["eligible"])
        self.assertTrue(info["override_eligible"])

    def test_installed_production_model_remains_loadable_after_upgrade(self):
        from agents.training import FEATURE_VERSION, TRAINING_POLICY_VERSION
        meta = {"ticker": "AAPL", "features": FEATURES, "feature_version": FEATURE_VERSION,
            "training_policy_version": TRAINING_POLICY_VERSION, "decision_threshold": .5}
        self.assertTrue(self.trainer._guardar("AAPL", FixedClassifier(), meta))
        loaded = EntrenadorAgentes(self.config)
        self.assertTrue(loaded.modelo_disponible("AAPL"))

    def test_dashboard_api_exposes_selected_sources_readiness_and_saved_economics(self):
        import server
        metrics = self.train()
        self.trainer.retraining.mark_result("AAPL", total_count=120, accepted=True,
            reason="test", metrics=metrics)
        with server.app.test_request_context("/api/ai/status"), patch.object(
            server, "_ai_trainer_for_status", return_value=self.trainer
        ), patch.object(server, "_veto_summary_rows", return_value=[]), patch.object(
            server, "_learning_summary_rows", return_value=[]
        ), patch.object(server, "_auto_ai_read_config", return_value=self.config
        ), patch.dict(server.BOT_STATE, {"tickers": ["AAPL"]}):
            response = server.ai_retraining_status()
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["learning_policy"]["closed_trade_sources"], ["REAL", "PAPER"])
        self.assertEqual(payload["summary"]["eligible_closed_real"], 40)
        self.assertEqual(payload["summary"]["eligible_closed_paper"], 80)
        self.assertTrue(payload["tickers"][0]["training_readiness"]["ready"])
        self.assertEqual(payload["tickers"][0]["economic_test"], metrics["economic_test"])

    def test_real_xgboost_smoke_uses_only_temporary_artifacts(self):
        try:
            import xgboost  # noqa: F401
        except ImportError:
            self.skipTest("XGBoost not installed")
        self.trades().to_csv(self.trainer.feedback_path, index=False)
        metrics = self.trainer.entrenar_ticker("AAPL", automatic=True,
                                              allow_production_activation=False)
        self.assertIsNotNone(metrics)
        self.assertTrue(metrics["temporal_integrity_passed"])
        self.assertTrue(metrics["economic_test"]["evaluated"])
        self.assertFalse(metrics["activado"])
        self.assertTrue(Path(metrics["candidate_archive"]).is_relative_to(self.root))


if __name__ == "__main__":
    unittest.main()

