# Schwab request handling — October 6, 2026

## Evidence and limits

The supplied console screenshot shows repeated order-list failures within a
few seconds: the actual HTTP response is **400**, while its JSON error contains
`status: 500`. These are broker HTTP failures. The excerpt does not show a 429
or establish why the original Akamai denial occurred.

The other supplied log excerpt contains `COOLDOWN_ACTIVE`/`SchwabRequestPaused`
messages. Those attempts were refused locally, without a network request; they
cannot be counted as HTTP calls. Old logs do not record every successful send,
so an exact historical calls-per-minute count cannot be reconstructed.

Previously, `order_sync.poll_interval_ms: 100` also controlled the perpetual
background order monitor. A failed individual-order read immediately fell back
to an order listing; both could repeat on subsequent monitor iterations. The
shared gate enforced a 403 cooldown but had no general request budget, ordinary
429 handling, or central redacted HTTP failure file.

The user mentioned a limit of 120. Its official scope has not been verified:
the application defaults below are conservative engineering settings, not a
claim that Schwab permits 120 calls/minute for all APIs. Ask the application
owner to confirm the current quota and scope in Schwab's developer resources.

## Changes for review

- Shared, locked admission counts **outbound HTTP dispatch attempts**, including
  retry/fallback sends. Default: at most 90 in a rolling 60-second window and
  six in a rolling second. Background reads are limited to 60/minute and four
  per second, reserving capacity for explicit order mutations, OAuth and bounded
  acknowledgement/reconciliation reads. Control traffic still obeys the total
  cap. There is no unbounded waiting or queued/replayed mutation.
- Background monitoring defaults to 2 seconds, independently of the existing
  short acknowledgement polling. `order_sync.monitor_interval_ms` can override
  it, with a minimum of 1000. Short acknowledgement deadlines remain unchanged.
- Order read HTTP 400/5xx/network failures trigger shared per-account read
  backoff of 5, 10, 20, 40, then 60 seconds. One explicit listing fallback per
  failure streak preserves recovery from an unreliable individual-order
  endpoint. It cannot bypass the total budget or 403/429 cooldown. A successful
  read clears that account's error streak. 401/403/429 never fan out to a listing.
- Ordinary 429 responses pause globally for at least 60 seconds or the larger
  numeric/HTTP-date `Retry-After`. A 429 during recovery from a 403 preserves the
  10-minute minimum. Pauses survive restart; only one safe recovery probe is
  allowed, and mutations cannot serve as that probe.
- Reconciliation stops its immediate retries when it receives a known local
  pause/admission refusal. Unknown order/position reads remain unknown; malformed
  WORKING-order responses cannot become an authoritative empty list.
- API redirects are disabled in the shared caller to prevent unbudgeted hops.
- `logs/schwab_http_errors.jsonl` rotates at 5 MiB with two backups. Records
  include UTC time, redacted endpoint template, method, actual HTTP status,
  numeric broker error statuses, validated UUID correlation IDs and an Akamai
  Reference ID when present. No request headers, tokens, raw bodies, query
  strings, account IDs or order IDs are included.
- `GET /api/schwab/request-metrics` only reads local state. It does not refresh
  tokens or probe Schwab. It returns rolling send counts, safe endpoint counts,
  response counts, locally deferred counts and configured budgets. Existing
  `request_pause` snapshots include the same metrics and pause reason.

Counters are per process, since its start; they are not a historical account or
network-packet audit. Transport/DNS failures count as dispatch attempts even if
no HTTP response arrives. Response counts use completion time. The rolling
budget and counters reset on restart; the 403/429 pause does not. Multiple bot
processes/hosts using the same app do not share the minute budget. Run one bot
owner; frequent restarts are not a way to bypass admission. Other clients and
manual curl requests are outside these counters.

Heavy background traffic can still use its share of the budget and defer data
or order observations. The patch cannot guarantee a particular close time or
that every symbol is ready. REAL decisions must continue to fail closed on
unknown broker state or untrusted data. Do not raise quotas or shorten guards
to make a live trade pass; use measured traffic to review batching/caching next.

## Other client complaints

- A green OAuth/quote check does not prove successful order reads, cancellations
  or fills. Yellow may reflect a shared pause or a transient failure; it does
  not by itself prove invalid credentials.
- The supplied configuration has no one-trade-per-day rule. Fixed quantity one
  means one share; entry models, hard vetoes, broker readiness, existing
  exposure, strategy cooldown and per-ticker execution mode affect eligibility.
  Model requirements remain enabled for REAL even when training is disabled.
- Five REAL and twelve PAPER selections explain different execution modes;
  identifying why a particular symbol was blocked requires its decision log.
- Stopping the engine is not evidence that Schwab is flat. The earlier merged
  exit-control change preserves unresolved exposure and reports failed closes.
  This patch retains those guards.
- The original VPS later returned a credential-free JSON 401 instead of an
  Akamai HTML 403. That demonstrated access to that tested endpoint at that
  time; it did not prove credentials valid, an IP was the root cause, or the
  issue permanently resolved.

## Offline validation

- 28 new stdlib regression scenarios execute production transport definitions
  and selected application methods with simulated responses and clocks.
- 26 previous REAL-exit/stop/broker-read regression scenarios pass.
- Eight previous dashboard-control VM scenarios pass.
- Changed Python files compile; `git diff --check` passes.

The environment lacks the installed Flask/ML/requests dependency stack. The
offline harness replaces requests exception types and extracts actual method
bodies with AST; the full integration/pytest suite has **not** run. No broker
traffic, credentials, real orders or VPS deployment were used. VERSION remains
3.3.14; identify the installed source by its Git commit, not only that label.

## VPS rollout and evidence

1. The client checks Schwab positions **and** pending orders directly. Keep
   REAL disabled until exposure and cancellation outcomes are understood.
2. Back up `.env`, local config and runtime `data` outside the deployment copy.
   Do not overwrite or delete tokens, reconciliation records, ticker modes,
   pending execution files or the cooldown file during an update. Apply code
   changes to a staging copy first; retain a code rollback.
3. Review the PR and run its offline checks plus the installed application's
   full pytest suite. Use one active session owner/process on one VPS. Stop the
   old service supervisor so it cannot restart a second copy during migration.
4. Start the dashboard with the trading engine stopped. After any required
   cooldown, perform one intentional connection/read verification; confirm
   positions/orders can be read. Do not repeatedly click verification or run
   curl probes during a pause. The local metrics endpoint is safe to inspect
   repeatedly and does not generate broker traffic.
5. Inspect the new redacted log and metrics before enabling REAL. Verify the
   deployed Git commit and capture UTC time, actual HTTP status, safe endpoint,
   correlation/Reference IDs and counts. Send that evidence to Schwab if the
   original HTTP failure persists. A successful quote alone is insufficient.
6. Begin validation with PAPER. Any REAL restart remains an explicit operator
   action after broker checks; this change does not activate it automatically.

The app owner must rotate the client secret exposed in the chat through their
Schwab developer account and update it securely on the chosen VPS. Do not send
credentials, token files or full account responses to support.
