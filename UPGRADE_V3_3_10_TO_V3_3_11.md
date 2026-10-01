# Upgrade v3.3.10 → v3.3.11

1. Keep the bot PARADO.
2. Verify broker positions/orders directly in Schwab.
3. Back up the project folder.
4. Pull the new main branch or replace the updated code.
5. Do not delete .env, config.yaml, data, models, logs, trade history, or ticker routing.
6. Run: Get-Content .\VERSION
7. Expected: 3.3.11
8. Start with .\START_BOT.bat
9. Refresh the browser with Ctrl+F5.

On Señales, verify:
- Aprendizaje de señales shows pending/training counters and supplemental rows used in the last retrain.
- Estado de procesamiento por ticker lists all monitored tickers and a stage/reason for each.

If Schwab returns HTML Access Denied, the Config diagnostic should show it as temporary and retries should be rate-limited by backoff.
