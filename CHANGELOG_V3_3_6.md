# Chocoletra GALA10 v3.3.6 — REAL Selection Preservation Hotfix

## Incident fixed
On 2026-09-24, the four operator-selected REAL tickers were silently persisted back to PAPER after transient synthetic/insufficient market-data events. The operator and client did not request those mode changes.

Exact automatic fallbacks recorded in the order audit:
- TSLA — 13:04:01 NY — `LIVEGUARD_PAPER_FALLBACK` — `Synthetic market data`
- META — 13:12:01 NY — `LIVEGUARD_PAPER_FALLBACK` — `Synthetic market data`
- NVDA — 13:27:02 NY — `LIVEGUARD_PAPER_FALLBACK` — `Synthetic market data`
- NKE — 14:37:03 NY — `LIVEGUARD_PAPER_FALLBACK` — `Synthetic market data`

`ticker_execution.json` shows the matching persisted PAPER timestamps. `real_mode_audit.jsonl` contains no operator `EXECUTION_MODE_CHANGE_REQUESTED` records for those changes, confirming they were automatic safety fallbacks rather than dashboard edits.

## Root cause
Both runtime isolation (`engine/bot_engine.py`) and startup preflight isolation (`server.py`) could write `mode=PAPER` into the persistent per-ticker routing registry after a ticker-local REAL safety failure.

That behavior confused two distinct concepts:
- **operator routing selection**: PAPER or REAL, which should persist until explicitly changed;
- **temporary safety state**: a REAL ticker that is temporarily blocked/quarantined because market data/model/inference is not trustworthy.

## v3.3.6 behavior
- Temporary ticker-local failures no longer edit `data/ticker_execution.json`.
- A selected REAL ticker remains selected REAL while its runtime state is `REAL_BLOCKED_TEMPORARY`.
- New REAL entries remain fail-closed while quarantined.
- Existing REAL exposure continues to be treated as REAL and remains under live reconciliation/exit management.
- Market-data quarantines require two consecutive healthy confirmed-data cycles before automatic recovery.
- Model/inference quarantines recover only after the corresponding subsystem succeeds again.
- Startup preflight failures create an in-memory startup quarantine instead of a persisted `REAL -> PAPER` fallback.
- Dashboard wording now identifies these states as `REAL bloqueados temporalmente`.
- Global hard-safety failures remain fail-closed and still block engine startup.

## Important migration note
v3.3.6 does not guess or rewrite operator routing during installation. If v3.3.5 already persisted the desired REAL tickers to PAPER, restore those tickers manually after installing v3.3.6 and after confirming Schwab/account safety.
