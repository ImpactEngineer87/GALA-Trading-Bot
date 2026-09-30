# Chocoletra GALA10 v3.3.1 — Runtime Reliability Hotfix

v3.3.1 is a reliability/observability update on top of v3.3.0. It does **not** change GALA10 signal generation, stop/TP mathematics, model files, PAPER/REAL routing, or the v3.3 execution-quality policy.

## Fixed

### 1. Signal marker misaligned with the closed 1-minute source bar
The engine evaluates a fully closed candle just after the next minute boundary. In the UAL incident, the decision was emitted at `13:34:04` from the candle that opened at `13:33:00`. v3.3.0 stored/plotted only the decision wall-clock timestamp, while the local candle archive ended at the actual source candle (`13:33:00`), so the chart filtered the marker out.

v3.3.1 now persists the exact GALA10 source-bar timestamp separately from the decision timestamp and plots the marker against that candle. Legacy signal rows infer the previous closed minute for display without rewriting the ledger.

### 2. Wrong threshold shown for SHADOW/PAPER decisions
The signal feed could display the production trainer fallback threshold instead of the threshold used by the exact model that made the decision. For UAL this produced `umbral: 0%` even though the SHADOW model decision used `0.41` (41%).

The decision snapshot is now authoritative. Historical v3.3.0 signal rows are also normalized on read from `ai_context.threshold`, so old rows display correctly after restart without rewriting the ledger.

### 3. Duplicate shutdown / kill-switch race
An operator stop and the engine worker's `run()` cleanup could enter `detener()` concurrently. Shutdown is now serialized and idempotent, so liquidation/reconciliation runs once per engine instance.

### 4. Broker reconciliation JSON write race on Windows
Concurrent persistence previously reused `broker_reconciliation_state.json.tmp`, which could trigger WinError 32 during `os.replace`. v3.3.1 serializes writes and uses a unique temporary file for every atomic replacement.

### 5. OAuth outage log flooding
When the Schwab user OAuth session becomes unusable, many tickers/cycles can observe the same outage. The engine now rate-limits the operator-facing auth error and emits one recovery message when OAuth is healthy again.

The safety behavior is unchanged: REAL orders remain blocked and PAPER trading does not use synthetic/fallback prices as if they were real market data.

### 6. Persistent dashboard/control log
Dashboard start/stop/control messages are now also written to `logs/dashboard_server.log`. A future incident can therefore distinguish an operator/dashboard stop from a scheduler stop or startup guard even after the process exits.

## Version

- Previous: `3.3.0`
- Current: `3.3.1`
- No config migration required.
