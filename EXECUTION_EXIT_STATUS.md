# Protected normal LIMIT closes — v3.3.14

## Confirmed October 23 evidence

The operator's fresh read-only Schwab audit confirms actual LIMIT SELL_SHORT
fills for BA and NKE. NKE's final close was MARKET BUY_TO_COVER, with gross
fill-derived P/L +$3.55, before fees. This is not Schwab/TOS account P/L.
BA's closing ID and META's entry/exit IDs remain unresolved. BA's 10:17:36 NY
SELL was blocked by REAL_ACCOUNTING_UNRESOLVED. No historical IDs, prices,
performance eligibility, training counters or production models are repaired
or reset by this release. Current settings alone do not prove the historical
routing path of a particular order.

## New ordinary close policy

All ordinary REAL closes use SmartExit LIMIT orders, including when legacy
Execution V2 is disabled. They cannot fall through to MARKET, even with the
old market_fallback flag true. SELL closes a long; BUY_TO_COVER closes a short.
A recent non-delayed bid/ask is required. The existing maximum quote age
(default 5 seconds) and price buffer (default 5 basis points) remain in force.
These are marketable LIMITs based on a fresh quote, not promises of execution
at the signal price, zero slippage, or guaranteed execution.

The engine checks the quote before canceling protective orders. If that check
fails, the existing STOP and TP are retained. SmartExit checks again before
each submission. A LIMIT waits normal_limit_wait_sec (default 10 seconds),
then requests cancellation and checks terminal broker details. At most one
ordinary LIMIT is submitted per call. An unknown submission, missing accepted
order ID, unknown order state or unconfirmed cancellation prevents duplication.

After a confirmed terminal LIMIT leaves exposure, SmartExit requires a fresh
matching position and an empty WORKING-order snapshot before permitting a
replacement STOP. The engine independently rechecks both, restores one STOP
at the existing protective level for the broker-confirmed remaining shares,
and tracks accepted-but-not-yet-visible STOP IDs without duplicating them.
A failed or ambiguous restoration raises an accounting blocker; the existing
monitor reconciles it and retains the emergency unprotected-position path.

The monitor makes up to three ordinary attempts, separated by at least ten
seconds and requiring a reconciled protective STOP between attempts. Each
attempt uses a fresh quote. After three unsuccessful attempts, the position
remains tracked and protected, with an operator warning. A new explicit exit
request can retry; the ordinary retry budget never becomes MARKET.

## Partial fills and evidence

Every exact exit leg remains in the durable pending journal and final close
audit. Partial LIMIT fills do not fabricate TP1 execution or close the local
position. Their broker-confirmed gross P/L and remaining exposure contribute
to the daily risk calculation, even when unrealized valuation is disabled.
The full exit is booked once after complete broker fill proof; no completed
AI-learning observation is created while shares remain open.

A partial normal exit creates its own durable NORMAL_EXIT_PROGRESS block.
Only the same position/exit intent with a complete, exact, consistent fill-leg
set and a persisted final daily P/L can resolve that particular transient key.
Older accounting blocks are preserved. Missing prices, conflicting quantities,
restart identity mismatches, or a protective STOP winning the race without a
complete aggregate exit proof retain quarantine and require review. Being
flat does not manufacture P/L or training eligibility.

STOP, kill-switch, daily-loss and unprotected-position emergency closes keep
the existing emergency MARKET capability. A protective STOP may itself execute
at market after triggering. This release does not change that protection into
a stop-limit order. Reversals require the old position to be confirmed flat.

## Read-only audit and validation

scripts/audit_schwab_execution_prices.py still performs GET-only broker
operations, with existing OAuth refresh persistence. It reports actual matching
broker order types/instructions and distinguishes this source revision's normal
policy from obsolete fallback/timing settings. This metadata does not prove the
running process version. Missing order identities stay unresolved.
Multiple exit legs require all exact IDs for fresh whole-trade reconciliation;
the diagnostic does not infer a whole-trade result from one final partial ID.

Validation: 387 offline regression tests passed, with zero skips, including
27 new tests exercising actual SmartExit and engine orchestration against an
in-memory broker. No live order was sent. Existing manual/automatic AI controls,
SHADOW-only automatic training and production approval gates remain unchanged.
The upgraded code must also pass the full isolated VPS suite before activation.

## Controlled installation

Use /opt/gala/GALA-Trading-Bot. Fetching does not activate this release.
Before stopping gala.service, independently verify engine OFF, scheduler manual,
AI worker idle, and a fresh successful broker check with no positions or working
orders. Arrange the maintenance window with all dashboard operators: stopping
the engine/service can invoke REAL liquidation. Do not infer flatness from an
old audit or closed market. Back up .env, config.yaml and all runtime/model/history
files privately; preserve their ownership and permissions. Fast-forward only,
make public source readable by service user gala, and run the complete suite
as gala under unshare -n before starting the dashboard with the engine OFF.
Never use git reset --hard, git clean, broad chmod/chown, or clear reconciliation
state to force entries. Successful deployment is not approval to resume REAL.
