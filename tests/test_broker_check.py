import tempfile
import unittest
from pathlib import Path

import pandas as pd

from api.broker_check import ejecutar_broker_check, ejecutar_broker_latency


class FakeReadOnlyClient:
    def __init__(self, config):
        self.config = config

    def _asegurar_token(self):
        return True

    def obtener_account_hash(self, account):
        return "safe-hash"

    def obtener_posiciones(self, account_hash):
        return []

    def obtener_ordenes_abiertas(self, account_hash):
        return []

    def obtener_historico(self, ticker, intervalo, dias):
        return pd.DataFrame([{
            "open": 1, "high": 2, "low": 1, "close": 2, "volume": 100,
        }])


class FakeLatencyClient:
    instance = None

    def __init__(self, config):
        self.config = config
        self.calls = {
            "auth": 0,
            "hash": 0,
            "positions": 0,
            "open_orders": 0,
            "quote": 0,
        }
        type(self).instance = self

    def _asegurar_token(self):
        self.calls["auth"] += 1
        return True

    def obtener_account_hash(self, account):
        self.calls["hash"] += 1
        return "safe-hash"

    def obtener_posiciones(self, account_hash):
        self.calls["positions"] += 1
        return []

    def obtener_ordenes_abiertas(self, account_hash):
        self.calls["open_orders"] += 1
        return []

    def obtener_precio_actual(self, ticker):
        self.calls["quote"] += 1
        return 390.25


class BrokerCheckTests(unittest.TestCase):
    def test_read_only_preflight_passes_without_order_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yaml"
            cfg.write_text("risk:\n  paper_trading: true\n", encoding="utf-8")
            result = ejecutar_broker_check(
                str(cfg),
                "12345678",
                "MSFT",
                client_factory=FakeReadOnlyClient,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["steps"]["positions"]["count"], 0)

    def test_warm_latency_is_read_only_and_excludes_warmup(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yaml"
            reports = Path(tmp) / "reports"
            cfg.write_text("risk:\n  paper_trading: true\n", encoding="utf-8")
            result = ejecutar_broker_latency(
                str(cfg),
                "12345678",
                "MSFT",
                samples=3,
                output_dir=str(reports),
                client_factory=FakeLatencyClient,
            )

            self.assertTrue(result["ok"])
            self.assertTrue(result["read_only"])
            self.assertTrue(result["warmup_excluded"])
            self.assertTrue(Path(result["report_path"]).exists())
            self.assertTrue((reports / "broker_latency_latest.json").exists())
            for metric in result["metrics"].values():
                self.assertEqual(metric["samples"], 3)
                self.assertEqual(metric["failures"], 0)

            # One warm-up call plus the three reported samples.
            calls = FakeLatencyClient.instance.calls
            self.assertEqual(calls["positions"], 4)
            self.assertEqual(calls["open_orders"], 4)
            self.assertEqual(calls["quote"], 4)


if __name__ == "__main__":
    unittest.main()
