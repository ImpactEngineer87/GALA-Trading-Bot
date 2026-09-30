# AI Feature Schema v3 — Safe Rerun

This patch does not change the GALA10 BUY/SELL equations. It adds causal
closed-candle context to the AI veto model and blocks cold-start execution in
real mode.

## What changed

- GALA10 remains the only entry trigger.
- The veto model now receives normalized histogram, EMA slope, breakout,
  candle-quality, volatility-regime and session-time context from the same
  closed 1-minute candle that produced the signal.
- Training and live inference use the same extraction function.
- Model metadata uses `feature_version: 3`; older or corrupt artifacts are
  moved to an `.incompatible_<UTC timestamp>` archive path.
- Real mode requires an approved, compatible and loaded model per ticker.
  Paper/backtest remains available in cold start.
- `AUDITAR_FEATURES_IA.py` reports missing, constant and drifting features.

## Local validation sequence

Run from the project root:

```powershell
.\.venv\Scripts\Activate.ps1
python -m compileall -q .
python -m unittest discover -s tests -p "test_*.py" -v
```

The overlay does not replace your local `config.yaml`. Confirm this key exists
under `agents` -> `entrenamiento` (the code also defaults it to `true`):

```yaml
requerir_modelo_aprobado_en_real: true
```

Because the feature schema changed, the old feedback cannot be mixed with the
new data. `FeedbackStore` archives it automatically on first initialization,
but making an explicit audit backup is recommended:

```powershell
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backup = "data\ai_schema_v2_backup_$stamp"
New-Item -ItemType Directory -Force "$backup\models" | Out-Null

if (Test-Path "data\agentes_feedback.csv") {
    Copy-Item "data\agentes_feedback.csv" $backup
}
Get-ChildItem "data\modelos" -File -ErrorAction SilentlyContinue |
    Copy-Item -Destination "$backup\models"
```

Generate the fresh schema-v3 feedback and candidates:

```powershell
python PRE_ENTRENAR.py 2>&1 |
    Tee-Object -FilePath "logs\pre_entrenar_feature_v3_$stamp.log"
```

Then audit feature quality before interpreting model performance:

```powershell
python AUDITAR_FEATURES_IA.py

Import-Csv data\reportes\auditoria_features_latest.csv |
    Where-Object status -ne "OK" |
    Format-Table ticker,feature,missing_pct,unique_values,standardized_mean_shift,status -Auto
```

Finally inspect the model gates:

```powershell
Import-Csv data\reportes\metricas_modelos_latest.csv |
    Format-Table ticker,balanced_accuracy,precision_lift_pp,recall,approval_rate_pct,perdedoras_filtradas_pct,activado,modelo_cargado,feature_version,top_features -Auto
```

Do not lower acceptance thresholds to force a model through. If no candidate
passes, keep real mode disabled and use the feature-audit report to decide the
next experiment without reusing the final test block for tuning.
