"""Offline regressions: terminal REAL stops must not leave stale local exposure.

These tests use mocks only.  They never contact Schwab or submit an order.
A broker-flat observation is NOT proof of the historical exit price/identity.
"""
import threading
import unittest
from unittest.mock import Mock, patch

from engine.bot_engine import ChocoletraBot


class TerminalStopReconciliationTests(unittest.TestCase):
    def setUp(self):
        request = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Unexpected broker/HTTP request"),
        )
        socket = patch(
            "socket.socket.connect",
            side_effect=AssertionError("Unexpected network connection"),
        )
        self.request = request.start()
        self.socket = socket.start()
        self.addCleanup(request.stop)
        self.addCleanup(socket.stop)

    def tearDown(self):
        self.request.assert_not_called()
        self.socket.assert_not_called()

    def bot(self, status, remaining):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot._activo = True
        bot.account_number = "offline-account"
        bot._lock = threading.RLock()
        position = {
            "tipo": "SELL", "entrada": 184.43,
            "sl_actual": 185.00,
            "sl_order_id": "old-stop",
            "sl_reconcile_pending": False,
            "salida_parcial": False,
            "tp1_hit": False,
            "normal_exit_progress": {},
            "tp_final": 0.0,
        }
        bot._posiciones = {"BA": position}
        bot._asegurar_token_trading = Mock(return_value=True)
        bot._cantidad_real_broker = Mock(return_value=remaining)
        bot._cerrar_posicion = Mock(return_value=True)
        bot._audit_event = Mock()
        bot.ejecutor_hibrido = Mock()
        bot.ejecutor_hibrido._estado_orden.return_value = status
        bot.ejecutor_hibrido.colocar_salida.side_effect = AssertionError(
            "No new exit order may be placed by terminal-STOP triage"
        )
        bot.ejecutor_hibrido.liquidar_posicion.side_effect = AssertionError(
            "No duplicate liquidation may be sent by terminal-STOP triage"
        )
        return bot, position

    def test_flat_terminal_stop_reconciles_local_tracking_not_broker_orders(self):
        for status in ("CANCELED", "REPLACED", "REJECTED", "EXPIRED"):
            with self.subTest(status=status):
                bot, position = self.bot(status, 0)
                bot._gestionar_real("BA", position, 183.07)
                bot._cantidad_real_broker.assert_called_once_with("BA", "SELL")
                bot._cerrar_posicion.assert_called_once_with(
                    "BA", 183.07,
                    motivo="reconciliacion: STOP terminal, broker plano",
                )
                bot._audit_event.assert_called_once()
                self.assertEqual(
                    bot._audit_event.call_args.args[0], "STOP_TERMINAL_BROKER_FLAT"
                )
                self.assertFalse(position["sl_reconcile_pending"])
                bot.ejecutor_hibrido.colocar_salida.assert_not_called()
                bot.ejecutor_hibrido.liquidar_posicion.assert_not_called()

    def test_unknown_broker_position_does_not_release_or_replace(self):
        bot, position = self.bot("CANCELED", None)
        bot._gestionar_real("BA", position, 183.07)
        self.assertFalse(position["sl_reconcile_pending"])
        self.assertEqual(position["sl_order_id"], "old-stop")
        bot._cerrar_posicion.assert_not_called()
        bot._audit_event.assert_not_called()
        bot.ejecutor_hibrido.colocar_salida.assert_not_called()

    def test_exposure_with_terminal_stop_enters_guarded_reconciliation(self):
        bot, position = self.bot("REPLACED", 5)
        bot._gestionar_real("BA", position, 183.07)
        self.assertTrue(position["sl_reconcile_pending"])
        self.assertEqual(position["sl_order_id"], "old-stop")
        bot._cerrar_posicion.assert_not_called()
        bot._audit_event.assert_called_once()
        self.assertEqual(
            bot._audit_event.call_args.args[0], "STOP_TERMINAL_RECONCILE_REQUIRED"
        )
        bot.ejecutor_hibrido.colocar_salida.assert_not_called()
        bot.ejecutor_hibrido.liquidar_posicion.assert_not_called()

    def test_terminal_stop_is_checked_before_any_tp1_submission(self):
        bot, position = self.bot("REPLACED", 5)
        position.update(
            salida_parcial=True, tp1_order_id="", tp1_hit=False,
            sl_reconcile_pending=False, orden=Mock(cantidad_parcial=3),
            tp1=182.50,
        )
        bot._gestionar_real("BA", position, 183.07)
        self.assertTrue(position["sl_reconcile_pending"])
        bot.ejecutor_hibrido.colocar_salida.assert_not_called()
        bot._cerrar_posicion.assert_not_called()

    def test_verified_stop_fill_keeps_existing_close_path(self):
        bot, position = self.bot("FILLED", 0)
        bot._gestionar_real("BA", position, 183.07)
        bot._cerrar_posicion.assert_called_once_with(
            "BA", 185.00, motivo="SL(real)"
        )
        bot._cantidad_real_broker.assert_not_called()
        bot._audit_event.assert_not_called()

    def test_working_stop_does_not_trigger_extra_broker_read(self):
        bot, position = self.bot("WORKING", 5)
        bot._gestionar_real("BA", position, 183.07)
        bot._cantidad_real_broker.assert_not_called()
        bot._cerrar_posicion.assert_not_called()
        bot._audit_event.assert_not_called()


if __name__ == "__main__":
    unittest.main()
