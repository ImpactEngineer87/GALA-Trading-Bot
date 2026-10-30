#!/usr/bin/env python3
"""Start GALA MIXED with PAPER entries only while accounting is held.

Requires a local, authenticated dashboard and fresh verified-flat Schwab
account. Leaves saved PAPER/REAL routing unchanged. Does not submit orders
itself. The dashboard engine may later submit simulated PAPER orders.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from core.mixed_accounting_start import (
    unresolved_real_accounting, broker_confirmed_flat,
)

EXPECTED_REAL = {"BA", "META", "NKE", "NVDA", "TSLA"}
EXPECTED_PAPER = {
    "AAPL", "ADBE", "AMD", "AMZN", "MSFT", "NCLH",
    "NFLX", "PINS", "PYPL", "RCL", "UAL", "UNH",
}


def require(condition, label):
    if not condition:
        raise RuntimeError(label)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-stopped", action="store_true",
                        help="Read-only dashboard/broker safety check before service restart")
    args = parser.parse_args()
    load_dotenv(PROJECT / ".env")
    cfg = yaml.safe_load((PROJECT / "config.yaml").read_text()) or {}
    live = cfg.get("live_trading") or {}
    require(live.get("per_ticker_execution") is True,
            "Per-ticker MIXED routing is not enabled")
    value = Path(live.get("ticker_execution_path",
                          "data/ticker_execution.json"))
    path = value if value.is_absolute() else PROJECT / value
    routing = json.loads(path.read_text())
    rows = routing.get("tickers")
    require(isinstance(rows, dict), "Invalid saved ticker routing")
    real, paper = set(), set()
    for ticker, row in rows.items():
        mode = str((row or {}).get("mode", "")).upper()
        require(mode in {"PAPER", "REAL"}, "Unknown ticker mode")
        (real if mode == "REAL" else paper).add(str(ticker).upper())
    require(real == EXPECTED_REAL and paper == EXPECTED_PAPER,
            "Ticker selections have changed; refusing unattended startup")

    account = os.getenv("SCHWAB_ACCOUNT_NUMBER", "")
    held, reason = unresolved_real_accounting(cfg, PROJECT, account)
    require(held, "REAL accounting hold is not active; refuse this PAPER-only starter")
    print("MIXED profile confirmed: 12 PAPER and 5 REAL selections")
    print("REAL accounting hold:", reason)
    print("Schwab broker-flat verification: checking via GET only")
    require(broker_confirmed_flat(cfg, account),
            "Schwab positions/order status not confirmed flat; abort")

    username = input("Dashboard username: ").strip()
    password = getpass.getpass("Dashboard password (hidden): ")
    require(bool(username and password), "Dashboard credentials required")
    with requests.Session() as session:
        base = "http://127.0.0.1:5000"
        response = session.post(base + "/api/login",
                                json={"username": username, "password": password},
                                timeout=12)
        require(response.status_code == 200,
                "Dashboard authentication failed")
        auth = response.json()
        require(auth.get("status") == "ok"
                and isinstance(auth.get("csrf_token"), str)
                and auth["csrf_token"], "Dashboard login invalid")
        csrf = auth["csrf_token"]
        before = session.get(base + "/api/status", timeout=15)
        require(before.status_code == 200, "Cannot inspect existing engine")
        status = before.json()
        require(status.get("status") == "ok"
                and isinstance(status.get("bot"), dict),
                "Dashboard status invalid")
        require(status["bot"].get("running") is False,
                "Engine already running; refusing to restart or alter it")
        if args.check_stopped:
            print("RESULT: PRE_RESTART_SAFE_ENGINE_STOPPED_BROKER_FLAT")
            print("NO SERVICE OR ENGINE CHANGES")
            return
        require(
            status["bot"].get("mixed_accounting_startup_policy")
            == "REAL_ACCOUNTING_HOLD_PAPER_ONLY_V1",
            "Dashboard process is not running the patched mixed startup code"
        )

        # The POST uses the official controller and CSRF guard. The server-side
        # accounting quarantine is verified again at engine startup.
        started = session.post(
            base + "/api/bot/start", json={"mode": "mixed"},
            headers={"X-CSRF-Token": csrf}, timeout=45)
        body = started.json()
        require(started.status_code == 200 and body.get("status") == "ok"
                and body.get("running") is True,
                "Engine startup refused by dashboard preflight")
        message = str(body.get("message", ""))
        require("REAL entries remain blocked by accounting" in message,
                "Start returned without confirmed accounting quarantine")
        final = session.get(base + "/api/status", timeout=20)
        require(final.status_code == 200, "Started engine status unavailable")
        state = final.json()
        bot = state.get("bot") or {}
        risk = state.get("daily_risk") or {}
        accounting = risk.get("real_accounting") or {}
        require(bot.get("running") is True,
                "Engine did not remain running after startup")
        require(accounting.get("entry_blocked") is True,
                "REAL accounting entry block not confirmed")
        print("RESULT: ENGINE_RUNNING_MIXED_PAPER_ONLY")
        print("Effective PAPER tickers:", ", ".join(sorted(paper)))
        print("REAL selected but runtime-quarantined:", ", ".join(sorted(real)))
        print("Historical accounting hold remains active")
        print("PRODUCTION ACCOUNTING MODIFIED: False")
        print("TICKER ROUTING MODIFIED: False")


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt):
        # Dashboard/account tokens or request bodies must not leak on stdout.
        print("RESULT: START_REFUSED_OR_NOT_VERIFIED; check dashboard safely", file=sys.stderr)
        raise SystemExit(2)
