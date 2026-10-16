"""Persist auditable per-ticker AI metrics as JSON and CSV."""

from __future__ import annotations

import csv
import json
import os
import stat
import tempfile
import threading
from datetime import datetime, timezone
from typing import Dict


_PUBLISH_LOCK = threading.Lock()


REPORT_COLUMNS = [
    "ticker", "muestras", "muestras_fit", "muestras_validation",
    "muestras_train", "muestras_test",
    "muestras_fit_reales", "muestras_fit_suplementarias",
    "validation_test_actual_only", "temporal_integrity_passed",
    "economic_gate_passed", "economic_net_r", "economic_expectancy_r",
    "economic_profit_factor", "economic_max_drawdown_r",
    "economic_accepted_trades", "economic_round_trip_cost_bps",
    "accuracy", "baseline_accuracy", "accuracy_lift_pp",
    "balanced_accuracy", "precision", "base_win_rate_pct",
    "precision_lift_pp", "recall", "f1", "approval_rate_pct",
    "perdedoras_filtradas_pct", "ganadoras_vetadas_pct",
    "decision_threshold", "validation_balanced_accuracy",
    "scale_pos_weight",
    "tn", "fp", "fn", "tp",
    "ganadoras", "perdedoras", "incumbent_balanced_accuracy",
    "incumbente_cargado", "activado", "modelo_cargado",
    "motivo_activacion", "training_policy_version", "modelo_path",
    "feature_version", "top_features",
    "modelo_existe", "modelo_bytes", "entrenado_utc",
]


def _atomic_write(path: str, write):
    """Publish one complete report without sharing a temporary file name."""
    path = os.fspath(path)
    try:
        existing_mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        existing_mode = None
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="",
            dir=os.path.dirname(path) or ".",
            prefix=f".{os.path.basename(path)}.", suffix=".tmp", delete=False,
        ) as handle:
            tmp = handle.name
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        if existing_mode is not None:
            os.chmod(tmp, existing_mode)
        # Windows can reject overlapping replacements of the same destination;
        # serialize the short publication step within this service process.
        with _PUBLISH_LOCK:
            os.replace(tmp, path)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


def _atomic_json(path: str, data):
    _atomic_write(path, lambda handle: json.dump(
        data, handle, ensure_ascii=False, indent=2
    ))


def _atomic_csv(path: str, rows):
    def write(handle):
        writer = csv.DictWriter(handle, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    _atomic_write(path, write)


def guardar_reporte_modelos(metricas: Dict[str, dict],
                            carpeta: str = "data/reportes") -> dict:
    os.makedirs(carpeta, exist_ok=True)
    generado = datetime.now(timezone.utc)
    stamp = generado.strftime("%Y%m%dT%H%M%SZ")
    rows = []
    detalle = {}

    for ticker in sorted(metricas):
        item = dict(metricas[ticker] or {})
        model_path = item.get("modelo_path", "")
        model_exists = bool(model_path and os.path.exists(model_path))
        confusion = item.get("matriz_confusion") or {}
        economics = item.get("economic_test") or {}
        if not isinstance(economics, dict):
            economics = {}
        item["modelo_existe"] = model_exists
        item["modelo_bytes"] = os.path.getsize(model_path) if model_exists else 0
        detalle[ticker] = item
        rows.append({
            "ticker": ticker,
            "muestras": item.get("muestras", 0),
            "muestras_fit": item.get("muestras_fit", 0),
            "muestras_validation": item.get("muestras_validation", 0),
            "muestras_train": item.get("muestras_train", 0),
            "muestras_test": item.get("muestras_test", 0),
            "muestras_fit_reales": item.get(
                "muestras_fit_reales", item.get("muestras_fit", "")),
            "muestras_fit_suplementarias": item.get("muestras_fit_suplementarias", ""),
            "validation_test_actual_only": item.get("validation_test_actual_only", ""),
            "temporal_integrity_passed": item.get("temporal_integrity_passed", ""),
            "economic_gate_passed": item.get("economic_gate_passed", ""),
            "economic_net_r": economics.get("net_r", ""),
            "economic_expectancy_r": economics.get("expectancy_r", ""),
            "economic_profit_factor": economics.get("profit_factor", ""),
            "economic_max_drawdown_r": economics.get("max_drawdown_r", ""),
            "economic_accepted_trades": economics.get("accepted_trades", ""),
            "economic_round_trip_cost_bps": economics.get("round_trip_cost_bps", ""),
            "accuracy": item.get("accuracy", ""),
            "baseline_accuracy": item.get("baseline_accuracy", ""),
            "accuracy_lift_pp": item.get("accuracy_lift_pp", ""),
            "balanced_accuracy": item.get("balanced_accuracy", ""),
            "precision": item.get("precision", ""),
            "base_win_rate_pct": item.get("base_win_rate_pct", ""),
            "precision_lift_pp": item.get("precision_lift_pp", ""),
            "recall": item.get("recall", ""),
            "f1": item.get("f1", ""),
            "approval_rate_pct": item.get("approval_rate_pct", ""),
            "perdedoras_filtradas_pct": item.get("perdedoras_filtradas_pct", ""),
            "ganadoras_vetadas_pct": item.get("ganadoras_vetadas_pct", ""),
            "decision_threshold": item.get("decision_threshold", ""),
            "validation_balanced_accuracy": item.get(
                "validation_balanced_accuracy", ""),
            "scale_pos_weight": item.get("scale_pos_weight", ""),
            "tn": confusion.get("tn", 0),
            "fp": confusion.get("fp", 0),
            "fn": confusion.get("fn", 0),
            "tp": confusion.get("tp", 0),
            "ganadoras": item.get("ganadoras", 0),
            "perdedoras": item.get("perdedoras", 0),
            "incumbent_balanced_accuracy": item.get(
                "incumbent_balanced_accuracy", ""),
            "incumbente_cargado": item.get("incumbente_cargado", False),
            "activado": item.get("activado", False),
            "modelo_cargado": item.get("modelo_cargado", False),
            "motivo_activacion": item.get("motivo_activacion", ""),
            "training_policy_version": item.get("training_policy_version", ""),
            "modelo_path": model_path,
            "feature_version": item.get("feature_version", ""),
            "top_features": "|".join(item.get("top_features") or []),
            "modelo_existe": model_exists,
            "modelo_bytes": item["modelo_bytes"],
            "entrenado_utc": item.get("entrenado_utc", ""),
        })

    payload = {
        "generado_utc": generado.isoformat(),
        "tickers_evaluados": len(detalle),
        "modelos_activados": sum(1 for x in detalle.values() if x.get("activado")),
        "modelos_cargados": sum(1 for x in detalle.values() if x.get("modelo_cargado")),
        "metricas": detalle,
    }
    json_path = os.path.join(carpeta, f"metricas_modelos_{stamp}.json")
    csv_path = os.path.join(carpeta, f"metricas_modelos_{stamp}.csv")
    _atomic_json(json_path, payload)
    _atomic_csv(csv_path, rows)
    _atomic_json(os.path.join(carpeta, "metricas_modelos_latest.json"), payload)
    _atomic_csv(os.path.join(carpeta, "metricas_modelos_latest.csv"), rows)
    return {"json": json_path, "csv": csv_path}
