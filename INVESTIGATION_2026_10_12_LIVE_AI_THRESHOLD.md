# Per-ticker AI threshold, editable while running — v3.3.25

## Client report (October 12)

> The threshold setting on the tickers isn't adjusted because some tickers have
> a very high threshold and block signals. The client must be able to set the
> threshold for each ticker.

The Señales table showed the most AI vetoes on two REAL tickers: META (400)
and TSLA (307). Their x/50 progress had not moved for days.

## Why v3.3.19's per-ticker threshold went unused

The override itself works. `EntrenadorAgentes.evaluar` reads the operator
threshold on every decision. Two things made it unusable in practice:

1. **It required a stopped engine.** `POST/DELETE /api/ai/threshold/<ticker>`
   returned 409 while the bot ran. Stopping the engine runs the kill switch,
   which liquidates open positions. So the threshold could not be adjusted
   during a trading session.
2. **It was hard to reach.** The control sat at the bottom of the "Ver modelo"
   dialog, below the REAL authorization form, and required a written reason.

## Change

- **Live changes.** The API accepts changes while the engine runs. The next AI
  decision for that ticker uses the new value. Open positions, protective
  orders and the model's own threshold are untouched. Each change is still
  written to `data/real_mode_audit.jsonl` with the user, and now also to the
  bot log.
- **Reason optional.** It defaults to "Ajuste desde el dashboard".
- **Control in the ticker table.** Each row of "Tickers y tamaño de las
  órdenes" has an **Umbral IA** box (30–95%), **Aplicar**, and **Usar modelo**
  to clear the override. The note under it shows the model's threshold, the
  manual value, and how many of that ticker's vetoes were **threshold vetoes**.
  It stays editable while the engine runs; mode and share changes still require
  a stopped engine.
- **Threshold veto count.** `/api/ai/status` reports `veto_threshold` per ticker:
  vetoes recorded as `AI_THRESHOLD`. Only these change with the threshold.
  Range/macro hard vetoes, regime and long-only blocks do not.

## What a lower threshold does not change

- **PAPER tickers:** they already approve every signal while
  `umbral_meta_filtro` is 0. The threshold matters for REAL.
- **No PRODUCTION model:** a REAL ticker still cannot trade. The saved value
  applies once a model is loaded.
- **Other blocks still apply:** capital, daily loss and broker checks.

## Validation

- **New Python tests:** `tests/test_v3_3_25_live_ai_threshold.py` (4 tests).
  - A change while running applies to the next decision.
  - Reason is optional; changes are audited and logged.
  - Out-of-range values are refused.
  - Threshold vetoes are counted separately.
- **New dashboard test:** `tests/test_v3_3_25_dashboard_ticker_threshold.cjs`
  covers render, draft, apply, invalid values, reset, refusal and no-model.
- **Updated test:** the v3.3.19 route test (no 409 while running, reason
  optional).
- **Full suite:** `pytest tests` has the same 28 pre-existing failures and 3
  collection errors as before; nothing new. All dashboard VM tests pass.
- **Not run:** the layout was not checked in a browser (no playwright here).
