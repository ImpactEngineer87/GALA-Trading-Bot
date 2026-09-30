"""Auditable train/serve feature quality checks for the per-ticker veto models."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Dict

import numpy as np
import pandas as pd

from agents.training import FEATURES, FEATURE_VERSION


AUDIT_COLUMNS = [
    "ticker", "feature", "feature_version", "rows",
    "missing_pct", "unique_values", "std_total",
    "fit_mean", "test_mean", "standardized_mean_shift",
    "status",
]


def _atomic_csv(path: str, frame: pd.DataFrame):
    tmp = path + ".tmp"
    frame.to_csv(tmp, index=False)
    os.replace(tmp, path)


def _atomic_json(path: str, payload: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def auditar_features(
    feedback_path: str = "data/agentes_feedback.csv",
    reportes_dir: str = "data/reportes",
) -> Dict[str, str]:
    if not os.path.exists(feedback_path):
        raise FileNotFoundError(f"No existe el feedback: {feedback_path}")

    df = pd.read_csv(feedback_path)
    if "execution_valid" in df.columns:
        valid = pd.to_numeric(
            df["execution_valid"], errors="coerce"
        ).fillna(0)
        df = df[valid == 1].copy()
    # `tipo_num` is intentionally not persisted: training derives it from the
    # canonical BUY/SELL `tipo` column immediately before building X. The audit
    # must reproduce that same transformation instead of treating the derived
    # column as a schema requirement.
    if "tipo_num" not in df.columns and "tipo" in df.columns:
        df["tipo_num"] = (df["tipo"].astype(str).str.upper() == "BUY").astype(float)
    required = {"ticker", "ganadora", *FEATURES}
    missing = sorted(required.difference(df.columns))
    if missing:
        raise ValueError(
            "El feedback usa un esquema anterior; faltan columnas: "
            + ", ".join(missing)
        )

    if "timestamp" in df.columns:
        df["_timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.sort_values(["ticker", "_timestamp"], kind="stable")

    rows = []
    summaries = {}
    for ticker, group in df.groupby("ticker", sort=True):
        group = group.copy()
        n = len(group)
        split_test = max(1, n - max(1, int(round(n * 0.20))))
        fit = group.iloc[:split_test]
        test = group.iloc[split_test:]
        target = pd.to_numeric(group["ganadora"], errors="coerce")
        summaries[str(ticker)] = {
            "rows": int(n),
            "first_timestamp": (
                str(group["_timestamp"].min())
                if "_timestamp" in group else ""
            ),
            "last_timestamp": (
                str(group["_timestamp"].max())
                if "_timestamp" in group else ""
            ),
            "win_rate_pct": round(float(target.mean() * 100.0), 2),
            "features_missing_over_20pct": [],
            "features_constant": [],
            "features_drift_over_1std": [],
        }

        for feature in FEATURES:
            total = pd.to_numeric(group[feature], errors="coerce")
            fit_values = pd.to_numeric(fit[feature], errors="coerce")
            test_values = pd.to_numeric(test[feature], errors="coerce")
            missing_pct = float(total.isna().mean() * 100.0)
            unique_values = int(total.nunique(dropna=True))
            std_total = float(total.std(ddof=0)) if total.notna().any() else float("nan")
            fit_mean = float(fit_values.mean()) if fit_values.notna().any() else float("nan")
            test_mean = float(test_values.mean()) if test_values.notna().any() else float("nan")
            shift = (
                (test_mean - fit_mean) / std_total
                if np.isfinite(std_total) and std_total > 1e-12
                and np.isfinite(fit_mean) and np.isfinite(test_mean)
                else float("nan")
            )

            if missing_pct >= 95.0:
                status = "MISSING"
            elif unique_values <= 1:
                status = "CONSTANT"
            elif np.isfinite(shift) and abs(shift) >= 1.0:
                status = "DRIFT"
            else:
                status = "OK"

            if missing_pct > 20.0:
                summaries[str(ticker)]["features_missing_over_20pct"].append(feature)
            if unique_values <= 1:
                summaries[str(ticker)]["features_constant"].append(feature)
            if np.isfinite(shift) and abs(shift) >= 1.0:
                summaries[str(ticker)]["features_drift_over_1std"].append(feature)

            rows.append({
                "ticker": ticker,
                "feature": feature,
                "feature_version": FEATURE_VERSION,
                "rows": int(n),
                "missing_pct": round(missing_pct, 4),
                "unique_values": unique_values,
                "std_total": round(std_total, 8) if np.isfinite(std_total) else "",
                "fit_mean": round(fit_mean, 8) if np.isfinite(fit_mean) else "",
                "test_mean": round(test_mean, 8) if np.isfinite(test_mean) else "",
                "standardized_mean_shift": (
                    round(shift, 6) if np.isfinite(shift) else ""
                ),
                "status": status,
            })

    os.makedirs(reportes_dir, exist_ok=True)
    generated = datetime.now(timezone.utc)
    stamp = generated.strftime("%Y%m%dT%H%M%SZ")
    audit = pd.DataFrame(rows, columns=AUDIT_COLUMNS)
    csv_path = os.path.join(reportes_dir, f"auditoria_features_{stamp}.csv")
    json_path = os.path.join(reportes_dir, f"auditoria_features_{stamp}.json")
    _atomic_csv(csv_path, audit)
    _atomic_csv(os.path.join(reportes_dir, "auditoria_features_latest.csv"), audit)
    payload = {
        "generado_utc": generated.isoformat(),
        "feature_version": FEATURE_VERSION,
        "features": FEATURES,
        "tickers": summaries,
    }
    _atomic_json(json_path, payload)
    _atomic_json(
        os.path.join(reportes_dir, "auditoria_features_latest.json"), payload)
    return {"csv": csv_path, "json": json_path}
