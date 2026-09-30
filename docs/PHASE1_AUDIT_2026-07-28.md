# GALA10 / Schwab Bot — Phase 1 Technical Audit

Date: 2026-07-28  
Baseline: `chocoletra_bot_1(1).zip`

## Outcome

The project is a viable base, but the uploaded version was not safe to approve
for live trading. Its strategy, 70/30 calculations, reversal intent, Schwab
client, dashboard, historical data and per-ticker model files are present.
The main defects were state synchronization and an invalid AI feature schema.

This phase introduces a development-only correction baseline. It must remain in
paper mode until the TOS parity test, rebuilt model report and multi-session
market validation are completed.

## Confirmed findings

1. A successful HTTP cancellation was treated as if the order had already
   reached `CANCELED`.
2. A market liquidation was treated as complete immediately after POST, before
   confirming `FILLED` and a zero broker position.
3. Entry, TP1 and stop status monitoring shared the 60-second strategy loop.
   A fast fill could therefore remain unprotected until the next candle cycle.
4. Several reversal paths could continue after an unconfirmed cancellation.
5. The old feedback file contains 114,516 rows across 25 tickers, but its schema
   has no volume or liquidity features.
6. Existing `.pkl` files have no feature-version metadata or validation guard.
7. Training used a random split, which is inappropriate for time-ordered
   financial observations.
8. The uploaded package does not contain the exact GALA10 ThinkScript or a TOS
   signal export. The Python strategy is named/modeled as GALA6 and comments
   call it GALA10, but exact signal parity cannot be certified from names or
   comments.
9. Dashboard authentication is currently bypassed in `server.py`. Security is
   a separate scope, but the dashboard must not be exposed publicly in this
   state.

## Implemented in this development baseline

### Schwab order lifecycle

- Async polling primitives with configurable 100 ms cadence.
- POST/DELETE acknowledgement separated from broker-state confirmation.
- Cancellation returns success only after a terminal broker state is observed.
- Fill races are detected and are never reported as successful cancellations.
- Market liquidation returns `OK` only after the order/position is reconciled
  and the ticker is confirmed flat.
- Per-symbol re-entrant lock serializes cancel → close → flat → opposite entry.
- A dedicated real-order monitor runs independently from the 1-minute signal
  clock.
- Initial protection places the stop before TP1.
- TP1 stop replacement waits for the old stop to become terminal before placing
  the 30% stop.
- If the old stop is canceled but the replacement cannot be confirmed, the
  remaining position is liquidated fail-safe.
- Every intraday entry, exit and liquidation payload forces `duration: DAY`.
- REST sessions are thread-local to avoid sharing one `requests.Session` across
  ticker workers.

### One-minute decision clock

- The engine wakes at the next minute boundary plus a 250 ms publication margin.
- The 1-minute history is refreshed on every boundary.
- The forming 1-minute candle is excluded.
- Each closed bar timestamp is evaluated once per ticker.
- GALA remains the only entry direction/trigger; AI output can approve or veto.

### AI and persistence

- Added causal features computed from closed candles only:
  relative volume, volume z-score, log dollar liquidity, range/spread proxy and
  Amihud price-impact proxy.
- Training requires the new volume/liquidity schema.
- Train/test separation is chronological.
- Reports include samples, accuracy, precision, recall, F1, confusion matrix,
  losing-trade veto rate and incorrectly vetoed winning trades.
- Candidate models are activated only if validation thresholds pass and they do
  not materially degrade the incumbent.
- Model and metadata files are written atomically.
- Feature version and sample count are persisted beside every `.pkl`.
- Old unversioned models are intentionally not loaded.
- Retraining runs outside the order thread after a configurable number of closed
  trades and at session end.
- Dashboard manual sizing supports exact fixed shares or ATR-based sizing. AI
  probability cannot multiply an exact fixed-share override.

## Verification completed offline

- Python compilation: passed.
- YAML parse: passed.
- Unit tests: 13 passed.
- Tested cases include cancellation polling, cancel/fill race, unknown-state
  timeout, stop-before-TP protection, DAY payloads, fixed sizing, ATR sizing,
  forming-candle exclusion and invalid-volume fail-closed behavior.

No live Schwab request was sent during this phase.

## Required inputs before strategy certification

1. Exact GALA10 ThinkScript currently used in ThinkOrSwim.
2. A TOS export containing timestamps and BUY/SELL signals for at least two
   market sessions and several agreed tickers.
3. A failed reversal/cancellation log with order IDs and timestamps, with secrets
   removed.
4. Confirmation of whether Schwab testing will use the client's paper/sandbox
   facility or a one-share live validation.

## Safe next sequence

1. Keep production untouched and deploy this version to a separate development
   directory.
2. Install `requirements.txt` in a dedicated virtual environment.
3. Run all unit tests.
4. Keep `risk.paper_trading: true`.
5. Supply the exact TOS script and run bar-by-bar parity comparison.
6. Archive the old feedback/model artifacts, rebuild the dataset with the new
   volume schema and run `PRE_ENTRENAR.py`.
7. Review `data/reportes/metricas_modelos_latest.csv` together. Do not activate a
   ticker whose sample count or out-of-sample metrics are insufficient.
8. Run controlled paper tests for entry, TP1, break-even stop, reversal and kill
   switch while recording broker-state transitions and elapsed milliseconds.
9. Validate during 3–5 separate market sessions, starting with the hard one-share
   cap.
10. Only after acceptance, promote the tested commit to production.

## Credential warning

Any VPS, email or API credentials posted in chat must be treated as exposed and
rotated. The development archive excludes `.env`; existing VPS secrets should be
re-entered directly on the server and never committed or sent inside a ZIP.
