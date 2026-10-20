#!/usr/bin/env python3
"""Read-only model artifact inventory for the existing production project.

No pickle deserialization, writes, broker calls, or model retraining. Intended
to diagnose MODEL_UNAVAILABLE after deployment without touching saved models.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.project_paths import resolve_runtime_path


DEFAULT_TICKERS = (
    "AAPL", "AMD", "AMZN", "BA", "META", "NVDA", "RCL", "UAL",
    "TSLA", "UNH", "PINS", "NCLH", "MSFT", "NKE", "PYPL", "NFLX",
    "ADBE",
)


def inspect_model_directory(
    directory: Path, tickers, expected_features=None,
    expected_version=None, expected_policy=None,
):
    result = {}
    for ticker in tickers:
        ticker = str(ticker).strip().upper()
        model = directory / f"modelo_{ticker}.pkl"
        metadata = directory / f"modelo_{ticker}.meta.json"
        archived = sorted(directory.glob(f"modelo_{ticker}.pkl.incompatible_*"))
        if not model.exists():
            status = "MISSING_PKL"
        elif not metadata.exists():
            status = "MISSING_METADATA"
        else:
            try:
                meta = json.loads(metadata.read_text(encoding="utf-8-sig"))
                if not isinstance(meta, dict):
                    status = "INVALID_METADATA"
                elif (
                    expected_features is not None
                    and (meta.get("features") != expected_features or
                         meta.get("feature_version") != expected_version or
                         meta.get("training_policy_version") != expected_policy)
                ):
                    status = "SCHEMA_MISMATCH"
                else:
                    status = "FILES_AND_METADATA_PRESENT"
            except Exception:
                status = "INVALID_METADATA"
        result[ticker] = (status, archived)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--tickers", nargs="*", default=DEFAULT_TICKERS)
    args = parser.parse_args()
    import yaml
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    models_cfg = ((cfg.get("agents") or {}).get("entrenamiento") or {})
    directory = Path(resolve_runtime_path(models_cfg.get("modelos_dir", "data/modelos")))
    print(f"Production model directory: {directory}")
    print("Read-only check; artifacts are not opened as pickle or modified.")
    print("PRESENT means filenames/metadata match, NOT that the model can be safely loaded.")
    # Comparing against the exact current feature schema avoids guessing.
    from agents.training import FEATURES, FEATURE_VERSION, TRAINING_POLICY_VERSION
    result = inspect_model_directory(
        directory, args.tickers,
        expected_features=FEATURES, expected_version=FEATURE_VERSION,
        expected_policy=TRAINING_POLICY_VERSION,
    )
    for ticker, (status, archives) in result.items():
        note = f" | previously renamed models={len(archives)}" if archives else ""
        print(f"{ticker:5} {status}{note}")
    print("Models requiring investigation:",
          ", ".join(t for t, (status, _) in result.items()
                    if status != "FILES_AND_METADATA_PRESENT") or "none")


if __name__ == "__main__":
    main()
