# 3.3.23: isolate external exposure by ticker

Incident session: November 9, 2026, New York. Local maintenance date: November 10.

## Cause confirmed on the VPS

The engine remained alive. `_real_accounting_status()` escalated any ticker's
`EXTERNAL_BROKER_POSITION` quarantine into a global REAL accounting hold. An
unlinked MARKET entry in one selected ticker therefore blocked entries in all
seven selected REAL tickers. The API order tag does not identify who initiated it.

At 14:43 NY, META and BA had unlinked positions; at 15:00 NY, those had closed and
an unlinked AAPL short remained. At 15:10 NY the account was flat and the old
global hold had already cleared automatically. The five reviewed historical
accounting records had not changed.

## Correction

- External positions remain unowned and isolated to their own REAL ticker.
  GALA neither adopts nor cancels, closes, or replaces their orders.
- Other REAL tickers continue through their existing AI, entry, buying-power,
  position ownership, and risk checks. Two fresh flat position/order reads are
  still required to clear an external ticker's isolation.
- Known GALA P/L remains visible. Its risk scope is explicitly GALA-managed
  trades/positions, with external exposure disclosed separately.
- Unknown GALA executions, pending verification of a managed manual close,
  unreadable accounting state, and the daily loss latch retain global holds.
- The dashboard reports isolated tickers, and periodic broker snapshots replace
  stale FLAT information without clearing a prior failed reconciliation.
- Status reads use a copy of the quarantine map so automatic recovery cannot
  invalidate a dictionary iteration while the dashboard is reading it.
- Entry audit/API evidence identifies `EXTERNAL_POSITION_ISOLATED`.
  Such observations are excluded from executable-trade training samples.

## Scope and verification

Implementation: `engine/bot_engine.py`, `server.py`,
`core/signal_execution_evidence.py`, and `dashboard/index.html`.
Regression coverage: `tests/test_external_position_isolation.py` and
`tests/test_manual_order_ownership.py`.

Initial Windows focused gate: 60 tests passed; initial Linux full suite: 649
passed. The final candidate adds a concurrent quarantine-release regression;
its verification and deployment results are recorded after the gates complete.

No models, thresholds, ticker selections, loss limits, historical fills, or
accounting exceptions are changed by this release. Fixed entry-limit pricing and
slow candle-analysis batches are separate remaining limitations. A high AI score
does not prove that an order was submitted or filled.

Existing startup policy still requires a fresh flat account before using the
reviewed historical accounting exception; this release changes runtime isolation.
