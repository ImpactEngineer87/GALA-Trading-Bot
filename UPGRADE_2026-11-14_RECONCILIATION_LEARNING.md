# BA reconciliation and signal-learning audit — v3.3.28

## Confirmed incident

The November 13 session used v3.3.27. BA entered a five-share short at
**$170.15** through GALA order `1008667165138`.

- **12:09:02 New York:** an opposite BUY signal requested the short's close.
- GALA canceled its linked target/stop allocations.
- **12:09:35:** a fresh bid/ask was unavailable for the normal LIMIT close.
  The executor deferred that close and preserved protection.
- GALA submitted replacement STOP `1008668013310` for all five shares.
- **12:09:41:** Schwab filled that STOP at **$170.57**, closing the short.
- **12:09:55:** the engine failed to recover the replacement STOP's receipt and
  queued BA for reconciliation. That pending queue blocked new REAL entries.

The close's verified gross P/L is **-$2.10**. Account flatness alone was not
used to infer its price. The queue was incorrectly labeled as a pending manual
close even though this was GALA's own protective STOP.

The screenshot's five old TSLA accounting discrepancies were still covered by
the existing reviewed exception. The new BA pending queue was the active
entry blocker. PAPER trading continued; the evidence does not show a complete
process freeze at 12:09. At the first inspection the engine was stopped, with
its thread ending at 15:45:41 NY and no last thread error recorded.

## Repairs

| Code location | Change |
| --- | --- |
| `engine/bot_engine.py:5153` | Recover the independently verified standalone STOP after canceled native OCO exits. Retain and quantity-check any earlier native fills. |
| `engine/bot_engine.py:5989` | Continue checking flat-position execution receipts after the three submission attempts have been exhausted. Do not send another order to an already flat symbol. |
| `scripts/recover_pending_managed_stop.py:28` | Verify an exact queued position, audited GALA entry, position-owned STOP submission, complete broker execution legs, side, quantity and price. |
| `scripts/recover_pending_managed_stop.py:60` | Recover the verified close once in history, audit, risk and the original feature ledger before removing its queue record. Guarded maintenance requires inactive service and a fresh flat/no-orders check. |
| `agents/training.py:559` | Surface an unreadable existing supplemental signal ledger as `SIGNAL_LEARNING_READ_FAILED`; do not silently substitute zero samples. |
| `agents/training.py:913` | Retain per-fit supplemental selection evidence, including low-rated winners available/selected, late-outcome exclusions, weights and cutoff. |
| `agents/training_report.py:26`, `agents/retraining_lifecycle.py:306` | Preserve the same evidence in reports and the evaluation status for subsequent training runs. |

No probability threshold, ticker routing, risk limit or production model is
changed by this release. Verified complete replacements can resume the existing
normal close path; incomplete or contradictory execution evidence remains held.

## How low-rated winners are handled

A low model score does not become a loss label. For an executed GALA trade,
verified entry/exit prices determine its outcome. For a signal rejected before
execution, the separate signal ledger follows subsequent confirmed candles to
its target or stop; an unambiguous outcome with complete features can supplement
FIT at **0.35 weight**. Both winners and losers are retained. These observations
are hypothetical outcomes, not actual brokerage fills, and never enter economic
validation/TEST or advance the verified-closed-trade x/50 counter.

**The audit found that none of the following recent supplemental observations
was selected by the current chronological FIT splits.** All resolved candidates
fell after their respective FIT cutoff. Claiming they had already improved the
current production models would be incorrect.

| Ticker | Low-score winning candidates | All resolved complete candidates | FIT cutoff, UTC | Supplemental selected |
| --- | ---: | ---: | --- | ---: |
| TSLA | 115 | 320 | 2026-08-20 17:01:14.999999999 | 0 |
| BA | 81 | 282 | 2026-09-09 15:51:00.999999999 | 0 |
| META | 103 | 293 | 2026-08-14 13:42:01.999999999 | 0 |
| AMD | 3 | 52 | 2026-09-24 15:57:01.999999999 | 0 |
| PINS | 2 | 21 | 2026-09-30 15:29:02.999999999 | 0 |

This is a read-only audit of the source data before BA's pending close was
recovered, not a new model fit.
The temporal boundary was preserved: inserting these later outcomes into an
older FIT block would contaminate its historical validation/test results.
Inclusion in a future candidate depends on that candidate's actual chronological
split and data sufficiency; it does not happen immediately after each signal.
No production improvement is claimed from this diagnostics release.

## Verification

- **732 Linux regression tests passed** in isolated VPS and GitHub environments,
  with external networking disabled.
- [CI run](https://github.com/ImpactEngineer87/GALA-Trading-Bot/actions/runs/43515413075)
  tested source `fb13c6b9c8c47e54dd1b8ed1885cbccfd655902f`.
- Tests cover canceled OCOs followed by a filled standalone STOP, mixed native
  and replacement fills, incomplete prices/quantities, receipt recovery after
  the retry limit, original-feature recovery without duplicate P/L, and low-score
  winners/losers entering FIT with the correct weight and temporal exclusions.
- The read-only live recovery preview independently verified BA's five-share
  STOP fill and the -$2.10 gross loss.

## Deployment

**Deployed on November 14 at approximately 02:03 Saratov** (November 13,
17:03 New York), source `fb13c6b9c8c47e54dd1b8ed1885cbccfd655902f`.
Final fresh verification completed at **02:05:55 Saratov**:

- Dashboard reports **v3.3.28**; the engine is stopped and its thread is dead.
- Schwab confirms **0 positions and 0 pending orders**.
- **New REAL entries are no longer held by accounting**; the daily-loss latch
  is inactive. No order was placed or canceled during this maintenance.
- BA's exact replacement STOP is recorded once in history, audit, risk and
  the original feature ledger. The feedback row is execution-valid and labeled
  as a loss; its close time is Schwab's actual fill time.
- The queued BA record is removed after the verified writes. BA now has **423
  eligible closes, 15 new since its last training attempt**; all other ticker
  counts and every ticker's last-attempt counter are preserved.
- Local verified GALA realized REAL gross P/L is **+$4.87**, including BA's
  **-$2.10** close. This is not Schwab's complete account P/L or a net-fee total.
- Every production and SHADOW model file, threshold override, ticker selection,
  risk limit and configuration file is preserved. TSLA remains on its previously
  approved model with its 52% threshold.
- The historical accounting guard remains byte-identical. Recovery did not
  erase old discrepancies or manufacture missing execution prices.

The new training diagnostics are emitted by subsequent candidate fits; the
separate read-only audit supplies today's inclusion/exclusion evidence without
rewriting old evaluation records or claiming a new production model was fitted.

Receipts and backups:

- `data/upgrade_verification_20261114_3328.json`.
- `/opt/gala/backups/upgrade-3.3.28-zz0pryvl/`.
- `/opt/gala/backups/managed-stop-recovery-f11dbubq/`.
- Read-only per-ticker learning audit:
  `/opt/gala/backups/incident-20261114-1x1rq0cw/supplemental_fit_audit.json`.

Private incident evidence: `/opt/gala/backups/incident-20261114-1x1rq0cw/`.
