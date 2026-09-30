import csv
import os
import tempfile
import unittest

import numpy as np

from agents.contracts import DecisionFinal
from agents.shadow_store import ShadowDecisionStore
from agents.training import EntrenadorAgentes, FEATURES


class _FakeModel:
    n_features_in_ = len(FEATURES)

    def __init__(self, probability):
        self.probability = float(probability)

    def predict_proba(self, x):
        assert x.shape[1] == len(FEATURES)
        p = self.probability
        return np.array([[1.0 - p, p]], dtype=float)


def _config(tmpdir):
    return {
        "agents": {
            "entrenamiento": {
                "activo": False,
                "modelos_dir": os.path.join(tmpdir, "active"),
                "feedback_path": os.path.join(tmpdir, "feedback.csv"),
                "umbral_meta_filtro": 0.0,
            },
            "shadow_mode": {
                "enabled": True,
                "tickers": ["AMD"],
                "modelos_dir": os.path.join(tmpdir, "shadow"),
                "default_threshold": 0.55,
                "save_all_candidates": True,
            },
        }
    }


class ShadowTrainerTests(unittest.TestCase):
    def test_shadow_veto_never_changes_production_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trainer = EntrenadorAgentes(_config(tmpdir))
            trainer._modelos_shadow["AMD"] = _FakeModel(0.20)
            trainer._umbrales_shadow["AMD"] = 0.55
            trainer._shadow_sources["AMD"] = "candidate.pkl"
            decision = DecisionFinal(ticker="AMD", operar=True, tipo="BUY")

            probability, approved = trainer.evaluar(decision)
            shadow = trainer.evaluar_shadow(decision)

            self.assertIsNone(probability)
            self.assertTrue(approved)
            self.assertTrue(shadow["model_loaded"])
            self.assertFalse(shadow["would_approve"])
            self.assertAlmostEqual(shadow["probability"], 0.20)

    def test_unconfigured_ticker_does_not_use_shadow_model(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trainer = EntrenadorAgentes(_config(tmpdir))
            trainer._modelos_shadow["MSFT"] = _FakeModel(0.90)
            decision = DecisionFinal(ticker="MSFT", operar=True, tipo="BUY")
            shadow = trainer.evaluar_shadow(decision)
            self.assertFalse(shadow["model_loaded"])
            self.assertIsNone(shadow["would_approve"])


class ShadowStoreTests(unittest.TestCase):
    def test_open_close_ledger_records_non_blocking_outcome(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "shadow.csv")
            store = ShadowDecisionStore(path)
            decision = DecisionFinal(
                ticker="AMD",
                operar=True,
                tipo="BUY",
                shadow_enabled=True,
                shadow_model_loaded=True,
                shadow_prob=0.20,
                shadow_threshold=0.55,
                shadow_would_approve=False,
                shadow_model_source="candidate.pkl",
            )
            opened = store.abrir(decision, precio_entrada=100.0, stop_loss=98.0)
            closed = store.cerrar("AMD", precio_salida=97.0)

            self.assertIsNotNone(opened)
            self.assertIsNotNone(closed)
            self.assertEqual(closed["ganadora"], 0)
            self.assertEqual(closed["veto_would_help"], 1)
            self.assertEqual(closed["shadow_correct"], 1)
            self.assertAlmostEqual(closed["r_multiple"], -1.5)

            with open(path, "r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["event"] for row in rows], ["OPEN", "CLOSE"])
            self.assertEqual(rows[0]["signal_id"], rows[1]["signal_id"])


if __name__ == "__main__":
    unittest.main()
