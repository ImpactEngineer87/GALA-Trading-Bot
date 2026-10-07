# Market-data recovery — v3.3.16

## Evidence

The October 7 screenshot shows v3.3.15, an active engine, a green Schwab
indicator and cycle 1 with 17 monitored tickers at approximately 10:00 New York.
Its warnings include synthetic data being discarded and one-minute prices not
refreshing. This explains why these pipelines do not execute trades. A green
connection/quote check does not verify historical candle availability. The
screenshot contains no actual HTTP 403/429 response and cannot establish a new
IP denial, invalid credentials or the precise current VPS failure.

Source inspection confirmed a recovery regression exposed by the request gate:
concurrent history reads could exceed the local background burst allowance;
`_pedir_pricehistory` converted local admission refusals into missing data;
`obtener_multitemporal` could replace missing sources with demo data; and the
engine cached that synthetic result as a successful macro download. Subsequent
micro updates did not clear its synthetic flag. The trading safeguard correctly
rejected it, but the recovery path could remain stuck until macro cache expiry.
Large repeated paginated downloads also competed with the conservative budget.

## Changes

- Only safe pricehistory GETs may wait up to five seconds for local admission.
  The lock is released while waiting. Rolling/burst caps and control capacity
  reservations stay in force. HTTP 403/429 pauses still refuse immediately;
  mutations are never queued or automatically replayed.
- Successful older minute pages use stable UTC-day request boundaries and a
  bounded 24-hour cache (maximum 512 entries). The recent window is always
  downloaded. Empty/failed responses are not cached. Requested depth is retained
  and may include up to one extra day; each page remains at most ten days.
  Cached historical prices can receive broker corrections after cache expiry.
- Successful source frames survive interrupted warmup, with acquisition-based
  macro TTL and at most 256 entries. Reads do not extend that TTL. A single 30m
  source still supplies 1H and 4H. The 1m source is fetched last, after warmup.
- The engine/provider return empty, non-synthetic `WAITING` data when auth,
  admission or a required history source is unavailable. All seven requested
  frames must be present before provider readiness; previously partial bundles
  could pass. Existing demo helpers remain available for explicit development.
- Pending/synthetic bundles are not stored as successful engine cache entries.
  A failed micro refresh retains good macro data for recovery but does not
  publish old micro data to that cycle's price/strategy/risk pipeline. Cache
  freshness is timestamped at download completion. Ticker processing records
  `WAITING / MARKET_DATA`, the reason and any local retry minimum.
- Existing synthetic-data, confirmed-bar, model, order and risk guards remain.
  This patch changes neither credential persistence nor REAL routing/start
  authorization. Broker-side protection and account reconciliation must still
  be verified independently; pending data is not evidence that positions closed.

## Offline verification

```bash
python -m unittest discover -s tests -p test_v3_3_16_market_data_recovery.py
python -m unittest discover -s tests -p test_schwab_request_budget_offline.py
python -m unittest discover -s tests -p test_v3_3_15_exit_control.py
node tests/test_v3_3_15_dashboard_control.cjs
```

Results: 20 new + 28 budget + 26 exit/control Python checks and eight dashboard
scenarios passed. The new suite exercises actual production method bodies with
real pandas, fake HTTP responses and a virtual clock. It covers 17-ticker
recovery under the shared gate, five-second admission bounds, unchanged HTTP
pauses/control reserve, interrupted download reuse, UTC paging/cache expiry,
401 retry admission, synthetic-cache recovery, failed micro recovery and no
price/order actions from pending data. Python compilation and whitespace checks
also passed. Full application integration and live VPS/broker verification
were not performed; required application dependencies are unavailable here.

## VPS verification after review

1. Check actual Schwab positions and working orders before any stop/restart.
   Stopping can request liquidation with this project's saved configuration;
   do not infer a flat account from the dashboard alone.
2. Apply reviewed source changes and VERSION together, preserving `.env`,
   `config.yaml`, `data/`, logs, token state and authorization files. Do not
   delete tokens or repeatedly click verification to resolve a data wait. Run
   only one bot instance. A backend restart is required for v3.3.16.
3. Confirm `/api/status` and `/api/schwab/request-metrics` report 3.3.16. Reading
   request metrics sends no Schwab traffic. Use the existing authenticated
   dashboard session if required by server access controls.
4. Verify fresh real candles and per-ticker processing in the normal dashboard
   before authorizing REAL operation. Initial historical warmup can span several
   cycles. A completed download is not a trade promise; AI/strategy/risk criteria
   must still pass. Additional monitoring/quote traffic can defer history reads.
5. If pending persists, collect the request-metrics JSON and recent redacted
   `logs/schwab_http_errors.jsonl` entries for `/marketdata/v1/pricehistory`, plus
   pending reasons from `logs/chocoletra.log`. Distinguish local budget deferrals
   from actual HTTP 400, 401, 403, 429, 5xx or empty history. Do not share tokens,
   secrets or raw account/order responses.

The gate covers this Python process, not another deployment or another client
using the same Schwab application. Its 90/minute total and 60/minute background
settings are engineering limits, not a statement of Schwab's contractual quota
or a guarantee against infrastructure blocks. Changing config limits alone
does not fix the synthetic-cache recovery defect.
