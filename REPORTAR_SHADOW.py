"""Build per-ticker Shadow + market-regime performance reports from CLOSE events."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pandas as pd
import yaml


def main() -> int:
    with open("config.yaml", "r", encoding="utf-8-sig") as handle:
        config = yaml.safe_load(handle) or {}
    cfg = config.get("agents", {}).get("shadow_mode", {})
    source = cfg.get("decisions_path", "data/shadow_decisions.csv")
    reports_dir = cfg.get("reports_dir", "data/reportes_shadow")
    if not os.path.exists(source):
        print(f"[SHADOW] No existe {source}")
        return 1

    df = pd.read_csv(source)
    if "event" not in df.columns:
        print("[SHADOW] Esquema inválido: falta event")
        return 1
    df = df[df["event"] == "CLOSE"].copy()
    if df.empty:
        print("[SHADOW] Aún no hay operaciones cerradas para evaluar")
        return 0

    numeric_cols = (
        "shadow_prob", "shadow_threshold", "shadow_would_approve",
        "r_multiple", "ganadora", "shadow_correct", "veto_would_help",
        "market_regime_confidence", "market_regime_would_block",
        "regime_correct", "regime_veto_would_help", "regime_false_block",
    )
    for col in numeric_cols:
        if col not in df.columns:
            df[col] = float("nan")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "market_regime" not in df.columns:
        df["market_regime"] = ""
    if "market_regime_reason" not in df.columns:
        df["market_regime_reason"] = ""
    if "market_regime_policy" not in df.columns:
        df["market_regime_policy"] = ""
    if "execution_mode" not in df.columns:
        df["execution_mode"] = "UNKNOWN"
    df["execution_mode"] = (
        df["execution_mode"].fillna("").astype(str).str.upper().replace("", "UNKNOWN")
    )
    df = df.dropna(subset=["ticker", "r_multiple", "ganadora"])

    rows = []
    for (execution_mode, ticker), group in df.groupby(["execution_mode", "ticker"], sort=True):
        model_group = group.dropna(subset=["shadow_would_approve"]).copy()
        approved = model_group[model_group["shadow_would_approve"] == 1]
        vetoed = model_group[model_group["shadow_would_approve"] == 0]
        regime_group = group.dropna(subset=["market_regime_would_block"]).copy()
        regime_passed = regime_group[regime_group["market_regime_would_block"] == 0]
        regime_vetoed = regime_group[regime_group["market_regime_would_block"] == 1]
        raw_win_rate = float(group["ganadora"].mean()) if len(group) else 0.0
        approved_win_rate = float(approved["ganadora"].mean()) if len(approved) else float("nan")
        losers = int((model_group["ganadora"] == 0).sum())
        winners = int((model_group["ganadora"] == 1).sum())
        losers_filtered = int(((model_group["shadow_would_approve"] == 0) & (model_group["ganadora"] == 0)).sum())
        winners_vetoed = int(((model_group["shadow_would_approve"] == 0) & (model_group["ganadora"] == 1)).sum())
        raw_net_r = float(group["r_multiple"].sum())
        shadow_net_r = float(approved["r_multiple"].sum())

        regime_losers = int((regime_group["ganadora"] == 0).sum())
        regime_winners = int((regime_group["ganadora"] == 1).sum())
        regime_losers_filtered = int(
            ((regime_group["market_regime_would_block"] == 1) & (regime_group["ganadora"] == 0)).sum()
        )
        regime_winners_vetoed = int(
            ((regime_group["market_regime_would_block"] == 1) & (regime_group["ganadora"] == 1)).sum()
        )
        regime_net_r = float(regime_passed["r_multiple"].sum())
        range_count = int((regime_group["market_regime"].astype(str) == "RANGE").sum())
        policies = sorted({
            str(v).strip().lower() for v in group.get("market_regime_policy", [])
            if str(v).strip() and str(v).lower() != "nan"
        })
        rows.append({
            "execution_mode": execution_mode,
            "ticker": ticker,
            "regime_policies_seen": ",".join(policies),
            "closed_trades": int(len(group)),
            "raw_win_rate_pct": round(raw_win_rate * 100, 2),
            "shadow_model_samples": int(len(model_group)),
            "shadow_approval_rate_pct": round(len(approved) / len(model_group) * 100, 2) if len(model_group) else None,
            "approved_win_rate_pct": round(approved_win_rate * 100, 2) if len(model_group) else None,
            "precision_lift_pp": round((approved_win_rate - raw_win_rate) * 100, 2) if len(model_group) else None,
            "losers_filtered_pct": round((losers_filtered / losers * 100) if losers else 0.0, 2),
            "winners_vetoed_pct": round((winners_vetoed / winners * 100) if winners else 0.0, 2),
            "raw_net_r": round(raw_net_r, 4),
            "shadow_net_r": round(shadow_net_r, 4),
            "shadow_delta_r": round(shadow_net_r - raw_net_r, 4),
            "decision_accuracy_pct": round(float(model_group["shadow_correct"].mean()) * 100, 2) if len(model_group) else None,
            "regime_samples": int(len(regime_group)),
            "range_samples": range_count,
            "regime_veto_rate_pct": round(len(regime_vetoed) / len(regime_group) * 100, 2) if len(regime_group) else None,
            "regime_losers_filtered_pct": round((regime_losers_filtered / regime_losers * 100) if regime_losers else 0.0, 2),
            "regime_winners_vetoed_pct": round((regime_winners_vetoed / regime_winners * 100) if regime_winners else 0.0, 2),
            "regime_net_r": round(regime_net_r, 4),
            "regime_delta_r": round(regime_net_r - raw_net_r, 4),
            "regime_decision_accuracy_pct": round(float(regime_group["regime_correct"].mean()) * 100, 2) if len(regime_group) else None,
        })

    report = pd.DataFrame(rows)
    os.makedirs(reports_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    csv_path = os.path.join(reports_dir, f"shadow_metrics_{stamp}.csv")
    json_path = os.path.join(reports_dir, f"shadow_metrics_{stamp}.json")
    latest_csv = os.path.join(reports_dir, "shadow_metrics_latest.csv")
    latest_json = os.path.join(reports_dir, "shadow_metrics_latest.json")
    report.to_csv(csv_path, index=False)
    report.to_csv(latest_csv, index=False)
    payload = report.to_dict(orient="records")
    for path in (json_path, latest_json):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    print(report.to_string(index=False))
    print(f"[SHADOW] CSV: {csv_path}")
    print(f"[SHADOW] JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
