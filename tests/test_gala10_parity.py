import tempfile
import unittest
from datetime import datetime

import numpy as np
import pandas as pd

from agents.contracts import (
    DatosMultiTemporal,
    Direccion,
    DireccionDia,
    FairValueGap,
    PropuestaOperacion,
    Veredicto,
    VeredictoAuditor,
)
from agents.historico_loader import cargar_historico_local
from agents.orchestrator import OrquestadorAgentes
from core.strategy import GALA6Strategy, GALA10Strategy


class Gala10StrategyTests(unittest.TestCase):
    def test_legacy_name_is_only_an_alias(self):
        self.assertIs(GALA6Strategy, GALA10Strategy)

    def test_cooldown_resets_on_every_raw_signal_like_thinkscript(self):
        raw = [True] + [False] * 8 + [True, True] + [False] * 7 + [True]
        df = pd.DataFrame({
            "buy_signal_raw": raw,
            "sell_signal_raw": False,
            "omitida_buy_rango": False,
            "omitida_sell_rango": False,
            "omitida_buy_tendencia": False,
            "omitida_sell_tendencia": False,
        })
        out = GALA10Strategy({"strategy": {"cooldown_bars": 8}})._aplicar_cooldown(df)

        # Barra 0 pasa. Barra 9 también pasa (>8 desde el raw de barra 0).
        # El raw suprimido de barra 10 reinicia igualmente la memoria; por eso
        # la barra 18 todavía está bloqueada.
        self.assertTrue(out.loc[0, "buy"])
        self.assertTrue(out.loc[9, "buy"])
        self.assertFalse(out.loc[10, "buy"])
        self.assertFalse(out.loc[18, "buy"])
        self.assertEqual(out.loc[18, "barras_desde_buy"], 8)

    def test_final_plots_are_exported_independently(self):
        df = pd.DataFrame({
            "buy_signal_raw": [False],
            "sell_signal_raw": [False],
            "omitida_buy_rango": [True],
            "omitida_sell_rango": [False],
            "omitida_buy_tendencia": [False],
            "omitida_sell_tendencia": [False],
        })
        out = GALA10Strategy({})._aplicar_cooldown(df)
        self.assertIn("buy", out)
        self.assertIn("sell", out)
        self.assertEqual(out.loc[0, "señal"], "OM-R")


class _Macro:
    def analizar(self, datos, precio_actual, **kwargs):
        return DireccionDia(
            ticker=datos.ticker,
            direccion=Direccion.ALCISTA,
            confianza=0.8,
            timestamp=kwargs["ahora"],
        )


class _Micro:
    def analizar(self, datos, direccion, precio_actual, **kwargs):
        fvg = FairValueGap(
            tf="1m",
            direccion="alcista",
            fondo=99.0,
            tope=100.0,
            indice=10,
            timestamp=kwargs["ahora"],
        )
        return PropuestaOperacion(
            ticker=datos.ticker,
            tipo="BUY",
            precio_entrada=99.5,
            stop_loss=98.0,
            take_profit=102.0,
            ratio_rb=1.8,
            fvg=fvg,
            timestamp=kwargs["ahora"],
        )


class _Auditor:
    def auditar(self, *args, **kwargs):
        return VeredictoAuditor(
            veredicto=Veredicto.APROBADA,
            atr_relativo=0.2,
            es_horario_institucional=True,
            datos_suficientes=True,
        )


class _Trainer:
    def evaluar(self, decision):
        return 0.9, True


class Gala10AgentBoundaryTests(unittest.TestCase):
    def test_agents_cannot_replace_master_entry_with_fvg_limit(self):
        orchestrator = OrquestadorAgentes.__new__(OrquestadorAgentes)
        orchestrator.config = {
            "risk": {"stop_loss_factor": 2.0, "sl_min_pct_precio": 0.15},
            "agents": {"agente2": {"ratio_rb_minimo": 1.8}},
        }
        orchestrator.activo = True
        orchestrator.agente1 = _Macro()
        orchestrator.agente2 = _Micro()
        orchestrator.agente3 = _Auditor()
        orchestrator.entrenador = _Trainer()

        idx = pd.date_range("2026-07-27 13:00", periods=30, freq="min")
        close = np.linspace(99.0, 100.0, len(idx))
        df = pd.DataFrame({
            "open": close - 0.05,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": 1000.0,
        }, index=idx)
        decision = orchestrator.auditar_gala10(
            DatosMultiTemporal("MSFT", {"1m": df}),
            precio_actual=100.0,
            senal_gala10="BUY",
            atr=0.5,
            ahora=datetime(2026, 7, 27, 10, 0),
            actual_en_formacion=False,
        )

        self.assertTrue(decision.operar)
        self.assertEqual(decision.tipo, "BUY")
        self.assertEqual(decision.propuesta.precio_entrada, 100.0)
        self.assertIsNone(decision.propuesta.fvg)
        self.assertGreater(decision.fvg_contexto_tam_rel, 0.0)


class HistoricalSessionTests(unittest.TestCase):
    def test_rth_only_csv_is_rejected_when_tos_uses_extended_hours(self):
        with tempfile.TemporaryDirectory() as tmp:
            idx = pd.date_range("2026-07-27 13:30", periods=100, freq="min")
            csv = pd.DataFrame({
                "datetime": idx,
                "open": 100.0,
                "high": 100.1,
                "low": 99.9,
                "close": 100.0,
                "volume": 1000.0,
            })
            csv.to_csv(f"{tmp}/MSFT.csv", index=False)
            result = cargar_historico_local(
                "MSFT",
                {
                    "market": {"include_extended_hours_data": True},
                    "ai": {"historico_dir": tmp, "historico_tz": "UTC"},
                },
            )
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
