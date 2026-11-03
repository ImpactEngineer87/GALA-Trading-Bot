"""Manual broker positions and orders are never changed by GALA recovery."""
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from core.order_audit import OrderAuditLedger
from core.real_accounting import RealAccountingGuard
from engine.bot_engine import ChocoletraBot
from tests.test_manual_close_reconciliation import order


class ManualOwnershipTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bot = ChocoletraBot.__new__(ChocoletraBot)
        bot = self.bot
        bot._lock = threading.RLock()
        bot.config = {}
        bot._posiciones = {}
        bot.tickers = ["TSLA"]
        bot.account_number = "offline-account"
        bot.order_audit = OrderAuditLedger(Path(temp.name) / "audit.csv")
        bot.real_accounting_guard = RealAccountingGuard(Path(temp.name) / "accounting.json")
        bot._emit_log = Mock()
        bot._audit_event = Mock()
        bot._set_broker_reconciliation = Mock(return_value={"ok": False, "flat": False})
        bot._refresh_broker_reconciliation = Mock(return_value={"ok": False, "flat": False})
        bot._has_real_tickers = Mock(return_value=True)
        bot._real_tickers = Mock(return_value=["TSLA"])
        bot._asegurar_token_trading = Mock(return_value=True)
        bot._is_paper_ticker = Mock(return_value=False)
        bot._ticker_quarantine = {}
        bot._ticker_quarantine_health = {}
        bot._liquidados_recientes = {}
        bot._ahora_et = lambda: datetime.now(ZoneInfo("America/New_York"))
        bot.schwab = Mock()
        bot.ejecutor_hibrido = Mock()
        bot._activo = True
        self.position = {"symbol": "TSLA", "tipo": "BUY", "cantidad": 3,
                         "precio": 410.8}
        self.manual_order = order("chart-entry", "BUY", 410.8,
                                  bot._ahora_et(), tag="API_TOS:CHART")
        self.working = {"orderId": "chart-stop", "status": "WORKING",
                        "orderLegCollection": [{"instrument": {
                            "assetType": "EQUITY", "symbol": "TSLA"}}]}

    def test_startup_quarantines_manual_position_without_adoption(self):
        bot = self.bot
        bot._broker_read_with_retry = Mock(side_effect=[
            (True, [self.position], ""), (True, [], "")])
        bot.schwab.obtener_ordenes_sesion.return_value = [self.manual_order]
        bot._state_machine = Mock()
        self.assertTrue(bot._reconciliar_al_iniciar())
        self.assertEqual(bot._ticker_quarantine["TSLA"], "EXTERNAL_BROKER_POSITION")
        self.assertNotIn("TSLA", bot._posiciones)
        bot._state_machine.assert_not_called()
        self.assertTrue(bot._real_accounting_status()["entry_blocked"])

    def test_working_manual_order_is_never_canceled(self):
        bot = self.bot
        bot._broker_read_with_retry = Mock(return_value=(True, [self.working], ""))
        self.assertFalse(bot._asegurar_ticker_limpio("TSLA"))
        self.assertFalse(bot._cancelar_ordenes_simbolo("TSLA"))
        bot.ejecutor_hibrido.cancelar_orden.assert_not_called()
        bot.schwab.obtener_ordenes_abiertas.return_value = [self.working]
        self.assertFalse(bot._cancelar_todas_ordenes_pendientes("test"))
        bot.ejecutor_hibrido.cancelar_orden.assert_not_called()

    def test_shutdown_sweep_does_not_liquidate_manual_position(self):
        bot = self.bot
        bot.ks_max_liquidation_attempts = 1
        bot.ks_retry_delay_sec = 0
        bot.schwab.obtener_posiciones.return_value = [self.position]
        bot.schwab.obtener_ordenes_sesion.return_value = [self.manual_order]
        bot._liquidar_todo("test")
        bot.ejecutor_hibrido.liquidar_posicion.assert_not_called()


if __name__ == "__main__":
    unittest.main()
