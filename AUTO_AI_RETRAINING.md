# Auto AI controls — v3.3.12

The Signals page now has a persistent **Activar / Desactivar Auto IA** button.
An OFF label reports the saved automatic-retraining setting; it does not mean
the manual worker failed. Manual **Reentrenar pendientes** remains available
with Auto IA OFF, provided the engine is stopped and tickers genuinely qualify.
Installing this update preserves the current ON/OFF setting.

The automatic worker checks every 60 seconds, independently of the trading
scheduler. It works with the scheduler in `manual` mode. Training can start on
Monday–Friday after 16:15 America/New_York, or the configured session end / Auto
AI start time if later, while the trading engine is stopped and no local
positions remain pending. It never starts or stops the trading engine. This
is a weekday time window, not an exchange holiday calendar. Enabling Auto IA
does not start training immediately during the trading session.

Only tickers returned by the existing execution-valid x/50 eligibility check
are queued. The switch does not reset counts, create trades or change data
eligibility, model artifacts, validation gates, risk limits or trading mode.
Data-ready 17/17, READY_REVIEW and sufficient total historical samples do not
mean a ticker has 50 **new** eligible closes. With zero pending tickers, waiting
is expected. Resolved non-executed signals retain their reduced-weight FIT-only
role; they cannot supply missing real fills or become VALIDATION/TEST closes.

The normal worker records TRAINING and each evaluated / rejected / deferred
result, including actual FIT/VALIDATION/TEST counts and reasons. Completed
evaluations consume their batch through the existing lifecycle. Insufficient
data leaves counts pending; the same unchanged deferred batch is not retried
every minute within one running service. New source data / eligible tickers or
the next weekday session permit another check. Restarting the service clears
this in-memory retry suppression, not the persisted lifecycle or x/50 state.

Status explains OFF, waiting for the session, an active engine, sufficient new
closes, an occupied worker, or new data. A queued job is not an accepted model.
The dashboard keeps the trainer instance while a worker updates artifacts, and
reserves a batch before starting its thread to prevent duplicate workers.
Engine startup waits for training to finish. Switching Auto IA OFF prevents
subsequent automatic tickers; an already training ticker may complete normally.

All automatic and dashboard-manual training is SHADOW-only, including legacy
configurations containing `auto_activate_production: true`. Passing production
gates is evidence for review, never automatic permission to activate. Existing
production models and the explicit production approval process remain separate.

Authenticated GET `/api/ai/automatic` reports policy and worker state.
POST accepts exactly `{"enabled": true}` or `{"enabled": false}` and requires
the existing session plus `X-CSRF-Token`. It atomically saves the setting in
`config.yaml`, preserving file permissions, and fixes `only_after_session: true`
and `auto_activate_production: false`. Other configuration values, secrets and
runtime/history files are preserved. YAML formatting/comments may be normalized.
`/api/ai/status` also includes `auto_ai_policy` and `auto_ai_service`.

Optional scheduling configuration under
`agents.entrenamiento.automatic_retraining.not_before_ny` uses `HH:MM` NY time;
values earlier than 16:15 cannot move the worker into the trading session.
Keep the existing `trigger_samples`, state paths and validation settings.

Before VPS activation, use the controlled-maintenance procedure in
`DASHBOARD_DATA_PROVENANCE.md`: fresh broker position/order checks, private
backup, fast-forward source update, readable source for service user `gala`,
and the full offline regression suite before starting the service. Do not make
`.env` or `config.yaml` public to resolve source-file permissions. This release
does not reconcile old unverified trades or authorize REAL trading.
