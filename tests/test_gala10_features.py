import unittest

import numpy as np
import pandas as pd

from agents.gala10_features import (
    GALA10_FEATURE_NAMES,
    extraer_features_gala10,
)


class Gala10FeatureTests(unittest.TestCase):
    def _frame(self):
        index = pd.date_range(
            "2026-07-27 09:30", periods=3, freq="min",
            tz="America/New_York",
        )
        return pd.DataFrame({
            "señal": [None, "BUY", None],
            "atr": [1.0, 1.0, 999.0],
            "hist_line": [0.10, 0.16, 999.0],
            "ema_slope": [0.01, 0.03, 999.0],
            "range_pct": [0.2, 0.3, 999.0],
            "cross_bars": [1, 2, 999],
            "fast_bull_confirm": [False, True, False],
            "fast_bear_confirm": [False, False, False],
            "cross_high": [99.9, 100.0, 999.0],
            "cross_low": [99.0, 99.1, -999.0],
            "breakout_buffer": [0.02, 0.02, 999.0],
            "atr_regime_100": [1.0, 1.2, 999.0],
            "open": [100.0, 100.1, 999.0],
            "high": [100.2, 100.6, 999.0],
            "low": [99.8, 100.0, -999.0],
            "close": [100.1, 100.5, 999.0],
        }, index=index)

    def test_closed_signal_features_are_finite(self):
        frame = self._frame()
        features = extraer_features_gala10(
            frame, posicion=1, senal="BUY", timestamp=frame.index[1])

        self.assertEqual(set(features), set(GALA10_FEATURE_NAMES))
        self.assertTrue(all(np.isfinite(value) for value in features.values()))
        self.assertEqual(features["gala_fast_entry"], 1.0)
        self.assertEqual(features["gala_session_minute"], 1.0)
        self.assertGreater(features["gala_breakout_atr"], 0.0)

    def test_future_row_does_not_change_selected_candle(self):
        frame = self._frame()
        before = extraer_features_gala10(frame, posicion=1, senal="BUY")
        numeric_columns = frame.select_dtypes(include=[np.number]).columns
        frame.loc[frame.index[2], numeric_columns] = 1e12
        after = extraer_features_gala10(frame, posicion=1, senal="BUY")
        self.assertEqual(before, after)

    def test_utc_naive_history_timestamp_is_converted_to_new_york(self):
        frame = self._frame()
        features = extraer_features_gala10(
            frame,
            posicion=1,
            senal="BUY",
            timestamp=pd.Timestamp("2026-07-27 13:31:00"),
        )
        self.assertEqual(features["gala_session_minute"], 1.0)


if __name__ == "__main__":
    unittest.main()
