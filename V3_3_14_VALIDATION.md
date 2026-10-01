# v3.3.14 validation

Validated against remote main `5a8fa7af4ebd9a67ddf30f0b28bff500b758c135` (v3.3.13). Schwab HTTP responses, credentials, and clocks used in the recovery tests are isolated test fixtures. No live broker requests or trades were made.

## Passing checks

- **165 Python tests passed** across the 17 modules listed below.
- The Node dashboard interaction test passed. It exercises the actual pause and OAuth-opening functions with a mocked DOM and API, including button disablement, countdown, persistence warnings, and refusal to open an absent OAuth URL. This is not a full browser or live VPS test.
- Python compilation passed for the shared transport, OAuth, client, both order executors, and server.
- All extracted dashboard inline JavaScript passed `node --check`.
- `git diff --check` passed.

```bash
.venv/bin/python -m pytest -q \
  tests/test_v3_3_14_schwab_request_pause.py \
  tests/test_v3_3_14_schwab_server_pause.py \
  tests/test_v3_3_13_schwab_client_refresh.py \
  tests/test_v3_3_13_schwab_server_recovery.py \
  tests/test_v3_3_13_schwab_oauth_transport.py \
  tests/test_v3_3_12_schwab_edge_recovery.py \
  tests/test_v3_3_8_schwab_oauth_reliability.py \
  tests/test_schwab_auth_notification.py \
  tests/test_dashboard_security.py \
  tests/test_v3_3_execution.py \
  tests/test_v3_3_1_runtime_reliability.py \
  tests/test_v3_3_2_kill_switch_recovery.py \
  tests/test_v3_3_3_broker_flat_sync.py \
  tests/test_v3_3_5_auto_exit_recovery.py \
  tests/test_v3_3_9_signal_learning_visibility.py \
  tests/test_v3_3_10_signal_learning_all_filter.py \
  tests/test_v3_3_11_processing_training_visibility.py

node tests/test_v3_3_14_dashboard_pause.cjs
```

## Recovery evidence

- HTML and structured JSON 403 start the same minimum 600-second quiet period. Calls just before the deadline cannot dispatch. A longer numeric `Retry-After` extends it.
- Restarting with the saved runtime record preserves the remaining pause and denial count. Changing the wall clock while the process runs cannot shorten its monotonic timer.
- Repeated denials extend the wait across restarts. One read-only or OAuth recovery request is admitted after the deadline; competing requests and order mutations are blocked locally.
- A recovery timeout, 429, or 5xx starts another quiet period. A late in-flight 403 cannot be cleared by a concurrent successful recovery response.
- The state file contains no request secrets. Malformed state fails closed; a write failure preserves the in-memory pause, cleans up temporary files, and exposes an operator warning.
- Engine clients, dashboard helpers, forced status checks, authorization-code exchange, and network diagnostics share the pause. Token synchronization and authentication-state resets cannot bypass it.
- Network diagnostics dispatch only the first probe when that probe receives a denial. Omitted probes explicitly report no request sent.
- Saved tokens and reauthorization status are preserved. An authorization-code exchange rejected locally before HTTP does not consume its attempted-code fingerprint.
- Both order executors report a paused entry as an error with no order ID, no HTTP POST, and no change to PAPER mode. Paused cancellations report failure and dispatch no DELETE.
- Prior refresh, rotation, token rejection, execution, security, and signal-learning compatibility tests still pass. Stale version-literal assertions were changed to check consistency with `VERSION`.

## Scope and existing limitations

This is a targeted regression run, not a claim that the entire project suite passes. [The v3.3.13 validation report](V3_3_13_VALIDATION.md) records full-suite collection failures from missing migration modules and nine reproduced baseline failures in older execution/configuration fixtures. Those unrelated issues were not part of this request.

The shared pause supports threads and restarts of one bot process. Separate workers, other machines, browser requests, and external probes are outside that coordination. Persisting the wait requires a writable runtime directory and retaining its state file during upgrades.

No access to the actual VPS or live Schwab credentials was available. Publishing this patch does not deploy it. After deployment, the operator must complete the quiet period, verify the actual connection, and reconcile broker positions and outstanding orders before resuming REAL entries. Support's retry guidance does not guarantee removal of the upstream security restriction.
