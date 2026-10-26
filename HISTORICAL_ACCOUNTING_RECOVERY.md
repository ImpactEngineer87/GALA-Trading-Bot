# Exact ID historical accounting recovery

`scripts/reconcile_verified_history.py` prepares a reviewable private plan and
can commit one strictly bounded historical accounting reconciliation. It does
not deploy or activate the trading engine. Application version remains 3.3.15.

## Eligible scope

Only a past New York session is eligible. Require one exact runtime position
key, one corresponding closed-history row, complete position/execution/side
identity, and the same existing entry and exit order IDs in runtime state and
history. Both the exact ENTRY_FILLED and POSITION_CLOSED audit records, and an
exact EXIT_SUBMITTED event, must exist. Quantities must agree. Partial exits,
missing IDs, duplicate closes, inconsistent audit identities, previously
archived positions and malformed state are refused.

Fresh GETs against the explicitly configured Schwab account must return those
exact two equity orders with complete terminal FILLED execution legs. Account,
symbol, instructions, quantities, entry cost basis, NY session and causal fill
times are checked. A LIMIT price, local FILLED flag, broker flatness, similar
price/time, candidate order, or order replacement state is never a substitute.

## Preparation

Run from the original installation with its existing Python environment:

```bash
./.venv/bin/python scripts/reconcile_verified_history.py \
  --prepare --runtime-root /opt/gala/GALA-Trading-Bot \
  --date 2026-10-23 --position-ref d744cdfefff2
```

Preparation performs broker order GETs only; the normal OAuth client may rotate
authentication tokens. It creates a 0700 private folder beneath
`/opt/gala/backups` containing a 0600 plan and fresh broker evidence. Printed
output masks identifiers. Do not upload either private JSON file. No active
accounting block or trading/training/history/model/risk artifact is changed.

## Commit maintenance requirements

For the reviewed NKE incident, `scripts/maintain_nke_reconciliation.sh` accepts
the exact reviewed branch commit and private plan path. It must be run only
after dashboard/TOS operators have coordinated a pause. It checks the plan and
unchanged guard, manual scheduler, Auto AI window/headroom and idle worker,
disabled automatic promotion, matching configured/dashboard account, zero
local REAL positions and fresh zero broker positions/orders before requesting
the normal authenticated CSRF-protected engine stop. This may close PAPER
positions under the existing shutdown policy. It waits for the engine thread
to end, delegates update/testing to the guarded helper in hold-stopped mode,
applies only the reviewed plan, verifies three BA/META blocks remain and fresh
broker flatness, then starts the dashboard service with trading OFF. It asks
for dashboard credentials again to verify the new process. It never starts
the trading engine, changes Auto AI settings, or assigns missing order IDs.
Any unexpected check halts; there is no automatic rollback or restart-on-error.

Before stopping anything, coordinate a maintenance window with operators and
obtain fresh engine, scheduler, AI-worker and broker-position/working-order
checks. Stopping this installation may liquidate REAL exposure. Never infer
flatness from a closed market or an earlier dashboard snapshot.

After those checks and coordinated operator pause, the original installation
maintenance helper accepts a reviewed commit plus `--hold-stopped`. It backs
up, updates, requires all 444 isolated tests as gala, checks preserved files
and fresh broker flatness, then leaves gala.service stopped for reconciliation.
Without that flag its existing dashboard-start behavior is unchanged. Do not
use it as a substitute for the required pre-maintenance operator checks.

Apply refuses unless `gala.service` is inactive with MainPID=0. It never stops
the service itself. It requires root, then fresh broker positions and working
orders both exactly empty, another exact-fill GET verification, an unchanged
guard file and unchanged scoped local evidence. A prepared plan does not
authorize a service stop or permit trading risk checks to be bypassed.

The apply syntax is deliberately separate:

```bash
./.venv/bin/python scripts/reconcile_verified_history.py \
  --runtime-root /opt/gala/GALA-Trading-Bot \
  --apply /absolute/path/to/reviewed/plan_private.json
```

Before the atomic guard replacement, archive the original guard bytes, reviewed
plan and fresh raw broker evidence in a private backup folder. The guard stores
the verified historical gross result, original target records and private
evidence location under `verified_historical_reconciliations`; raw broker
account numbers/responses remain only in the private evidence file. Remove
only the proven runtime and historical-summary blocks for that same position.
Preserve all other guard records, existing guard ownership/mode/xattrs, and
existing archived proof. Replay is idempotent and never removes newly raised
blocks. Failed or stale broker evidence leaves the guard blocked. An I/O error
after the atomic replacement requires inspection of the guard and private
backup before retry; the archived plan fingerprint makes replay idempotent.

## Data and risk remain separate

Raw trade/audit CSVs, AI feedback and training history, x/50 counts, models,
config and current-day risk state are preserved. The original historical rows
remain as originally recorded and remain subject to their existing display
and AI evidence requirements. The new archive records recovered gross
accounting separately; it does not fabricate an old live fill event, rewrite
closed_utc, supply fees/costs, approve AI eligibility or imply net account P/L.
Past gross P/L is not added to today's daily-loss baseline.

Resolving one position does not unblock REAL while other unresolved records
remain. This workflow cannot automatically repair BA's missing close ID or
META's adopted position with missing entry/exit IDs. Those associations need
independent exact evidence and a separate reviewed recovery path. Correcting
historical accounting also does not repair the independently rejected TP1
protection orders or establish readiness to resume REAL trading.

The original maintenance updater now requires all 444 regression tests with
zero skips when deploying this branch. No tests or commands may submit live
orders or promote SHADOW models.
