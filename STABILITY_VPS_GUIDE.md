# October 2026 stability fix — ORIGINAL VPS folder only

This is a **review** checklist for `/opt/gala/GALA-Trading-Bot`. It must not be
deployed into live REAL trading until broker fills, positions, and open orders
are independently reconciled. None of these changes modifies `data/`,
`config.yaml`, `.env`, models, training history, the hybrid order executor,
or execution limits.

## Already confirmed

- Kernel OOM killed `gala.service` Python processes three times on 2026-10-19
  at roughly 7.1 GiB RSS; systemd automatically restarted the Flask process.
- VPS has 7.6 GiB usable RAM and no swap.
- `data/agentes_feedback.csv` had 291,201 lines (102 MB). It was repeatedly
  parsed for AI status.
- `engine.max_workers=2`. The 17-workloads-concurrently theory is **false**.
- systemd already manages the service. Adding PM2 creates no memory fix and
  risks two engines using the same brokerage account.

## Update only the original source tree

Run as the operator on VPS. Do **not** erase data or use `git reset --hard`.

```bash
cd /opt/gala/GALA-Trading-Bot
git status --short
git branch --show-current
git fetch origin
git pull --ff-only origin fix/stream-ai-feedback-status-20261019
git diff --name-only master..HEAD
```

Only fast-forward if the checked-out branch is already
`fix/stream-ai-feedback-status-20261019` and tracked modifications have been
reviewed. The PR changes Python source and test files, not persisted models.

## Check model availability, read-only

```bash
cd /opt/gala/GALA-Trading-Bot
./.venv/bin/python scripts/audit_model_artifacts.py
find data/modelos -maxdepth 1 -type f \
  \( -name 'modelo_AMD*' -o -name 'modelo_UAL*' -o -name 'modelo_RCL*' \) \
  -printf '%f %s bytes\n' | sort
```

`FILES_AND_METADATA_PRESENT` means only that model files and metadata exist
and metadata matches the current feature policy. **It does not mean** the
pickle is healthy or that a model is approved for REAL inference. Separate
runtime load checks and broker safety validation remain required.

`MISSING_PKL` with a nonzero incompatible archive count indicates that old
code may have renamed the artifact after a schema change; **do not blindly
rename or deserialize it for REAL trading.** Restore only verified,
policy-compatible model/metadata pairs from the existing historical backups
with proper approval. Legacy or incompatible models stay fail-closed.

## Run offline tests without starting a second bot

```bash
cd /opt/gala/GALA-Trading-Bot
./.venv/bin/python -m compileall -q \
    core/project_paths.py core/memory_diagnostics.py \
    agents/learning_dataset.py agents/retraining_lifecycle.py \
    agents/training.py engine/bot_engine.py server.py \
    scripts/audit_model_artifacts.py

for t in test_ai_feedback_memory.py test_model_artifact_preservation.py \
         test_ai_learning_training.py test_ai_dashboard_learning.py \
         test_learning_feedback.py test_ai_status_initialization.py \
         test_retraining_lifecycle_persistence.py; do
    ./.venv/bin/python -m unittest discover -s tests -p "$t" -v || break
done
```

Check no tests report `FAIL` or `ERROR`. The initial three feedback tests
passed on VPS *before* this subsequent model/path/telemetry patch; all later
changes require another test run.

## Broker and deployment safety

**DO NOT restart gala.service while there may be managed Schwab positions or
protective orders.** Shutdown/restart can lose in-memory engine state, and
the bot stop routine may liquidate REAL exposure. Check the brokerage account
directly; earlier logs showed NKE short 10 shares and an outstanding order.
A dashboard `PARADO` status does **not** imply the broker is flat.

After confirming there are no unmanaged or managed REAL positions or working
orders and preserving a consistent backup of runtime files and models, plan a
controlled `systemctl restart gala.service` during a safe maintenance window.
Restart is needed to load new code. It should bring up Flask, **not**
automatically issue new REAL trading orders.

Keep using **systemd**, which already has observed automatic restarts. Verify
`systemctl cat gala.service`, `systemctl show gala.service -p Restart -p NRestarts
-p MemoryMax -p WorkingDirectory`, then verify correct `WorkingDirectory`
points to the original root. Do not add PM2 or a second service.

## Observe one full PAPER-only session before considering REAL

```bash
journalctl -u gala.service -f -o short-iso | \
  grep --line-buffered -E '\[MEMORY\]|\[AI-MEMORY\]|MODEL|PRODUCTION|ERROR|KILL'
```

The `[MEMORY]` lines report RSS/peak at trading cycle boundaries, and
`[AI-MEMORY]` lines show memory before parsing, after pandas CSV parse,
and after status feature preparation. They do not stop/kill the engine.
Compare these against `systemctl show gala.service -p MemoryCurrent -p
MemoryPeak -p NRestarts` over a complete session, while checking all 17
ticker model statuses and AI dashboard counters. Investigate continued RSS
growth instead of merely increasing RAM.

**Separate blockers not fixed by this patch:** Schwab HTTP 400/500 order-read
failures, and the disputed META +/-$17 P&L. Reconcile REAL fills and P&L
against broker execution reports before authorizing REAL orders.

## Rollback

Source rollback must be an intentional, reviewed Git operation while the
broker is flat, not `git reset --hard`. **Never delete or restore live
`data/` wholesale as a code rollback.** The current branch changes no
persisted data migration format.


## 2026-10-20: evidence and UI recovery additions

The same review branch now includes:

- A streaming `FeedbackStore` execution-side cache; it no longer retains
  the full 291k-record feedback dataset in memory. Crash recovery indexes only
  pending trade identifiers, and x/50 counters still use last-valid-close
  identity rules. An unrecognized historical CSV schema now fails closed
  **without renaming or starting a blank training dataset**.
- Original-root anchored signal-learning, AI-veto, feedback and override paths.
- Version-pinned manual threshold from former PR #12: one compact "Umbral IA"
  dialog per PRODUCTION ticker. It does **not** bypass trading/risk controls.
- Honest dashboard counters: recorded signals, resolved signals, FIT-eligible
  supplemental signals, verified closed trades, and historic FIT evaluations
  are different things; no invalid rows are invented to make TEST pass.
- A clear indicator when automatic retraining is disabled. The earlier VPS
  config showed `automatic_retraining.enabled=false`; it will not retrain on
  its own until explicitly enabled with appropriate operational safeguards.

### Extra read-only diagnostics

```bash
cd /opt/gala/GALA-Trading-Bot
./.venv/bin/python scripts/audit_signal_learning.py
./.venv/bin/python scripts/audit_model_artifacts.py
grep -nE 'allowed_tickers|automatic_retraining|reentrenar_fin_sesion' config.yaml
```

Do not post `.env`, authorization tokens or complete account numbers.

### Additional tests before service restart

```bash
cd /opt/gala/GALA-Trading-Bot
for t in test_feedback_streaming_recovery.py test_manual_ai_threshold.py \
         test_ai_feedback_memory.py test_model_artifact_preservation.py \
         test_learning_feedback.py test_signal_learning.py \
         test_ai_learning_training.py test_ai_status_initialization.py \
         test_retraining_lifecycle_persistence.py; do
    ./.venv/bin/python -m unittest discover -s tests -p "$t" -v || break
done
```

The newest changes have not been verified on the VPS. Do NOT claim they fix
all memory issues or that AMD/UAL/RCL are production-ready until artifact
diagnostics and the PAPER-only soak run. Do not auto-activate REAL.

### September comparison

A September baseline must be identified by actual commit/tag and verified
runtime records, not an assumed downgrade. Switching to a September branch
without assessing model-schema and broker changes could obscure learning data.
