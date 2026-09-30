#!/usr/bin/env python3
"""Safely add v3.2.0 config keys to an existing v3.1.0 config.yaml.

- Creates a timestamped backup before changing config.yaml.
- Preserves the user's existing ticker modes, share counts, risk values and credentials.
- Adds/updates only the v3.1.1/v3.2 policy keys needed for the upgrade.
"""
from __future__ import annotations
from pathlib import Path
from datetime import datetime
import shutil
import sys
import yaml

root = Path(__file__).resolve().parent
cfg_path = root / "config.yaml"
if not cfg_path.exists():
    print(f"ERROR: {cfg_path} not found")
    sys.exit(1)

backup = root / f"config.yaml.before_v3.2.0_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
shutil.copy2(cfg_path, backup)

with cfg_path.open("r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}

agents = cfg.setdefault("agents", {})
regime = agents.setdefault("market_regime", {})
regime.setdefault("enabled", True)
# PAPER can enforce the new range/chop protection; REAL observes until validated.
regime["execution_policy"] = {"PAPER": "enforce", "REAL": "observe"}
# Retain the old field for backward compatibility with v3.1.0-era tooling.
regime.setdefault("enforcement", "shadow")

live = cfg.setdefault("live_trading", {})
live["preflight_isolate_ticker_failures"] = True
live.setdefault("broker_preflight_retry_attempts", 3)
live.setdefault("broker_preflight_retry_delay_sec", 0.75)

with cfg_path.open("w", encoding="utf-8", newline="\n") as f:
    yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)

print("v3.2.0 config migration completed")
print(f"Backup: {backup.name}")
print("PAPER market-regime policy: enforce")
print("REAL market-regime policy: observe")
print("Ticker preflight isolation: enabled")
