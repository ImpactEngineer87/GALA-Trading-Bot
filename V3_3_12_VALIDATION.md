# v3.3.12 validation notes

Added regression coverage for:
- Akamai Reference ID extraction;
- network-change action for authorization-code edge denial;
- network diagnostics endpoint;
- server/client edge circuit breaker state;
- 5-minute proactive refresh window;
- v3.3.12 dashboard branding.

The network diagnostic is read-only and sends no real access token, refresh token, account number, or Client Secret.
