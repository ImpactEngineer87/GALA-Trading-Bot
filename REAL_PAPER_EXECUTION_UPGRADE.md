# REAL/PAPER execution upgrade

Set `live_trading.real_exit_policy: FULL_POSITION` to use the existing full-size
STOP and serialized full-position exit flow for REAL. PAPER continues using
`salida_parcial` (including its configured 70/30 split). The default policy,
`CONFIGURED`, retains the existing refusal of unsupported REAL partial exits.
This option does not change saved ticker selections or quantities, authorize a
model, or bypass account, capacity, order, fill, or daily-loss checks.

The dashboard reports entry blockers separately from saved REAL/PAPER routing.
Its accounting banner follows `entry_blocked`; unresolved historical records
remain visible under an active reviewed exception. PnL freshness uses the price
observation time, while retaining the source candle timestamp for inspection.

The exact seven-record October accounting archive has a separate, explicit
operator release. It retains the guard, original archive, history, training and
unverified PnL exclusions. A root-owned grant binds the current guard bytes,
configured account and New York date after fresh empty positions and pending
order checks. Any new or changed guard record, invalid grant, different account
or next New York date blocks REAL again. There is no automatic renewal.

With the engine stopped normally and `gala.service` already inactive, run:

```bash
.venv/bin/python scripts/authorize_archived_accounting_resume.py \
  --ack 'I AUTHORIZE ARCHIVED ACCOUNTING FOR THIS NY SESSION' \
  --reviewer 'authorized operator'
```

Enable `live_trading.archived_accounting_resume: true` only for this reviewed
release. On a later session, `--renew-expired` requires the same stopped-service
and fresh broker checks and preserves the previous grant in a private backup.
Never remove accounting state to renew it. Restart the dashboard, verify the
grant and REAL/PAPER plans, and use normal startup preflight. A running engine's
startup quarantine remains in force until a controlled engine restart.
