# 3.3.21: keep candle analysis independent of order waits

## Incident verified on the VPS

On November 6 at **19:54 Saratov / 10:54 New York**, version 3.3.20 was running.
The stored TSLA SELL emitted at **10:30:47 New York** belonged to the candle
opening at **10:28** and carried `MISSED_EVALUATION`, without a model score or
an order. Schwab confirmed a long of 3 shares at 359.50, with one working SELL
STOP for 3 shares at 355.76. The inspection submitted no orders and did not
stop or restart the running engine.

The same missing-analysis path affected REAL and PAPER, including META. The
dashboard's generic "Entrada bloqueada/no enviada" label obscured the timing
reason; it did not establish a global accounting hold. Effective REAL and daily
loss blocks were both false at the incident read.

## Baseline defect and exact source locations

Baseline is commit `8fffe3b`, version 3.3.20:

- `engine/bot_engine.py:1393`: two configured workers processed all 17 tickers.
  The main loop waited for the complete batch. Each worker also performed
  position management and order waits before its strategy/model check at
  baseline line 1749.
- Baseline lines **2311–2329** discovered intermediate candle signals after a
  slow cycle, recorded them with no probability, and sent no order. The AI
  pipeline received only a signal on the latest candle. An opposite signal on
  an intermediate candle consequently could leave an older position open.
- `core/signal_execution_evidence.py` treated the timing skip as a generic
  execution block. `dashboard/index.html` displayed that label in both modes.

The two-worker configuration predates this upgrade. This change retains the
configured number of analysis workers and separates the order work.

## Implementation

| File / entry point | Change |
|---|---|
| `core/symbol_work.py:11` | Bounded per-symbol order mailboxes: one running task and one latest pending snapshot per symbol; separate threads from analysis. |
| `core/symbol_work.py:60` | Causal snapshot helper removes forming/future native frames for delayed analysis. |
| `engine/bot_engine.py:1356` | Create up to 18 lightweight order workers for the 17 tickers plus account reconciliation. Heavy analysis retains the two configured workers. |
| `engine/bot_engine.py:1766` | Calculate and publish the signal/model decision before submitting position/order work. |
| `engine/bot_engine.py:1803` | Remember the newest analysed master direction; an opposite event can close an older position on a later heartbeat. It cannot close a position opened after that event or open a historical entry. |
| `engine/bot_engine.py:1852` | Run order/position work under the existing symbol lock, recheck current state, and explicitly explain stopped/superseded snapshots. |
| `engine/bot_engine.py:1898` | Shared live/delayed AI pipeline, preserving each ticker's model and threshold. Exceptions receive an explicit reason rather than disappearing before a signal receipt. |
| `engine/bot_engine.py:2507` | Analyse every discovered intermediate signal using data settled at its original close, the original New York time and causal GALA features. |
| `engine/bot_engine.py:4284` | Recheck position tracking and fresh broker exposure under the symbol lock before asynchronous orphan-order cleanup. |
| `agents/orchestrator.py:268` | Existing insufficient-context/RTH refusals now carry explicit reason codes. No additional strategy gate. |
| `core/signal_execution_evidence.py:90` | Separate historical unevaluated signals, delayed analysed signals, unavailable models/context and broker order outcomes. Existing submission/fill evidence wins. |
| `dashboard/index.html:1840` | Human-readable timing labels and explanation of causal delayed scores. |

Shutdown marks the engine inactive and drains order work before liquidation.
No new entry may start from a queued task after Stop. Per-symbol serialization,
capacity, daily-loss limits, order timeouts and actual-fill verification remain
in force. Replaced/stopped/error tasks retain explicit audit reasons.

## Delayed analysis and learning semantics

- A delayed score uses settled frames available at the original candle close.
  It is labelled `LATE_ANALYZED`, with its score, threshold, model decision,
  entry-policy decision and original candle identity.
- This is explicitly a causal reconstruction with settled macro frames, not a
  claim to reproduce an unrecorded live forming-macro snapshot exactly. GSCO
  values from a later time are not used in a historical observation.
- A missing/unusable model or context stays N/A, with
  `LATE_ANALYSIS_UNAVAILABLE` and the available reason. No fabricated rating.
- Delayed entries are not replayed into the broker. An older tracked position
  can still close when the newest analysed master event opposes it, using the
  normal verified close path and current execution snapshot. A newer opposite
  master event supersedes an older direction.
- Non-executed observations remain in the separate signal-learning ledger.
  They do not count as actual fills, realized P/L or execution-valid x/50 closes.
  The existing low-weight, FIT-only supplementary learning policy is retained.
- Original historical signals and decisions are not rewritten. Their old
  `MISSED_EVALUATION` rows receive a clearer read-only display explanation.
- Production/shadow model artifacts, model thresholds, routing selections and
  REAL/PAPER loss limits are not changed by this implementation.

## Verification status

- **45 focused tests pass locally**, including actual orchestrator inference,
  delayed candle scoring, causal input isolation, single processing per candle,
  broker-wait isolation, mailbox coalescing, shutdown races, same-symbol
  serialization, late opposite closes, newer-position protection, orphan
  cleanup races, supplementary learning identity and actual Node rendering.
- Windows full discovery exposed Linux-only `fcntl`/POSIX-mode checks, an
  existing Windows file replacement limitation and a Windows encoding-dependent
  historical-log fixture. The old timing-label expectation was updated to the
  new explicit unavailable-analysis outcome for its deliberately incomplete
  context. Linux full-suite verification is the deployment gate.
- **All 610 tests pass on Linux**, run as `gala` with networking disabled in
  `/opt/gala/GALA-Trading-Bot-review-20261106` at code commit `4b60935`.
  The production checkout and trading process were untouched during testing.
- An isolated replay used copied production TSLA/META models, the active
  threshold overrides and actual Schwab candles. All four formerly unevaluated
  signals produced a model score; each reconstructed GALA direction matched the
  saved direction. The replay submitted no orders and changed no positions or
  models. Results below use settled macro context, not the unrecorded exact live
  forming-macro input.
- The implementation is not yet loaded into the VPS process. Deployment must
  wait for independently verified broker flat/no pending orders; the client
  previously selected waiting for positions to close normally.

### Production-model replay (November 6, New York emission time)

| Signal | Reconstructed score | Active threshold | New-entry model decision |
|---|---:|---:|---|
| TSLA SELL 09:36:54 | 66.42% | 55% | APPROVED |
| TSLA SELL 10:30:47 | 47.79% | 55% | VETO |
| META BUY 10:17:57 | 30.65% | 56% | VETO |
| META BUY 10:46:38 | 40.00% | 56% | VETO |

A veto of a new short does not veto closing an existing long on the opposite
GALA signal. The late-opposite heartbeat path is covered by execution tests.
These are labelled reconstruction results, not replacements for historical
decisions or evidence of trades that never occurred.

Private replay evidence: `data/timing-model-replay-20261106-veazopoe/result.json`.

Incident packet on VPS: `data/timing-review-2026-11-06-q145_mzw/private.json`
(0600). This contains the exact read-only broker, signal and audit snapshot.

Network failures, missing models and provider delays can still make a score
unavailable or late; this upgrade makes those cases explicit. Offline tests
do not establish future profitability or guarantee LIMIT fills.
