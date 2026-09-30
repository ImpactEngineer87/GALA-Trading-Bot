# v3.3.8 Schwab OAuth Reliability — Technical Notes

## Problem

The v3.3.7 dashboard surfaced a Schwab `/v1/oauth/token` rejection only as a generic
HTTP 403 exception. At the same time, the running process could contain several
`SchwabClient` instances, each holding a copy of OAuth credentials/tokens. This
made two operational problems hard to distinguish:

1. Schwab rejecting the *new authorization-code exchange* (for example
   `invalid_client` or `invalid_grant`).
2. A long-lived component trying to refresh with an older credential/token copy
   after another component had rotated it.

## v3.3.8 design

### Shared token-exchange primitive

`api/schwab_oauth.py` contains the common authorization-code and refresh-token
exchange functions and one process-wide `RLock`. Both dashboard and engine-side
clients use this lock.

### Safe diagnostics

The diagnostic whitelist contains only:

- phase;
- HTTP status;
- OAuth error identifier;
- sanitized description;
- recommended action;
- last four Client ID characters;
- redirect URI;
- optional request/correlation ID.

The sanitizer explicitly removes Client Secret, authorization code, access token,
refresh token, and long token-looking strings.

### App credential rotation

When `SCHWAB_CLIENT_ID` or `SCHWAB_CLIENT_SECRET` changes, previously issued tokens
are removed because they belong to the old OAuth app identity. The dashboard then
requires a fresh authorization-code exchange.

### Multi-client refresh protection

Before a `SchwabClient` refreshes, it re-reads current process credentials and the
latest access/refresh token. If another component has already refreshed and the
new access token is still valid, it adopts that token and skips another refresh.

## Operational interpretation

v3.3.8 does not manufacture a connection when Schwab rejects the app. Instead, it
makes the failure actionable:

- `invalid_client` → verify the Client ID/Secret for the same Ready-For-Use app;
- `invalid_grant` → open OAuth again and use a brand-new callback/code;
- `state_mismatch` / `state_expired` → restart the OAuth flow from the dashboard;
- network/5xx/429 → retry later without treating the app credentials as invalid.

REAL trading remains fail-closed until Schwab authentication and broker
reconciliation are verified.
