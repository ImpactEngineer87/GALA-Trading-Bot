"""Offline regressions for REAL order-monitor startup and executor compatibility."""

import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from api.hybrid_order_executor import EjecutorHibridoDeOrdenes
from engine.bot_engine import ChocoletraBot


class OrderMonitorTests(unittest.TestCase):
    def setUp(self):
        request_patch = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Unexpected broker/HTTP request"),
        )
        socket_patch = patch(
            "socket.socket.connect",
            side_effect=AssertionError("Unexpected network connection"),
        )
        self.request = request_patch.start()
        self.socket_connect = socket_patch.start()
        self.addCleanup(request_patch.stop)
        self.addCleanup(socket_patch.stop)

    def tearDown(self):
        self.request.assert_not_called()
        self.socket_connect.assert_not_called()

    def executor(self, config=None):
        executor = EjecutorHibridoDeOrdenes(paper_trading=False, config=config)
        self.addCleanup(executor._session.close)
        return executor

    def bot(self, positions=None, orders=None, config=None):
        # Avoid the full constructor: it loads operator configuration, model
        # artifacts, runtime state and notification services unrelated to this
        # startup regression. The production monitor and executor still run.
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.config = config or {}
        bot.ejecutor_hibrido = self.executor(config)
        provider = Mock(return_value=[] if orders is None else orders)
        bot.ejecutor_hibrido.set_working_orders_provider(provider)
        bot.paper_trading = False
        bot.per_ticker_execution = False
        bot.tickers = ["AAPL"]
        bot.account_number = "offline-account"
        bot._activo = True
        bot._posiciones = positions or {}
        bot._lock = threading.RLock()
        bot._order_monitor_stop = threading.Event()
        bot._order_monitor_thread = None
        bot._emit_log = Mock()
        bot._gestionar_entrada_pendiente = Mock()
        bot._gestionar_real = Mock()
        self.addCleanup(bot._detener_monitor_ordenes)
        return bot, provider

    def run_one_iteration(self, bot, interval_s=1.0):
        def finish_iteration(timeout):
            bot._order_monitor_stop.set()
            return True

        with patch.object(
            bot._order_monitor_stop, "wait", side_effect=finish_iteration,
        ) as wait:
            bot._order_monitor_loop()
        wait.assert_called_once_with(interval_s)
        self.assertFalse(any(
            call.args[0] == "ERROR" for call in bot._emit_log.call_args_list
        ), bot._emit_log.call_args_list)

    def test_monitor_defaults_are_independent_of_fast_order_sync(self):
        executor = self.executor()
        self.assertEqual(executor.monitor_interval_ms, 1000)
        self.assertEqual(executor.poll_interval_ms, 100)
        self.assertEqual(executor._sync.poll_interval, 0.1)

    def test_monitor_override_preserves_fast_order_sync_configuration(self):
        executor = self.executor({"order_sync": {
            "monitor_interval_ms": "2500", "poll_interval_ms": 75,
        }})
        self.assertEqual(executor.monitor_interval_ms, 2500)
        self.assertEqual(executor.poll_interval_ms, 75)
        self.assertEqual(executor._sync.poll_interval, 0.075)

    def test_monitor_configuration_has_one_second_minimum(self):
        for interval in (-10, 0, 100, 999, 1000):
            with self.subTest(interval=interval):
                executor = self.executor({
                    "order_sync": {"monitor_interval_ms": interval},
                })
                self.assertEqual(executor.monitor_interval_ms, 1000)

    def test_real_monitor_starts_once_and_stops_without_broker_calls(self):
        bot, provider = self.bot()
        bot._iniciar_monitor_ordenes()
        worker = bot._order_monitor_thread
        self.assertIsNotNone(worker)
        self.assertTrue(worker.is_alive())
        bot._emit_log.assert_called_once_with(
            "INFO", "Monitor de órdenes activo cada 1000ms",
        )

        bot._iniciar_monitor_ordenes()
        self.assertIs(bot._order_monitor_thread, worker)
        bot._emit_log.assert_called_once()

        bot._detener_monitor_ordenes()
        self.assertFalse(worker.is_alive())
        self.assertIsNone(bot._order_monitor_thread)
        provider.assert_not_called()

    def test_pending_real_position_is_monitored_without_optional_scope(self):
        position = {
            "execution_mode": "REAL", "pendiente": True,
            "orden": SimpleNamespace(precio_limite=123.45),
        }
        orders = [{"orderId": 123, "status": "WORKING"}]
        bot, provider = self.bot({"AAPL": position}, orders)
        self.assertFalse(hasattr(bot.ejecutor_hibrido, "order_observation_scope"))
        self.run_one_iteration(bot)
        provider.assert_called_once_with("offline-account")
        bot._gestionar_entrada_pendiente.assert_called_once_with(
            "AAPL", position, 123.45,
        )
        bot._gestionar_real.assert_not_called()

    def test_optional_observation_scope_wraps_position_management(self):
        position = {"execution_mode": "REAL", "entrada": 123.45}
        orders = [{"orderId": 123, "status": "WORKING"}]
        bot, provider = self.bot({"AAPL": position}, orders)
        events = []

        @contextmanager
        def scope(snapshot):
            self.assertIs(snapshot, orders)
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        factory = Mock(side_effect=scope)
        bot._gestionar_real.side_effect = lambda *args: events.append("manage")
        with patch.object(
            bot.ejecutor_hibrido, "order_observation_scope", factory, create=True,
        ):
            self.run_one_iteration(bot)
        factory.assert_called_once_with(orders)
        provider.assert_called_once_with("offline-account")
        self.assertEqual(events, ["enter", "manage", "exit"])
        bot._gestionar_real.assert_called_once_with("AAPL", position, 123.45)

    def test_monitor_skips_paper_and_reserved_positions(self):
        real = {"execution_mode": "REAL", "entrada": 123.45}
        bot, provider = self.bot({
            "AAPL": real,
            "MSFT": {"execution_mode": "PAPER", "entrada": 200.0},
            "NVDA": {"execution_mode": "REAL", "reservado": True},
        })
        self.run_one_iteration(bot)
        provider.assert_called_once_with("offline-account")
        bot._gestionar_real.assert_called_once_with("AAPL", real, 123.45)
        bot._gestionar_entrada_pendiente.assert_not_called()

    def test_paper_only_bot_does_not_start_real_monitor(self):
        bot, provider = self.bot()
        bot.paper_trading = True
        bot._iniciar_monitor_ordenes()
        self.assertIsNone(bot._order_monitor_thread)
        bot._emit_log.assert_not_called()
        provider.assert_not_called()

    def test_engine_retains_interval_floor_for_older_executor_instances(self):
        bot, provider = self.bot()
        bot.ejecutor_hibrido.monitor_interval_ms = 100
        self.run_one_iteration(bot, interval_s=1.0)
        provider.assert_not_called()

    def test_configured_slower_interval_reaches_monitor_wait(self):
        bot, provider = self.bot(config={
            "order_sync": {"monitor_interval_ms": 2500},
        })
        self.run_one_iteration(bot, interval_s=2.5)
        provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
