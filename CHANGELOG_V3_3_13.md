# GALA10 v3.3.13 — Schwab authentication recovery

Fixes recovery defects found after the v3.3.12 edge-denial patch.

- Proactive refresh actually renews the current token; a token rejected by 401 cannot be reloaded from the environment.
- A late 401 from an older request preserves a newer synchronized token. Repeated rejection respects cooldowns.
- Dashboard and engine token exchanges keep the shared rotation lock through publication. Runtime tokens remain current if disk persistence fails.
- Direct dashboard refresh and reconnect paths honor the retry pause. Attempted authorization codes cannot be resubmitted.
- API HTML 403 and permission 403 no longer trigger OAuth refresh or an expired-token alert.
- Expired tokens are not sent by dashboard helpers while refresh is paused.
- Network diagnostics distinguish transport failure, inconclusive responses, and structured OAuth endpoint responses. They identify the bot server and never claim to verify credentials.
- Connection guidance now refers to the VPS sending token requests. Changing the laptop network does not change VPS egress.
- OAuth POSTs do not follow redirects; encoded Akamai references are captured without displaying raw HTML.

See [upgrade and verification steps](UPGRADE_V3_3_12_TO_V3_3_13.md). Live VPS authentication requires a check from that deployment; tests use mocked broker responses.
