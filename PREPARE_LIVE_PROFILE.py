# -*- coding: utf-8 -*-
"""Compatibility helper for GALA10 v2.9.2.

v2.9.2 no longer has one global PAPER/REAL profile.  The safe preparation step
is the v2.9.2 migration; afterwards mode and shares are selected manually per
ticker from the dashboard.  Re-running this helper never changes AI state or
silently switches a ticker to REAL.
"""

from pathlib import Path
import yaml

from MIGRATE_V2_9_2 import main as migrate_v292


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yaml"


def main() -> int:
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8-sig")) or {}
    live = cfg.get("live_trading", {}) or {}
    if not bool(live.get("per_ticker_execution", False)):
        print("v2.9.2 per-ticker execution is not configured yet.")
        print("Running the safe v2.9.2 migration now...")
        return migrate_v292()

    print("GALA10 v2.9.2 is already prepared for per-ticker execution.")
    print("No configuration or AI state was changed.")
    print("Use Dashboard -> Ejecución por Ticker to choose PAPER/REAL and shares.")
    print("Keep the bot stopped while changing those settings.")
    print("Run VERIFY_LIVE_READY.py before starting any ticker in REAL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
