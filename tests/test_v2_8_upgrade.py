import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import server
from MIGRATE_CONFIG_V2_8 import migrate


class V28ConfigMigrationTests(unittest.TestCase):
    def test_migration_preserves_daily_limit_and_other_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                yaml.safe_dump({
                    "risk": {
                        "paper_trading": True,
                        "fixed_shares": 10,
                        "max_acciones_por_orden": 10,
                    },
                    "kill_switch": {
                        "daily_loss": {"paper_limit_usd": 700.0}
                    },
                    "scheduler": {"hora_fin": "15:45"},
                    "logging": {"historial_operaciones": "custom/history.csv"},
                }),
                encoding="utf-8",
            )

            migrate(path)
            cfg = yaml.safe_load(path.read_text(encoding="utf-8"))

        self.assertEqual(cfg["risk"]["fixed_shares"], 20)
        self.assertEqual(cfg["risk"]["max_acciones_por_orden"], 20)
        self.assertEqual(cfg["risk"]["stop_loss_factor"], 3.5)
        self.assertEqual(cfg["risk"]["take_profit_factor"], 4.5)
        self.assertEqual(cfg["kill_switch"]["daily_loss"]["paper_limit_usd"], 700.0)
        self.assertEqual(cfg["scheduler"]["hora_fin"], "15:45")
        self.assertEqual(
            cfg["logging"]["historial_operaciones"], "custom/history.csv"
        )
        self.assertEqual(
            cfg["logging"]["slippage_entradas"], "data/slippage_entradas.csv"
        )

    def test_dashboard_saves_both_atr_factors_only_after_confirmation(self):
        html = (Path(__file__).parents[1] / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="cfg_stop_loss_factor"', html)
        self.assertIn('id="cfg_take_profit_factor"', html)
        self.assertIn("stop_loss_factor: stopFactor", html)
        self.assertIn("take_profit_factor: takeFactor", html)
        self.assertIn("Las posiciones abiertas no cambiarán", html)


class V28ConfigApiTests(unittest.TestCase):
    def setUp(self):
        server.app.config.update(TESTING=True)
        self.client = server.app.test_client()
        with self.client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["csrf_token"] = "test-csrf"

    def test_api_validates_and_persists_independent_atr_factors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                yaml.safe_dump({
                    "risk": {
                        "paper_trading": True,
                        "stop_loss_factor": 2.0,
                        "take_profit_factor": 4.0,
                    },
                    "scheduler": {"modo": "manual"},
                }),
                encoding="utf-8",
            )
            with patch.object(server, "CONFIG_PATH", str(path)):
                response = self.client.post(
                    "/api/config",
                    json={
                        "risk": {
                            "sizing_mode": "fixed",
                            "fixed_shares": 20,
                            "stop_loss_factor": 3.5,
                            "take_profit_factor": 4.5,
                        }
                    },
                    headers={"X-CSRF-Token": "test-csrf"},
                )
                payload = response.get_json()
                cfg = yaml.safe_load(path.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["engine_restart_required"])
        self.assertEqual(cfg["risk"]["stop_loss_factor"], 3.5)
        self.assertEqual(cfg["risk"]["take_profit_factor"], 4.5)
        self.assertTrue(cfg["risk"]["paper_trading"])

    def test_api_rejects_take_profit_not_above_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                yaml.safe_dump({
                    "risk": {
                        "paper_trading": True,
                        "stop_loss_factor": 2.0,
                        "take_profit_factor": 4.0,
                    }
                }),
                encoding="utf-8",
            )
            with patch.object(server, "CONFIG_PATH", str(path)):
                response = self.client.post(
                    "/api/config",
                    json={
                        "risk": {
                            "stop_loss_factor": 4.5,
                            "take_profit_factor": 3.5,
                        }
                    },
                    headers={"X-CSRF-Token": "test-csrf"},
                )

        self.assertEqual(response.status_code, 400)
        self.assertIn("mayor", response.get_json()["message"])


if __name__ == "__main__":
    unittest.main()
