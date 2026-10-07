# Market-data warmup admission — v3.3.17

## Evidence

The October 7 console log (v3.3.16, MIXED, 5 REAL + 12 PAPER) shows the engine
start at 10:31:42 NY and the first cycle at 10:32:00. Between 10:32:06 and
10:32:15 every one of the 17 tickers reported `Datos Schwab pendientes`: four
with `control_burst_reserved`, then thirteen with `control_capacity_reserved`
and a 12–16 s minimum retry. These are **local admission refusals**: no request
was sent and no HTTP status exists for them. The log contains no Schwab HTTP
403, 429, 4xx or 5xx. The read-only LiveGuard preflight at 10:31:28–35
succeeded for all five REAL tickers.

At 10:32:16 the dashboard status check logged `Verificación no disponible:
SchwabRequestDeferred`, and the indicator changed to `Schwab ✗`. At 10:32:26 the
network diagnostic reported `NETWORK_PROBE_FAILED` and recommended checking the
VPS internet, proxy and DNS. Its Authorize and Trader probes were never sent
(`SchwabRequestDeferred`). The credential-free token probe reached Schwab and
received the expected HTTP 401. The engine was stopped at 10:33:05, about one
minute after its first cycle, so the log cannot show whether warmup would have
finished.

## Root cause

- A cold ticker needs about 19 `pricehistory` pages: 1W, 1D, 30m over 60 days
  (7), 15m over 40 days (5), 5m over 20 days (3) and 1m over 10 days (2). For 17
  tickers that is ~320 requests. The local gate admits 60 background requests
  per rolling minute (90 total, 30 reserved for orders, OAuth and
  reconciliation). These limits are unchanged.
- v3.3.16 ran all 17 downloads concurrently inside the one-minute cycle. Each
  ticker obtained a few pages and was then refused. The uncached most-recent
  page was downloaded again on every attempt, and REAL tickers had no priority.
- After warmup, each ticker's five-minute macro refresh (~6 pages) also ran
  inside the cycle and competed with the other tickers' 1m reads. When those
  reads were refused, bars were skipped.
- `SchwabRequestDeferred` subclasses `requests.RequestException`. The status
  check and network diagnostic therefore reported a local refusal as a network
  failure. The same applied to the dashboard token refresh, which escalated
  to a 30–300 s network backoff, and to the OAuth code exchange, which marked
  a code as used although it was never sent.
- The v3.3.16 offline simulation fetched tickers sequentially, so it could not
  reproduce this cross-ticker starvation.

## Changes

- **Background history refresher** (`engine/bot_engine.py`). One daemon thread
  per engine downloads complete bundles and refreshes due macro frames. Order:
  macro refreshes past halfway to the maximum age (oldest first), then cold
  tickers (REAL first, then configured order), then other due refreshes. It
  waits for local capacity instead of giving up until the next minute, and
  pauses while a cycle runs. A ticker-specific data failure (empty or failed
  history) backs off for that ticker only (30 s doubling to 300 s). Successful
  pages and frames remain cached across interruptions, as in v3.3.16.
- **Cycle reads only 1m.** A cold ticker reports `WAITING / WARMUP_PENDING`
  without sending a request. A warm ticker uses its last complete macro frames
  plus a fresh 1m read. Macro frames are used for at most
  `engine.mtf_macro_max_stale_sec`, by default 2 × `mtf_refresh_macro_sec` =
  600 s; after that the ticker reports `MACRO_STALE` and is not traded. If the
  refresher thread is not alive, the cycle downloads synchronously as before.
  A 1m update never overwrites a newer bundle that the refresher stored.
- **Warmup traffic class** (`api/schwab_transport.py`).
  `schwab_warmup_requests(headroom, allowed)` marks the refresher's requests:
  - They leave eight background slots per rolling minute free and are refused
    (`warmup_paused`) while a cycle runs.
  - OAuth and control traffic are unaffected.
  - Total, background, burst and HTTP 403/429 rules are unchanged.
- **Logs.** One line per cycle (`Históricos en segundo plano: n/17 tickers
  listos | pendientes: …`), one line per completed ticker, and no per-ticker
  warning for `WARMUP_PENDING`. `/api/schwab/request-metrics` counts
  `warmup_headroom_reserved` and `warmup_paused` refusals.
- **Server** (`server.py`):
  - **Status check:** a refused check keeps a `connected` result verified
    within the last 5 minutes. Otherwise it reports that verification was
    postponed and nothing was sent.
  - **Network diagnostic:** refused probes are marked not sent, with a new
    `LOCAL_BUDGET_BUSY` conclusion.
  - **Token refresh:** a refused refresh is reported as postponed, with no
    network backoff and no pause countdown.
  - **Code exchange:** a refused exchange keeps the code unused and returns
    HTTP 503 with `Retry-After`.
- **Dashboard.** Shows `LOCAL_BUDGET_BUSY` as an amber "probes not sent" result
  instead of a red network failure.
- **Not changed:** request limits, HTTP pause and recovery handling, order and
  REAL routing, LiveGuard, risk, model and synthetic-data guards, and
  credential storage.

## Behaviour changes to review

- Higher-timeframe frames (1W, 1D, 4H, 1H, 15m) can be up to 10 minutes old
  instead of 5. This happens only while the refresher is still working through
  cold tickers. 1m (and 5m, which is resampled from 1m) is fetched every cycle,
  as before.
- At start, a cold ticker is skipped until its bundle completes. This was
  already the outcome in v3.3.16, but it is now explicit and ordered.

## Offline verification

A deterministic concurrent simulation runs:
- 17 ticker workers in parallel, the refresher and a 20 s status poll
- real threads under a virtual clock, with 0.3 s request latency
- the production gate, client and engine methods
- the start-up conditions above: a 20-request LiveGuard preflight, and the
  engine starting 17 s before a bar

| 14 cycles, 17 tickers | REAL ready by | all ready by | warm bars missed | status checks refused |
|---|---|---|---|---|
| v3.3.16 (2, 5 or 8 REAL) | cycle 8 | cycle 8 | 9 | 9 of 42 |
| v3.3.17, 2 REAL | cycle 2 | cycle 9 | 0 | 0 of 41 |
| v3.3.17, 5 REAL | cycle 3 | cycle 9 | 0 | 0 of 41 |
| v3.3.17, 8 REAL | cycle 4 | cycle 9 | 0 | 0 of 41 |

The first REAL ticker is ready in cycle 1. The complete set takes one cycle
longer than v3.3.16, because v3.3.16 finished by taking slots from tickers that
were already warm. These are simulation results with ideal history responses,
not a promise of live timing; real latency and other traffic change them.

```bash
python -m unittest discover -s tests -p test_v3_3_17_warmup_admission.py
python -m unittest discover -s tests -p test_v3_3_16_market_data_recovery.py
python -m unittest discover -s tests -p test_schwab_request_budget_offline.py
python -m unittest discover -s tests -p test_v3_3_15_exit_control.py
node tests/test_v3_3_17_dashboard_diagnostics.cjs
node tests/test_v3_3_15_dashboard_control.cjs
node tests/test_v3_3_14_dashboard_pause.cjs
```

On Windows, set `PYTHONUTF8=1`. Two existing tests read `server.py` without an
explicit encoding.

The full `pytest tests` suite was also run with `requirements-dev.txt`
installed (Python 3.13, Windows). This branch passes 495 tests; unmodified
`main` passes 466. The difference is the new tests. The same 31 tests fail on
both trees, and three modules cannot be collected on either because they import
`MIGRATE_CONFIG_V2_8/V2_9` scripts that are not in the repository. These
failures predate this change. No live broker, credentials, VPS or real orders
were used.

## VPS verification after review

1. Check actual Schwab positions and working orders before any stop or
   restart. Stopping can request liquidation under this project's saved
   configuration.
2. Apply the reviewed source and `VERSION` together. Preserve `.env`,
   `config.yaml`, `data/`, logs and token state, and run one bot instance.
   Restart the backend and confirm 3.3.17 in `/api/status`.
3. Start the engine and leave it running for at least 10 minutes. Expect a
   `Históricos en segundo plano` line every cycle and `[BA] Históricos
   completos (1/17 …)` lines, REAL tickers first. Once a ticker is complete,
   its row should not show `control_capacity_reserved` or
   `control_burst_reserved`.
4. During warmup the network diagnostic may report `LOCAL_BUDGET_BUSY`. That is
   accurate: repeat it later or with the engine stopped. Do not repeatedly
   click verification.
5. If tickers stay `WARMUP_PENDING` or become `MACRO_STALE`, collect:
   - `/api/schwab/request-metrics` (local only; it sends no Schwab traffic)
   - recent redacted `logs/schwab_http_errors.jsonl` entries for
     `/marketdata/v1/pricehistory`
   - `Históricos` lines from `logs/chocoletra.log`

## Possible follow-ups (not in this patch)

- 1W and 1D have no `endDate`, so they are never page-cached and are
  downloaded again on every macro refresh.
- Minute history is paged in 10-day windows. If Schwab serves longer ranges per
  call (unverified here), cold warmup cost would drop roughly threefold.
- The LiveGuard preflight repeats account reads for each REAL ticker, about 20
  requests at start.
- The 90/60/30 per-minute settings are engineering limits. Confirm Schwab's
  actual quota before changing them.
