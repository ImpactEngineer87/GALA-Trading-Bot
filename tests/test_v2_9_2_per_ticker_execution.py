import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.live_guard import validate_live_start
from core.risk_manager import GestorDeRiesgo
from core.ticker_execution import TickerExecutionRegistry


class TickerExecutionRegistryTests(unittest.TestCase):
    def test_mode_and_shares_persist_without_touching_retraining_state(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            ai_state = root / "retraining_state.json"
            original = '{"META":{"new_observations":24,"retrain_samples":50}}\n'
            ai_state.write_text(original, encoding="utf-8")

            registry = TickerExecutionRegistry(
                str(root / "ticker_execution.json"),
                default_mode="PAPER",
                default_shares=20,
            )
            rows = registry.ensure_tickers(["META", "NVDA"], persist=True)
            self.assertEqual(rows[0]["mode"], "PAPER")
            self.assertEqual(rows[0]["shares"], 20)

            registry.update("META", mode="REAL", shares=1)
            fresh = TickerExecutionRegistry(str(root / "ticker_execution.json"))
            self.assertEqual(fresh.get("META")["mode"], "REAL")
            self.assertEqual(fresh.get("META")["shares"], 1)
            self.assertEqual(ai_state.read_text(encoding="utf-8"), original)

    def test_snapshot_is_read_only_and_defaults_missing_ticker_without_persisting(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ticker_execution.json"
            registry = TickerExecutionRegistry(str(path), default_shares=7)
            rows = registry.snapshot(["NKE"])
            self.assertEqual(rows[0]["mode"], "PAPER")
            self.assertEqual(rows[0]["shares"], 7)
            self.assertFalse(path.exists())


class PerTickerLiveGuardTests(unittest.TestCase):
    def _cfg(self):
        return {
            "risk": {
                "paper_trading": True,
                "sizing_mode": "fixed",
                "fixed_shares": 20,
                "max_acciones_por_orden": 20,
            },
            "scheduler": {"modo": "manual"},
            "agents": {
                "entrenamiento": {
                    "requerir_modelo_aprobado_en_real": True,
                    "automatic_retraining": {"auto_activate_production": False},
                }
            },
            "kill_switch": {
                "liquidar_al_apagar": True,
                "liquidar_al_cierre_mercado": True,
                "daily_loss": {"enabled": True},
            },
            "live_trading": {
                "per_ticker_execution": True,
                "allowed_tickers": ["NVDA", "META", "NKE"],
                "max_tickers": 2,
                "max_shares_per_order": 1,
                "require_manual_start": True,
                "require_approved_models": True,
                "require_broker_preflight": True,
                "arm_env": "GALA10_LIVE_ARMED",
                "arm_value": "YES",
            },
        }

    def test_only_real_tickers_are_sent_to_model_and_broker_preflight(self):
        cfg = self._cfg()
        calls = {"models": [], "broker": []}

        def model_checker(tickers):
            calls["models"].append(list(tickers))
            return []

        def broker_checker(account, ticker):
            calls["broker"].append((account, ticker))
            return {"ok": True, "errors": []}

        ok, errors, checked = validate_live_start(
            cfg,
            get_env=lambda key, default="": "YES",
            account_number="123",
            model_checker=model_checker,
            broker_checker=broker_checker,
            real_tickers=["NVDA"],
            ticker_settings={
                "NVDA": {"mode": "REAL", "shares": 1},
                "META": {"mode": "PAPER", "shares": 20},
            },
        )
        self.assertTrue(ok, errors)
        self.assertEqual(checked, ["NVDA"])
        self.assertEqual(calls["models"], [["NVDA"]])
        self.assertEqual(calls["broker"], [("123", "NVDA")])

    def test_paper_only_needs_no_live_arm_or_broker(self):
        cfg = self._cfg()
        ok, errors, checked = validate_live_start(
            cfg,
            get_env=lambda key, default="": "",
            real_tickers=[],
            ticker_settings={},
        )
        self.assertTrue(ok, errors)
        self.assertEqual(errors, [])
        self.assertEqual(checked, [])

    def test_real_share_cap_is_fail_closed(self):
        cfg = self._cfg()
        cfg["live_trading"]["require_approved_models"] = False
        cfg["live_trading"]["require_broker_preflight"] = False
        ok, errors, checked = validate_live_start(
            cfg,
            get_env=lambda key, default="": "YES",
            real_tickers=["META"],
            ticker_settings={"META": {"mode": "REAL", "shares": 2}},
        )
        self.assertFalse(ok)
        self.assertEqual(checked, ["META"])
        self.assertTrue(any("shares REAL" in err for err in errors))

    def test_max_two_real_tickers(self):
        cfg = self._cfg()
        cfg["live_trading"]["require_approved_models"] = False
        cfg["live_trading"]["require_broker_preflight"] = False
        ok, errors, _ = validate_live_start(
            cfg,
            get_env=lambda key, default="": "YES",
            real_tickers=["NVDA", "META", "NKE"],
            ticker_settings={
                "NVDA": {"shares": 1},
                "META": {"shares": 1},
                "NKE": {"shares": 1},
            },
        )
        self.assertFalse(ok)
        self.assertTrue(any("máximo permitido 2" in err for err in errors))


class PerTickerSizingTests(unittest.TestCase):
    def test_explicit_quantity_override_wins_over_global_fixed_shares(self):
        cfg = {
            "risk": {
                "sizing_mode": "fixed",
                "fixed_shares": 20,
                "max_acciones_por_orden": 0,
                "risk_per_trade_pct": 1.0,
                "stop_loss_factor": 3.5,
                "take_profit_factor": 4.5,
            },
            "salida_parcial": {"activo": False},
        }
        risk = GestorDeRiesgo(cfg)
        order = risk.calcular_orden(
            ticker="META",
            tipo="BUY",
            precio_actual=100.0,
            atr=1.0,
            capital_disponible=10000,
            cantidad_override=3,
        )
        self.assertIsNotNone(order)
        self.assertEqual(order.cantidad, 3)


if __name__ == "__main__":
    unittest.main()

class EngineRoutingHelperTests(unittest.TestCase):
    def test_registry_routes_real_and_paper_without_global_mode_toggle(self):
        from engine.bot_engine import ChocoletraBot

        class _Registry:
            modes = {"NVDA": "REAL", "META": "PAPER"}
            quantities = {"NVDA": 5, "META": 7}
            def mode(self, ticker):
                return self.modes[ticker]
            def shares(self, ticker):
                return self.quantities[ticker]
            def snapshot(self, tickers):
                return [
                    {"ticker": t, "mode": self.modes[t], "shares": self.quantities[t]}
                    for t in tickers
                ]

        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.per_ticker_execution = True
        bot.paper_trading = True  # safe legacy fallback is irrelevant in v2.9.2
        bot.ticker_execution = _Registry()
        bot.live_max_shares_per_order = 1
        bot.tickers = ["NVDA", "META"]

        self.assertEqual(bot.execution_mode_for("NVDA"), "REAL")
        self.assertEqual(bot.execution_mode_for("META"), "PAPER")
        self.assertEqual(bot._real_tickers(), ["NVDA"])
        self.assertEqual(bot.shares_for_ticker("NVDA"), 1)  # REAL hard cap
        self.assertEqual(bot.shares_for_ticker("META"), 7)  # PAPER flexibility

    def test_open_position_keeps_original_execution_mode(self):
        from engine.bot_engine import ChocoletraBot

        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.per_ticker_execution = True
        bot.paper_trading = True
        bot.ticker_execution = type("R", (), {"mode": lambda self, ticker: "PAPER"})()
        pos = {"execution_mode": "REAL"}
        self.assertFalse(bot._is_paper_ticker("META", pos=pos))
