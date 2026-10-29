"""Offline guards for the Schwab TSLA 3-share / STOP 3 / TP1 2 conflict.

The tests never create a SchwabClient or make a network request.
"""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from api.hybrid_order_executor import EjecutorHibridoDeOrdenes
from engine.bot_engine import ChocoletraBot


class RealPartialExitSafetyTests(unittest.TestCase):
    def test_real_partial_protection_never_submits_overlapping_tp1(self):
        executor = EjecutorHibridoDeOrdenes.__new__(EjecutorHibridoDeOrdenes)
        executor.paper_trading = False
        executor.colocar_salida = Mock(return_value={
            "status": "OK", "order_id": "stop-working",
        })
        executor._audit = Mock()
        order = SimpleNamespace(
            ticker="TSLA", tipo="SELL", cantidad=3,
            stop_loss=406.62, cantidad_parcial=2,
            take_profit_parcial=404.77,
        )

        result = executor.montar_proteccion_parcial(order, "fake-account")

        executor.colocar_salida.assert_called_once_with(
            "TSLA", "SELL", 3, 406.62, "STOP", "fake-account"
        )
        self.assertEqual(result["sl"]["order_id"], "stop-working")
        self.assertEqual(result["tp1"], {})
        self.assertTrue(result["partial_exit_suspended"])
        self.assertEqual(
            executor._audit.call_args.args[0], "TP1_SUSPENDED_CONFLICT"
        )

    def test_failed_stop_does_not_submit_tp1(self):
        executor = EjecutorHibridoDeOrdenes.__new__(EjecutorHibridoDeOrdenes)
        executor.paper_trading = False
        executor.colocar_salida = Mock(return_value={
            "status": "RECONCILIAR", "order_id": "unconfirmed-stop",
        })
        executor._audit = Mock()
        order = SimpleNamespace(
            ticker="TSLA", tipo="SELL", cantidad=3,
            stop_loss=406.62, cantidad_parcial=2,
            take_profit_parcial=404.77,
        )
        result = executor.montar_proteccion_parcial(order, "fake-account")
        self.assertEqual(result["tp1"], {})
        executor.colocar_salida.assert_called_once()

    def test_real_entry_is_blocked_before_any_broker_order(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.config = {"atr": {"period": 14}}
        bot.capital = 10000.0
        bot._is_paper_ticker = Mock(return_value=False)
        bot._real_accounting_status = Mock(
            return_value={"entry_blocked": False}
        )
        bot._daily_blocked = Mock(return_value=False)
        bot.shares_for_ticker = Mock(return_value=3)
        bot._audit_event = Mock()
        bot._emit_log = Mock()
        bot.ejecutor_hibrido = Mock()
        order = SimpleNamespace(
            salida_parcial_activa=True, cantidad_parcial=2, cantidad=3
        )
        bot.gestor_riesgo = SimpleNamespace(
            calcular_orden=Mock(return_value=order)
        )
        decision = SimpleNamespace(
            propuesta=SimpleNamespace(
                ticker="TSLA", tipo="SELL", precio_entrada=405.69,
                stop_loss=406.62, take_profit=403.50,
            )
        )

        with patch("agents.smc_primitives.atr_wilder",
                   return_value=pd.Series([1.0])):
            opened = bot._abrir_operacion(decision, 405.69, object())

        self.assertFalse(opened)
        self.assertEqual(
            decision.execution_skip_reason,
            "REAL_PARTIAL_EXIT_UNSUPPORTED",
        )
        bot.ejecutor_hibrido.enviar_entrada.assert_not_called()
        self.assertEqual(
            bot._audit_event.call_args.args[0], "ENTRY_BLOCKED"
        )

    def _open_short(self, tp1_id="tp1-rejected"):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot._activo = True
        bot._lock = threading.RLock()
        bot.account_number = "fake-account"
        bot._asegurar_token_trading = Mock(return_value=True)
        bot._audit_event = Mock()
        bot._emit_log = Mock()
        broker = Mock()
        broker._estado_orden.side_effect = (
            lambda account, oid:
            "WORKING" if oid == "stop-working" else "REJECTED"
        )
        bot.ejecutor_hibrido = broker
        position = {
            "tipo": "SELL", "salida_parcial": True,
            "tp1_hit": False, "tp1_order_id": tp1_id,
            "sl_order_id": "stop-working",
            "sl_reconcile_pending": False,
            "tp_final": 403.5,
        }
        bot._posiciones = {"TSLA": position}
        return bot, position

    def test_existing_rejected_tp1_is_reported_once_without_retry(self):
        bot, pos = self._open_short()
        bot._gestionar_real("TSLA", pos, 405.69)
        bot._gestionar_real("TSLA", pos, 405.69)

        self.assertTrue(pos["tp1_terminal_reported"])
        self.assertEqual(pos["tp1_order_id"], "tp1-rejected")
        self.assertEqual(pos["sl_order_id"], "stop-working")
        self.assertEqual(pos["tp1_reconcile_pending"], False)
        self.assertEqual(bot._audit_event.call_count, 1)
        self.assertEqual(
            bot._audit_event.call_args.args[0], "TP1_TERMINAL_UNFILLED"
        )
        bot.ejecutor_hibrido.colocar_salida.assert_not_called()
        bot.ejecutor_hibrido.cancelar_orden_confirmada.assert_not_called()

    def test_existing_position_without_tp1_does_not_auto_retry(self):
        bot, pos = self._open_short(tp1_id="")
        bot._gestionar_real("TSLA", pos, 405.69)
        bot._gestionar_real("TSLA", pos, 405.69)

        self.assertTrue(pos["tp1_suspended_reported"])
        self.assertEqual(pos["sl_order_id"], "stop-working")
        self.assertEqual(bot._audit_event.call_count, 1)
        self.assertEqual(
            bot._audit_event.call_args.args[0], "TP1_SUSPENDED_CONFLICT"
        )
        bot.ejecutor_hibrido.colocar_salida.assert_not_called()


if __name__ == "__main__":
    unittest.main()
