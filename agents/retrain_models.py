"""Re-evaluate all per-ticker veto models from the persisted clean feedback."""

import sys

import yaml

from agents.training import EntrenadorAgentes
from agents.training_report import guardar_reporte_modelos


def main() -> int:
    with open("config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    tickers = config.get("market", {}).get("tickers", [])
    trainer = EntrenadorAgentes(config)
    metrics = trainer.entrenar_todos(tickers)
    report_dir = config.get("agents", {}).get("entrenamiento", {}).get(
        "reportes_dir", "data/reportes")
    paths = guardar_reporte_modelos(metrics, report_dir)
    print(f"[IA] Reporte JSON: {paths['json']}")
    print(f"[IA] Reporte CSV: {paths['csv']}")
    if not any(item.get("modelo_cargado") for item in metrics.values()):
        print("[IA] No hubo modelos elegibles; revise muestras y esquema de volumen.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
