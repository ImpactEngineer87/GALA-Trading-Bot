import threading
import types
import unittest
from contextlib import contextmanager

from engine.bot_engine import ChocoletraBot


class _SequencedExecutor:
    def __init__(self, events):
        self.events = events

    @contextmanager
    def secuencia_simbolo(self, ticker):
        self.events.append(("lock", ticker))
        yield

    def cancelar_orden_confirmada(self, account, order_id):
        self.events.append(("cancel_confirmed", order_id))
        return {"status": "CANCELED", "order_id": order_id}

    def liquidar_posicion(self, ticker, tipo, cantidad, account, motivo):
        self.events.append(("flat_confirmed", ticker, tipo, cantidad, motivo))
        return {"status": "OK", "order_id": "close-1"}


class ExecutionModeSafetyTests(unittest.TestCase):
    def test_paper_mode_updates_engine_and_executor(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot._activo = False
        bot.config = {"risk": {"paper_trading": False}}
        bot.paper_trading = False
        bot._trading_listo = True
        bot.ejecutor_hibrido = types.SimpleNamespace(
            paper_trading=False,
            set_token=lambda value: None,
        )

        bot.configurar_modo_ejecucion(True)

        self.assertTrue(bot.paper_trading)
        self.assertTrue(bot.ejecutor_hibrido.paper_trading)
        self.assertTrue(bot.config["risk"]["paper_trading"])
        self.assertFalse(bot._trading_listo)

    def test_mode_cannot_change_while_running(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot._activo = True
        with self.assertRaises(RuntimeError):
            bot.configurar_modo_ejecucion(True)

    def test_periodic_reconciliation_ignores_unmanaged_symbol(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.paper_trading = False
        bot.account_number = "hash"
        bot._activo = True
        bot.tickers = ["MSFT"]
        bot._lock = threading.RLock()
        bot._posiciones = {}
        bot._liquidados_recientes = {}
        bot._orden_huerfana_ciclos = {}
        bot.COOLDOWN_LIQUIDACION_SEG = 180
        bot._asegurar_token_trading = lambda: True
        bot._emit_log = lambda *args: None
        bot.schwab = types.SimpleNamespace(
            obtener_posiciones=lambda account: [
                {"symbol": "AAPL", "tipo": "BUY", "cantidad": 4, "precio": 100},
                {"symbol": "MSFT", "tipo": "BUY", "cantidad": 1, "precio": 200},
            ],
            obtener_ordenes_abiertas=lambda account: [],
        )
        adopted = []
        bot._adoptar_huerfana = lambda ticker, pos: adopted.append(ticker)
        bot.ejecutor_hibrido = types.SimpleNamespace(cancelar_orden=lambda *a: True)

        bot._reconciliar_huerfanas()

        self.assertEqual(adopted, ["MSFT"])

    def test_failed_position_read_is_not_interpreted_as_flat(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.account_number = "hash"
        bot.schwab = types.SimpleNamespace(
            obtener_posiciones=lambda account: None
        )
        self.assertIsNone(bot._cantidad_real_broker("MSFT", "BUY"))

    def test_opposite_broker_position_is_not_interpreted_as_flat(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.account_number = "hash"
        bot._emit_log = lambda *args: None
        bot.schwab = types.SimpleNamespace(
            obtener_posiciones=lambda account: [
                {"symbol": "MSFT", "tipo": "SELL", "cantidad": 2}
            ]
        )
        self.assertIsNone(bot._cantidad_real_broker("MSFT", "BUY"))

    def test_real_final_target_uses_confirmed_close_sequence(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot._activo = True
        bot._lock = threading.RLock()
        bot.account_number = "hash"
        bot._posiciones = {}
        bot._asegurar_token_trading = lambda: True
        closes = []
        bot._cerrar_posicion = lambda ticker, price, motivo="": closes.append(
            (ticker, price, motivo)
        )
        bot.ejecutor_hibrido = types.SimpleNamespace(
            _estado_orden=lambda *args: "WORKING",
            colocar_salida=lambda *args: {"status": "OK", "order_id": "stop"},
        )
        pos = {
            "pendiente": False,
            "sl_order_id": "stop",
            "salida_parcial": True,
            "tp1_hit": True,
            "tp1_order_id": "tp1",
            "tipo": "BUY",
            "cantidad": 3,
            "cantidad_resto": 1,
            "sl_actual": 100.0,
            "tp_final": 110.0,
        }
        bot._posiciones["MSFT"] = pos

        bot._gestionar_real("MSFT", pos, 110.25)

        self.assertEqual(closes, [("MSFT", 110.0, "TP final")])

    def _reversal_bot(self, close_ok=True):
        events = []
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot._activo = True
        bot._lock = threading.RLock()
        bot._cerrando = set()
        bot._posiciones = {
            "MSFT": {
                "tipo": "BUY",
                "entrada": 100.0,
                "cantidad": 10,
                "cantidad_resto": 10,
                "tp1_hit": False,
                "pnl_realizado": 0.0,
                "pendiente": False,
                "reservado": False,
                "tp1_order_id": "tp-old",
                "sl_order_id": "sl-old",
                "orden": types.SimpleNamespace(cantidad=10),
            }
        }
        bot._resumen = {"reversiones": 0}
        bot.reversion_activa = True
        bot.paper_trading = False
        bot.account_number = "safe-hash"
        bot.gestor_riesgo = types.SimpleNamespace(max_posiciones=3)
        bot.ejecutor_hibrido = _SequencedExecutor(events)
        bot._asegurar_token_trading = lambda: True
        bot._asegurar_ticker_limpio = lambda ticker: (
            events.append(("orders_clear", ticker)) or close_ok
        )
        bot._cantidad_real_broker = lambda ticker, tipo: (
            events.append(("broker_qty", ticker, tipo)) or 10
        )
        bot._marcar_liquidado = lambda ticker: events.append(
            ("marked_flat", ticker)
        )
        bot.motor_señales = types.SimpleNamespace(
            registrar_salida=lambda ticker: events.append(
                ("signal_closed", ticker)
            )
        )
        bot.orquestador = types.SimpleNamespace(
            al_cerrar=lambda ticker, price: events.append(
                ("learning_closed", ticker)
            )
        )
        bot._emit_trade = lambda *args: None
        bot._emit_log = lambda *args: None
        bot._registrar_operacion_historial = lambda *args: None

        def open_opposite(decision, price, df_micro):
            events.append(("opposite_open", decision.ticker, decision.tipo))
            bot._posiciones[decision.ticker] = {
                "tipo": decision.tipo,
                "reservado": False,
            }
            return True

        bot._abrir_operacion = open_opposite
        return bot, events

    def test_reversal_confirms_cancel_and_flat_before_opposite_open(self):
        bot, events = self._reversal_bot(close_ok=True)
        decision = types.SimpleNamespace(ticker="MSFT", tipo="SELL")

        bot._abrir_o_revertir(decision, 99.5, df_micro=None)

        names = [event[0] for event in events]
        self.assertEqual(
            names[:6],
            [
                "lock",
                "cancel_confirmed",
                "cancel_confirmed",
                "orders_clear",
                "broker_qty",
                "flat_confirmed",
            ],
        )
        self.assertLess(
            names.index("flat_confirmed"),
            names.index("opposite_open"),
        )
        self.assertEqual(bot._posiciones["MSFT"]["tipo"], "SELL")
        self.assertEqual(bot._resumen["reversiones"], 1)

    def test_reversal_never_opens_opposite_when_cleanup_is_unconfirmed(self):
        bot, events = self._reversal_bot(close_ok=False)
        decision = types.SimpleNamespace(ticker="MSFT", tipo="SELL")

        bot._abrir_o_revertir(decision, 99.5, df_micro=None)

        names = [event[0] for event in events]
        self.assertNotIn("flat_confirmed", names)
        self.assertNotIn("opposite_open", names)
        self.assertEqual(bot._posiciones["MSFT"]["tipo"], "BUY")
        self.assertEqual(bot._resumen["reversiones"], 0)

    def test_restart_readopts_only_managed_position_and_protections(self):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.paper_trading = False
        bot.account_number = "safe-hash"
        bot.tickers = ["MSFT"]
        bot._lock = threading.RLock()
        bot._posiciones = {}
        bot.gestor_riesgo = types.SimpleNamespace(modo_resto="BREAKEVEN")
        bot._emit_log = lambda *args: None
        bot.schwab = types.SimpleNamespace(
            obtener_posiciones=lambda account: [
                {
                    "symbol": "MSFT",
                    "tipo": "BUY",
                    "cantidad": 10,
                    "precio": 390.0,
                },
                {
                    "symbol": "AAPL",
                    "tipo": "SELL",
                    "cantidad": 5,
                    "precio": 210.0,
                },
            ],
            obtener_ordenes_abiertas=lambda account: [
                {
                    "orderId": "msft-stop",
                    "orderType": "STOP",
                    "stopPrice": 388.0,
                    "orderLegCollection": [{
                        "instrument": {"symbol": "MSFT"},
                    }],
                },
                {
                    "orderId": "msft-tp1",
                    "orderType": "LIMIT",
                    "orderLegCollection": [{
                        "instrument": {"symbol": "MSFT"},
                    }],
                },
                {
                    "orderId": "aapl-stop",
                    "orderType": "STOP",
                    "stopPrice": 212.0,
                    "orderLegCollection": [{
                        "instrument": {"symbol": "AAPL"},
                    }],
                },
            ],
        )

        bot._reconciliar_al_iniciar()

        self.assertEqual(set(bot._posiciones), {"MSFT"})
        recovered = bot._posiciones["MSFT"]
        self.assertTrue(recovered["readoptada"])
        self.assertEqual(recovered["sl_order_id"], "msft-stop")
        self.assertEqual(recovered["tp1_order_id"], "msft-tp1")
        self.assertEqual(recovered["sl_actual"], 388.0)


if __name__ == "__main__":
    unittest.main()
