# 3.3.20: GALA10 signal and individual model threshold

## Confirmed incident and code provenance

The saved TSLA signal at **2026-11-05 11:43:24 New York / 20:43:24 Saratov**
was a GALA10 SELL at 349.16. Its unchanged production model returned
**0.8023396134376526**, above its **0.55** threshold. The saved decision was
`AI_VETO:MACRO_NEUTRAL`, with a policy score capped to 0.30. Five other recorded
TSLA signals on November 5 were also rejected for MACRO_NEUTRAL despite their
raw model scores exceeding 0.55 at the time of this investigation.

The source is `agents/orchestrator.py` at baseline commit `8dbc7ee`:

| Baseline lines | Behavior |
| --- | --- |
| 74–85 | Load enabled macro/range rules and the 0.30 probability cap. |
| 344–374 | Turn neutral macro context / lateral range into a hard veto. |
| 395–405 | Cap the shadow score and replace its model approval. |
| 445–463 | After model approval, cap the production score and reject entry. |
| 465–482 | An enforced market-regime policy can additionally reject entry. |

`git blame` and the complete repository history trace these blocks to the
initial import, **0ea221cd44bc7895acbe3c2a7d359b482d510eab**, recorded
**2026-09-30 04:17:31 Saratov / 00:17:31 UTC**. No later commit modifies this
orchestrator before the present fix. The available history cannot establish
who originally wrote these rules or their earlier introduction date. Their
comments claimed client authorization; that claim is not proof of agreement.
They were not introduced by the 3.3.19 reconciliation fix.

A passing model could previously trade when these context conditions were
absent and be rejected when the macro became neutral. This explains how the
same binary could behave differently between signals or sessions.

## Exact change

Entry-policy implementation commit: `56c05c3`.

| File and current entry point | Change |
| --- | --- |
| `agents/orchestrator.py:281` | Stamp the GALA10 model-threshold entry policy. |
| `agents/orchestrator.py:319` | Regime analytics are observe/off, including legacy enforce configuration. |
| `agents/orchestrator.py:369` | Record macro/range observations separately from operational veto fields. |
| `agents/orchestrator.py:407` | Retain model rejection; remove subsequent macro/range/regime rejection branches and probability caps. |
| `agents/contracts.py:259` | Carry the entry policy and observational context in the decision receipt. |
| `engine/bot_engine.py:1888` | Persist that receipt beside the original GALA10 direction and exact model score. |
| `engine/bot_engine.py:2125` | Preserve the actual rejection reason in the veto audit. |
| `engine/bot_engine.py:3865` | Explain approved decisions with their actual entry policy. |
| `dashboard/index.html:1861` | Distinguish new observation-only context from original historical rejection rules. |

Before, after the model had approved:

```python
if decision.hard_veto and production_veto_enabled:
    decision.prob_meta = min(decision.prob_meta, 0.30)
    decision.operar = False
    return decision
```

After, for GALA10:

```python
decision.context_filter_reasons = "|".join(hard_veto_reasons)
prob, aprueba = self.entrenador.evaluar(decision, requerir_modelo=True)
decision.prob_meta = prob
if not aprueba:
    decision.operar = False
    # Record AI_THRESHOLD or AI_MODEL_UNAVAILABLE.
    return decision
# Macro/range/regime observations cannot overturn model approval.
```

The snippet abbreviates the REAL path; PAPER retains its existing collection
and shadow-model behavior. The individual loaded model, its configured threshold
and existing explicit manual authorization remain the AI decision authority.
Macro/range data still supplies the original model features. Passing approval
advances to the existing execution pipeline, which checks exposure, account
state, daily loss, buying power, valid order sizing, fresh data, and broker
readiness before an order attempt. Existing positions, same-side signals,
pending entries, and unresolved closes retain their established lifecycle rules.

Historical signals remain evidence of their original decisions. New decisions
carry `entry_policy=GALA10_MODEL_THRESHOLD_V1` and observational context. The
upgrade does not retroactively submit old missed signals or turn approvals
into claimed broker fills. LIMIT fills depend on the broker market; repricing
and the entry timeout are separate execution behavior.

## Verification

- 27 focused tests passed locally, covering actual orchestrator inference,
  engine entry attempts, execution receipts, dashboard rendering and LIMIT
  execution controls.
- Before the additional target fix, the **586-test full suite passed** in the isolated Linux worktree as `gala` with network
  disabled. Added cases cover BUY/SELL in REAL/PAPER, simultaneous macro/range/
  regime conditions, exact threshold equality, below-threshold rejection,
  unavailable models, shadow score preservation, missing data and trading hours.
- A network-disabled replay used the exact saved TSLA feature vector and the
  unchanged production artifact whose SHA256 matches the original signal's
  model identity. Both versions reproduced the original probability exactly,
  0.8023396134376526. The baseline rejected it as MACRO_NEUTRAL and capped its
  score to 0.30; the candidate approved with decision score 0.802 and no rejection
  reason. No broker requests or orders were made by the replay. This verifies
  entry eligibility, not a retrospective execution or fill.

Review evidence: `/opt/gala/backups/entry-policy-review-acoyrzy2`.

## Additional defect found while waiting for BA's normal close

At 21:43:35 Saratov / 12:43:35 New York, Schwab still reported BA SELL 5 shares
with its protective STOP working at 172.65. The engine's snapshot showed entry
171.58, target 170.04, and latest observed price 169.895. The REAL manager's
target branch at baseline `engine/bot_engine.py:5621` required `tp1_hit`.
The full-position exit policy disables partial exits, so that flag never becomes
true. This skips the configured target even when the observed price reaches it.
The existing protective STOP and opposite-signal close paths remain available.

The guard is already present in initial import `0ea221c`. Full-position REAL
exit policy was added in `003180c` on October 30 at 21:34:10 Saratov. This is an
incompatibility between the full-position mode and the older partial-only target
check. It is distinct from the macro entry veto.

Fix commit **`b223648`**, `engine/bot_engine.py:5625`: evaluate the existing target
when `tp1_hit` is true **or** partial exits are disabled. Reuse the existing
verified LIMIT close path. The tests cover long/short targets, exact equality,
prices before target, partial plans before/after TP1, and a stopped engine.
During review, the existing engine continued managing the trade while waiting
for a flat restart window. The source edit did not close the live BA trade.

The final combined candidate passed **590 tests** on Linux in the isolated
worktree with network disabled. Fresh broker reads at **21:48:28 Saratov /
12:48:28 New York** showed no positions and no working orders. The operator had
explicitly selected waiting for BA to close normally before deployment.

## Completed deployment

The client selected waiting for BA's normal close. The existing engine closed
that 5-share short through its reversal path at **21:47:38 Saratov / 12:47:38
New York**, entry 171.58, exit 170.4799, gross profit 5.5005 USD. Its linked audit
and history are accounting-verified. The update had not yet been loaded then.

The maintenance helper independently confirmed broker flat/no pending orders
before stopping the engine. It waited for the runner to terminate before the
web service stopped. Ten PAPER positions closed by that maintenance stop were
explicitly excluded from training and performance, preserving their original
prices and outcomes. Other observations retained their existing eligibility.

Authenticated post-deployment checks at **21:54:42 Saratov / 12:54:42 New York**
confirmed:

- Version **3.3.20**, active web service, engine off and no runner thread.
- An independently verified empty broker account position list and no pending
  orders, including the unfiltered account/order verification.
- REAL `entry_blocked=false`; empty pending manual-close queue.
- All **47 protected configuration, routing, production/shadow model and
  original accounting guard file hashes unchanged**.
- REAL recorded gross realized risk 88.07 USD; REAL daily loss limit 200 USD.
  The protected configuration retains the PAPER daily loss limit of 700 USD.
- Clean deployed Git tree at the reviewed code and documentation commit.

Maintenance backup: `/opt/gala/backups/upgrade-3.3.20-d4m1xpz6`.
Verification receipt: `data/upgrade_verification_20261105_3320.json`.
Replay receipt: `data/entry_policy_replay_20261105.json`.

GitHub and the VPS contain both code fixes. The trading engine was left off
for the client's dashboard start. The saved-signal replay and offline tests
demonstrate the corrected approval/target paths; a new post-upgrade live order
or fill has not been used as a deployment test.
