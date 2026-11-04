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
- Recover missing legacy learning metadata only from exact linked entry, close
  and history ledgers. The initial entry STOP supplies planned risk; the later
  recorded close timestamp supplies availability time. Matching identities,
  prices, quantities, direction and gross P/L are required. REAL additionally
  requires broker-confirmed execution evidence. Superseding rows retain original
  prices, outcomes and eligibility; no time or fill is estimated.
- Match the stopped dashboard's risk mode to its selected REAL tickers in MIXED
  routing. The global PAPER default previously showed the wrong loss bucket.
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

The final code candidate passed all 566 tests in an isolated Linux worktree as gala with
network access disabled. Focused startup/manual-close/accounting regressions
also passed on Windows. The full Windows suite encountered existing POSIX file
permission, fcntl and encoding assumptions; Linux is the deployment test gate.
An offline preview of the incident recovery matched the preserved broker packet,
the two original history rows and exactly the four new guard records.

## Applied data review

The exact Nov 4 BA incident was reconciled to the independently retrieved broker
fill. The original five historical Nov 2 accounting records and their reviewed
exception were preserved. BA's gross manual loss of 2.2595 USD was included once
in daily REAL risk; manual results remain excluded from GALA performance and
training. At review time the REAL risk total was -4.1595 USD, including NKE.

116 ambiguous PAPER observations from 58 duplicate strategy-candle groups were
quarantined. The original source rows remain in the ledgers. Nov 4 PAPER
performance is consequently unavailable as a reliable complete-session result;
the conservative persisted PAPER risk total was retained, without adding loss
capacity. Limits remain REAL 200 USD / PAPER 700 USD.

6436 eligible legacy observations had provable missing close/risk metadata and
were repaired by append. Eligible unique observations remain 8326; this metadata
recovery adds no trades and never revives quarantined records. 535 observations
still lack sufficient metadata and remain outside the complete-evidence cohort.
Recovery backups are root-private under /opt/gala/backups, including
learning-metadata-proof-wij7ud5a, ba-manual-verified-lfe_z467 and
paper-duplicate-quarantine-1sg4ozfv.

Archived-candle replay reproduced an ADBE SELL at 10:45 NY and RCL BUY at 10:34 NY
that were missing from live signal history. These replay observations do not
prove thinkorswim parity and were not inserted as executed trades. The exact
client candle time, study inputs and extended-hours setting remain requested.

## Final model evaluation and deployment

After metadata recovery, all 17 tickers were evaluated again with complete
evidence, chronological holdouts and production activation disabled. Every
evaluation passed the temporal and cost-label integrity checks. Production
artifact hashes and feedback hashes were unchanged by training. Eligible totals
and new-observation counters also stayed unchanged by this maintenance evaluation.

META, PYPL and NFLX updated their SHADOW artifacts. AAPL, UNH and PYPL passed the
configured production validation gates. AAPL and UNH did not replace their SHADOW
incumbents because the new candidates scored worse on the same TEST block. These
are separate decisions: passing gates is neither promotion nor a REAL routing
change. No production model was activated, and the five existing REAL tickers
remain BA, META, NVDA, TSLA and NKE.

| Ticker | Complete usable closes | Candidate balanced accuracy | SHADOW updated | Production gates passed |
| --- | ---: | ---: | --- | --- |
| PINS | 663 | 51.87% | No | No |
| PYPL | 473 | 55.59% | Yes | Yes |
| NVDA | 84 | 39.42% | No | No |
| TSLA | 206 | 60.39% | No | No |
| META | 75 | 70.00% | Yes | No |

Full metrics and individual rejection reasons are preserved on the VPS in
data/evidence_policy_reevaluation_20261104.json. Superseded intermediate training
runs and the original SHADOW baseline were preserved in root-private backups;
the final evaluation backup is final-clean-training-emqjl76b.

The final authenticated dashboard check confirmed version 3.3.17, Schwab
connected, a fresh flat/no-working-orders broker snapshot, engine off with no
live runner, REAL entry_blocked=false, REAL daily risk -4.16 USD and its unchanged
200 USD limit. The AI service is waiting for new eligible closes; no model is
left in TRAINING. The web service is active. GitHub and VPS source are synchronized.

Missing or different chart BUY/SELL signals remain the client's reported issue;
trading hours alone do not explain it. The two confirmed signal-history defects
were corrected, but screenshots alone do not establish exact thinkorswim parity
or verify behavior during the next live session. No new broker orders were
placed as part of this investigation or verification.
