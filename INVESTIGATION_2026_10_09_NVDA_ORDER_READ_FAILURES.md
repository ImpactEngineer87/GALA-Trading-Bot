# NVDA order-read failures and queued-exit safety — v3.3.21

Reviewed deployed main `537eea54ba27013b9fc084880004328b9a508ff1`
(v3.3.20; PR #5 merged at 09:35:41 UTC on October 9).

## What the supplied logs establish

- From 09:49–09:57 UTC, both individual-order and unfiltered recent-order
  GETs repeatedly returned HTTP 400 with a JSON error status of 500.
  These are order-read failures, not evidence of another Akamai HTML 403,
  an HTTP 429, or a confirmed API-quota overrun. The supplied error-log tail
  does not measure all traffic from the account or process.
- At 09:57:44.228 UTC, the NVDA audit records `EXIT_SUBMITTED`. The following
  records do not confirm a fill, cancellation or flat position. A submission
  is not a completed liquidation.
- At 09:58:20.401 UTC, the bot retained its exit journal because its additional
  ticker WORKING-order snapshot was unavailable. The supplied HTTP tail does
  not establish why that snapshot or the earlier position read was unavailable.
- 09:57 UTC is 05:57 New York time on this date. SmartExit's emergency order
  is MARKET / NORMAL / DAY. A regular-session order cannot execute during
  premarket; Schwab must confirm its actual status. QUEUED is a possibility,
  not a fact established by these logs.

Schwab's session table states that regular-session orders are valid for
execution from 09:30–16:00 ET and extended-hours sessions accept limit orders:
https://www.schwab.com/node/41941

The HTTP 400 / JSON 500 combination alone cannot distinguish a bad request,
account-specific problem or Schwab backend failure. Both failing endpoints
must be investigated; changing the list date window alone is not a proven fix
for the independently failing individual-order endpoint.

## Confirmed source defect and correction

Previously, `orden_trabajando` returned False whenever an order was absent
from a successful status=WORKING list. Cancellation and SmartExit interpreted
that as permission to reconcile and potentially replace an old close. A queued,
conditional or pending-activation order can still execute without appearing
in that filtered list. No duplicate order in this client's incident has been
established; the unsafe inference is independently reproducible in source.

v3.3.21 changes the proof used by those callers:

1. WORKING presence stays a fast path with no extra detail request. Absence
   requires an individual/list order read. A nonterminal status means the
   order may still execute; unreadable, missing or unknown status stays unknown.
   Only a definite terminal broker status returns False. Cancellation is no
   longer confirmed from WORKING absence alone.
2. The legacy ticker-order helper now reads unfiltered recent orders and
   retains all nonterminal states, child orders and matching legs. Unreadable,
   capped (500 results) or malformed lists cannot authorize replacement.
   Its existing two-day query window is unchanged; known older journal orders
   additionally require their individual terminal status. This is not an
   account-wide history/pagination implementation for arbitrary external GTC
   orders, nor a promise that an unreadable account can be flattened automatically.
3. A tracked pending REAL close prevents reconciliation from declaring the
   engine flat based only on empty positions and a WORKING-only list. Before
   releasing that pending exposure it also needs a readable unfiltered symbol
   check. Queued orders remain visible and unreadable checks block restart.
   This guard applies to the retained engine's tracked exit context; broker
   review remains necessary when replacing/restarting a process.
4. Exit audit details include session and duration. Pending messages explain
   that submission/fill/cancellation is unconfirmed and NORMAL orders execute
   in regular market hours. An unavailable position read no longer implies
   that a submission necessarily happened before the failure.

Existing global budgets, backoff, control reserve, the v3.3.20 close-only worker
and ambiguity protections remain. No automatic extended-hours order policy,
API-limit increase, credential reset, configuration change or journal deletion
is part of this patch. Reads can remain blocked longer when order terminality
cannot be verified; the operator must resolve that uncertainty with Schwab.

## Verification

192 focused unittest regressions passed, including 16 new tests loading actual
executor, SmartExit, engine reconciliation and request-gate code with fake I/O.
Coverage includes queued/conditional/pending orders absent from WORKING,
HTTP 400 / JSON 500 and backoff, durable restart without another POST,
terminal and unknown cancellation, child/multi-leg orders, capped/malformed
responses, and pending-exit flatness proof. Existing long/short, partial fill,
cancel/fill race, cooldown, shutdown and traffic-budget tests remain passing.

Nine existing kill-switch test functions also passed using the same offline
production-code harness. They were not run through pytest: this workspace
lacks pytest, requests, Flask and python-dotenv. Compilation and
`git diff --check` passed. No live orders or VPS changes were performed.

Run the focused suites:

```bash
python -m unittest discover -s tests -p test_v3_3_15_exit_control.py
python -m unittest discover -s tests -p test_v3_3_16_market_data_recovery.py
python -m unittest discover -s tests -p test_v3_3_17_warmup_admission.py
python -m unittest discover -s tests -p test_v3_3_18_order_read_backoff.py
python -m unittest discover -s tests -p test_v3_3_19_reliable_real_exits.py
python -m unittest discover -s tests -p test_schwab_request_budget_offline.py
python -m unittest discover -s tests -p test_v3_3_20_pending_exit_recovery.py
python -m unittest discover -s tests -p test_v3_3_21_queued_exit_safety.py
```

## Immediate operator recovery

1. Keep strategy entries stopped. Have the account owner inspect NVDA's
   actual position and **all existing NVDA orders**, including queued and
   conditional orders, directly in Schwab. Do not repeatedly click Retry or
   place another close while the submitted order's status is unknown.
2. If an existing close is pending, decide with Schwab whether to retain it
   or cancel it. Confirm cancellation/remaining exposure before a replacement
   or manual close so both orders cannot execute against the same shares.
   Stopping the bot process does not cancel an accepted broker order.
3. Preserve config, OAuth state, execution journals and reconciliation data.
   This patch does not resolve the client's open position by itself. After
   reviewing/resolving live orders, deploy from the reviewed PR, verify the
   running version, and use read-only reconciliation before allowing entries.
   Do not erase `execution_pending.json` to force an apparently green state.
4. If order reads still fail, the account owner should reply to Trader API
   support with endpoint, HTTP 400 / JSON status 500, UTC times and correlation
   IDs. Example failures at 09:57:12 UTC:
   - Individual order: `cf35f7f0-f3bb-339e-46d7-9397e99887f4`
   - Unfiltered list: `91e2bb44-4339-7a95-271c-f3a53562e739`
   Ask support to investigate the account's order-read errors, separately from
   the earlier Akamai incident. Never send tokens, secrets or personal account
   information by email.
