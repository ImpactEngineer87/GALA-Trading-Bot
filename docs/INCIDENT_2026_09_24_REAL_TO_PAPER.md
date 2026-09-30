# Incident 2026-09-24 — REAL tickers changed to PAPER automatically

## Finding
This was caused by the bot, not by an operator/dashboard change.

The persistent execution registry records:
- TSLA -> PAPER at 2026-09-24T17:04:01Z
- META -> PAPER at 2026-09-24T17:12:01Z
- NVDA -> PAPER at 2026-09-24T17:27:02Z
- NKE -> PAPER at 2026-09-24T18:37:03Z

The order audit records `LIVEGUARD_PAPER_FALLBACK` at the same timestamps, each with reason `Synthetic market data`.

The operator audit has no matching `EXECUTION_MODE_CHANGE_REQUESTED` entries, so the mode changes were not initiated by the admin UI.

## Why
v3.3.5 treated ticker-local safety isolation as a permanent routing edit. Once the broker was flat for a quarantined REAL ticker, `_apply_ticker_quarantine()` called `ticker_execution.update(..., mode='PAPER')`. Startup preflight isolation had a similar persisted fallback path.

## Correct policy
A temporary data/model/inference fault should block new REAL entries while preserving the operator's routing intent. That is the policy implemented in v3.3.6.
