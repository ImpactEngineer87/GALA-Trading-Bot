# Per-ticker AI threshold control — v3.3.19

## Client request (October 8)

> The AI is too restrictive. For example, it doesn't operate in BA. The
> threshold in META is 69%, and 63% of the signals are disapproved. We should
> lower the ticker threshold so it can operate.

## How the threshold worked until now

- Each ticker's AI threshold is the `decision_threshold` that training chose
  and saved in `data/modelos/modelo_<T>.meta.json`. Training picks it on the
  validation split (`agents/training.py::_seleccionar_umbral`).
- A signal is approved when `probability >= threshold`. In REAL this check
  always applies (`agents/training.py::evaluar`).
- There was no clamp and no per-ticker or global override. The only operator
  control was the manual REAL authorization, which skips the threshold
  entirely for one model version.
- The repository's metadata files show BA at 0.79 and META at 0.62. The
  client's live META value (0.69) comes from their own retrained model.

## Change

- **New threshold store.** `core/ai_threshold_overrides.py` keeps an operator
  threshold per ticker in `data/ai_threshold_overrides.json`, between 30% and
  95%. Every change needs a reason and is written to the REAL audit log
  (`data/real_mode_audit.jsonl`) before it is applied.
- **Applied in the decision.** `evaluar` applies the operator threshold to
  that ticker's PAPER and REAL decisions when a model is loaded. The decision
  snapshot records `threshold` (applied), `model_threshold` and
  `threshold_source: OPERATOR`. The dashboard, signal history and veto
  ledgers therefore show the threshold that was actually used. The model's
  own threshold and files are not changed.
- **Dashboard.** "Ver modelo" → **Umbral IA del ticker** shows the model and
  applied thresholds. It lets the operator set a value (%) with a reason, or
  return to the model's threshold. Changes require the engine to be stopped,
  like other REAL controls. The IA / MODELO column shows `Umbral 60%
  (manual)`. API: `GET/POST/DELETE /api/ai/threshold/<ticker>`.
- **What it does not do.** It only moves the probability cut-off. These
  blocks still apply and are often the larger share of "VETO / OM-R" signals:
  - **Hard vetoes:** neutral daily macro and lateral range.
  - **Regime policy:** market-regime vetoes, where enforced.
  - **REAL long-only:** a SELL from flat is blocked in REAL when
    `live_trading.allow_short` is false.
  - **No production model:** in REAL, a ticker showing **"En validación"** has
    only a SHADOW model and cannot trade until promoted.
  - **Other checks:** capital, daily loss, and broker checks.

## Choosing values

The "63%" figure counts every AI veto type, not only threshold vetoes. Before
lowering a threshold, check each ticker's veto reasons in Señales
(`AI_VETO:<reason>`):
- `AI_THRESHOLD` vetoes: a lower threshold approves them.
- `HARD_VETO:*`, regime or long-only blocks: a lower threshold does not
  change them.

For BA, first check whether the IA / MODELO column says "En validación" or
"Producción". A lower threshold also admits signals the model considered
weaker. Watch BA and META in PAPER, or with small REAL size, after changing
it.

## Verification

New tests:
- `tests/test_v3_3_19_ai_threshold_override.py` (9 tests): the store (bounds,
  persistence, unreadable file, failed write), the decision (a lower or
  stricter threshold, a missing model still blocked in REAL, PAPER
  observation unchanged), and the API (set, clear, audit, refusals while
  running or out of range).
- `tests/test_v3_3_19_dashboard_ai_threshold.cjs`: the dialog section, save
  and clear.

The existing AI threshold tests still pass.
