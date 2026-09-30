import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import yaml

from agents import training as training_mod
from agents.training import EntrenadorAgentes, FEATURES


class _FixedModel:
    n_features_in_ = len(FEATURES)

    def __init__(self, probability):
        self.probability = float(probability)

    def predict_proba(self, x):
        return np.array([[1.0 - self.probability, self.probability]], dtype=float)


class _Decision:
    ticker = "NVDA"


class LiveInferenceGateTests(unittest.TestCase):
    def _config(self, root):
        return {
            "agents": {
                "entrenamiento": {
                    "activo": False,
                    "modelos_dir": str(Path(root) / "models"),
                    "feedback_path": str(Path(root) / "feedback.csv"),
                    "umbral_meta_filtro": 0.0,
                    "automatic_retraining": {
                        "enabled": True,
                        "only_after_session": True,
                        "state_path": str(Path(root) / "state.json"),
                    },
                },
                "shadow_mode": {
                    "enabled": False,
                    "modelos_dir": str(Path(root) / "shadow"),
                },
            }
        }

    def test_real_mode_uses_approved_model_even_when_training_is_inactive(self):
        with TemporaryDirectory() as tmp:
            trainer = EntrenadorAgentes(self._config(tmp))
            trainer._modelos["NVDA"] = _FixedModel(0.61)
            trainer._umbrales["NVDA"] = 0.60
            with patch.object(training_mod, "construir_features", return_value={}):
                prob, approved = trainer.evaluar(_Decision(), requerir_modelo=True)
            self.assertEqual(prob, 0.61)
            self.assertTrue(approved)

    def test_real_mode_applies_per_ticker_threshold_when_global_veto_is_zero(self):
        with TemporaryDirectory() as tmp:
            trainer = EntrenadorAgentes(self._config(tmp))
            trainer._modelos["NVDA"] = _FixedModel(0.49)
            trainer._umbrales["NVDA"] = 0.60
            with patch.object(training_mod, "construir_features", return_value={}):
                prob, approved = trainer.evaluar(_Decision(), requerir_modelo=True)
            self.assertEqual(prob, 0.49)
            self.assertFalse(approved)

    def test_paper_mode_remains_observation_only_when_training_is_inactive(self):
        with TemporaryDirectory() as tmp:
            trainer = EntrenadorAgentes(self._config(tmp))
            trainer._modelos["NVDA"] = _FixedModel(0.10)
            trainer._umbrales["NVDA"] = 0.60
            with patch.object(training_mod, "construir_features", return_value={}):
                prob, approved = trainer.evaluar(_Decision(), requerir_modelo=False)
            self.assertIsNone(prob)
            self.assertTrue(approved)


class LiveProfileConfigTests(unittest.TestCase):
    def test_shipped_config_is_paper_by_default_with_live_guardrails(self):
        path = Path(__file__).resolve().parents[1] / "config.yaml"
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertTrue(cfg["risk"]["paper_trading"])
        self.assertEqual(cfg["live_trading"]["allowed_tickers"], ["NVDA", "META"])
        self.assertEqual(cfg["live_trading"]["max_tickers"], 2)
        self.assertEqual(cfg["live_trading"]["max_shares_per_order"], 1)
        self.assertFalse(cfg["live_trading"]["allow_short"])
        self.assertTrue(
            cfg["agents"]["entrenamiento"]["requerir_modelo_aprobado_en_real"]
        )
        self.assertFalse(
            cfg["agents"]["entrenamiento"]["automatic_retraining"]["auto_activate_production"]
        )


class LiveStartGuardPolicyTests(unittest.TestCase):
    def _base_cfg(self):
        path = Path(__file__).resolve().parents[1] / "config.yaml"
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        cfg["live_trading"]["require_approved_models"] = False
        cfg["live_trading"]["require_broker_preflight"] = False
        cfg["risk"]["fixed_shares"] = 1
        cfg["risk"]["max_acciones_por_orden"] = 1
        cfg["risk"]["max_simultaneous_positions"] = 2
        cfg["risk"]["sizing_mode"] = "fixed"
        cfg["scheduler"]["activo"] = False
        cfg["scheduler"]["modo"] = "manual"
        return cfg

    def test_live_guard_requires_explicit_arm(self):
        from core.live_guard import validate_live_start
        cfg = self._base_cfg()
        ok, errors, allowed = validate_live_start(
            cfg, get_env=lambda key, default="": ""
        )
        self.assertFalse(ok)
        self.assertEqual(allowed, ["NVDA", "META"])
        self.assertTrue(any("GALA10_LIVE_ARMED=YES" in e for e in errors))

    def test_live_guard_accepts_safe_local_profile_when_armed(self):
        from core.live_guard import validate_live_start
        cfg = self._base_cfg()
        ok, errors, allowed = validate_live_start(
            cfg, get_env=lambda key, default="": "YES"
        )
        self.assertTrue(ok, errors)
        self.assertEqual(errors, [])
        self.assertEqual(allowed, ["NVDA", "META"])


if __name__ == "__main__":
    unittest.main()
