# REAL staged profit targets — 3.3.26

## Status

The upgrade is implemented, tested, and published on GitHub `master`.
The source changes are in commits `c595aef` and `4ba34fa`; the latter passed the
final Linux regression run. VPS deployment is pending SSH access.

On November 12 this workspace can establish TCP to `204.168.175.19:22`,
but SSH times out waiting for the server identification banner, before
authentication. The dashboard also times out from this workspace. The user
reports that the dashboard opens from their connection. This does not establish
whether the cause is a provider firewall, fail2ban, routing, or the SSH service.
This workspace's public IP was `172.111.36.128` when checked.

No VPS files, configuration, orders, or engine state have been changed for this
upgrade. November 11 live order receipts and current broker exposure have not
been inspected. The diagnosis below comes from repository code and the last
verified deployment, rather than a fresh inspection of yesterday's orders.

## Why REAL sold the whole position

The last verified VPS configuration used
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
- A full Windows run exposed existing POSIX-only locking/mode tests and a
  Windows file-replacement fixture limitation. Linux is the production gate.
- No artificial REAL test order has been placed. Live Schwab acceptance of the
  two-allocation plan remains to be verified after deployment.

## Deployment

The migration script defaults to a dry run. Applying it requires the service
already stopped, root maintenance access, and a fresh strict broker check of
zero positions and zero pending orders. It creates a private backup and proves
that the only parsed configuration change is
`live_trading.real_exit_policy: LINKED_70_30`.

Wait for REAL positions to close normally before maintenance; do not use Stop
to force-close positions for deployment. Verify current live configuration and
yesterday's receipts when access returns, back up protected state, deploy the
tested source, run the guarded migration, and verify the service/version and
preservation of models, history, routing, risk limits, and quantities. Do not
start the engine or submit test orders as a deployment check.

Rollback likewise requires a broker-flat window and restoration of the source
and its matching previous exit policy. Do not downgrade over live native exits.

### Restoring maintenance access

Use the VPS provider console to check the SSH service, provider firewall, host
firewall, and fail2ban for source IP `172.111.36.128`. The SSH target is already
confirmed as `root@204.168.175.19`; resending credentials cannot resolve a timeout
that occurs before authentication. A firewall block has not been established
as the cause.
