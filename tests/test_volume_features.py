import math
import unittest

import numpy as np
import pandas as pd

from agents.contracts import DatosMultiTemporal
from agents.volume_features import calcular_features_volumen


class VolumeFeatureTests(unittest.TestCase):
    def test_live_mode_excludes_the_forming_one_minute_candle(self):
        index = pd.date_range("2026-07-27 13:30:00", periods=22, freq="min")
        volume = np.full(22, 100.0)
        volume[-2] = 200.0
        volume[-1] = 100000.0  # forming spike must not leak into the decision
        close = np.linspace(100.0, 101.0, 22)
        df = pd.DataFrame({
            "open": close - 0.05,
            "high": close + 0.10,
            "low": close - 0.10,
            "close": close,
            "volume": volume,
        }, index=index)
        data = DatosMultiTemporal("TEST", {"1m": df})

        live = calcular_features_volumen(data, actual_en_formacion=True)
        historical = calcular_features_volumen(data, actual_en_formacion=False)

        self.assertLess(live["volumen_relativo_20"], 3.0)
        self.assertGreater(historical["volumen_relativo_20"], 100.0)

    def test_missing_volume_fails_closed_to_nan_features(self):
        index = pd.date_range("2026-07-27 13:30:00", periods=25, freq="min")
        df = pd.DataFrame({
            "open": 100.0,
            "high": 100.1,
            "low": 99.9,
            "close": 100.0,
            "volume": 0.0,
        }, index=index)
        features = calcular_features_volumen(
            DatosMultiTemporal("TEST", {"1m": df}),
            actual_en_formacion=False,
        )
        self.assertTrue(all(math.isnan(value) for value in features.values()))


if __name__ == "__main__":
    unittest.main()
