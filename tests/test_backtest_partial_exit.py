import unittest
import sys
import types
from datetime import time
from types import SimpleNamespace

import pandas as pd

# The production project declares these dependencies, but the isolated unit
# test environment may omit them. No network or dotenv access is needed here.
try:
    import requests  # noqa: F401
except ImportError:
    requests_stub = types.ModuleType("requests")

    class _StubSession:
        def __init__(self):
            self.headers = {}

    requests_stub.Session = _StubSession
    requests_stub.exceptions = types.SimpleNamespace(RequestException=Exception)
    sys.modules["requests"] = requests_stub

try:
    import dotenv  # noqa: F401
except ImportError:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub

from agents.backtest_trainer import PreEntrenadorHistorico


class BacktestPartialExitTests(unittest.TestCase):
    def _trainer(self, *, offset_pct=0.0):
        trainer = PreEntrenadorHistorico.__new__(PreEntrenadorHistorico)
        trainer.config = {
            "salida_parcial": {
                "activo": True,
                "pct_primer_objetivo": 0.70,
                "rb_primer_objetivo": 1.0,
                "modo_resto": "breakeven",
                "breakeven_offset_pct": offset_pct,
            }
        }
        trainer.max_barras_futuras = 390
        trainer.hora_cierre = time(15, 45)
        return trainer

    @staticmethod
    def _decision(tipo="BUY"):
        if tipo == "BUY":
            entrada, stop, final = 100.0, 98.0, 103.6
        else:
            entrada, stop, final = 100.0, 102.0, 96.4
        return SimpleNamespace(
            propuesta=SimpleNamespace(
                tipo=tipo,
                precio_entrada=entrada,
                stop_loss=stop,
                take_profit=final,
                fvg=None,
            )
        )

    @staticmethod
    def _bars(rows):
        index = pd.date_range(
            "2026-07-27 14:00:00", periods=len(rows), freq="min")
        return pd.DataFrame(rows, index=index)

    def test_buy_uses_distinct_tp1_and_final_targets(self):
        micro = self._bars([
            {"open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0},
            {"open": 100.0, "high": 102.2, "low": 100.0, "close": 102.0},
            {"open": 102.0, "high": 103.7, "low": 101.5, "close": 103.6},
        ])

        salida, barra = self._trainer()._simular_desenlace(
            micro, 0, self._decision("BUY"))

        # 70% at +2.0 and 30% at +3.6 => effective exit 102.48.
        self.assertEqual(barra, 2)
        self.assertAlmostEqual(salida, 102.48, places=6)

    def test_buy_moves_remaining_stop_to_breakeven_after_tp1(self):
        micro = self._bars([
            {"open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0},
            {"open": 100.0, "high": 102.2, "low": 100.1, "close": 102.0},
            {"open": 102.0, "high": 102.1, "low": 99.9, "close": 100.0},
        ])

        salida, barra = self._trainer()._simular_desenlace(
            micro, 0, self._decision("BUY"))

        # 70% exits at +2.0; the remaining 30% exits at exact breakeven.
        self.assertEqual(barra, 2)
        self.assertAlmostEqual(salida, 101.40, places=6)

    def test_sell_uses_distinct_tp1_and_final_targets(self):
        micro = self._bars([
            {"open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0},
            {"open": 100.0, "high": 100.0, "low": 97.8, "close": 98.0},
            {"open": 98.0, "high": 98.5, "low": 96.3, "close": 96.4},
        ])

        salida, barra = self._trainer()._simular_desenlace(
            micro, 0, self._decision("SELL"))

        self.assertEqual(barra, 2)
        self.assertAlmostEqual(salida, 97.52, places=6)


if __name__ == "__main__":
    unittest.main()
