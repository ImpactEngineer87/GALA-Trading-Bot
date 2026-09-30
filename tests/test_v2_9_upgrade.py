import csv
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import yaml

from MIGRATE_CONFIG_V2_9 import migrate
from agents.contracts import DecisionFinal
from agents.feedback_store import FeedbackStore, LEGACY_COLUMNAS
from agents.retraining_lifecycle import RetrainingLifecycle
from api.hybrid_order_executor import EjecutorHibridoDeOrdenes
from core.execution_quality import ExecutionQualityLedger
from core.order_audit import OrderAuditLedger
from core.position_state import (
    InvalidPositionTransition,
    PositionState,
    PositionStateMachine,
    entry_action_for_state,
    exit_action_for_state,
)
from core.trade_history import (
    HISTORY_FIELDS,
    LEGACY_HISTORY_FIELDS,
    TradeHistoryLedger,
)
from core.risk_manager import OrdenTradingCalculada
from engine.bot_engine import ChocoletraBot


class ExplicitPositionStateTests(unittest.TestCase):
    def test_out_long_out_short_transitions_are_guarded(self):
        machine = PositionStateMachine()
        self.assertEqual(machine.snapshot("AMD").state, PositionState.OUT)

        opened = machine.open("AMD", "BUY", "position-long")
        self.assertEqual(
            (opened.state_before, opened.state_after),
            (PositionState.OUT, PositionState.LONG),
        )
        with self.assertRaises(InvalidPositionTransition):
            machine.open("AMD", "SELL", "position-short")

        closed = machine.close(
            "AMD", expected_state=PositionState.LONG,
            position_id="position-long",
        )
        self.assertEqual(closed.state_after, PositionState.OUT)
        machine.open("AMD", "SELL", "position-short")
        self.assertEqual(machine.snapshot("AMD").state, PositionState.SHORT)

    def test_broker_order_actions_are_not_position_labels(self):
        self.assertEqual(entry_action_for_state(PositionState.LONG), "BUY")
        self.assertEqual(exit_action_for_state(PositionState.LONG), "SELL")
        self.assertEqual(entry_action_for_state(PositionState.SHORT), "SELL_SHORT")
        self.assertEqual(exit_action_for_state(PositionState.SHORT), "BUY_TO_COVER")


class ReversalHistoryTests(unittest.TestCase):
    def test_reversal_has_distinct_close_and_open_rows_with_one_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = TradeHistoryLedger(str(Path(tmp) / "history.csv"))
            common = {
                "modo": "PAPER", "ticker": "META", "signal_side": "SELL",
                "signal_id": "signal-sell", "reversal_id": "reversal-1",
                "correlation_id": "reversal-1",
            }
            ledger.record_close(
                **common,
                execution_id="old-execution", position_id="old-position",
                state_before="LONG", state_after="OUT", position_side="LONG",
                order_action="SELL", order_id="close-order",
                entrada=592.59, salida=590.30, cantidad=20, pnl_usd=-45.80,
                motivo="REVERSAL_CLOSE",
            )
            ledger.record_open(
                **common,
                execution_id="new-execution", position_id="new-position",
                state_before="OUT", state_after="SHORT", position_side="SHORT",
                order_action="SELL_SHORT", order_id="entry-order",
                entrada=590.30, cantidad=20, motivo="REVERSAL_OPEN",
            )

            rows = ledger.read(newest_first=False)
            self.assertEqual([row["evento"] for row in rows], [
                "POSITION_CLOSE", "POSITION_OPEN",
            ])
            self.assertEqual({row["reversal_id"] for row in rows}, {"reversal-1"})
            self.assertEqual(rows[0]["position_side"], "LONG")
            self.assertEqual(rows[0]["order_action"], "SELL")
            self.assertEqual(rows[1]["position_side"], "SHORT")
            self.assertEqual(rows[1]["order_action"], "SELL_SHORT")

    def test_v2_8_history_is_preserved_as_quarantined_legacy_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=LEGACY_HISTORY_FIELDS)
                writer.writeheader()
                writer.writerow({
                    "fecha": "2026-08-06", "hora_ny": "10:13:11",
                    "ticker": "META", "lado": "BUY", "entrada": "592.59",
                    "salida": "590.30", "cantidad": "20", "pnl_usd": "-45.8",
                    "resultado": "PERDIDA", "motivo": "reversión", "modo": "PAPER",
                })
            rows = TradeHistoryLedger(str(path)).read(newest_first=False)
            with path.open("r", encoding="utf-8", newline="") as handle:
                header = next(csv.reader(handle))

            self.assertEqual(header, HISTORY_FIELDS)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["evento"], "POSITION_CLOSE")
            self.assertEqual(rows[0]["position_side"], "LONG")
            self.assertEqual(rows[0]["order_action"], "SELL")
            self.assertTrue(list(Path(tmp).glob("history.csv.before_v2_9_*")))


class FeedbackIntegrityTests(unittest.TestCase):
    def test_only_fully_linked_execution_is_training_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "feedback.csv")
            store = FeedbackStore(path)
            decision = DecisionFinal(ticker="AMD", operar=True, tipo="SELL")
            opening = {
                "modo": "PAPER", "execution_id": "exec-1",
                "position_id": "position-1", "signal_id": "signal-1",
                "reversal_id": "reversal-1", "correlation_id": "reversal-1",
                "position_side": "SHORT", "signal_side": "SELL",
                "entry_action": "SELL_SHORT", "entry_order_id": "paper-entry",
                "state_before": "OUT", "state_after": "SHORT",
                "entry_confirmed": True, "audit_linked": True,
                "history_linked": True, "slippage_linked": True,
            }
            closing = {
                "modo": "PAPER", "signal_id": "signal-buy-later",
                "reversal_id": "reversal-2", "correlation_id": "reversal-2",
                "exit_action": "BUY_TO_COVER", "exit_order_id": "paper-close",
                "state_before": "SHORT", "state_after": "OUT",
                "exit_confirmed": True, "audit_linked": True,
                "history_linked": True,
            }
            store.abrir(decision, 100.0, 103.5, lifecycle=opening)
            row = store.cerrar("AMD", 98.0, lifecycle=closing)

            self.assertEqual(row["execution_valid"], 1)
            self.assertEqual(row["entry_reversal_id"], "reversal-1")
            self.assertEqual(row["exit_reversal_id"], "reversal-2")
            self.assertEqual(store.total_cerradas_ticker("AMD"), 1)

    def test_v2_8_feedback_is_preserved_but_cannot_train(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feedback.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=LEGACY_COLUMNAS)
                writer.writeheader()
                row = {column: "" for column in LEGACY_COLUMNAS}
                row.update({"ticker": "META", "tipo": "SELL", "ganadora": 0})
                writer.writerow(row)

            store = FeedbackStore(str(path))
            with path.open("r", encoding="utf-8", newline="") as handle:
                migrated = next(csv.DictReader(handle))
            self.assertEqual(migrated["execution_valid"], "0")
            self.assertEqual(migrated["exclusion_reason"], "LEGACY_V2_8_UNLINKED")
            self.assertEqual(store.total_cerradas_ticker("META"), 0)

    def test_retraining_counter_ignores_invalid_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feedback = root / "feedback.csv"
            with feedback.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["ticker", "ganadora", "execution_valid"]
                )
                writer.writeheader()
                writer.writerow({"ticker": "AMD", "ganadora": 0, "execution_valid": 0})
                writer.writerow({"ticker": "AMD", "ganadora": 1, "execution_valid": 1})
            lifecycle = RetrainingLifecycle(
                feedback_path=str(feedback), state_path=str(root / "state.json"),
                shadow_models_dir=str(root / "models"), warning_samples=30,
                retrain_samples=50,
            )
            self.assertEqual(lifecycle._count_feedback_rows(), {"AMD": 1})


class EngineReversalCorrelationTests(unittest.TestCase):
    class _Orchestrator:
        def __init__(self, path):
            self.feedback = FeedbackStore(path)

        def al_abrir(self, decision, price, stop, lifecycle=None):
            self.feedback.abrir(
                decision, price, stop, lifecycle=lifecycle
            )

        def al_cerrar(self, ticker, price, lifecycle=None):
            return self.feedback.cerrar(
                ticker, price, lifecycle=lifecycle
            )

    @staticmethod
    def _order(side, price):
        return OrdenTradingCalculada(
            ticker="META", tipo=side, precio_limite=price,
            stop_loss=price - 3.5 if side == "BUY" else price + 3.5,
            trailing_stop_pct=0.0, cantidad=20, riesgo_usd=70.0,
            atr_actual=1.0,
            take_profit=price + 4.5 if side == "BUY" else price - 4.5,
            take_profit_parcial=price + 3.5 if side == "BUY" else price - 3.5,
            cantidad_parcial=14, cantidad_resto=6,
            modo_resto="breakeven", salida_parcial_activa=True,
            stop_loss_atr_factor=3.5, take_profit_atr_factor=4.5,
        )

    def test_engine_links_reversal_close_open_and_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bot = ChocoletraBot.__new__(ChocoletraBot)
            bot.config = {"logging": {
                "historial_operaciones": str(root / "history.csv"),
                "auditoria_ordenes": str(root / "audit.csv"),
                "slippage_entradas": str(root / "slippage.csv"),
            }}
            bot.paper_trading = True
            bot._lock = threading.RLock()
            bot._posiciones = {}
            bot._resumen = {"buy": 0, "sell": 0, "reversiones": 0}
            bot._reversal_contexts = {}
            bot._audit_context_local = threading.local()
            bot._hooks = {}
            bot._cerrando = set()
            bot.position_states = PositionStateMachine()
            bot.trade_history = TradeHistoryLedger(str(root / "history.csv"))
            bot.order_audit = OrderAuditLedger(str(root / "audit.csv"))
            bot.execution_quality = ExecutionQualityLedger(str(root / "slippage.csv"))
            bot.ejecutor_hibrido = EjecutorHibridoDeOrdenes(
                paper_trading=True, config={"risk": {"sizing_mode": "fixed"}}
            )
            bot.ejecutor_hibrido.set_audit_hook(bot._audit_event)
            bot.motor_señales = SimpleNamespace(
                registrar_entrada=lambda **kwargs: None,
                registrar_salida=lambda ticker: None,
            )
            bot.orquestador = self._Orchestrator(str(root / "feedback.csv"))
            bot.telegram = SimpleNamespace(enviar_alerta_temporal=lambda *a, **k: None)
            bot._ahora_et = lambda: datetime(2026, 8, 6, 10, 13, 7)
            bot._emit_log = lambda *args: None
            bot._emit_trade = lambda *args: None
            bot._registrar_pnl_realizado_diario = lambda *args, **kwargs: None
            bot._evaluar_limite_perdida_diaria = lambda: False

            buy_decision = DecisionFinal(
                ticker="META", operar=True, tipo="BUY",
                timestamp=datetime(2026, 8, 6, 9, 37, 3), resumen="BUY META",
            )
            buy_decision.signal_id = "signal-buy"
            self.assertTrue(bot._registrar_posicion_abierta(
                "META", self._order("BUY", 592.59), buy_decision,
                entry_order_id="entry-buy",
                execution_context={
                    "signal_price": 592.59,
                    "submitted_limit_price": 592.70,
                    "fill_price": 592.59,
                    "submitted_order_type": "MARKETABLE_LIMIT",
                },
            ))
            old_execution_id = bot._posiciones["META"]["execution_id"]

            bot._reversal_contexts["META"] = {
                "reversal_id": "reversal-meta",
                "correlation_id": "reversal-meta",
                "signal_id": "signal-sell",
                "signal_side": "SELL",
            }
            self.assertTrue(bot._cerrar_posicion(
                "META", 590.30, motivo="reversión"
            ))
            self.assertEqual(
                bot.position_states.snapshot("META").state, PositionState.OUT
            )

            sell_decision = DecisionFinal(
                ticker="META", operar=True, tipo="SELL",
                timestamp=datetime(2026, 8, 6, 10, 13, 7), resumen="SELL META",
            )
            sell_decision.signal_id = "signal-sell"
            sell_decision.reversal_id = "reversal-meta"
            sell_decision.correlation_id = "reversal-meta"
            self.assertTrue(bot._registrar_posicion_abierta(
                "META", self._order("SELL", 590.30), sell_decision,
                entry_order_id="entry-sell",
                execution_context={
                    "signal_price": 590.30,
                    "submitted_limit_price": 590.20,
                    "fill_price": 590.30,
                    "submitted_order_type": "MARKETABLE_LIMIT",
                },
            ))

            history = bot.trade_history.read(newest_first=False)
            reversal_rows = [
                row for row in history
                if row["reversal_id"] == "reversal-meta"
            ]
            self.assertEqual(
                [row["evento"] for row in reversal_rows],
                ["POSITION_CLOSE", "POSITION_OPEN"],
            )
            self.assertEqual(reversal_rows[0]["execution_id"], old_execution_id)
            self.assertNotEqual(
                reversal_rows[0]["execution_id"],
                reversal_rows[1]["execution_id"],
            )
            self.assertEqual(reversal_rows[0]["order_action"], "SELL")
            self.assertEqual(reversal_rows[1]["order_action"], "SELL_SHORT")

            with (root / "feedback.csv").open(
                "r", encoding="utf-8", newline=""
            ) as handle:
                feedback = list(csv.DictReader(handle))
            self.assertEqual(len(feedback), 1)
            self.assertEqual(feedback[0]["execution_valid"], "1", feedback[0])
            self.assertEqual(feedback[0]["exit_reversal_id"], "reversal-meta")


class V29MigrationAndUiTests(unittest.TestCase):
    def test_config_migration_preserves_custom_daily_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(yaml.safe_dump({
                "risk": {"fixed_shares": 20, "stop_loss_factor": 3.5},
                "kill_switch": {"daily_loss": {"paper_limit_usd": 700.0}},
                "scheduler": {"hora_fin": "15:45"},
            }), encoding="utf-8")
            migrate(path)
            config = yaml.safe_load(path.read_text(encoding="utf-8"))

            self.assertEqual(
                config["kill_switch"]["daily_loss"]["paper_limit_usd"], 700.0
            )
            self.assertEqual(config["scheduler"]["hora_fin"], "15:45")
            self.assertTrue(
                config["agents"]["entrenamiento"]["require_execution_integrity"]
            )

    def test_dashboard_uses_explicit_direction_labels(self):
        html = (Path(__file__).parents[1] / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("Trading Algorítmico v2.9", html)
        self.assertIn("position_side", html)
        self.assertIn("signal_side", html)
        self.assertIn("order_action", html)
        self.assertIn("reversal_id", html)

    def test_installer_supports_windows_powershell_5_1(self):
        installer = (
            Path(__file__).parents[2]
            / "GALA10_PATCH_v2.9_REVERSAL_STATE_AUDIT"
            / "APPLY_V2_9.ps1"
        ).read_text(encoding="utf-8")
        self.assertNotIn("[System.IO.Path]::GetRelativePath", installer)
        self.assertIn("function Get-ProjectRelativePath", installer)
        self.assertIn('@("2.8.0", "2.9.0")', installer)


if __name__ == "__main__":
    unittest.main()
