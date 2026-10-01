# Chocoletra GALA10 v3.3.11 — Processing / Retraining Visibility + Schwab Refresh Resilience

v3.3.11 is a runtime-observability and Schwab-connectivity reliability release on top of v3.3.10.

## Added

- Per-ticker processing visibility for all monitored tickers.
- Exact stage/reason: market data, GALA10 signal, AI decision, risk/broker execution, quarantine.
- Retraining visibility: x/50 state, pending/training state, last retrain time/result, supplemental FIT rows actually used.
- Schwab HTML 403 Access Denied classification as a transient edge failure.
- Exponential refresh backoff: 30s, 60s, 120s, up to 300s.

## Important behavior

- Signal-learning observations still do not increment x/50 or independently trigger retraining.
- Eligible supplemental observations are consumed only by the next eligible/manual retraining FIT block.
- invalid_client / invalid_grant remain hard OAuth failures.
- HTML Access Denied / edgesuite responses are treated as transient and retried with backoff.
- REAL entry remains fail-closed whenever valid Schwab authorization cannot be confirmed.

## Unchanged safety

- GALA10 strategy logic and per-ticker thresholds are unchanged.
- PAPER/REAL routing is unchanged.
- VALIDATION/TEST remain execution-valid actual trades only.
- No new broker write endpoint is introduced.

Previous: 3.3.10
Current: 3.3.11
