import unittest

from agents.training import FEATURES, FEATURE_VERSION, TRAINING_POLICY_VERSION


class AIFeatureV4Tests(unittest.TestCase):
    def test_unusable_features_are_not_model_inputs(self):
        for name in ("ratio_rb", "gsco_match", "gsco_dist_pct", "hora"):
            self.assertNotIn(name, FEATURES)

    def test_required_causal_features_remain(self):
        required = {
            "tipo_num", "confianza_macro", "atr_relativo", "fvg_tam_rel",
            "hueco_apertura", "confluencia", "es_rango_lateral",
            "volumen_relativo_20", "liquidez_dolar_log",
            "gala_hist_strength_atr", "gala_session_minute",
        }
        self.assertTrue(required.issubset(set(FEATURES)))

    def test_feature_and_policy_versions_are_v4(self):
        self.assertEqual(FEATURE_VERSION, 4)
        self.assertEqual(TRAINING_POLICY_VERSION, 4)


if __name__ == "__main__":
    unittest.main()
