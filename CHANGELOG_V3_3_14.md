# GALA10 v3.3.14 — Shared Schwab quiet period after HTTP 403

Schwab Trader API support's reply supplied on October 1, 2026 recommends a consistent environment, reduced retry activity, and at least ten minutes before retrying after HTTP 403. v3.3.13 could retry OAuth after 30 seconds while other Schwab requests continued. This release applies the pause to all bot HTTP callers.

- OAuth, quotes, candles, account and position queries, order submission, cancellation, and server network probes share one request gate.
- The first 403 starts a ten-minute pause. Repeated denials extend it to 20, 40, then 60 minutes. A longer numeric `Retry-After` is honored.
- No new bot Schwab requests are dispatched during the pause. Requests already in flight can finish; a later 403 extends the deadline.
- After the deadline, one OAuth or read-only request checks the route. Order POSTs and DELETEs cannot serve as that check. A failed recovery through timeout, 429, or 5xx starts another quiet period.
- The credential-free `data/schwab_request_cooldown.json` preserves the pause across restarts when the runtime directory is writable. A storage failure is shown to the operator. Malformed saved state starts a conservative ten-minute pause.
- Saved OAuth tokens are retained during denials and local pauses. Synchronizing a token, resetting authentication fields, or forcing a dashboard check cannot bypass the gate.
- The dashboard displays the remaining wait and disables Connect, Test, OAuth opening, and network diagnostics during it. Diagnostics stop dispatching after the first denied probe and distinguish locally omitted requests from HTTP responses.
- Paused order calls report failure; the transport does not queue or replay orders. The update does not change execution modes or assert that broker positions are closed.
- Version assertions in earlier compatibility tests now compare the dashboard/server with `VERSION`.

The OAuth User-Agent remains `GALA10-Chocoletra/3.3.13` deliberately, keeping the request identity stable. There are no new environment variables or dependencies.

Support's reply is consistent with an upstream security control; it does not identify its exact rule or guarantee recovery after ten minutes. This update implements the retry guidance and requires verification from the actual VPS after deployment.

See [upgrade instructions](UPGRADE_V3_3_13_TO_V3_3_14.md) and [validation](V3_3_14_VALIDATION.md).
