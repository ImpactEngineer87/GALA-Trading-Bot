# GALA10 v3.3.8 — Schwab 403 + AI x/50 diagnostic hotfix

## Scope (review before deploying)
- Keep the original \`restore-initial-version\` branch untouched.
- No entry/exit equations, REAL/PAPER routes, order placement, cancels,
  position reconciliation, kill switch or trade risk gates are changed.
- Use one shared (per Python process) pacing guard for Schwab **market-data**
  GETs from bot and Flask dashboard; default gap 0.20 s, configurable with
  \`SCHWAB_MARKET_MIN_INTERVAL_SEC\`.
- Respect Retry-After seconds on market-data HTTP 429, with bounded cooldown.
- For market-data HTTP 403, pause market-data reads for 20 s and record safe
  HTTP status/endpoint/request id. **403 is not proof of rate limiting.**
- The dashboard must **not** refresh/rotate OAuth tokens after HTTP 403.
  A **401** may still trigger one controlled refresh.
- Broker trading and critical reconciliation requests are deliberately not
  gated by this market-data guard, and retain fail-closed behavior.

## Why NVDA can show 25/50 despite BUY and SELL markers
Those chart markers are strategy signals, not necessarily matched,
execution-confirmed open/close pairs. The model's new-observations counter
counts \`execution_valid=1\` closed feedback rows since the last training
attempt, after removing ineligible or incomplete executions.

A BUY and SELL marker **must not** add two to the counter. They may
describe a single open/close, two signals filtered before fill, or neither.
Inspect **Trades en Vivo**, order audit and feedback before calling it
a counter bug. The diagnostic intentionally does not modify the data.

## VPS validation (PowerShell in repository root)
1. Stop the bot when it is safe to do so. **Review REAL open positions and
   outstanding orders first**; the normal Stop action may trigger configured
   liquidation/kill-switch behavior. Do not indiscriminately stop a live bot
   with broker exposure without confirming the safety plan.
2. Back up .env, config.yaml, data/, logs/, and any running model artifacts
   outside Git (especially \`data/modelos/*.pkl\`, shadow models and
   \`data/agentes_feedback.csv\`). Do not commit secrets or backup data.
3. Fetch and inspect:
   \`\`\`powershell
   git fetch origin
   git switch --track origin/fix/schwab-403-ai-counter-audit-v338
   git diff restore-initial-version...HEAD --stat
   \`\`\`
4. Inspect NVDA counter in read-only mode:
   \`\`\`powershell
   python AUDITAR_CONTADOR_IA.py --ticker NVDA
   \`\`\`
   Check \`execution_valid_closed\`, \`excluded_closed\`,
   \`exclusion_reasons\`, \`last_attempt_count\` and
   \`new_observations_x50\`. Compare the relevant entry/exit order IDs in
   local order audit, Schwab broker history and
   \`data/historial_operaciones.csv\`.
5. Run static and offline tests:
   \`\`\`powershell
   python -m compileall -q api server.py AUDITAR_CONTADOR_IA.py
   python -m unittest discover -s tests -p "test_schwab_market_guard.py" -v
   \`\`\`
   Then run the existing repository regression suite as appropriate
   (particularly OAuth, order sync, execution safety and counter lifecycle).
6. Verify PAPER first and watch logs for
   \`[Schwab market-data] HTTP 403/429\`. Capture the **HTTP status**,
   affected **endpoint**, **request ID** and **Retry-After**, if provided.
   Do not paste tokens, secrets or unredacted account details.
7. Before resuming REAL execution, confirm working Schwab OAuth,
   actual market-data freshness and valid model gate; independently
   confirm broker positions, open orders and protective exits. A 403 can
   require checking developer-portal app permissions/approval.

### Configuration (optional .env)
\`\`\`ini
SCHWAB_MARKET_MIN_INTERVAL_SEC=0.20
SCHWAB_MARKET_403_COOLDOWN_SEC=20
SCHWAB_MARKET_429_COOLDOWN_SEC=30
\`\`\`
If the VPS runs the Flask dashboard and bot in different Python processes,
the guard is **per process**; coordinate limits separately or use a
shared external rate limiter. If 403 persists even at low request volume,
inspect endpoint/app permissions or Schwab status rather than blindly
slowing calls, refreshing tokens or raising execution thresholds.

### Rollback
Switch back to \`restore-initial-version\` only after normal trading
shutdown and broker-state verification. Restore the copied runtime
\`data/\`/config only if they were actually changed. Do not reset
models/history just to revert this source-only fix.
