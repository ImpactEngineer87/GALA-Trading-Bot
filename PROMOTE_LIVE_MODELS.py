# -*- coding: utf-8 -*-
"""
Safely promote the currently accepted NVDA/META Shadow model pairs into
PRODUCTION for the guarded live-validation profile.

DEFAULT = DRY RUN.
Use --apply only after the dry run reports both tickers VALID.

This script:
- refuses to run if paper_trading is already false;
- promotes only live_trading.allowed_tickers;
- requires exactly 1-2 allowed tickers;
- requires shadow_candidate_accepted == true;
- requires production_gate_passed == true;
- validates feature/policy compatibility;
- validates the model's feature count and decision threshold;
- uses EntrenadorAgentes._persist_model_pair(), which archives any incumbent
  production pair and atomically writes model + metadata;
- never edits .env, never arms live trading, never changes paper_trading,
  and never sends/cancels broker orders.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
from datetime import datetime, timezone
from pathlib import Path

import yaml

from agents.training import (
    EntrenadorAgentes,
    FEATURE_VERSION,
    FEATURES,
    TRAINING_POLICY_VERSION,
)


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Metadata inválida: {path}")
    return payload


def _validate_one(trainer: EntrenadorAgentes, ticker: str):
    ticker = ticker.upper()

    shadow_model_path = Path(trainer._ruta_shadow_modelo(ticker))
    shadow_meta_path = Path(trainer._ruta_shadow_meta(ticker))

    errors = []
    if not shadow_model_path.exists():
        errors.append(f"falta Shadow model: {shadow_model_path}")
    if not shadow_meta_path.exists():
        errors.append(f"falta Shadow metadata: {shadow_meta_path}")

    if errors:
        return None, None, errors

    try:
        meta = _load_json(shadow_meta_path)
    except Exception as exc:
        return None, None, [f"metadata ilegible: {type(exc).__name__}: {exc}"]

    if str(meta.get("ticker", "")).upper() != ticker:
        errors.append(
            f"ticker metadata={meta.get('ticker')!r} no coincide con {ticker}"
        )

    if meta.get("shadow_candidate_accepted") is not True:
        errors.append("shadow_candidate_accepted no es true")

    if meta.get("production_gate_passed") is not True:
        errors.append("production_gate_passed no es true")

    if int(meta.get("feature_version", -1)) != int(FEATURE_VERSION):
        errors.append(
            f"feature_version={meta.get('feature_version')} "
            f"!= actual {FEATURE_VERSION}"
        )

    if int(meta.get("training_policy_version", -1)) != int(TRAINING_POLICY_VERSION):
        errors.append(
            f"training_policy_version={meta.get('training_policy_version')} "
            f"!= actual {TRAINING_POLICY_VERSION}"
        )

    if meta.get("features") != FEATURES:
        errors.append("lista de features no coincide con el código actual")

    try:
        threshold = float(meta.get("decision_threshold"))
        if not (0.0 < threshold < 1.0):
            raise ValueError
    except (TypeError, ValueError):
        threshold = None
        errors.append(
            f"decision_threshold inválido: {meta.get('decision_threshold')!r}"
        )

    model = None
    if not errors:
        try:
            with shadow_model_path.open("rb") as handle:
                model = pickle.load(handle)
        except Exception as exc:
            errors.append(f"modelo Shadow ilegible: {type(exc).__name__}: {exc}")

    if model is not None:
        feature_count = int(getattr(model, "n_features_in_", -1))
        if feature_count != len(FEATURES):
            errors.append(
                f"n_features_in_={feature_count} != actual {len(FEATURES)}"
            )

    return model, meta, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Realiza la promoción. Sin esta bandera solo valida.",
    )
    args = parser.parse_args()

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8-sig")) or {}

    # Promotion is deliberately allowed only while the bot remains PAPER.
    paper = bool((cfg.get("risk", {}) or {}).get("paper_trading", True))
    if not paper:
        print("[BLOCKED] paper_trading debe permanecer TRUE durante la promoción.")
        return 2

    live_cfg = cfg.get("live_trading", {}) or {}
    allowed = list(dict.fromkeys(
        str(t).strip().upper()
        for t in (live_cfg.get("allowed_tickers") or [])
        if str(t).strip()
    ))

    from core.live_capacity import max_real_tickers
    max_tickers = max_real_tickers(live_cfg)
    if not allowed:
        print("[BLOCKED] live_trading.allowed_tickers está vacío.")
        return 2
    if len(allowed) > max_tickers or len(allowed) > 2:
        print(
            f"[BLOCKED] allowed_tickers={allowed}; "
            f"máximo permitido={min(max_tickers, 2)}"
        )
        return 2

    trainer = EntrenadorAgentes(cfg)

    print("=" * 72)
    print("GALA10 MANUAL SHADOW -> PRODUCTION PROMOTION")
    print("MODE:", "APPLY" if args.apply else "DRY RUN")
    print("paper_trading:", paper)
    print("tickers:", ", ".join(allowed))
    print("=" * 72)

    validated = {}
    any_errors = False

    # Validate ALL tickers before writing anything.
    for ticker in allowed:
        model, meta, errors = _validate_one(trainer, ticker)
        print(f"\n[{ticker}]")
        if errors:
            any_errors = True
            for error in errors:
                print("  [BLOCKED]", error)
            continue

        candidate_ba = meta.get("balanced_accuracy")
        threshold = meta.get("decision_threshold")
        trained = meta.get("entrenado_utc")
        print("  VALID")
        print("  balanced_accuracy:", candidate_ba)
        print("  decision_threshold:", threshold)
        print("  trained_utc:", trained)
        print("  shadow_candidate_accepted:", meta.get("shadow_candidate_accepted"))
        print("  production_gate_passed:", meta.get("production_gate_passed"))
        validated[ticker] = (model, meta)

    if any_errors or len(validated) != len(allowed):
        print("\nRESULT: BLOCKED — no se escribió ningún modelo PRODUCTION.")
        return 2

    if not args.apply:
        print("\nRESULT: VALIDATED.")
        print("No se modificó data/modelos.")
        print("Si todo es correcto, ejecute otra vez con --apply.")
        return 0

    promoted = []
    try:
        for ticker in allowed:
            model, shadow_meta = validated[ticker]

            production_meta = dict(shadow_meta)
            production_meta.update({
                "ticker": ticker,
                "production_approved": True,
                "production_gate_passed": True,
                "activado": True,
                "modelo_cargado": True,
                "production_activation_allowed": True,
                "manual_promotion": True,
                "manual_promotion_source": "accepted_shadow_candidate",
                "manual_promotion_reason": "guarded_two_ticker_live_validation",
                "manual_promotion_utc": datetime.now(timezone.utc).isoformat(),
                "modelo_path": trainer._ruta_modelo(ticker),
            })

            trainer._persist_model_pair(
                ticker=ticker,
                modelo=model,
                metricas=production_meta,
                models_dir=trainer.modelos_dir,
                model_path=trainer._ruta_modelo(ticker),
                meta_path=trainer._ruta_meta(ticker),
                tier="PRODUCTION",
            )
            promoted.append(ticker)
            print(f"[PROMOTED] {ticker}")

    except Exception as exc:
        print(
            "\n[ERROR] La promoción no terminó completamente:",
            f"{type(exc).__name__}: {exc}",
        )
        print("Promovidos antes del error:", promoted or "NONE")
        print(
            "Revise data/modelos/archive y los pares PRODUCTION antes de continuar."
        )
        return 3

    # Fresh loader = same compatibility path used at runtime.
    verify = EntrenadorAgentes(cfg)
    failures = []
    print("\nPRODUCTION LOAD CHECK")
    for ticker in allowed:
        loaded = verify.modelo_disponible(ticker)
        threshold = verify._umbrales.get(ticker)
        print(
            f"  {ticker}: production_loaded={loaded} "
            f"threshold={threshold}"
        )
        if not loaded or threshold is None:
            failures.append(ticker)

    if failures:
        print(
            "\nRESULT: BLOCKED — producción escrita pero no cargable:",
            ", ".join(failures),
        )
        return 4

    print("\nRESULT: PROMOTION COMPLETE")
    print("paper_trading sigue TRUE.")
    print("GALA10_LIVE_ARMED no fue modificado.")
    print("Siguiente paso: configure GALA10_LIVE_ARMED=YES y ejecute VERIFY_LIVE_READY.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
