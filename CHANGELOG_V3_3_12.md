# GALA10 v3.3.12 — Schwab Edge Recovery

Focused reliability release for repeated Schwab/Akamai HTTP 403 Access Denied failures.

## Changes
- OAuth requests now use a stable GALA10 User-Agent and documented form headers.
- HTML Access Denied responses extract the Akamai Reference ID.
- Authorization-code edge failures no longer tell the operator to rotate valid credentials.
- After an edge denial, the UI explicitly requires changing the network/public IP before generating a new OAuth code.
- Repeated edge denials open a circuit breaker after 3 failures for 15 minutes.
- Access tokens keep their hard expiry; refresh starts 5 minutes early.
- If proactive refresh is temporarily blocked, a still-valid access token remains usable until hard expiry.
- Added a read-only network-path diagnostic for Authorize, Token, and Trader routes.

## Safety
REAL entry still fails closed once no valid access token is available.
Hard OAuth errors such as invalid_client and invalid_grant still require reauthorization.
