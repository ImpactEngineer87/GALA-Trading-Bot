"""Audit source/economic report exports and concurrent atomic publication."""

import csv
import json
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agents import training_report


class TrainingReportTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_csv_exposes_training_sources_and_cost_aware_test_evidence(self):
        metrics = {
            "AAPL": {
                "muestras_fit": 32, "muestras_fit_reales": 32,
                "muestras_fit_suplementarias": 8,
                "validation_test_actual_only": True,
                "temporal_integrity_passed": True,
                "economic_gate_passed": False,
                "economic_test": {
                    "evaluated": True, "net_r": -1.25,
                    "expectancy_r": -0.125, "profit_factor": 0.7,
                    "max_drawdown_r": 2.5, "accepted_trades": 10,
                    "round_trip_cost_bps": 8,
                    "cost_basis": "estimated reserve",
                },
            },
            "NVDA": {
                "muestras_fit_reales": 40, "muestras_fit_suplementarias": 0,
                "validation_test_actual_only": True,
                "temporal_integrity_passed": True,
                "economic_gate_passed": True,
                "economic_test": {
                    "evaluated": True, "net_r": 3.0, "expectancy_r": 0.3,
                    "profit_factor": None, "no_loss_trades": True,
                    "max_drawdown_r": 0, "accepted_trades": 10,
                    "round_trip_cost_bps": 0,
                },
            },
        }
        original = json.loads(json.dumps(metrics))
        paths = training_report.guardar_reporte_modelos(metrics, str(self.root))
        with open(paths["csv"], encoding="utf-8", newline="") as handle:
            rows = {row["ticker"]: row for row in csv.DictReader(handle)}
        self.assertEqual(rows["AAPL"]["muestras_fit_reales"], "32")
        self.assertEqual(rows["AAPL"]["muestras_fit_suplementarias"], "8")
        self.assertEqual(rows["AAPL"]["validation_test_actual_only"], "True")
        self.assertEqual(rows["AAPL"]["temporal_integrity_passed"], "True")
        self.assertEqual(rows["AAPL"]["economic_gate_passed"], "False")
        self.assertEqual(rows["AAPL"]["economic_net_r"], "-1.25")
        self.assertEqual(rows["AAPL"]["economic_expectancy_r"], "-0.125")
        self.assertEqual(rows["AAPL"]["economic_profit_factor"], "0.7")
        self.assertEqual(rows["AAPL"]["economic_max_drawdown_r"], "2.5")
        self.assertEqual(rows["AAPL"]["economic_accepted_trades"], "10")
        self.assertEqual(rows["AAPL"]["economic_round_trip_cost_bps"], "8")
        self.assertEqual(rows["NVDA"]["muestras_fit_suplementarias"], "0")
        self.assertEqual(rows["NVDA"]["economic_profit_factor"], "")
        self.assertEqual(rows["NVDA"]["economic_max_drawdown_r"], "0")
        self.assertEqual(rows["NVDA"]["economic_round_trip_cost_bps"], "0")
        payload = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["metricas"]["AAPL"]["economic_test"], metrics["AAPL"]["economic_test"])
        self.assertTrue(payload["metricas"]["NVDA"]["economic_test"]["no_loss_trades"])
        self.assertEqual(metrics, original, "Export must not modify training metrics")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_legacy_reports_leave_unmeasured_evidence_blank(self):
        paths = training_report.guardar_reporte_modelos({"OLD": {"muestras_fit": 30}}, str(self.root))
        with open(paths["csv"], encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["muestras_fit_reales"], "30")
        for name in (
            "muestras_fit_suplementarias", "validation_test_actual_only",
            "economic_gate_passed", "temporal_integrity_passed",
            "economic_net_r", "economic_expectancy_r", "economic_profit_factor",
            "economic_max_drawdown_r", "economic_accepted_trades",
            "economic_round_trip_cost_bps",
        ):
            self.assertEqual(row[name], "", name)

    def test_concurrent_json_and_csv_writers_use_distinct_sibling_files(self):
        for kind in ("json", "csv"):
            with self.subTest(kind=kind):
                target = self.root / f"latest.{kind}"
                barrier = threading.Barrier(8)
                original_replace = os.replace
                original_fsync = os.fsync
                seen = []
                lock = threading.Lock()

                def synchronized_replace(source, destination):
                    with lock:
                        seen.append(Path(source))
                    self.assertEqual(Path(source).parent, target.parent)
                    original_replace(source, destination)

                def synchronized_fsync(descriptor):
                    original_fsync(descriptor)
                    barrier.wait(timeout=5)

                def write(index):
                    if kind == "json":
                        training_report._atomic_json(target, {"writer": index, "values": [index] * 100})
                    else:
                        training_report._atomic_csv(target, [{"ticker": str(index), "muestras": index}])

                with patch("agents.training_report.os.replace", side_effect=synchronized_replace), patch(
                    "agents.training_report.os.fsync", side_effect=synchronized_fsync
                ):
                    with ThreadPoolExecutor(max_workers=8) as workers:
                        list(workers.map(write, range(8)))
                self.assertEqual(len(seen), 8)
                self.assertEqual(len(set(seen)), 8)
                self.assertEqual(list(self.root.glob("*.tmp")), [])
                if kind == "json":
                    payload = json.loads(target.read_text(encoding="utf-8"))
                    self.assertEqual(payload["values"], [payload["writer"]] * 100)
                else:
                    with target.open(encoding="utf-8", newline="") as handle:
                        rows = list(csv.DictReader(handle))
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["ticker"], rows[0]["muestras"])

    def test_failed_serialization_preserves_report_and_cleans_temporary_file(self):
        target = self.root / "latest.json"
        target.write_text('{"valid": true}', encoding="utf-8")
        with self.assertRaises(TypeError):
            training_report._atomic_json(target, {"unsupported": object()})
        self.assertEqual(target.read_text(encoding="utf-8"), '{"valid": true}')
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_failed_replacement_preserves_csv_and_cleans_temporary_file(self):
        target = self.root / "latest.csv"
        target.write_text("previous complete report", encoding="utf-8")
        with patch("agents.training_report.os.replace", side_effect=OSError("replace denied")):
            with self.assertRaisesRegex(OSError, "replace denied"):
                training_report._atomic_csv(target, [{"ticker": "AAPL"}])
        self.assertEqual(target.read_text(encoding="utf-8"), "previous complete report")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    @unittest.skipIf(os.name == "nt", "POSIX file permission preservation")
    def test_existing_report_permissions_survive_replacement(self):
        for kind in ("json", "csv"):
            with self.subTest(kind=kind):
                target = self.root / f"restricted.{kind}"
                target.write_text("previous", encoding="utf-8")
                target.chmod(0o640)
                if kind == "json":
                    training_report._atomic_json(target, {"valid": True})
                else:
                    training_report._atomic_csv(target, [{"ticker": "AAPL"}])
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)


if __name__ == "__main__":
    unittest.main()
