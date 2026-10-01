# v3.3.13 validation

Validated against main `6ffc2bae9f76e6f746d30b64f822b2a004efec8e` (v3.3.12), with mocked Schwab responses and isolated test credentials.

## Passing checks

- **84 tests passed** across OAuth notification/reliability, v3.3.9–v3.3.13 compatibility, new client/server/transport recovery tests, and dashboard security.
- `python -m py_compile api/schwab_oauth.py api/schwab_client.py server.py` passed.
- Extracted dashboard inline JavaScript passed `node --check`.
- `git diff --check` passed.

Coverage includes proactive token renewal, rejection tracking, late 401s, unchanged-token adoption prevention, expired-token suppression, retry/circuit pauses, HTTP 403 classifications, bot-host diagnostic identity, transport/probe outcomes, concurrent token publication, disk write failure, redirected POST refusal, malformed token payloads, and encoded error-reference redaction.

## Existing broader-suite limitations

The full suite cannot collect three older test modules because main does not contain `MIGRATE_CONFIG_V2_8.py`, `MIGRATE_CONFIG_V2_9.py`, or `MIGRATE_CONFIG_V2_9_1.py`.

A broader run including execution safety, live money guards and daily kill-switch tests produced **9 failures**. An isolated pristine snapshot of the same main commit reproduced exactly those nine failures, with network access prohibited:

- Two reversal tests and the cancel-all test use mock executors that do not accept the current `ticker` keyword.
- Three live-profile checks and one multiasset check require the deployment's untracked `config.yaml`, which is not in this repository.
- One synthetic-data fixture omits `_ticker_processing`.
- One broker-sweep test expects a liquidation call but records none.

These are recorded rather than changing unrelated trading logic or manufacturing a deployment configuration. This release does not claim the entire project suite passes.

## Live verification still required

No client secrets or live account tokens were supplied for this work, and the actual VPS was not accessed. A real Schwab token exchange and broker reconciliation must be checked after deployment. A passing unauthenticated route probe is not a credential verification, and an upstream access denial cannot be removed by unit tests or guaranteed away by this update.
