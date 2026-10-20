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
    from core.ticker_execution import TickerExecutionRegistry
    live = cfg.get("live_trading", {}) or {}
    risk = cfg.get("risk", {}) or {}
    per_ticker = bool(live.get("per_ticker_execution", False))
    registry_path = resolve_runtime_path(
        live.get("ticker_execution_path", "data/ticker_execution.json")
    )
    registry = TickerExecutionRegistry(
        registry_path, default_mode="PAPER",
        initial_real_tickers=live.get("real_tickers", []),
        default_shares=max(1, int(risk.get("fixed_shares", 1) or 1)),
    )
    settings = {row["ticker"]: row for row in registry.snapshot(args.tickers)}
    global_mode = "PAPER" if bool(risk.get("paper_trading", True)) else "REAL"

    shadow_cfg = (cfg.get("agents") or {}).get("shadow_mode", {}) or {}
    shadow_dir = Path(resolve_runtime_path(
        shadow_cfg.get("modelos_dir", "data/modelos_shadow")
    ))
    shadow_enabled = bool(shadow_cfg.get("enabled", False))
    shadow_scope = {str(t).strip().upper() for t in (shadow_cfg.get("tickers") or [])}
    shadows = inspect_model_directory(
        shadow_dir, args.tickers, expected_features=FEATURES,
        expected_version=FEATURE_VERSION, expected_policy=TRAINING_POLICY_VERSION,
    )

    real_issues = []
    paper_missing = []
    print("Ticker  Mode    PRODUCTION                    SHADOW                         Note")
    for ticker, (status, archives) in result.items():
        mode = settings[ticker]["mode"] if per_ticker else global_mode
        if not shadow_enabled:
            shadow_status = "DISABLED"
        elif shadow_scope and ticker not in shadow_scope:
            shadow_status = "OUT_OF_SCOPE"
        else:
            shadow_status = shadows[ticker][0]
        if mode == "REAL" and status != "FILES_AND_METADATA_PRESENT":
            note = "REAL_MODEL_BLOCKER"
            real_issues.append(ticker)
        elif mode == "PAPER" and status != "FILES_AND_METADATA_PRESENT":
            note = "PAPER_NO_PROD_EXPECTED"
            paper_missing.append(ticker)
        else:
            note = "FILES_ONLY_NOT_APPROVAL"
        if archives:
            note += f" | previous_archive={len(archives)}"
        print(f"{ticker:6}  {mode:5}   {status:29} {shadow_status:29} {note}")
    print(f"Configured mixed execution: {per_ticker}; mode file: {registry_path}")
    print("REAL tickers requiring production artifact investigation:",
          ", ".join(real_issues) or "none")
    print("PAPER tickers without PRODUCTION (normal during evaluation):",
          ", ".join(paper_missing) or "none")
    print("Presence of files does NOT prove model usability, validation, or broker approval.")
    print("Never promote SHADOW automatically or bypass production TEST gates.")


if __name__ == "__main__":
    main()
