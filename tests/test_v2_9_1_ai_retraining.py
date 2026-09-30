import csv
import json
import pickle
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agents.orchestrator import OrquestadorAgentes
from agents.retraining_lifecycle import RetrainingLifecycle
from agents.training import EntrenadorAgentes
from MIGRATE_CONFIG_V2_9_1 import migrate
from MIGRATE_RETRAINING_STATE_V2_9_1 import migrate as migrate_state
import yaml


def _write_integrity_feedback(path: Path, *, valid: int, invalid: int = 0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["ticker", "execution_valid", "ganadora"]
        )
        writer.writeheader()
        for index in range(valid):
            writer.writerow({
                "ticker": "AMD", "execution_valid": 1,
                "ganadora": index % 2,
            })
        for index in range(invalid):
            writer.writerow({
                "ticker": "AMD", "execution_valid": 0,
                "ganadora": index % 2,
            })


def _lifecycle(root: Path) -> RetrainingLifecycle:
    return RetrainingLifecycle(
        feedback_path=str(root / "feedback.csv"),
        state_path=str(root / "state.json"),
        shadow_models_dir=str(root / "shadow"),
        warning_samples=30,
        retrain_samples=50,
    )


class EligibleCounterRecoveryTests(unittest.TestCase):
    def test_legacy_15000_baseline_rebases_to_v29_valid_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_integrity_feedback(root / "feedback.csv", valid=3, invalid=5)
            (root / "state.json").write_text(json.dumps({
                "schema_version": 1,
                "tickers": {
                    "AMD": {
                        "total_observations": 15003,
                        "last_attempt_count": 15000,
                        "last_success_count": 14950,
                        "new_observations": 3,
                        "status": "COLLECTING",
                        "last_attempt_utc": "2026-08-06T19:45:08+00:00",
                        "last_result": "REJECTED",
                    }
                },
            }), encoding="utf-8")

            row = _lifecycle(root).snapshot(["AMD"])[0]

            self.assertEqual(row["eligible_observations"], 3)
            self.assertEqual(row["quarantined_observations"], 5)
            self.assertEqual(row["lifetime_observations"], 8)
            self.assertEqual(row["last_attempt_count"], 0)
            self.assertEqual(row["new_observations"], 3)
            self.assertEqual(row["legacy_last_attempt_count"], 15000)
            self.assertEqual(row["counting_basis"], "execution_valid")
            saved = json.loads((root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], 2)

    def test_missing_state_counts_existing_valid_v29_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_integrity_feedback(root / "feedback.csv", valid=7, invalid=2)

            row = _lifecycle(root).snapshot(["AMD"])[0]

            self.assertEqual(row["last_attempt_count"], 0)
            self.assertEqual(row["new_observations"], 7)

    def test_missing_feedback_does_not_zero_persisted_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state.json").write_text(json.dumps({
                "schema_version": 2,
                "tickers": {
                    "AMD": {
                        "total_observations": 12,
                        "last_attempt_count": 0,
                        "last_success_count": 0,
                        "new_observations": 12,
                        "status": "COLLECTING",
                        "last_attempt_utc": "",
                        "counting_basis": "execution_valid",
                    }
                },
            }), encoding="utf-8")

            row = _lifecycle(root).snapshot(["AMD"])[0]

            self.assertFalse(row["feedback_available"])
            self.assertEqual(row["total_observations"], 12)
            self.assertEqual(row["new_observations"], 12)

    def test_deferred_training_keeps_the_50_row_batch_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_integrity_feedback(root / "feedback.csv", valid=50)
            lifecycle = _lifecycle(root)
            self.assertEqual(
                lifecycle.snapshot(["AMD"])[0]["status"], "PENDING_RETRAIN"
            )

            lifecycle.mark_training("AMD", 50)
            lifecycle.mark_result(
                "AMD", total_count=50, accepted=False,
                reason="chronological blocks need both classes",
                consume_batch=False, deferred=True,
            )
            row = lifecycle.snapshot(["AMD"])[0]

            self.assertEqual(row["last_result"], "WAITING_DATA")
            self.assertEqual(row["last_attempt_count"], 0)
            self.assertEqual(row["new_observations"], 50)
            self.assertEqual(row["status"], "PENDING_RETRAIN")


class RetrainingPolicyAndRetentionTests(unittest.TestCase):
    def _trainer(self, root: Path) -> EntrenadorAgentes:
        return EntrenadorAgentes({
            "agents": {
                "shadow_mode": {
                    "enabled": True,
                    "tickers": ["AMD"],
                    "modelos_dir": str(root / "shadow"),
                },
                "entrenamiento": {
                    "activo": False,
                    "feedback_path": str(root / "feedback.csv"),
                    "modelos_dir": str(root / "production"),
                    "min_muestras": 25,
                    "min_test_muestras": 20,
                    "min_validation_muestras": 20,
                    "automatic_retraining": {
                        "enabled": True,
                        "warning_samples": 30,
                        "retrain_samples": 50,
                        "min_fit_samples": 25,
                        "min_validation_samples": 10,
                        "min_test_samples": 10,
                        "state_path": str(root / "state.json"),
                    },
                },
            }
        })

    def test_automatic_50_row_split_is_30_10_10(self):
        with tempfile.TemporaryDirectory() as tmp:
            trainer = self._trainer(Path(tmp))
            self.assertEqual(
                trainer._chronological_split_sizes(50, automatic=True),
                (30, 10, 10),
            )
            self.assertIsNone(
                trainer._chronological_split_sizes(50, automatic=False)
            )

    def test_shadow_replacement_archives_the_previous_model_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trainer = self._trainer(root)
            metadata = {"ticker": "AMD", "version": 1}
            self.assertTrue(
                trainer._guardar_shadow("AMD", {"version": 1}, metadata)
            )
            self.assertTrue(
                trainer._guardar_shadow(
                    "AMD", {"version": 2}, {"ticker": "AMD", "version": 2}
                )
            )

            archives = list((root / "shadow" / "archive" / "AMD").glob("*"))
            self.assertEqual(len(archives), 1)
            with (archives[0] / "modelo_AMD.pkl").open("rb") as handle:
                self.assertEqual(pickle.load(handle)["version"], 1)
            with (root / "shadow" / "modelo_AMD.pkl").open("rb") as handle:
                self.assertEqual(pickle.load(handle)["version"], 2)
            active_meta = json.loads(
                (root / "shadow" / "modelo_AMD.meta.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(active_meta["previous_model_archive"])

    def test_config_migration_preserves_700_limit_and_adds_50_row_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(yaml.safe_dump({
                "kill_switch": {
                    "daily_loss": {"paper_limit_usd": 700.0}
                },
                "agents": {
                    "entrenamiento": {
                        "automatic_retraining": {"retrain_samples": 50}
                    }
                },
            }), encoding="utf-8")

            migrate(path)
            config = yaml.safe_load(path.read_text(encoding="utf-8"))

            self.assertEqual(
                config["kill_switch"]["daily_loss"]["paper_limit_usd"], 700.0
            )
            automatic = config["agents"]["entrenamiento"][
                "automatic_retraining"
            ]
            self.assertEqual(automatic["retrain_samples"], 50)
            self.assertEqual(automatic["min_fit_samples"], 25)
            self.assertEqual(automatic["min_validation_samples"], 10)
            self.assertEqual(automatic["min_test_samples"], 10)

    def test_offline_state_migration_reports_valid_and_quarantined_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.yaml").write_text(yaml.safe_dump({
                "market": {"tickers": ["AMD"]},
                "agents": {
                    "shadow_mode": {
                        "tickers": ["AMD"],
                        "modelos_dir": "data/modelos_shadow",
                    },
                    "entrenamiento": {
                        "feedback_path": "data/agentes_feedback.csv",
                        "automatic_retraining": {
                            "state_path": "data/retraining_state.json",
                            "warning_samples": 30,
                            "retrain_samples": 50,
                        },
                    },
                },
            }), encoding="utf-8")
            _write_integrity_feedback(
                root / "data" / "agentes_feedback.csv", valid=4, invalid=6
            )

            result = migrate_state(root)

            self.assertEqual(result["eligible_observations"], 4)
            self.assertEqual(result["quarantined_observations"], 6)
            state = json.loads(
                (root / "data" / "retraining_state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["schema_version"], 2)
            self.assertEqual(state["tickers"]["AMD"]["new_observations"], 4)


class FeedbackCloseHardeningTests(unittest.TestCase):
    def test_close_without_in_memory_opener_is_safe_and_cleans_shadow(self):
        closed = []
        orchestrator = OrquestadorAgentes.__new__(OrquestadorAgentes)
        orchestrator.feedback = SimpleNamespace(
            cerrar=lambda *args, **kwargs: None
        )
        orchestrator.shadow = SimpleNamespace(
            cerrar=lambda ticker, price: closed.append((ticker, price))
        )
        orchestrator.entrenador = SimpleNamespace(
            quizas_reentrenar_ticker=lambda *args, **kwargs: self.fail(
                "an incomplete close must not reach retraining"
            )
        )

        self.assertIsNone(orchestrator.al_cerrar("AMD", 100.0))
        self.assertEqual(closed, [("AMD", 100.0)])


class V291PackageAndDashboardTests(unittest.TestCase):
    def test_dashboard_exposes_valid_and_quarantined_counts(self):
        html = (
            Path(__file__).parents[1] / "dashboard" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("Trading Algorítmico v2.9.1", html)
        self.assertIn("eligible_observations", html)
        self.assertIn("quarantined_observations", html)

    def test_installer_is_powershell_5_1_compatible_and_backs_up_models(self):
        installer = (
            Path(__file__).parents[2]
            / "GALA10_PATCH_v2.9.1_AI_RETRAINING_RECOVERY"
            / "APPLY_V2_9_1.ps1"
        ).read_text(encoding="utf-8")
        self.assertNotIn("[System.IO.Path]::GetRelativePath", installer)
        self.assertIn("function Get-ProjectRelativePath", installer)
        self.assertIn('@("2.9.0", "2.9.1")', installer)
        self.assertIn('"data\\modelos", "data\\modelos_shadow"', installer)
        self.assertIn("MIGRATE_RETRAINING_STATE_V2_9_1.py", installer)


if __name__ == "__main__":
    unittest.main()
