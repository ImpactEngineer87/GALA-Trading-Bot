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

Final Windows focused gate: **61 tests passed**. Final Linux full suite:
**650 tests passed in 4.974 seconds**, as `gala`, with networking disabled,
in `/opt/gala/GALA-Trading-Bot-review-3323-20261110` at `12c103a`.

The candidate also replayed copies of the actual accounting state and trusted
historical approval. META/BA and AAPL external-exposure cases allowed a clean
TSLA entry through to a mocked executor, blocked entries in the isolated symbols,
and preserved the global hold for a new unknown GALA fill. Networking was disabled;
the production guard and approval were unchanged. Receipt:
`data/upgrade_3323_offline_replay.json` on the VPS.

### Code locations at the tested commit

| Location | Change |
| --- | --- |
| `engine/bot_engine.py:817` | Remove external-symbol escalation from the global accounting entry gate; publish a stable per-ticker isolation snapshot. |
| `engine/bot_engine.py:3175` | Record the exact isolated-ticker reason and exclude that conflict from executable-trade training observations. |
| `engine/bot_engine.py:3245` | Prevent the direct entry path from bypassing an external ticker's isolation. |
| `engine/bot_engine.py:4439` | Publish fresh periodic broker exposure without clearing prior reconciliation failures. |
| `server.py:4206` | Return the per-ticker blocker to the dashboard. |
| `dashboard/index.html:1678` | Display partial REAL availability and list the isolated tickers. |

## Verified VPS deployment

Verification completed at **2026-11-09 20:50:45 UTC / November 9 15:50:45 NY /
November 10 00:50:45 Saratov**.

- VPS version **3.3.23**, tested executable source at
  `12c103ab41ff278d6c07d78d45df6e45d10a2233`.
- The engine completed its configured session shutdown before deployment. Its
  thread was no longer alive. Maintenance sent **zero engine Stop/Start requests**
  and **zero broker order writes**; the normal session shutdown closed positions.
- Independent strict Schwab GET checks confirmed **zero positions and zero
  pending orders**, including child orders.
- The REAL accounting entry hold and daily loss latch were **false**, with no
  pending manual-close reconciliation.
- All seven REAL selections were preserved: **AAPL, BA, META, NVDA, TSLA, NKE,
  PYPL**. The ticker API reported no entry blockers for these selections.
- **All 63 protected files were byte-identical**, including production/shadow
  models, thresholds, routing, configuration, feedback, trade/signal ledgers,
  accounting approvals and risk state. Dashboard statistics also matched.

Private backup: `/opt/gala/backups/upgrade-3.3.23-d0toyj1u`.
Verification receipt: `data/upgrade_verification_20261110_3323.json` on the VPS.

The web service is active; the trading engine remains **OFF after the normal
session cutoff**. Refresh the dashboard to load the new display. A live entry
under the upgraded engine has not yet been exercised; the next session still
needs live observation.

No models, thresholds, ticker selections, loss limits, historical fills, or
accounting exceptions are changed by this release. Fixed entry-limit pricing and
slow candle-analysis batches are separate remaining limitations. A high AI score
does not prove that an order was submitted or filled.

Existing startup policy still requires a fresh flat account before using the
reviewed historical accounting exception; this release changes runtime isolation.
