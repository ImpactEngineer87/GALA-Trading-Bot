# REAL staged profit targets — 3.3.26

## Status

The upgrade is implemented, tested, and published on GitHub `master`.
The source changes are in commits `c595aef` and `4ba34fa`; the latter passed the
final Linux regression run. The tested release is pinned at `dc21c1b` (its
additional change is documentation).

SSH access was restored at approximately **10:26 New York on November 12**.
Earlier attempts timed out before authentication; their network cause has not
been established. Live source remains `cd12c70`, version **3.3.25**. The engine
is actively trading, so the running source and exit configuration have not yet
been changed.

The VPS has an isolated 3.3.26 checkout, verified tests, private incident
evidence, and a guarded one-time deployment scheduled for **November 12 at
15:50 New York (20:50 UTC)**. Deployment proceeds only after the engine has
already stopped and strict broker-flat checks succeed. It never invokes the
engine Stop or Start action. This is a scheduled attempt, not a completed
deployment.

## Why REAL sold the whole position

The live VPS configuration inspected on November 12 uses
`live_trading.real_exit_policy: FULL_POSITION`. In
`core/exit_policy.py:15`, `effective_partial_enabled()` explicitly disables
partials for REAL under that policy. PAPER retains its configured partial plan.
That explains why PAPER could show 70/30 while REAL closed the full quantity.

The policy was introduced in commit
`003180ca67a4c1f222b2666be96e12ee06f5eb12` on October 30. It addressed a real
broker conflict: separate, unlinked TP1 and full-size STOP orders could reserve
more shares than the position held. For example, a three-share short with a
three-share STOP plus a two-share TP1 caused an exit rejection. Simply enabling
the old unlinked plan would reintroduce that conflict.

This is execution policy, not an AI decision to sell all shares.

### Live evidence from November 11

- The history records **41 REAL closes**, including **8 labelled `TP final`**.
- All eight final-target closes match exact Schwab order IDs: the broker
  reports filled **LIMIT** orders for the full recorded position quantity.
  Examples are NVDA 5 shares at 10:38:10 NY, META 3 shares at 13:05:11 NY,
  and PYPL 3 shares at 13:31:31 NY.
- The REAL audit contains **zero TP1 events** for that session.
- At 10:28 NY on November 12, Schwab and GALA agreed on an NVDA long of 5
  shares and a PYPL short of 3 shares; each local plan had a planned partial
  quantity of zero. REAL entries and the daily risk gate were not blocked at
  that snapshot. Subsequent reads showed those trades close and another ticker
  open; these are time-specific observations.
- The broker returned 117 orders for the November 11 session and 375 in the
  broader 59-day verification window at review time. Neither response reached
  the 500-order truncation guard.

Private evidence is retained under
`/opt/gala/backups/upgrade-3.3.26-review-s1e3diy3/`, with restricted permissions.

## What changes

`LINKED_70_30` uses two native OCO allocations after a confirmed entry fill:

| Position | TP1 allocation | Final allocation |
| --- | --- | --- |
| 10 shares | LIMIT 7 + STOP 7, linked OCO | LIMIT 3 + STOP 3, linked OCO |
| 5 shares | LIMIT 4 + STOP 4, linked OCO | LIMIT 1 + STOP 1, linked OCO |
| 3 shares | LIMIT 2 + STOP 2, linked OCO | LIMIT 1 + STOP 1, linked OCO |

Each pair shares one allocation; the two allocations sum to the actual entry
quantity. Integer rounding follows the existing share-plan calculation. Sizes
too small for the existing partial-plan minimum retain full-position exits.

- Both profit targets and the initial stops are submitted to Schwab. A target
  fill is counted only from verified execution quantities and prices.
- After TP1, the runner's OCO can be replaced with its configured breakeven stop
  while preserving its final target. Only unfilled shares are replaced, after
  confirmed cancellation and fresh position reconciliation.
- A fresh position/receipt check precedes every allocation submission. A
  manual close, conflicting working order, missing receipt, or stop fill during
  setup prevents another allocation from being submitted blindly.
- Accepted order IDs and ambiguous submission attempts are persisted before
  further processing. An ambiguous POST is not repeated automatically.
- Existing signal reversal and manual-close paths cancel and reconcile all
  native parents/children before closing the remaining actual quantity.
- Native execution evidence is included in close accounting exactly once;
  manual trading remains excluded from GALA performance and training.
- The monitor reuses its shared order and position snapshots during normal
  operation, with fresh confirmation at cancellation/fill discrepancies.

A stop loss, opposite-signal close, manual close, session close, or emergency
close can still exit the entire remaining position before TP1. A 70/30 plan
does not override those exit conditions. Native orders operate at the broker;
moving the runner stop and recording accounting require the engine to run.
Full recovery/adoption of a live native position after a process restart is not
claimed by this release. Deployment and rollback require a broker-flat window.

## Main code locations

- `core/exit_policy.py:15`: the previous `FULL_POSITION` override.
- `core/native_exits.py:180`: durable installation of the two allocations.
- `core/native_exits.py:196`: fresh exposure check between submissions.
- `core/native_exits.py:275`: confirmed cancellation and remaining-share repair.
- `api/hybrid_order_executor.py:2240`: serialized native-order controller.
- `engine/bot_engine.py:3598`: native plan registration after entry confirmation.
- `engine/bot_engine.py:4979`: verified TP1 accounting.
- `engine/bot_engine.py:5967`: native exit monitoring and runner protection.
- `scripts/enable_linked_profit_targets.py:19`: configuration migration.

## Verification

- 43 focused tests passed locally, covering both order directions, integer
  allocation, partial fills, cancellation/fill races, missing prices, ambiguous
  submissions, manual closes between submissions, breakeven, actual engine
  accounting, existing REAL guards, and PAPER behavior.
- The initial candidate passed all **710 Linux regression tests**, with external
  networking disabled: [GitHub run](https://github.com/ImpactEngineer87/GALA-Trading-Bot/actions/runs/43239038921).
- The final candidate adds four exposure-race tests and explicitly uses Bash
  pipeline failure propagation in CI. All **714 Linux regression tests passed**:
  [final GitHub run](https://github.com/ImpactEngineer87/GALA-Trading-Bot/actions/runs/43240474202),
  source `4ba34fa4c43affa59174bd515bf6d4c27da9359f`.
- All **714 tests also passed on the VPS**, in the isolated checkout
  `/tmp/gala-3326-tests-1vsu3la2/`, running as `gala` with external networking
  disabled. The active application was not restarted for this verification.
- A full Windows run exposed existing POSIX-only locking/mode tests and a
  Windows file-replacement fixture limitation. Linux is the production gate.
- The deployment readiness check correctly refused with `ENGINE_STILL_RUNNING`
  before any source, configuration, or service changes.
- No artificial REAL test order has been placed. Live Schwab acceptance of the
  two-allocation plan remains to be verified after deployment.

## Deployment

The migration script defaults to a dry run. Applying it requires the service
already stopped, root maintenance access, and a fresh strict broker check of
zero positions and zero pending orders. It creates a private backup and proves
that the only parsed configuration change is
`live_trading.real_exit_policy: LINKED_70_30`.

Wait for the engine to stop normally and for REAL positions and pending orders
to clear before maintenance. Back up protected state, deploy the tested source,
run the guarded migration, and verify the service/version and preservation of
models, history, routing, risk limits, and quantities. Do not start the engine
or submit test orders as a deployment check.

Rollback likewise requires a broker-flat window and restoration of the source
and its matching previous exit policy. Do not downgrade over live native exits.

### Scheduled maintenance

- Timer: `gala-upgrade-3326.timer`, verified active, next run
  `2026-11-12 20:50:00 UTC`.
- Service: `gala-upgrade-3326.service`.
- Private deployment helper: incident folder's `deploy_when_stopped.py`.
- Helper SHA-256:
  `f01749418074e6782ca6698e985bd9f9131b407a68685f5124fe66119718e598`.
- A changed source baseline, dirty source, running engine, local positions,
  unknown broker state, non-flat broker account, truncated order response, or
  changed exit-policy baseline prevents deployment.
- The helper checks again immediately before stopping the web service. It
  backs up source/configuration/runtime state, applies only the exit-policy
  configuration change, restarts the web service, and checks version, stopped
  engine, protected hashes, routing, risk state, and fresh broker-flat evidence.
- The helper does not stop/start the trading engine, place/cancel broker
  orders, or force-close positions. Normal existing session-end behavior
  remains responsible for ending trading.
- Successful completion writes
  `data/upgrade_verification_20261112_3326.json`. Failure writes a private
  `deployment_pending.json`; a scheduled timer alone does not prove success.
- To cancel the pending attempt: `systemctl disable --now gala-upgrade-3326.timer`.
