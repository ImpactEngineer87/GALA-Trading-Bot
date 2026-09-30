@echo off
:: ============================================================
:: COMPATIBILIDAD: reentrenamiento manual de modelos GALA10 por ticker.
:: El motor también lo programa al final de sesión y cada N operaciones.
:: ============================================================

title GALA10 Bot — AI Retraining
color 0E

cd /d "%~dp0"

echo.
echo  ============================================================
echo   GALA10 — PER-TICKER MODEL VALIDATION
echo  ============================================================
echo.

echo Evaluating clean feedback with volume/liquidity features...
python -m agents.retrain_models

echo.
echo  ============================================================
echo   Finished. Review data\reportes\metricas_modelos_latest.csv
echo  ============================================================
echo.
pause
