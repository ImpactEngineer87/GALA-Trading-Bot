# Chocoletra GALA10 v3.3.8 — Schwab OAuth Reliability & Diagnostics

v3.3.8 is a focused broker-authentication reliability update on top of v3.3.7.
It does not change GALA10 signal generation, AI model files, per-ticker thresholds,
PAPER/REAL routing policy, stop/TP mathematics, or the v3.3.7 learning policy.

## Fixed / improved

### 1. Safe OAuth failure diagnostics
The dashboard no longer collapses a rejected Schwab token exchange into only
`403 Client Error: Forbidden`. It now preserves a sanitized diagnostic containing
only non-sensitive fields such as HTTP status, OAuth error name, recommended
action, callback URI, Client ID last four characters, and request/correlation ID
when Schwab supplies one.

Client Secret, authorization code, access token, and refresh token are never
returned to the browser or written to the new diagnostic log message.

### 2. Credential rotation invalidates old OAuth tokens
When the operator changes `SCHWAB_CLIENT_ID` or `SCHWAB_CLIENT_SECRET`, v3.3.8
clears the access token, refresh token, and expiry that belonged to the previous
Schwab app. The bot then requires a completely new authorization-code flow.

This prevents a newly-created Schwab app from accidentally trying to refresh a
token issued to the previous app.

### 3. Shared OAuth token-rotation lock
Dashboard token exchange and all `SchwabClient` refresh operations now share a
single process-wide OAuth rotation lock. Before an engine/client refresh, the
client adopts the newest access/refresh token already stored by another component.
This prevents an older client instance from replaying a superseded refresh token.

### 4. Current credentials are re-read before refresh
Long-lived engine clients no longer keep using the Client ID/Secret that were
present when the Python object was first created. Before refresh, credentials are
re-read from the current process environment.

### 5. Callback and authorization-code validation
- The callback URL is checked against `https://127.0.0.1` when a full redirect
  URL is pasted.
- A per-session OAuth `state` value is added and validated when Schwab returns it.
- A successfully-used authorization code is rejected if submitted again.
- Bare `code=...` input remains supported.

### 6. Dashboard UX
- Connect button is disabled while a token exchange is in progress, preventing
  accidental double submission.
- A safe diagnostic panel shows the exact OAuth error category and recommended
  next action after a rejected exchange.

## Version

- Previous: `3.3.7`
- Current: `3.3.8`
- No config migration required.
