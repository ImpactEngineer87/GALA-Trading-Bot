# November 4: duplicate engine, BA close and AI visibility

All event times below are America/New_York. The VPS investigation was performed
after the November 4 session, on November 5 in the operator's timezone.

## Evidence

Two engine runners started at 09:23:21 and 09:23:51. A stop request at 09:23:21
reported success while the first runner was still initializing. That runner
subsequently enabled itself at 09:23:24. Both runners continued operating until
their separate shutdowns. Identical signal/bar records and two BA position IDs
confirm that a single server process contained competing engine instances.

GALA's BA short entry filled 5 shares at 176.59 at 10:17:28. The other runner
adopted that exposure at 10:19:05. The independent managers repeatedly canceled
each other's protective stops (94 canceled broker STOP orders). An opposite
signal attempted a LIMIT close at 10:33, but Schwab rejected it because another
closing order reserved the shares. Two manual close attempts were rejected for
the same reason. The actual manual BUY_TO_COVER filled at 10:37:21, 5 shares at
177.0419. Gross trade loss before fees: 2.2595 USD. The duplicate local close
records lacked this fill identity, adding four accounting discrepancies and
invalidating the existing exact-scope historical entry exception.

PINS displayed 30% on all 23 recorded signals, but its raw model outputs ranged
from 32.2776% to 87.0276%. A hard-rule cap was being presented as the model
probability. PYPL and NVDA also had varying raw model outputs. TSLA's 21 recorded
signals were vetoed by either MACRO_NEUTRAL or the production model threshold;
crossing a model threshold does not override the configured hard rule.

Automatic SHADOW retraining ran on November 3 and 4. Most old eligible feedback
rows predate the recorded close-time/risk columns. Including these rows in every
evaluation permanently failed production temporal/economic evidence checks,
even when a sufficient complete recent cohort existed. Failed candidates also
leave the incumbent model unchanged. A forecast percentage is distinct from
validation accuracy, training progress, and a promotion decision.

## Changes

- Cancel the exact startup instance when Stop arrives during initialization.
  Retain its recovery object and block replacement until its runner terminates.
- Recheck orphan adoption under the symbol lock and against fresh broker exposure.
- Recover an independently verified manual close even with a rejected durable
  bot exit pending; retire that intent and retain the real risk loss. Exclude
  manual outcomes from GALA performance and training.
- Show the raw model score to two decimals, preserve the policy score separately,
  and retain the original strategy direction and hard-rule reason.
- Preserve the 1-minute indicator warmup across smaller refresh requests. Record
  signals from candles skipped by slow cycles as MISSED_EVALUATION with no
  retrospective order or invented AI score. Only the latest closed bar may trade.
- Add opt-in complete_evidence_only training. This selects recorded close times
  and finite cost/risk evidence without editing feedback or x/50 counters.
  Production validation and manual promotion requirements remain enforced.
- Quarantine both PAPER observations when concurrent engines opened distinct
  positions for the same ticker, side and exact strategy candle. Conflicting
  outcomes cannot be selected retrospectively. Superseding feedback eligibility
  and explicit audit exclusions preserve the original rows and remove ambiguous
  examples from training, x/50 counts and performance summaries. Raw historical
  risk tallies are retained; this never grants additional daily trading capacity.

## Bounded incident recovery

scripts/recover_ba_manual_incident.py verifies the exact Nov 4 entry/manual fill,
two duplicate close rows and four new guard keys. Any additional discrepancy
refuses recovery. Apply requires root, stopped service, unchanged source records
and fresh broker-flat/no-pending evidence. Private backups retain the original
guard, ledgers, risk and broker orders. A root-owned resolution file prevents
those exact unchanged history rows from relatching the resolved incident.
The original five Nov 2 review records remain intact. Risk is adjusted once;
the two duplicate rows never create two trades or training examples.

Exact agreement with the client's ThinkOrSwim chart has not been demonstrated
from screenshots alone. Retain closed-bar timestamps and compare identical
study inputs and candle feeds. Historical missed-bar records are never proof
that a broker order was sent or filled.

## Verification

The candidate passed all 557 tests in an isolated Linux worktree as gala with
network access disabled. Focused startup/manual-close/accounting regressions
also passed on Windows. The full Windows suite encountered existing POSIX file
permission, fcntl and encoding assumptions; Linux is the deployment test gate.
An offline preview of the incident recovery matched the preserved broker packet,
the two original history rows and exactly the four new guard records.
