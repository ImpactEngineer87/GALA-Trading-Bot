# AI Veto Policy v3 — Safe Rerun

The first volume-aware report exposed a class-imbalance failure: the models
vetoed 95.91% of winners and achieved lower accuracy than an "always veto"
baseline. Those policy-v2 models must not be used for live decisions.

## What v3 changes

- Chronological FIT / VALIDATION / TEST split.
- Class weighting calculated only from FIT.
- Per-ticker probability threshold selected only on VALIDATION.
- Final metrics calculated only on untouched TEST.
- Activation requires balanced accuracy, winner recall, precision enrichment,
  a useful approval rate and loser filtering.
- Policy-v2 metadata is incompatible and will not load.
- A global `umbral_meta_filtro: 0` still disables vetoes without disabling
  GALA or the contextual agents.

## Apply and verify

1. Stop the bot.
2. Back up `data/modelos`, `data/reportes` and `data/agentes_feedback.csv`.
3. Overlay the v3 patch in the development project.
4. Run:

   ```powershell
   python -m compileall -q .
   python -m unittest discover -s tests -p "test_*.py" -v
   ```

   Expected: 16 tests, `OK`.

5. Re-evaluate the existing clean dataset; do not replay two years again:

   ```powershell
   python -m agents.retrain_models
   ```

6. Review:

   ```text
   data/reportes/metricas_modelos_latest.csv
   ```

It is valid for strict v3 to reject every model. A rejected model is safer than
an active model that obtains high raw accuracy by vetoing almost everything.
Only tickers with `activado=True`, `modelo_cargado=True` and a useful untouched
TEST result may participate in the learned veto.

Keep `risk.paper_trading: true` until GALA10 parity and Schwab market-session
validation are complete.
