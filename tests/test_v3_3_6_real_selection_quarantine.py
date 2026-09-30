from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from core.ticker_execution import TickerExecutionRegistry
from engine.bot_engine import ChocoletraBot


class RealSelectionQuarantineTests(unittest.TestCase):
    def _bot(self, registry):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.config = {"live_trading": {"runtime_isolate_ticker_failures": True}}
        bot.ticker_execution = registry
        bot.per_ticker_execution = True
        bot._ticker_quarantine = {}
        bot._ticker_quarantine_health = {}
        bot._audit_event = lambda *args, **kwargs: None
        bot._emit_log = lambda *args, **kwargs: None
        return bot

    def test_runtime_market_data_quarantine_preserves_saved_real_selection(self):
        with TemporaryDirectory() as td:
            reg = TickerExecutionRegistry(str(Path(td) / "ticker_execution.json"))
            reg.update("META", mode="REAL", shares=3)
            bot = self._bot(reg)

            bot._quarantine_real_ticker("META", "Synthetic market data")

            self.assertEqual(reg.get("META")["mode"], "REAL")
            self.assertEqual(bot._ticker_quarantine["META"], "Synthetic market data")

    def test_market_data_quarantine_requires_two_healthy_cycles_then_recovers(self):
        with TemporaryDirectory() as td:
            reg = TickerExecutionRegistry(str(Path(td) / "ticker_execution.json"))
            reg.update("NVDA", mode="REAL", shares=3)
            bot = self._bot(reg)
            bot._quarantine_real_ticker("NVDA", "Synthetic market data")

            self.assertFalse(bot._recover_ticker_quarantine("NVDA", "market_data"))
            self.assertIn("NVDA", bot._ticker_quarantine)
            self.assertTrue(bot._recover_ticker_quarantine("NVDA", "market_data"))
            self.assertNotIn("NVDA", bot._ticker_quarantine)
            self.assertEqual(reg.get("NVDA")["mode"], "REAL")

    def test_quarantine_still_blocks_new_real_slot(self):
        with TemporaryDirectory() as td:
            reg = TickerExecutionRegistry(str(Path(td) / "ticker_execution.json"))
            reg.update("TSLA", mode="REAL", shares=3)
            bot = self._bot(reg)
            bot._ticker_quarantine["TSLA"] = "Synthetic market data"
            bot._activo = True
            bot._daily_blocked = lambda: False
            bot._posiciones = {}
            bot._lock = __import__('threading').RLock()
            bot.max_paper_positions = 30
            bot.max_real_positions = 30
            bot.max_total_positions = 60

            self.assertFalse(bot._reservar_slot("TSLA", "BUY"))
            self.assertEqual(reg.get("TSLA")["mode"], "REAL")

    def test_startup_preflight_no_longer_persists_paper_fallback(self):
        text = Path(__file__).resolve().parents[1].joinpath("server.py").read_text(encoding="utf-8")
        start = text.index("def _start_engine_controlled")
        end = text.index("def _stop_engine_controlled", start)
        section = text[start:end]
        self.assertIn("REAL EN CUARENTENA TEMPORAL", section)
        self.assertNotIn('mode="PAPER"', section)


if __name__ == "__main__":
    unittest.main()
