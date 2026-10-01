# Upgrade v3.3.12 to v3.3.13

Run this update in the existing bot checkout on the VPS. Keep the deployment's `.env`, configuration, REAL authorizations, runtime data and model files. Do not copy the laptop's token file over the VPS token file.

1. Check current broker positions and the bot's pending orders before scheduling a service restart. Use the existing operational procedure for stopping and restarting the bot.
2. In the existing checkout, run `git status --short`, then `git pull --ff-only origin main`. Resolve any local code changes before pulling; do not reset or delete runtime files.
3. Confirm `cat VERSION` prints `3.3.13`. This release introduces no new environment variables.
4. Restart the existing bot service after the update. Reload the dashboard and confirm its version reads v3.3.13.
5. Open Schwab connection settings and click **Diagnosticar red**. The result names the host that sends token requests; it should be the VPS when the bot runs there.
6. If the token probe reaches OAuth, wait for any retry pause, open a fresh authorization flow, log in on the client's laptop and paste the full new callback URL into the VPS dashboard once. Do not reuse an attempted code.
7. Confirm **Schwab conectado** with a successful live status check and perform the existing read-only broker reconciliation before resuming REAL entries.

## If Access Denied continues

The laptop performs browser login; the VPS performs code exchange and refresh. A successful laptop login does not confirm that the VPS can reach the token endpoint. Changing the laptop's Wi-Fi or using a phone hotspot leaves VPS egress unchanged.

An HTML 403 with an Akamai Reference ID indicates an access-layer denial. It does not identify the exact rule or prove that credentials are wrong, the VPS IP is blocked, or the problem will disappear after waiting. Review VPS HTTPS connectivity, outbound proxy, DNS and provider routing. For a persistent denial, send Schwab Trader API support the UTC timestamp, HTTP status, endpoint, VPS public egress IP obtained from the hosting provider, and Reference ID. Never send Client Secret, authorization codes or tokens.

The unauthenticated network probe can establish that it received an OAuth error. It cannot validate the real app credentials or guarantee that a subsequent authenticated request will be accepted. A code change cannot remove an upstream access restriction.

## One owner for tokens

Run one bot/dashboard process that owns this account's token rotation. Do not run a second bot on the laptop with a copied refresh token, or multiple independent Flask/Gunicorn workers sharing the same `.env`. The shared lock coordinates threads and client instances within one process; it does not coordinate separate services or machines.

## Interpretation of diagnostic results

| Result | Meaning and next step |
| --- | --- |
| `EDGE_ROUTE_BLOCKED` | An access-layer denial was observed. Review the VPS route and give the Reference ID to Schwab support. |
| `NETWORK_PROBE_FAILED` | At least one probe failed through transport/TLS. Inspect the reported failure on the VPS. |
| `DNS_OR_NETWORK_FAILURE` | Local DNS resolution failed. Review VPS DNS and connectivity. |
| `ROUTE_INCONCLUSIVE` | Responses do not establish OAuth reachability. Inspect HTTP statuses and retry after resolving the failure. |
| `OAUTH_ENDPOINT_REACHABLE` | The probe received an expected structured OAuth error. App credentials remain unverified; use a fresh authorization once the pause ends. |

Background refresh preserves a still-valid access token during an edge failure. When that token expires or is rejected, authentication helpers stop sending it. Retry pauses and a circuit breaker limit repeated OAuth requests.

## References

- [Schwab OAuth guide](https://developer.schwab.com/user-guides/get-started/authenticate-with-oauth)
- [HTTP 403 semantics](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.5.4)
- [OAuth authorization-code exchange](https://www.rfc-editor.org/rfc/rfc6749.html#section-4.1.3)
- [Akamai security API concepts](https://techdocs.akamai.com/application-security/reference/api-concepts)
