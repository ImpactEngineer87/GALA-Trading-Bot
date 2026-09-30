# Chocoletra GALA10 v3.3.7 — Complete Signal Learning

v3.3.7 closes the learning-data gap identified after the REAL/PAPER reviews: the
per-ticker AI models no longer have to learn only from signals that became a
completed trade.  The release adds a separate, auditable counterfactual signal
ledger for blocked/vetoed/missed signals while preserving the high-trust
executed-trade dataset as the authority for retraining gates.

## New

### 1. Signal-learning ledger
- New `agents/signal_learning_store.py`.
- Stores the exact GALA10/model feature snapshot for a non-executed signal.
- Records the signal disposition/reason, execution mode, AI probability/threshold,
  market-regime context and proposed entry/SL/TP.
- Follows confirmed bars until TP, SL, same-bar ambiguity or timeout.
- Persists open observations across restarts and deduplicates signal retries.

Default runtime files (created automatically):
- `data/signal_learning_observations.csv`
- `data/signal_learning_observations.csv.open.json`

### 2. Supplemental FIT learning, not synthetic validation
Only an unambiguous TP or SL outcome may be used as a supplemental training
sample.  The default policy is deliberately conservative:
- supplemental weight: `0.35`
- supplemental/actual FIT ratio cap: `1.0`
- VALIDATION: execution-valid actual closed trades only
- TEST: execution-valid actual closed trades only
- x/50 counter: execution-valid actual closed trades only

Same-bar TP+SL ambiguity and timeout/mark-to-market observations are never used
as supervised labels.

### 3. Execution-quality separation
Signals that were not executed because of execution/broker/capacity conditions
can be audited and, only when the hypothetical market outcome is clean and the
reason is considered eligible, can contribute as low-weight supplemental FIT
history.  Actual trades whose lifecycle/fill integrity is invalid continue to be
quarantined by the existing feedback rules and do not become trusted labels.

Policy/account-only blocks such as daily-loss protection and other hard safety
blocks are recorded for audit but are not training candidates.

### 4. Per-ticker learning status in dashboard
The AI status table now shows a separate `Aprendizaje señales` column with the
number of supplemental observations, resolved/trainable examples and open
observations.  The existing x/50 counter retains its original meaning.

### 5. Backward-compatible model policy
`TRAINING_POLICY_VERSION` remains `4`, so existing v3.3.x model artifacts are not
invalidated solely by this release.  v3.3.7 adds `LEARNING_POLICY_VERSION = 1`
to new training metrics so the supplemental-learning policy is auditable.

## Not changed
- GALA10 signal generation.
- The 17 per-ticker model architecture.
- Individual decision thresholds.
- Stop/TP mathematics.
- REAL/PAPER routing and v3.3.6 temporary REAL quarantine behavior.
- v3.3.5 exit recovery / v3.3.2 kill-switch safety.
- Existing accepted models are not automatically replaced.

## Version
- Previous: `3.3.6`
- Current: `3.3.7`
