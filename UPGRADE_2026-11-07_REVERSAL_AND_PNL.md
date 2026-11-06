# 3.3.22: resume deferred reversals and keep current-candle analysis timely

Prepared November 7, 2026 (Saratov), for the November 6 New York incident.
Baseline: `c6454b7`, version 3.3.21. This is a follow-up to the remaining
failures observed after the 3.3.21 deployment, not a claim that its offline tests
had demonstrated a successful live session.

## Independently observed VPS evidence

Read-only snapshot at **23:17 UTC November 6 / 03:17 Saratov November 7**:

- The client restarted 3.3.21 at **11:59:49 NY**. It stopped after the configured
  session cutoff at **15:46:59 NY**. Schwab was flat with no pending orders at
  inspection; effective REAL accounting and daily-loss entry blocks were false.
- After that restart, TSLA produced **7 scored VETOs and 4 LATE_ANALYZED signals**.
  Its BUYs at **12:37:39** (original 12:35 candle) and **12:59:43** (12:57 candle)
  scored **67.0% and 65.1%**, above its 55% threshold, but were observation-only.
  There were **no TSLA order submissions after the restart**. The client was
  right that TSLA did not resume trading; it was still evaluating signals.
- Across 189 instrument batches, **33 exceeded 60 seconds**, including 10 over
  90 seconds. Maximum **123,562 ms**, average **44,861 ms**. Separating broker waits
  in 3.3.21 had not removed the repeated full historical-download delay.
- BA BUY reversal at **12:28:05 NY** encountered unavailable fresh quotes at
  12:28 and 12:29. Its previous short ultimately closed at **12:30:20**, fill
  **172.83**, but its approved reverse entry was discarded. A later heartbeat
  closed the old position without continuing the approved reversal.
- PAPER totals were N/A because **10 deliberately excluded maintenance closes**
  poisoned the completeness test for the entire day's eligible PAPER P/L. The
  snapshot contained 125 eligible normal PAPER closes. No missing REAL fill
  price was responsible for the PAPER card.

Private incident packet: `data/incident-3322-qzu4am_a/private.json` (0600).

## Defects and code changes

| Location | Defect / correction |
|---|---|
| Baseline `engine/bot_engine.py:2543` | Every expired macro cache synchronously downloaded the full multi-timeframe history in one of two analysis workers. |
| `core/macro_refresh.py:7` | One background history worker, at most one outstanding refresh per ticker. History results cannot install after shutdown. |
| `api/schwab_client.py:846` | Incremental native 30m/15m/5m tails retain the full indicator warmup; daily/weekly context still comes from native Schwab reads. Full initial history retains its native 30m base. |
| `engine/bot_engine.py:2664` | Refresh macro context before its existing 300-second expiry, independently from the fresh 1m execution feed. A slow background snapshot cannot overwrite newer 1m prices. Cache expiry and provider-error fallback retain the configured limit. |
| Baseline `engine/bot_engine.py:2923` | A failed prior close returned and discarded the approved reversal. No subsequent crossover was guaranteed to retry it. |
| `engine/bot_engine.py:1824` | Retain a runtime reversal intent tied to its exact source signal and original position. |
| `engine/bot_engine.py:1845` | On the next confirmed candle, run actual AI again using current data, price and ATR. Preserve original signal attribution in `execution_intent`. |
| `engine/bot_engine.py:1953` | Recheck intent/position before execution; opposite GALA signals cancel pending entries even when AI vetoes a new opposite entry. A fill winning cancellation is closed through the verified path. |
| `engine/bot_engine.py:1894` | The late-opposite heartbeat also handles pending-entry cancellation. It never opens a historical observation. |
| Successful close accounting | Only a verified intended reversal close can keep an intent ready for its opposite entry. Manual/unverified closes, replacement positions, stopped engines, newer master signals and a fresh AI veto cannot reopen the old intent. A newer candle arriving during a close wait requires another fresh analysis. |
| `core/dashboard_evidence.py:139` | Mark only explicitly documented maintenance exclusions as intentional. Preserve their excluded performance/training status. |
| `server.py:602` | Sum eligible PAPER trades despite known maintenance exclusions; missing/invalid P/L and ambiguous records still produce N/A. Report the number of excluded maintenance closes separately. |
| `dashboard/index.html` | Label PAPER P/L as eligible performance, show maintenance exclusions, explain re-scored reversal retries and label saved rejection messages as history. |
| `server.py:_is_execution_retry` / chart markers / counters | Retain retry-analysis receipts for inspection without counting or plotting them as additional GALA master signals. |

Models, thresholds, ticker routing, REAL/PAPER risk limits, order timeouts,
normal LIMIT-only exits and confirmed cancellation/fill checks are not changed.
Historical signals are preserved. Supplementary signal-learning observations
remain separate from actual executed trades and x/50 close counters.

## Verification / deployment

- **117 focused Windows tests passed**, covering the affected analysis, native
  refresh, reversal, cancellation, close-accounting and dashboard paths.
- **All 635 Linux tests passed in 3.663 seconds**, as `gala`, with networking
  disabled in `/opt/gala/GALA-Trading-Bot-review-3322-20261107`, final tested code
  `cb377d5`. This includes the two additional receipt/counter/chart checks.
- Actual incident-ledger replay produced **-$265.38 eligible PAPER P/L** across
  **125 normal closes**, with **10 maintenance closes still excluded**. Original
  history, audit, feedback, risk files and model artifacts were not amended.
- A GET-only actual market-data measurement refreshed **17 native 1m feeds in
  4,353 ms** with two workers. For TSLA/BA/META, a cold full history batch took
  **11,681 ms**, current 1m reads during background history work **1,553 ms**,
  and all three incremental context updates completed in **5,563 ms**. The
  80 HTTP calls were GETs; no order was submitted and no position was modified.
  These are after-hours data measurements, not an open-session latency guarantee.
  Evidence: `data/incident-3322-qzu4am_a/candidate-replay-result.json`.
- Deployment verification is pending. The production checkout remains on
  3.3.21 at this writing.
- The client selected waiting for normal REAL closes before maintenance. The
  latest read confirmed flat; deployment will repeat the strict broker checks
  and leave the trading engine OFF, with the dashboard service available.

Cold startup, provider outages and genuine missing data can still delay
analysis. The live `[ANALYSIS]` and new `[ANALYSIS_TICKER]` timings distinguish
history/network delays from inference/execution. Tests and after-hours data
measurements cannot establish next-session fill performance or profitability.
