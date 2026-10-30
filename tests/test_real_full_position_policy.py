"""REAL full exits preserve PAPER partial plans and all broker-entry guards."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from core.exit_policy import effective_partial_enabled, real_exit_policy
from core.live_guard import validate_live_start_partitioned
from core.risk_manager import GestorDeRiesgo
from engine.bot_engine import ChocoletraBot


def config():
    return {"risk": {"paper_trading": True}, "salida_parcial": {"activo": True},
            "live_trading": {"real_exit_policy": "FULL_POSITION", "per_ticker_execution": True,
                             "max_shares_per_order": 10, "max_real_tickers": 5,
                             "require_approved_models": False, "require_broker_preflight": False,
                             "arm_env": ""},
            "scheduler": {"modo": "manual"}}


class FullPositionExitPolicyTests(unittest.TestCase):
    def test_real_preflight_accepts_full_exits_and_default_still_rejects_partial(self):
        cfg = config()
        def check():
            return validate_live_start_partitioned(cfg, get_env=lambda k, d="": d,
                real_tickers=["BA", "TSLA"], ticker_settings={"BA": {"shares": 5}, "TSLA": {"shares": 3}})
        self.assertEqual(check()["approved_real"], ["BA", "TSLA"])
        del cfg["live_trading"]["real_exit_policy"]
        self.assertEqual(check()["blocked_real"], ["BA", "TSLA"])
        cfg["live_trading"]["real_exit_policy"] = "unrecognized"
        self.assertFalse(check()["start_allowed"])
        self.assertTrue(check()["global_errors"])

    def test_one_shared_risk_manager_generates_both_books_without_mutation(self):
        cfg = config()
        manager = GestorDeRiesgo(cfg)
        kwargs = dict(ticker="BA", tipo="BUY", precio_actual=100, atr=1,
                      capital_disponible=10000, cantidad_override=10)
        real = manager.calcular_orden(**kwargs, partial_exit_override=False)
        paper = manager.calcular_orden(**kwargs)
        self.assertEqual((real.salida_parcial_activa, real.cantidad_parcial, real.cantidad_resto), (False, 0, 10))
        self.assertEqual((paper.salida_parcial_activa, paper.cantidad_parcial, paper.cantidad_resto), (True, 7, 3))
        self.assertTrue(manager.salida_parcial_activa)
        self.assertFalse(effective_partial_enabled(cfg, paper=False))
        self.assertTrue(effective_partial_enabled(cfg, paper=True))
        self.assertEqual(real_exit_policy(cfg), "FULL_POSITION")

    def test_engine_routes_actual_order_plan_to_the_correct_executor(self):
        for paper in (True, False):
            with self.subTest(paper=paper):
                bot = ChocoletraBot.__new__(ChocoletraBot)
                bot.config = config()
                bot.capital = 10000
                bot.account_number = "offline-account"
                bot._trading_listo = True
                bot._is_paper_ticker = Mock(return_value=paper)
                bot._real_accounting_status = Mock(return_value={"entry_blocked": False})
                bot._daily_blocked = Mock(return_value=False)
                bot._asegurar_token_trading = Mock(return_value=True)
                bot._confirm_real_ticker_flat_before_entry = Mock(return_value=True)
                bot._asegurar_ticker_limpio = Mock(return_value=True)
                bot._reserve_real_capacity = Mock(return_value=True)
                bot.shares_for_ticker = Mock(return_value=10)
                bot._audit_event = Mock()
                bot._emit_log = Mock()
                bot.gestor_riesgo = GestorDeRiesgo(bot.config)
                # Stop at a simulated executor rejection; never construct a broker.
                bot.ejecutor_paper = SimpleNamespace(ejecutar_con_persecucion=Mock(return_value={"status": "ERROR"}))
                bot.ejecutor_hibrido = SimpleNamespace(enviar_entrada=Mock(return_value={"status": "ERROR"}))
                decision = SimpleNamespace(veredicto=None, prob_meta=.8, confluencia_estrategia=False,
                    direccion_dia=None, propuesta=SimpleNamespace(ticker="BA", tipo="BUY", precio_entrada=100,
                                                               stop_loss=98, take_profit=104, fvg=None))
                with patch("agents.smc_primitives.atr_wilder", return_value=pd.Series([1.0])):
                    self.assertFalse(bot._abrir_operacion(decision, 100, object()))
                if paper:
                    order = bot.ejecutor_paper.ejecutar_con_persecucion.call_args.kwargs["orden"]
                    bot.ejecutor_hibrido.enviar_entrada.assert_not_called()
                else:
                    order = bot.ejecutor_hibrido.enviar_entrada.call_args.args[0]
                    bot.ejecutor_paper.ejecutar_con_persecucion.assert_not_called()
                    self.assertEqual(bot._real_accounting_status.call_count, 2)
                self.assertEqual(order.salida_parcial_activa, paper)
                self.assertEqual(order.cantidad_parcial, 7 if paper else 0)
                self.assertEqual(order.cantidad_resto, 3 if paper else 10)


if __name__ == "__main__":
    unittest.main()
