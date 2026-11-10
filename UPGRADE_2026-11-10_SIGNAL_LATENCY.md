# v3.3.24 — signal processing latency

## Incident confirmed on the VPS

On November 10, the active v3.3.23 engine processed 17 tickers with two analysis
workers. Completed batches included **120.061 and 148.424 seconds**. Some intervening
closed-candle signals were consequently recorded as `LATE_ANALYZED`, without
retroactive orders. A running engine and updating signals did not establish timely
execution.

The profile and logs identified three sources of avoidable work:

- Expired historical context triggered a complete historical download in an
  analysis worker, even while a background refresh for that symbol was pending.
- Two strategy recurrences constructed pandas indexers for each historical candle.
- Dashboard polling repeatedly parsed the complete execution audit and recomputed
  historical totals. Most newly appended audit rows were candle receipts, which
  cannot change fill attribution or closed-trade P/L.

## Changes

- `engine/bot_engine.py`, `_obtener_mtf`: warm context uses incremental native-frame
  refresh; expiry joins the existing refresh. Current 1m prices are still fetched
  separately. Failed refreshes cannot reset context age or bypass expiry.
- `core/macro_refresh.py`: two background GET workers, earlier prefetch, one
  outstanding refresh per symbol. Existing market-data pacing remains in effect.
- `core/strategy.py`: directly index arrays in the same sequential recurrences.
  Cross memory, cooldown counters, indicator formulas, and signal priority remain
  numerically identical in the offline comparison.
- `core/display_audit_cache.py`: read-only incremental CSV projections for display.
  Newer contradictory fill records supersede older evidence. Incomplete final CSV
  records are deferred; truncation/replacement invalidates the cache. Execution,
  risk, reconciliation, and training continue using their existing authoritative
  sources.
- `server.py`: cache normalized signal history and closed-trade summaries; only
  relevant new audit evidence invalidates historical attribution. New closes,
  quarantine evidence, and New York date changes refresh totals.
- `dashboard/index.html`: one signal fetch at a time; download 100 recent signals
  outside the Signals page and retain access to its 2,000-row history. Background
  performance/analysis/log polling follows the visible page.

AI models, thresholds, routing, position ownership, loss limits, and limit-order
pricing are unchanged by this release.

## Verification

- **661 Linux tests passed**, with network access disabled in an isolated
  worktree (6.934 seconds).
- Exact equality of **all strategy columns on 110,500 archived candles across all
  17 tickers**, against source revision `69ed5e2`.
- Linux strategy computation: **11,906.68 ms → 397.54 ms** summed over those 17
  inputs. This measures calculation only, not live signal-to-fill latency.
- Exact dashboard output equality on private copies of the VPS signal, trade, and
  audit ledgers, including additional unrelated candle receipts.
- Warm history summary: **1,053–1,142 ms → 0.19–0.22 ms**; recent 100-signal query:
  **820–874 ms → 24–25 ms**; 2,000-signal query: **905–948 ms → 76–122 ms**.
- Initial display-cache population took **3,537 ms** in the isolated comparison;
  the initial read is slower than subsequent incremental reads.
- Browser checks cover request backpressure, visible history limits, and explicit
  unavailable evidence after failed requests.

The archived candles are local OHLCV observations. Equality on these inputs is not
proof that every thinkorswim forming-candle marker matches Schwab's closed candles.

## Highlighted META signal

The screenshot's META signal was recorded at **09:59:13 New York**, from the 09:58
one-minute candle that closed at 09:59. Its model score was **27.588424%**, below
the **56%** threshold, so it did not authorize a new entry. The 13 seconds after
candle close for this event is distinct from the longer batch delays confirmed
elsewhere. A separate earlier META entry was submitted at 09:34:17 after its
09:34:11 decision.

## Rollout status

Deployed source revision `0155821` to the VPS at **10:44 New York**, after the REAL
position closed normally and Schwab verified zero positions and pending orders.
The engine resumed through the authenticated dashboard control with the existing
seven REAL selections: AAPL, BA, META, NVDA, TSLA, NKE, PYPL. Runtime checks show
the engine thread alive, with no global accounting hold or daily-loss latch.

Eight PAPER positions closed through the normal Stop procedure. Their exact
position/execution identities were marked as controlled maintenance, excluded
from training and performance using append-only evidence. After that maintenance,
**all 63 protected files were byte-identical across the source deployment and web
restart**, including models, thresholds, routing, guards, and ledgers.

Live observations through **10:53 New York**:

- Startup history loading: **68.574 seconds** for 17 tickers.
- First warm cycle: **6.383 seconds** for all 17 tickers.
- Six warm cycles, spanning the initial five-minute historical-cache period:
  **6.383, 13.804, 8.207, 12.545, 15.554, 7.510 seconds**. The latest cycle
  analyzed META **3 seconds** after candle close; its 17-ticker median delay was
  **4 seconds**, maximum **7 seconds**.
- That warm cycle's analysis delay after candle close: median **3 seconds**,
  maximum **6 seconds**; META **2 seconds** (649 ms within its analysis pipeline).
- Live loopback API sample: status **76 ms**, trades **60 ms**, recent signals
  **187 ms**. These are individual samples, not latency guarantees.

The refresh observation shows the shorter cycles continuing beyond the initial
cache-expiry window. These samples do not establish a guaranteed latency or a
limit-order fill time. REAL order submission/fill after the upgrade is a separate
verification item; no artificial REAL test order was placed. The maintenance
audit contains no REAL entry, exit, protective-order submission, or close event.

Private VPS evidence: `data/latency-review-20261110-kdji1qcf/`; offline source and
regression logs: `/tmp/gala-latency-3324-ymvp2k9t/`. Credentials and broker identities
are excluded from this report.
