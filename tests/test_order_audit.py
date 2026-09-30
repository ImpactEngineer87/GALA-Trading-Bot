import csv
import tempfile
import threading
import unittest
from pathlib import Path

from api.hybrid_order_executor import ContextoMercado, EjecutorHibridoDeOrdenes
from core.execution_quality import (
    EXECUTION_QUALITY_FIELDS,
    ExecutionQualityLedger,
)
from core.order_audit import AUDIT_FIELDS, OrderAuditLedger
from core.risk_manager import OrdenTradingCalculada


def _order():
    return OrdenTradingCalculada(
        ticker="MSFT",
        tipo="BUY",
        precio_limite=400.0,
        stop_loss=398.0,
        trailing_stop_pct=0.0,
        cantidad=10,
        riesgo_usd=20.0,
        atr_actual=1.0,
        take_profit=404.0,
        take_profit_parcial=402.0,
        cantidad_parcial=7,
        cantidad_resto=3,
        modo_resto="breakeven",
        salida_parcial_activa=True,
    )


class OrderAuditLedgerTests(unittest.TestCase):
    def test_events_persist_and_are_read_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.csv"
            ledger = OrderAuditLedger(str(path))
            first = ledger.record(
                "ENTRY_SUBMITTED",
                modo="paper",
                ticker="msft",
                lado="buy",
                estado="working",
                order_id="101",
                cantidad=10,
                precio=400,
            )
            second = ledger.record(
                "POSITION_CLOSED",
                modo="paper",
                ticker="msft",
                lado="buy",
                estado="closed",
                cantidad=10,
                precio=402,
                pnl_usd=20,
            )

            restarted = OrderAuditLedger(str(path))
            rows = restarted.read()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["event_id"], second["event_id"])
            self.assertEqual(rows[1]["event_id"], first["event_id"])
            self.assertEqual(rows[0]["evento"], "POSITION_CLOSED")
            self.assertEqual(rows[0]["modo"], "PAPER")

    def test_concurrent_writes_do_not_lose_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = OrderAuditLedger(str(Path(tmp) / "audit.csv"))

            def writer(index):
                ledger.record(
                    "CANCEL_CONFIRMED",
                    modo="REAL",
                    ticker="MSFT",
                    estado="CANCELED",
                    order_id=str(index),
                )

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(40)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            rows = ledger.read(limit=None)
            self.assertEqual(len(rows), 40)
            self.assertEqual(len({row["event_id"] for row in rows}), 40)

    def test_incompatible_previous_schema_is_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.csv"
            path.write_text("old,columns\n1,2\n", encoding="utf-8")
            ledger = OrderAuditLedger(str(path))
            ledger.record("ENTRY_REQUESTED", modo="PAPER", ticker="MSFT")

            self.assertTrue(Path(f"{path}.old1").exists())
            with path.open("r", encoding="utf-8", newline="") as fh:
                header = next(csv.reader(fh))
            self.assertEqual(header, AUDIT_FIELDS)


class ExecutorAuditHookTests(unittest.TestCase):
    def test_paper_entry_emits_persistent_lifecycle_event(self):
        events = []
        executor = EjecutorHibridoDeOrdenes(
            paper_trading=True,
            config={"risk": {"sizing_mode": "fixed"}},
        )
        executor.set_audit_hook(
            lambda event, **fields: events.append((event, fields)))
        result = executor.enviar_entrada(
            _order(),
            ContextoMercado(
                atr_relativo=0.3,
                es_rango_lateral=False,
                prob_ia=0.7,
                es_senal_maestra=False,
                es_entrada_mercado=True,
            ),
            account_number="paper",
        )

        self.assertEqual(result["status"], "SIMULADA")
        self.assertEqual(events[0][0], "ENTRY_SIMULATED")
        self.assertEqual(events[0][1]["ticker"], "MSFT")
        self.assertEqual(events[0][1]["cantidad"], 10)

    def test_reactive_entry_reports_marketable_limit_buffer_separately(self):
        executor = EjecutorHibridoDeOrdenes(
            paper_trading=True,
            config={
                "risk": {"sizing_mode": "fixed"},
                "ejecucion": {"max_slippage_pct": 0.2},
            },
        )
        result = executor.enviar_entrada(
            _order(),
            ContextoMercado(
                atr_relativo=0.3,
                es_rango_lateral=False,
                prob_ia=0.7,
                es_senal_maestra=False,
                es_entrada_mercado=True,
            ),
            account_number="paper",
        )

        self.assertEqual(result["signal_price"], 400.0)
        self.assertEqual(result["submitted_limit_price"], 400.8)
        self.assertEqual(result["fill_price"], 400.0)
        self.assertEqual(result["submitted_order_type"], "MARKETABLE_LIMIT")


class ExecutionQualityLedgerTests(unittest.TestCase):
    def test_entry_slippage_is_separate_and_signed_as_adverse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "slippage.csv"
            ledger = ExecutionQualityLedger(str(path))
            buy = ledger.record_entry(
                modo="REAL", ticker="MSFT", lado="BUY", cantidad=20,
                signal_price=400.0, submitted_limit_price=400.8,
                fill_price=400.25, order_id="entry-1",
                order_type="MARKETABLE_LIMIT",
            )
            sell = ledger.record_entry(
                modo="REAL", ticker="TSLA", lado="SELL", cantidad=20,
                signal_price=300.0, submitted_limit_price=299.4,
                fill_price=300.10, order_id="entry-2",
                order_type="MARKETABLE_LIMIT",
            )

            with path.open("r", encoding="utf-8", newline="") as fh:
                self.assertEqual(next(csv.reader(fh)), EXECUTION_QUALITY_FIELDS)
            self.assertEqual(float(buy["slippage_vs_signal_per_share"]), 0.25)
            self.assertEqual(float(buy["slippage_vs_signal_usd"]), 5.0)
            self.assertEqual(float(buy["slippage_vs_limit_per_share"]), -0.55)
            self.assertEqual(float(sell["slippage_vs_signal_per_share"]), -0.10)
            self.assertEqual(len(ledger.read()), 2)


if __name__ == "__main__":
    unittest.main()
