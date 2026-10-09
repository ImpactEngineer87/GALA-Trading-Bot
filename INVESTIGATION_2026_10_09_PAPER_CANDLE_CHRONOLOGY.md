# PAPER loss investigation and candle chronology — v3.3.22

Reviewed deployed main `0b9da14b572f86f716568d4a90f110c6d2ceaac5`
(v3.3.21). The client's October 9 screenshot shows:

- PAPER session PnL of -$642.68, entirely in closed trades; zero open PAPER trades.
- One unresolved REAL position reported as NVDA SELL 5, with NVDA orders open.
- The engine stopped, and the REAL daily-risk panel at $0 / -$200 with
  incomplete valuation. That $0 does not establish the open REAL position's PnL.
- Five symbols selected REAL and twelve PAPER. AAPL is displayed with a
  configured PAPER size of 20 shares; the executed trade sizes require the ledger.

The screenshot does not identify the losing trades, their entry/exit times,
whether the losses arose before/after a deployment, or which of the following
source issues affected those trades. It cannot establish that all or any of
the -$642.68 came from a code regression. No trading-performance result is
validated by the existing order-control regression suite.

## Reproduced PAPER simulation defect

`_gestionar_paper` applied the latest candle's complete high/low to the current
position every cycle. It checked neither entry time nor whether the candle
had already been handled for that position. Strategy signal deduplication
does not protect this independent exit routine.

A reproduction against v3.3.21 used:

- A candle starting 13:30 UTC, ending 13:31 UTC, with close 100 and low 98.
- A PAPER long opened at 09:31:05 New York / 13:31:05 UTC, entry 100, stop 99.
- The same already-closed candle returned by a later market-data cycle.

The actual production method called `_cerrar_posicion('AAPL', 99, motivo='SL')`,
using a low that occurred before the position existed. This is an incorrect
simulated loss independently of the day's market conditions. Pre-entry highs
can likewise award impossible targets. After TP1, reprocessing the same OHLC
could also close at a newly moved breakeven stop with no later candle.

This defect exists in the reviewed baseline. The chronology patch does not
establish when it was introduced or prove it caused today's reported losses.

## Correction

1. The actual PAPER opening path stores the exact signal-candle timestamp on
   the position. An invalid timestamp blocks before a simulated entry is sent.
2. PAPER OHLC management ignores the entry candle and older candles. For
   legacy positions it uses the opening instant to reject a wholly pre-entry
   micro candle. New York opening times and UTC candle indices are normalized.
3. Each eligible candle is handled once per position. TP1 cannot cause another
   poll to reuse its earlier range against the newly moved breakeven stop.
   A new position gets its own cursor; an older candle cannot move it backward.
4. The processed cursor is committed after the bar handler succeeds, so an
   exception does not suppress retry. Existing SL/TP values, stop precedence,
   partial quantities, sizing, entry AI thresholds and REAL closing paths are
   unchanged. Manual shutdown remains independent of the PAPER candle guard.

This makes PAPER event chronology more consistent. OHLC data still does not
provide intrabar tick order; when both SL and TP are touched, the existing
conservative stop-first policy remains. No live-performance improvement or
exact fill behavior is inferred from these offline corrections.

## Separate existing mixed-mode daily-risk limitation

`daily_risk_snapshot` selects REAL whenever any ticker is configured REAL.
The persistent guard accumulates PAPER and REAL realized totals separately,
but `_evaluar_limite_perdida_diaria` evaluates only that selected mode. In an
offline reproduction with PAPER limit 700 and REAL limit 200:

- PAPER -642.68 / REAL 0 produced a REAL net of 0, no trigger.
- PAPER -800 / REAL 0 also produced no trigger.

Therefore the current mixed-mode protection must not be described as a PAPER
-$700 automatic cap. The actual current PAPER limit on the client's VPS is
not visible in this screenshot. The -$642.68 screenshot alone also does not
show that a $700 threshold was crossed. This limitation is not changed here:
silently letting PAPER performance trigger liquidation of REAL positions would
change the execution policy and would not explain individual losing trades.

## Verification and limits

207 focused offline unittest regressions passed, including 15 new PAPER tests.
They use actual production opening, position registration and exit method
bodies with real pandas, controlled clocks and fake execution/learning hooks.
They cover long/short pre-entry stops, targets/partials, later valid stops,
unchanged sizing/SL/TP, repeated/out-of-order candles, TP1-to-breakeven handling,
new-position cursors, naive/aware timestamps, late execution, malformed timing,
exception retry, and the documented mixed-mode guard limitation.

Existing REAL exit, queued-order, ambiguity, request-budget, market-data and
shutdown tests also pass. Compilation and `git diff --check` passed. Full
application and live integration were not run; optional Flask/requests/pytest
dependencies are absent from this workspace. No VPS deployment, broker
orders, trade-history edits, token changes or model promotion was performed.

Run the new focused suite:

```bash
python -m unittest discover -s tests -p test_v3_3_22_paper_bar_chronology.py
```

## Required loss attribution and recovery

Keep new entries stopped. The account owner must check NVDA's current position
and existing orders directly in Schwab. A stopped process does not close or
cancel them, and no new blind close should race an unresolved accepted order.

To quantify the PAPER loss and investigate earlier updates, obtain the VPS
copies of today's rows from `data/historial_operaciones.csv`, the matching
`logs/chocoletra.log`, and the current `data/ticker_execution.json`. Exporting
the trade rows with entry/close events, times, sides, prices, quantities, PnL
and reasons allows attribution by ticker and matching to candle/execution
events. Files in GitHub are not substitutes for the client's session ledger.
Do not provide OAuth tokens, secrets or the full credential-bearing config.

Preserve the original history, feedback and model files. Do not erase losing
trades, reset PnL, delete journals, increase the loss threshold or promote
models to hide the result. The fix applies to future simulated events; any
affected historical rows require a separate evidence-based review and replay.
Review and validate the patch in PAPER before resuming REAL entries.
