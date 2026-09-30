# Upgrade v3.3.8 → v3.3.9

## Purpose
v3.3.9 adds dashboard/API visibility for the signal-learning layer introduced in v3.3.7. Trading logic is unchanged.

## Upgrade steps
1. Keep the bot **PARADO** and close the dashboard/server process.
2. Back up the current project folder.
3. Extract the v3.3.9 patch over the existing v3.3.8 installation.
4. Choose **Replace files in destination** when Windows asks.
5. Do not delete or replace `.env`, `config.yaml`, `data/`, `models/`, logs, trade history, or ticker routing.
6. Verify:
   ```powershell
   Get-Content .\VERSION
   ```
   Expected: `3.3.9`.
7. Start:
   ```powershell
   .\START_BOT.bat
   ```
8. Refresh the browser with `Ctrl+F5`.
9. Open **Señales del Bot** and review **Visibilidad · Aprendizaje de señales**.

## What you should see
The new card shows per-ticker x/50 progress, valid executed history, AI vetoes, missed signals, open observations, TP/SL counterfactual outcomes, avoided losses, blocked winners, supplemental FIT eligibility, exclusions, and recent observations.

## Data behavior
No migration is needed. Existing `data/signal_learning_observations.csv` data is read immediately. If the file is new/empty, the visibility card starts at zero and populates as new non-executed signals are observed.

## Safety
The visibility endpoint and dashboard are read-only. v3.3.9 does not change orders, thresholds, models, routing, stops, take profits, or REAL authorization.
