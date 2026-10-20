# Operator-controlled automatic AI training (SHADOW only)

This branch adds an **optional**, audited AI-training switch to the original
GALA10 mixed PAPER/REAL VPS deployment. It deliberately does **not** turn it
on merely by installing code. No historical files are replaced.

## What can and cannot count for training

- `data/agentes_feedback.csv` contains executed, integrity-verified REAL/PAPER
  closed trades. **New verified closes advance each ticker's x/50 lifecycle
  counter** and determine automatic retrain eligibility.
- `data/signal_learning_observations.csv` contains non-executed observations.
  Only nonambiguous TP/SL outcomes with valid features and resolution time
  before the FIT cutoff can supplement **FIT** at reduced weight and a cap.
  They are recorded and shown separately from x/50.
- Counterfactual signals are **not broker fills**, cannot advance x/50, and
  are excluded from VALIDACIÓN and TEST economic approval.
- Automatic work is queued after the New York market session, sequentially,
  for tickers with enough verified new closes. New SHADOW candidates are
  evaluated; `allow_production_activation=False` unconditionally for automatic
  jobs. REAL ticker modes, approvals, limits, and broker orders never change.
- The manual `Reentrenar pendientes` option may operate while the engine
  is STOPPED, even if the automatic switch is disabled. It requires pending
  eligible x/50 tickers and likewise does not auto-promote PRODUCTION.

## Activation via dashboard

After deploying the branch in a controlled maintenance window and verifying
the service can safely restart without leaving Schwab exposure:

1. Open the dashboard **Ciclo IA por ticker**.
2. Check that `Auto IA: desactivado` is shown.
3. Click the button, review the confirmation, select to enable.
4. The setting persists in `data/ai_training_control.json` with an append-only
   audit in `data/ai_training_control_audit.jsonl`. The live trainer reads it
   on next eligible close or after-session trigger without restarting.
5. The button can disable **future** automatic jobs. It does not cancel jobs
   already running.

The operator switch can be enabled only when `agents.shadow_mode.enabled=true`
and `agents.entrenamiento.reentrenar_fin_sesion=true` (default).
If either is off, the UI shows a blocker. The existing `config.yaml`
`agents.entrenamiento.automatic_retraining.enabled=false` continues to be
the fallback until an operator explicitly changes the switch.

DO NOT set `auto_activate_production=true`; this feature always forces
automatic training to SHADOW-only regardless of a legacy setting.

## Confirm first: safe to test?

Before checking out or restarting source files in the original directory,
independently verify live positions and orders in Schwab. The engine's stop
routine can liquidate positions. Do not restart solely to activate this
feature while BA/META/NVDA/TSLA/NKE have unresolved REAL exposure.

The current stable service can remain online untouched while this branch is
reviewed. The code must be loaded by a controlled application restart to
show the new dashboard button, but **enabling the switch later never
requires another service restart**.

## Review and regression commands (only during approved maintenance)

```bash
cd /opt/gala/GALA-Trading-Bot
git status --short
git fetch origin
# If worktree is clean and the currently checked-out branch is the base:
git switch -c feature/safe-auto-shadow-retraining-20261021 \
  --track origin/feature/safe-auto-shadow-retraining-20261021

./.venv/bin/python -m compileall -q \
    core/ai_training_control.py agents/training.py server.py \
    scripts/audit_signal_learning.py

for t in test_ai_training_control.py test_ai_learning_training.py \
         test_ai_status_initialization.py test_ai_feedback_memory.py \
         test_learning_feedback.py test_signal_learning.py \
         test_retraining_lifecycle_persistence.py \
         test_ai_dashboard_learning.py; do
   ./.venv/bin/python -m unittest discover -s tests -p "$t" -v || break
done
```

Node.js is necessary for `test_ai_dashboard_learning.py`; without Node,
that test will be marked SKIPPED rather than passing JavaScript assertions.

After safe activation, check the active `/api/ai/status` via the logged-in
dashboard: automatic_retraining, automatic_retraining_source,
end_session_training_hook_enabled, pending, training and FIT signal counts.

Read-only evidence check:

```bash
./.venv/bin/python scripts/audit_signal_learning.py
systemctl show gala.service -p MainPID -p MemoryCurrent -p MemoryPeak -p NRestarts
```

This branch is a review feature, NOT a claim that a full NY session has
completed or that a candidate passes economic TEST. No new REAL orders
should be approved until the separate Schwab order reconciliation and META
P&L sign issues are verified.
