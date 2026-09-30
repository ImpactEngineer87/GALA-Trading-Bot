"""Run the AI feature quality and chronological drift audit."""

import yaml

from agents.feature_audit import auditar_features


def main():
    with open("config.yaml", "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    training = config.get("agents", {}).get("entrenamiento", {})
    paths = auditar_features(
        feedback_path=training.get(
            "feedback_path", "data/agentes_feedback.csv"),
        reportes_dir=training.get("reportes_dir", "data/reportes"),
    )
    print(f"[IA] Auditoría de features CSV:  {paths['csv']}")
    print(f"[IA] Auditoría de features JSON: {paths['json']}")


if __name__ == "__main__":
    main()
