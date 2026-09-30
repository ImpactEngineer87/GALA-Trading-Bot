# v3.3.7 Complete Signal Learning — Technical Design

## Problem

Before v3.3.7, the per-ticker XGBoost lifecycle learned principally from
execution-valid CLOSED trades.  This is intentionally high quality, but it means
important evidence can remain outside the supervised history: an AI-vetoed
signal, a valid signal missed by an execution/capacity condition, or a signal
whose trade lifecycle is too unreliable to trust as an actual label.

Treating all of those cases as normal trades would be worse: broker failures,
slippage, missing fill evidence or same-bar ambiguity can create incorrect
labels.  v3.3.7 therefore does not merge everything into one dataset.

## Two trust levels

### A. Actual execution-valid feedback (highest trust)
This remains the authority for:
- x/50 progress and retraining trigger;
- chronological validation;
- chronological test metrics;
- candidate/incumbent comparison;
- production promotion gates.

### B. Counterfactual signal familiarization (supplemental)
A separate ledger captures a non-executed signal with the same numeric feature
schema used by the current model plus audit context such as:
- ticker and BUY/SELL direction;
- AI probability and model threshold;
- market regime and regime confidence;
- disposition/reason;
- proposed entry, stop and target;
- execution mode.

Confirmed subsequent bars are followed until one of four outcomes:
- `TP` — clean positive counterfactual label;
- `SL` — clean negative counterfactual label;
- `AMBIGUOUS_SAME_BAR` — not trainable because bar data cannot prove ordering;
- `TIMEOUT` — retained for analysis but not used as a supervised label.

## Model fitting

When an eligible retraining occurs:

1. The existing actual dataset is chronologically split exactly as before.
2. VALIDATION and TEST stay actual-only.
3. Resolved, eligible counterfactual rows whose resolution predates the actual
   FIT cutoff may be appended to FIT.
4. Supplemental rows receive lower sample weight (`0.35` by default).
5. Supplemental rows are capped (`<= 1.0 × actual FIT rows` by default).
6. Class weighting continues to be anchored to the actual FIT distribution.
7. The resulting candidate must still beat/pass the existing gates.

This means signal familiarization can help the candidate learn patterns that
were previously invisible without allowing hypothetical outcomes to make their
own validation look good.

## Execution-affected data

Execution problems are deliberately separated from signal quality.  An actual
trade with invalid fill/lifecycle evidence remains quarantined under the existing
feedback rules.  A non-executed signal can be followed counterfactually only when
its reason is eligible; hard policy/account protections are audit-only.

This prevents a good market setup from being labeled as a bad setup merely
because the broker/order path malfunctioned.

## Compatibility

The existing numeric model feature schema is preserved in v3.3.7 so installed
v3.3.x models remain structurally compatible.  Richer context is persisted for
audit and future evolution without forcing all 17 models to be retrained at
installation time.
