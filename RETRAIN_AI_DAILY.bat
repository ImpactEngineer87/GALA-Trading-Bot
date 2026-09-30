@echo off
title GALA10 Bot - Daily AI Validation
cd /d "%~dp0"

echo ============================================================
echo  GALA10 - DAILY PER-TICKER MODEL VALIDATION
echo ============================================================
python -m agents.retrain_models

if errorlevel 1 (
  echo No model was activated. Review the console and clean dataset.
  exit /b 1
)

echo Report: data\reportes\metricas_modelos_latest.csv
exit /b 0
