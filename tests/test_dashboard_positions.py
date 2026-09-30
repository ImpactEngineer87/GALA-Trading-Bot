import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from engine.bot_engine import ChocoletraBot


class DashboardPositionSnapshotTests(unittest.TestCase):
    def _bot(self, positions, last_prices=None):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot._lock = threading.RLock()
        bot._posiciones = positions
        bot._last_prices = last_prices or {}
        bot.paper_trading = True
        return bot

    def test_open_position_reports_initial_and_remaining_quantity(self):
        order = SimpleNamespace(
            cantidad=10,
            cantidad_parcial=7,
            cantidad_resto=3,
            tipo="BUY",
        )
        bot = self._bot({
            "MSFT": {
                "tipo": "BUY",
                "cantidad": 10,
                "cantidad_resto": 3,
                "entrada": 450.0,
                "sl": 445.0,
                "sl_actual": 445.0,
                "tp1": 455.0,
                "tp_final": 460.0,
                "tp1_hit": False,
                "modo_resto": "breakeven",
                "pnl_realizado": 0.0,
                "orden": order,
                "abierta_ts": datetime(2026, 7, 30, 10, 0, 0),
            }
        }, last_prices={"MSFT": 452.0})

        row = bot.dashboard_snapshot()[0]
        self.assertEqual(row["status"], "OPEN")
        self.assertEqual(row["initial_quantity"], 10)
        self.assertEqual(row["remaining_quantity"], 10)
        self.assertEqual(row["planned_partial_quantity"], 7)
        self.assertEqual(row["planned_remaining_quantity"], 3)
        self.assertFalse(row["breakeven_active"])
        self.assertEqual(row["last_price"], 452.0)
        self.assertEqual(row["floating_pnl"], 20.0)

    def test_partial_position_reports_seven_executed_and_three_remaining(self):
        order = SimpleNamespace(
            cantidad=10,
            cantidad_parcial=7,
            cantidad_resto=3,
            tipo="SELL",
        )
        bot = self._bot({
            "MSFT": {
                "tipo": "SELL",
                "cantidad": 10,
                "cantidad_resto": 3,
                "entrada": 449.84,
                "sl": 454.0,
                "sl_actual": 449.97,
                "tp1": 447.61,
                "tp_final": 445.83,
                "tp1_hit": True,
                "modo_resto": "breakeven",
                "pnl_realizado": 15.61,
                "partial_exit_at": datetime(2026, 7, 30, 11, 5, 0),
                "partial_exit_price": 447.61,
                "orden": order,
                "abierta_ts": datetime(2026, 7, 30, 10, 52, 0),
            }
        })

        row = bot.dashboard_snapshot()[0]
        self.assertEqual(row["status"], "PARTIAL")
        self.assertEqual(row["initial_quantity"], 10)
        self.assertEqual(row["partial_quantity_executed"], 7)
        self.assertEqual(row["remaining_quantity"], 3)
        self.assertTrue(row["breakeven_active"])
        self.assertEqual(row["stop_current"], 449.97)
        self.assertEqual(row["partial_exit_price"], 447.61)
        self.assertTrue(row["partial_exit_at"].startswith("2026-07-30T11:05"))

    def test_reserved_entries_are_not_shown_as_open_positions(self):
        bot = self._bot({"MSFT": {"reservado": True, "tipo": "BUY"}})
        self.assertEqual(bot.dashboard_snapshot(), [])


class DashboardMarkupTests(unittest.TestCase):
    def test_dashboard_distinguishes_positions_from_entry_events(self):
        html = (Path(__file__).parents[1] / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("Posiciones abiertas — estado real del motor", html)
        self.assertIn("ENTRY es un evento histórico de entrada", html)
        self.assertIn("Cantidad inicial", html)
        self.assertIn("Cantidad restante", html)
        self.assertIn("function renderPositions", html)
        self.assertIn("function renderTradeEvents", html)
        self.assertIn("floating_pnl", html)
        self.assertIn("P&amp;L flotante", html)


if __name__ == "__main__":
    unittest.main()
