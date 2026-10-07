# One REAL position at a time — v3.3.18

## Evidence

On October 7 (v3.3.17, 5 REAL + 12 PAPER) the dashboard showed only one REAL
trade open at a time. "Últimos rechazos" listed BA, META and NVDA with
`Persisten órdenes activas del símbolo`.

The VPS log shows the real reason. Every blocked REAL entry that day — NVDA
11:58, META 12:23, BA 12:35, META 12:54 and 13:01, NKE 13:09, 13:40 and
14:32, NVDA 14:07, META 14:11 — failed with:

```
[META] No se pudo confirmar las órdenes del broker (SchwabRequestDeferred: order_read_backoff; espere 33s)
```

No line reports cancelled or leftover orders. Schwab had been confirmed flat
with no working orders at 11:42. `logs/schwab_http_errors.jsonl` shows
`GET /trader/v1/accounts/{account}/orders/{order}` returning HTTP 400 (broker
status 500) once about every 61 seconds from 18:10 to 18:37 UTC. It also
shows two `DELETE` order requests returning HTTP 400 at 18:32 UTC.

## Root cause

- While a REAL position is open, the order monitor checks its TP1 and STOP
  order every 2 seconds through Schwab's single-order endpoint. On this
  account that endpoint failed on every call.
- Since 3.3.15, a failed order read paused **all** order reads for the
  account, the order list included. That pause allowed one list fallback per
  failure streak.
- If that one fallback was refused by the local limiter (budget or burst
  busy), the streak never ended. The pause grew to 60 seconds, and each
  minute one more single-order attempt failed and renewed it. The 61-second
  spacing in the error log is this loop.
- Every REAL entry first reads the account's working-order list to make sure
  nothing is left for that symbol. That read was refused (`order_read_backoff`),
  so the entry was blocked, but recorded under the misleading reason
  "Persisten órdenes activas del símbolo". When the open position closed,
  polling stopped and the pause expired, so the next REAL ticker could enter.
- The monitor itself learned order status at most about once a minute.
- Before 3.3.15, each failed single-order read fell back to the list every
  time: two requests per lookup and four every 2 seconds per position. The
  3.3.15 pause was added to stop that traffic, but its shared key and
  one-time fallback caused this lockout.

## Changes

- **Separate pauses** (`api/schwab_transport.py`). Single-order reads and the
  order list each have their own failure pause, still 5 s doubling to 60 s.
  A broken single-order endpoint no longer pauses the list that entry checks,
  reconciliation and order confirmations depend on. The one-time
  list-fallback allowance is removed because it is no longer needed.
- **Lookups that work while single-order reads are paused**
  (`api/hybrid_order_executor.py`). Instead of returning "unknown", a lookup
  reads the account order list (last two days, any status), the same
  workaround the code used before 3.3.15.
- **One list read shared by the monitor.** The open-position monitor reuses
  that list for up to `order_sync.monitor_status_max_age_ms` (default 5000,
  never less than the 2 s monitor interval). That covers both exit orders of
  every open position, and threads polling at once share one read. An order
  missing from a cached list (for example one just placed) triggers a fresh
  read. Confirmations, cancellations and every other caller always read
  fresh.
- **Room for the monitor during warmup** (`engine/bot_engine.py`). While REAL
  positions are open, a cold history download (which uses every slot it can)
  leaves room for the monitor's list reads: 8 + 60 / max age + 2 slots per
  minute instead of 8. Routine macro refreshes are unchanged.
- **Honest rejection reason.** A blocked entry now records
  `BROKER_ORDERS_UNREADABLE` with "No se pudieron leer las órdenes de Schwab
  (…)" when the order list could not be read. `BROKER_SYMBOL_NOT_CLEAN` with
  "Persisten N orden(es) activa(s) del símbolo tras cancelar" is reserved for
  real leftover orders.
- **Not changed:** request limits, HTTP 403/429 pauses, the order placement
  and cancellation rules, entry safety checks (the working-order list must
  still be read and must show nothing for the symbol), and position
  reconciliation.

## Offline verification

**Lockout, step by step.** The single-order read fails and the one fallback is
refused by the burst limit. Over the next 5 minutes, with one failed
single-order read each minute:

| | order-list reads refused |
|---|---|
| v3.3.17 | 10 of 10 (`order_read_backoff`) |
| v3.3.18 | 0 of 10 |

**10-minute scenario.** One REAL position (TP1 + STOP polled every 2 s), the
single-order endpoint always returning 400/500, and another REAL ticker's
pre-entry list read every 20 s:

| | monitor status unknown | single-order requests | list requests |
|---|---|---|---|
| v3.3.17 | 300 of 600 | 300 | 300 |
| v3.3.18 | 0 of 600 | 13 | 100 |

The new tests are in `tests/test_v3_3_18_order_read_backoff.py`. They cover:
- separate pauses, and a refused list read leaving no lasting pause
- list lookups while single-order reads are paused
- shared and fresh reads, new orders, concurrent monitor threads, and a list
  failure staying unknown
- the warmup room for the monitor
- both rejection reasons
- the monitor passing the shared list age

Two existing fixtures were extended for the new helpers. Their assertions are
unchanged.

```bash
python -m unittest discover -s tests -p test_v3_3_18_order_read_backoff.py
python -m unittest discover -s tests -p test_schwab_request_budget_offline.py
python -m unittest discover -s tests -p test_v3_3_17_warmup_admission.py
python -m unittest discover -s tests -p test_v3_3_16_market_data_recovery.py
python -m unittest discover -s tests -p test_v3_3_15_exit_control.py
```

On Windows, set `PYTHONUTF8=1`.

The full suite was also run (`pytest tests`, Python 3.13, excluding three
modules that cannot be collected because their `MIGRATE_CONFIG_V2_8/V2_9`
scripts are not in the repository). This branch passes 509 tests; `main`
(3.3.17) passes 495. The same 31 tests fail on both, so those failures
predate this change. No live broker, credentials, VPS or real orders were
used.

## VPS verification after review

1. Check Schwab positions and working orders before stopping or restarting.
2. Deploy `VERSION` and the source together, restart the backend, and confirm
   3.3.18.
3. With one REAL position open, a signal on another REAL ticker should now
   reach the broker steps. This check is safe to repeat and should return
   nothing for that day:
   ```powershell
   Select-String -Path logs\chocoletra.log -Encoding UTF8 -Pattern "order_read_backoff" | Where-Object { $_.Line -like "2026-10-08*" }
   ```
   `logs/schwab_http_errors.jsonl` will still show the single-order 400s about
   once a minute while a position is open. That endpoint is broken on
   Schwab's side and is retried only as often as the pause allows.
4. Other limits still apply and can legitimately stop a REAL entry: AI and
   strategy filters, the REAL capacity check (combined stop risk against the
   remaining daily loss margin), and real leftover orders.

## Follow-ups (not in this patch)

- Report the single-order endpoint failures to Schwab Trader API support with
  the UTC times and correlation IDs from `schwab_http_errors.jsonl`.
- The two `DELETE` order requests that returned HTTP 400 at 18:32 UTC are
  outside this change. The cancellation path already confirms such orders by
  their terminal state or by absence from the working list.
