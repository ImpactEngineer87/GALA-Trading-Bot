# Chocoletra GALA10 v3.3.4 — Accepted Model Score Clarity

v3.3.4 is an observability/UI hotfix on top of v3.3.3. It does **not** change GALA10 signal generation, model training mathematics, acceptance thresholds, production gates, PAPER/REAL routing, order execution, risk controls, or any model file.

## Why this release exists

Operators were interpreting a later `REJECTED / FAIL` retraining row as if the previously accepted SHADOW model had lost its historical score or had been downgraded. Those are different measurements:

- **BA aceptada SHADOW**: the Balanced Accuracy stored with the currently installed SHADOW artifact, measured on the historical evaluation window where that model was accepted.
- **Candidate BA**: the newest candidate measured on the newest TEST window.
- **Shadow Inc. BA**: the currently installed SHADOW model re-evaluated on that *same newest TEST window*, so it is directly comparable to Candidate BA.
- **Gate PROD**: the production gate result for the newest candidate only.

A rejected new candidate does not overwrite the installed accepted model.

## Changes

1. Adds a green **BA aceptada SHADOW** column to the per-ticker AI lifecycle table.
2. Renames the ambiguous `Shadow` result column to **Último candidato**.
3. Keeps the accepted score green across later rejected retraining runs because it belongs to the installed SHADOW artifact, not the latest candidate.
4. The AI status API now exposes the installed SHADOW snapshot separately:
   - `accepted_shadow_balanced_accuracy`
   - `accepted_shadow_validation_date`
   - `accepted_shadow_training_date`
   - `accepted_shadow_threshold`
   - `accepted_shadow_model_version`
5. `/api/ai/history/<ticker>` now returns the installed `shadow_model` snapshot in addition to production data.
6. Model details show the accepted SHADOW BA and acceptance-window date with an explanation that a later rejected candidate does not replace it.
7. Dashboard help text now explains historical accepted BA versus same-window re-evaluation BA.

## Example

If BA previously had an accepted SHADOW score of 57% and the next retraining produces:

- Accepted SHADOW historical BA: **57.00%**
- Latest Candidate BA: **46.09%**
- SHADOW incumbent on the new same TEST: **53.59%**
- Latest candidate: **REJECTED**
- Production gate: **FAIL**

v3.3.4 displays the 57% accepted score independently in green. The 46.09% / 53.59% comparison remains visible as the current validation experiment. The accepted model is not replaced by the rejected candidate.

## Version

- Previous: `3.3.3`
- Current: `3.3.4`
- No configuration migration required.
