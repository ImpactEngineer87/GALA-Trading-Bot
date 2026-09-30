import json
import threading
import types
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
import pandas as pd

from core.daily_loss_guard import DailyLossGuard
from engine.bot_engine import ChocoletraBot


class _Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class DailyLossGuardTests(unittest.TestCase):
    def _config(self, root):
        return {
            "kill_switch": {
                "daily_loss": {
                    "enabled": True,
                    "paper_limit_usd": 250,
                    "live_limit_usd": 200,
                    "include_unrealized": True,
                    "state_file": str(Path(root) / "daily_state.json"),
                }
            }
        }

    def test_mode_limits_and_restart_persistence(self):
        with TemporaryDirectory() as td:
            clock = _Clock(datetime(2026, 7, 30, 12, 0, 0))
            guard = DailyLossGuard(
                self._config(td),
                history_path=str(Path(td) / "history.csv"),
                now_provider=clock,
            )
            self.assertEqual(guard.limit_for(True), -250.0)
            self.assertEqual(guard.limit_for(False), -200.0)
            guard.add_realized(True, -75.0)
            self.assertTrue(
                guard.mark_triggered(True, -250.0, -175.0, "test limit")
            )

            restarted = DailyLossGuard(
                self._config(td),
                history_path=str(Path(td) / "history.csv"),
                now_provider=clock,
            )
            self.assertTrue(restarted.is_blocked())
            self.assertEqual(restarted.realized_pnl(True), -75.0)

            clock.value = datetime(2026, 7, 31, 9, 0, 0)
            self.assertTrue(restarted.ensure_session())
            self.assertFalse(restarted.is_blocked())
            self.assertEqual(restarted.realized_pnl(True), 0.0)

    def test_blocked_snapshot_keeps_trigger_mode_and_limit_coherent(self):
        with TemporaryDirectory() as td:
            clock = _Clock(datetime(2026, 9, 16, 12, 0, 0))
            guard = DailyLossGuard(
                self._config(td), history_path=str(Path(td) / "history.csv"),
                now_provider=clock,
            )
            guard.add_realized(True, -4975.94)
            guard.mark_triggered(False, -857.42, -850.0, "REAL trigger")
            snapshot = guard.snapshot(True, unrealized_pnl=0.0)
            self.assertEqual(snapshot["mode"], "REAL")
            self.assertEqual(snapshot["net_pnl"], -857.42)
            self.assertEqual(snapshot["limit_usd"], -200.0)
            self.assertEqual(snapshot["current_mode"], "PAPER")
            self.assertEqual(snapshot["current_realized_pnl"], -4975.94)

    def test_weekend_does_not_clear_friday_lock(self):
        with TemporaryDirectory() as td:
            clock = _Clock(datetime(2026, 9, 18, 15, 0, 0))  # Friday
            guard = DailyLossGuard(
                self._config(td), history_path=str(Path(td) / "history.csv"),
                now_provider=clock,
            )
            guard.mark_triggered(False, -200.0, 0.0, "test")
            clock.value = datetime(2026, 9, 19, 12, 0, 0)  # Saturday
            self.assertFalse(guard.ensure_session())
            self.assertTrue(guard.is_blocked())
            clock.value = datetime(2026, 9, 21, 0, 1, 0)  # Monday
            self.assertTrue(guard.ensure_session())
            self.assertFalse(guard.is_blocked())

    def test_initializes_realized_pnl_from_history(self):
        with TemporaryDirectory() as td:
            history = Path(td) / "history.csv"
            history.write_text(
                "fecha,hora_ny,ticker,lado,entrada,salida,cantidad,pnl_usd,resultado,motivo,modo\n"
                "2026-07-30,10:00:00,MSFT,BUY,100,99,10,-10,PERDIDA,SL,PAPER\n"
                "2026-07-30,11:00:00,AAPL,SELL,100,101,10,-10,PERDIDA,SL,REAL\n"
                "2026-07-29,11:00:00,AMD,BUY,100,99,10,-10,PERDIDA,SL,PAPER\n",
                encoding="utf-8",
            )
            clock = _Clock(datetime(2026, 7, 30, 12, 0, 0))
            guard = DailyLossGuard(
                self._config(td), history_path=str(history), now_provider=clock
            )
            self.assertEqual(guard.realized_pnl(True), -10.0)
            self.assertEqual(guard.realized_pnl(False), -10.0)


class DailyLossEngineTests(unittest.TestCase):
    def _bot(self, td, *, paper=True):
        clock = _Clock(datetime(2026, 7, 30, 12, 0, 0))
        config = {
            "kill_switch": {
                "daily_loss": {
                    "enabled": True,
                    "paper_limit_usd": 250,
                    "live_limit_usd": 200,
                    "include_unrealized": True,
                    "state_file": str(Path(td) / "daily_state.json"),
                }
            }
        }
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.paper_trading = paper
        bot.daily_loss_guard = DailyLossGuard(
            config,
            history_path=str(Path(td) / "history.csv"),
            now_provider=clock,
        )
        bot._lock = threading.RLock()
        bot._daily_kill_lock = threading.RLock()
        bot._daily_kill_in_progress = False
        bot._posiciones = {}
        bot._last_prices = {}
        bot._activo = True
        bot._emit_log = lambda *args: None
        bot._audit_event = lambda *args, **kwargs: None
        return bot

    def test_paper_limit_triggers_cancel_liquidate_and_persistent_block(self):
        with TemporaryDirectory() as td:
            bot = self._bot(td, paper=True)
            bot._posiciones = {
                "MSFT": {
                    "tipo": "BUY",
                    "entrada": 100.0,
                    "cantidad": 10,
                    "cantidad_resto": 10,
                    "tp1_hit": False,
                    "orden": types.SimpleNamespace(cantidad=10),
                }
            }
            bot._last_prices = {"MSFT": 75.0}
            calls = []
            bot._cancelar_todas_ordenes_pendientes = lambda motivo: calls.append(
                ("cancel", motivo)
            )
            bot._liquidar_todo = lambda motivo: calls.append(("liquidate", motivo))

            self.assertTrue(bot._evaluar_limite_perdida_diaria())
            self.assertTrue(bot.daily_loss_guard.is_blocked())
            self.assertEqual([x[0] for x in calls], ["cancel", "liquidate"])

            # Idempotent: a second worker sees the block but cannot trigger another
            # liquidation sequence.
            self.assertTrue(bot._evaluar_limite_perdida_diaria())
            self.assertEqual([x[0] for x in calls], ["cancel", "liquidate"])

    def test_synthetic_frame_never_reaches_prices_risk_or_execution(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.paper_trading = True
        bot.per_ticker_execution = False
        bot._lock = threading.RLock()
        bot._last_prices = {}
        bot._last_price_times = {}
        frame = pd.DataFrame({
            "open": [100.0] * 30, "high": [101.0] * 30,
            "low": [99.0] * 30, "close": [100.0] * 30,
            "volume": [1000.0] * 30,
        })
        synthetic = types.SimpleNamespace(
            sintetico=True,
            tf=lambda _name: frame,
        )
        bot._obtener_mtf = lambda ticker: synthetic
        bot._datos_micro_confirmados = lambda data: data
        bot._emit_log = lambda *args: None
        bot._quarantine_real_ticker = lambda *args: None
        bot._evaluar_limite_perdida_diaria = lambda: (_ for _ in ()).throw(
            AssertionError("synthetic data reached daily risk")
        )
        bot._gestionar_posiciones = lambda *args: (_ for _ in ()).throw(
            AssertionError("synthetic data reached execution")
        )

        bot._procesar_ticker("MSFT")
        self.assertEqual(bot._last_prices, {})
        self.assertEqual(bot._last_price_times, {})

    def test_paper_does_not_trigger_at_minus_249_99(self):
        with TemporaryDirectory() as td:
            bot = self._bot(td, paper=True)
            bot._posiciones = {
                "MSFT": {
                    "tipo": "BUY",
                    "entrada": 100.0,
                    "cantidad": 10,
                    "cantidad_resto": 10,
                    "tp1_hit": False,
                    "orden": types.SimpleNamespace(cantidad=10),
                }
            }
            bot._last_prices = {"MSFT": 75.001}
            bot._cancelar_todas_ordenes_pendientes = lambda motivo: None
            bot._liquidar_todo = lambda motivo: None
            self.assertFalse(bot._evaluar_limite_perdida_diaria())
            self.assertFalse(bot.daily_loss_guard.is_blocked())

    def test_live_does_not_trigger_at_minus_199_99(self):
        with TemporaryDirectory() as td:
            bot = self._bot(td, paper=False)
            bot._posiciones = {
                "MSFT": {
                    "tipo": "SELL",
                    "entrada": 100.0,
                    "cantidad": 10,
                    "cantidad_resto": 10,
                    "tp1_hit": False,
                    "orden": types.SimpleNamespace(cantidad=10),
                }
            }
            bot._last_prices = {"MSFT": 119.999}
            bot._cancelar_todas_ordenes_pendientes = lambda motivo: None
            bot._liquidar_todo = lambda motivo: None
            self.assertFalse(bot._evaluar_limite_perdida_diaria())
            self.assertFalse(bot.daily_loss_guard.is_blocked())

    def test_live_limit_is_minus_200(self):
        with TemporaryDirectory() as td:
            bot = self._bot(td, paper=False)
            bot._posiciones = {
                "MSFT": {
                    "tipo": "SELL",
                    "entrada": 100.0,
                    "cantidad": 10,
                    "cantidad_resto": 10,
                    "tp1_hit": False,
                    "orden": types.SimpleNamespace(cantidad=10),
                }
            }
            bot._last_prices = {"MSFT": 120.0}
            bot._cancelar_todas_ordenes_pendientes = lambda motivo: None
            bot._liquidar_todo = lambda motivo: None
            self.assertTrue(bot._evaluar_limite_perdida_diaria())
            self.assertEqual(bot.daily_risk_snapshot()["limit_usd"], -200.0)

    def test_new_entries_are_blocked_after_trigger(self):
        with TemporaryDirectory() as td:
            bot = self._bot(td, paper=True)
            bot.gestor_riesgo = types.SimpleNamespace(max_posiciones=17)
            bot.daily_loss_guard.mark_triggered(
                True, -250.0, 0.0, "test trigger"
            )
            self.assertFalse(bot._reservar_slot("MSFT", "BUY"))
            self.assertEqual(bot._posiciones, {})

    def test_final_broker_sweep_runs_even_without_local_positions(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.paper_trading = False
        bot.account_number = "hash"
        bot.tickers = ["MSFT"]
        bot._lock = threading.RLock()
        bot._posiciones = {}
        bot._liquidados_recientes = {}
        bot.COOLDOWN_LIQUIDACION_SEG = 180
        bot._emit_log = lambda *args: None
        bot._marcar_liquidado = lambda ticker: None
        bot.schwab = types.SimpleNamespace(
            obtener_posiciones=lambda account: [
                {"symbol": "MSFT", "cantidad": 10, "tipo": "BUY"}
            ]
        )
        calls = []
        bot.ejecutor_hibrido = types.SimpleNamespace(
            liquidar_posicion=lambda *args: (
                calls.append(args) or {"status": "OK"}
            )
        )

        bot._liquidar_todo("kill switch — pérdida diaria")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "MSFT")

    def test_cancel_all_orders_only_touches_managed_tickers(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.paper_trading = False
        bot.account_number = "hash"
        bot.tickers = ["MSFT"]
        bot._lock = threading.RLock()
        bot._posiciones = {}
        bot._asegurar_token_trading = lambda: True
        bot._emit_log = lambda *args: None
        bot._audit_event = lambda *args, **kwargs: None
        bot.schwab = types.SimpleNamespace(
            obtener_ordenes_abiertas=lambda account: [
                {
                    "orderId": "msft-order",
                    "orderLegCollection": [
                        {"instrument": {"symbol": "MSFT"}}
                    ],
                },
                {
                    "orderId": "aapl-order",
                    "orderLegCollection": [
                        {"instrument": {"symbol": "AAPL"}}
                    ],
                },
            ]
        )
        canceled = []
        bot.ejecutor_hibrido = types.SimpleNamespace(
            cancelar_orden_confirmada=lambda account, oid: (
                canceled.append(oid)
                or {"broker_status": "CANCELED", "confirmado": True}
            )
        )

        bot._cancelar_todas_ordenes_pendientes("daily test")
        self.assertEqual(canceled, ["msft-order"])


class MultiAssetConfigTests(unittest.TestCase):
    EXPECTED = [
        "AAPL", "AMD", "AMZN", "BA", "META", "NVDA", "RCL", "UAL",
        "TSLA", "UNH", "PINS", "NCLH", "MSFT", "NKE", "PYPL", "NFLX",
        "ADBE",
    ]

    def test_config_contains_confirmed_17_tickers_and_limits(self):
        root = Path(__file__).parents[1]
        with (root / "config.yaml").open("r", encoding="utf-8-sig") as fh:
            cfg = yaml.safe_load(fh)
        self.assertEqual(cfg["market"]["tickers"], self.EXPECTED)
        self.assertEqual(cfg["risk"]["fixed_shares"], 20)
        self.assertEqual(cfg["risk"]["max_acciones_por_orden"], 20)
        self.assertEqual(cfg["risk"]["stop_loss_factor"], 3.5)
        self.assertEqual(cfg["risk"]["take_profit_factor"], 4.5)
        self.assertEqual(cfg["risk"]["max_simultaneous_positions"], 15)
        self.assertTrue(cfg["risk"]["paper_trading"])
        daily = cfg["kill_switch"]["daily_loss"]
        self.assertEqual(daily["paper_limit_usd"], 250.0)
        self.assertEqual(daily["live_limit_usd"], 200.0)
        self.assertTrue(daily["include_unrealized"])

    def test_engine_defaults_match_confirmed_tickers(self):
        self.assertEqual(ChocoletraBot.TICKERS_DEFAULT, self.EXPECTED)

    def test_persistent_dashboard_ticker_file_matches_confirmed_tickers(self):
        root = Path(__file__).parents[1]
        saved = json.loads((root / "data" / "tickers_activos.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, self.EXPECTED)

    def test_dashboard_exposes_daily_risk_status(self):
        html = (
            Path(__file__).parents[1] / "dashboard" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("Kill Switch diario preparado", html)
        self.assertIn("PnL neto de hoy", html)
        self.assertIn("PAPER -$250", html)
        self.assertIn("REAL -$200", html)


if __name__ == "__main__":
    unittest.main()
