# Upgrade v3.3.13 to v3.3.14

Run the update in the existing VPS checkout. Schwab support recommends at least ten minutes before retrying after HTTP 403, a stable request environment, and reduced retry activity. The pause must include background traffic; stopping repeated Connect clicks alone does not stop an older bot's requests.

## Windows VPS procedure

1. Review existing broker positions and pending orders through Schwab's own application. Follow the deployment's existing procedure for supervising them while the bot is offline. During the request pause the bot cannot query, submit, or cancel broker orders, including protective orders; an existing broker order can still fill.
2. Stop the old bot using the deployment's normal service procedure. Stop other bot instances or scheduled scripts calling Schwab. Record when the last Schwab request ended. Do not repeat Connect, Test, network diagnostics, browser authorization attempts, or `curl` probes during the quiet period.
3. Preserve `.env`, `config.yaml`, saved authorization state, models, and runtime data. If present, also preserve `data/schwab_request_cooldown.json`. In PowerShell, from the existing project directory, update the tracked code:

   ```powershell
   git status --short
   git pull --ff-only origin main
   Get-Content .\VERSION
   ```

   `VERSION` must print `3.3.14`. If local code edits prevent the pull, review and merge them; do not use a destructive reset or delete runtime files. No new environment variables or dependencies are required.
4. Keep the old bot and external probes stopped for at least ten full minutes from the last Schwab attempt before starting the updated bot. This is necessary because an older version has no shared persisted timer. Follow a longer current dashboard deadline or a longer `Retry-After` when present.
5. Start exactly one bot/dashboard process on the same VPS using the normal service command. Keep egress and proxy settings consistent. Reload the dashboard and confirm v3.3.14. The directory `data` must be writable by the service account; resolve any pause-state persistence warning before restarting again.
6. Let the next status or OAuth check run after the countdown ends. Only one bot request is admitted as the recovery check. Avoid repeated manual tests alongside that check. If another 403 occurs, let the new countdown finish; consecutive denials produce 10, 20, 40, then 60-minute waits.
7. If tokens remain valid and the connection verifies, manual authorization is unnecessary. If a structured OAuth result confirms that reauthorization is required, open a fresh authorization flow after the pause, log in on the laptop, and paste the new callback URL into the VPS dashboard once. Do not reuse a code whose HTTP exchange was attempted. The VPS owns token exchange, storage, and refresh.
8. Before resuming REAL entries, use the existing read-only broker reconciliation procedure to confirm positions and outstanding orders. A successful HTTP route check alone does not confirm credentials, fills, cancellation, or flat positions.

## Shared pause and restart behavior

All Schwab bot HTTP callers use `api/schwab_transport.py`. HTTP 403 starts the pause even when it is a structured permissions response; the diagnostic still distinguishes permissions errors from HTML access denials. No request is sent for a local pause, and API responses can report `request_sent: false`, a retry interval, and a `Retry-After` header.

The runtime state file contains only a deadline, denial count, and recovery flag. It does not contain tokens, secrets, account details, or order data. It is ignored by Git and must not be overwritten during an upgrade. Restarting with the same writable runtime directory preserves the remaining wait. If saving fails, the current process still enforces the pause, but a restart cannot be assumed to preserve it; the dashboard reports the storage problem. An unreadable existing record causes a fresh ten-minute pause.

The shared gate coordinates threads within one process. It does not coordinate several independent processes, workers, machines, or tools outside the bot. Run one owner for the account's token refresh; do not run a laptop bot with copied refresh tokens or multiple Flask/Gunicorn workers. External browser and `curl` activity must be paused separately. Maintain a correct VPS system clock for restart deadline recovery.

At the end of a quiet period, one OAuth or read-only request may test the route. Order creation and cancellation stay locally blocked until that check completes. A timeout, 429, or 5xx during recovery starts another wait; a 403 increases the wait. A structured 400/401 can establish route reachability while still requiring the existing authentication or permissions handling.

## If the denial continues

Schwab support described behavior consistent with an upstream security control and said reduced retries from a stable environment are expected to allow recovery in some cases. Their reply did not identify the rule, verify the VPS IP as the cause, or promise resolution exactly ten minutes later. The bot cannot remove an upstream restriction.

Keep the Schwab and hosting-provider tickets open. Send the latest UTC timestamp, endpoint, HTTP status, Reference ID, confirmed public egress IP, and the duration of a fully quiet period. Explain that background requests and external probes were also stopped. Do not send authorization codes, tokens, Client Secret, or personal account information.

`COOLDOWN_ACTIVE` means the bot omitted the HTTP probes locally. `EDGE_ROUTE_BLOCKED` means an actual access-layer denial was observed. `OAUTH_ENDPOINT_REACHABLE` means a probe received the expected structured OAuth response; it does not validate the real credentials. A laptop login does not test the VPS's token-request route.

The update is available in the repository; deployment and a live VPS connection check remain operator actions.
