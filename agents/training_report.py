"""Persist auditable per-ticker AI metrics as JSON and CSV."""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from typing import Dict


REPORT_COLUMNS = [
    "ticker", "muestras", "muestras_fit", "muestras_validation",
    "muestras_train", "muestras_test",
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


def _atomic_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _atomic_csv(path: str, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


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
