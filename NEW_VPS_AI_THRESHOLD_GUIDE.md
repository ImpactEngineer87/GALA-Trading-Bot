# GALA Trading Bot: manual AI thresholds and NEW VPS checkout (Oct 19, 2026)

## Scope of this change

- Each monitored ticker's **loaded PRODUCTION model** has its own operator threshold control.
- Dashboard: **Tickers y tamaño de las órdenes** -> **IA / modelo** -> **Umbral IA %** -> **Aplicar IA**, or **Usar modelo** to clear the override.
- Range: **30%-95%** in whole percent steps (e.g. 60 for 0.60). The model's trained threshold is not rewritten.
- Overrides are saved to `data/ai_threshold_overrides.json`, audited to `data/ai_threshold_overrides_audit.jsonl`, and applied to **future** model decisions without stopping the engine. An override is tied to the current model's SHA identity and becomes inactive when production changes.
- Dashboard `/api/ai/status`: `production_threshold` (trained), `manual_threshold` (active only), `effective_threshold`, `threshold_source`.
- GET/POST/DELETE `/api/ai/threshold/<ticker>` require existing session authorization; mutating calls also require the session CSRF token. Never expose this API directly to the internet without TLS and access controls.
- Does not alter existing positions, broker orders, quantity limits, entry/exit rules, model promotion, auto-training criteria, REAL/PAPER modes, or risk gates.

## Existing training, not a guarantee

`/api/ai/status` already reports per ticker: valid REAL/PAPER closures, supplemental resolved signals, excluded rows, x/50 readiness, model installed/loaded state, FIT/validation/test data, economic gates, and latest training errors/rejections. A claimed 400+ signals **across all tickers** does not mean each ticker has 50 new eligible closed trades. Signals with hypothetical, unresolved or ambiguous outcomes must not be counted as verified broker closures. The new threshold override does **not** force retraining or silently promote a candidate.

Before claiming the AI is working correctly, confirm the following in **PAPER**: per-ticker state is updated after truly closed broker/PAPER trades; eligible counts and quarantine make sense; pending tickers retrain after the NY session if enabled; accepted SHADOW artifact appears; PRODUCTION model version is loaded, and the effective threshold matches the next decision audit. Compare holdout performance **after costs**; do not equate training success with positive returns.

## New VPS: safe Git setup

> Never run two REAL trading engines against the same Schwab account. Before changing services, independently confirm positions, open/working orders, and protective exits on Schwab. Do not stop the active engine merely to change AI thresholds: its stop procedure may close positions.

If the copied bot directory already contains a `.git` folder:

```bash
cd /path/to/GALA-Trading-Bot
git status --short
git remote -v
git remote set-url origin https://github.com/ImpactEngineer87/GALA-Trading-Bot.git
git fetch origin
git branch --show-current
git log -1 --oneline
```

If `origin` does not exist, use `git remote add origin https://github.com/ImpactEngineer87/GALA-Trading-Bot.git` instead. Review `git status` first: preserve all local edits and untracked runtime files. Do **not** run `git reset --hard`, `git clean -fd`, or copy the new Git checkout over live `data/`, `logs/`, `.env`, or `config.yaml`.

If there is no `.git`, clone into a **separate** directory without touching the currently running copy:

```bash
cd /opt
git clone https://github.com/ImpactEngineer87/GALA-Trading-Bot.git GALA-Trading-Bot-review
cd GALA-Trading-Bot-review
git fetch origin
```

Review this feature branch in the separate checkout:

```bash
git switch --track origin/feature/operator-ai-threshold-20261019
git status --short
python3 -m compileall -q core/ai_threshold_override.py agents/training.py api/v330_routes.py engine/bot_engine.py server.py
python3 -m unittest discover -s tests -p 'test_manual_ai_threshold.py' -v
```

To run the tests, first install dependencies from `requirements.txt` in a virtual environment if absent. In a clean development checkout, also run the existing suite and review failures before any production deployment. Do not paste Schwab credentials into Git or commit runtime data. After test completion, merge the reviewed PR to `master` through the normal process, and only then update the **one** active service during an agreed maintenance window. Use `git pull --ff-only origin master` in a clean checkout to obtain merged code; a copied working tree may require manual migration.

To discover the actual service name **without stopping anything**:

```bash
systemctl list-units --all --type=service | grep -Ei 'gala|trading|bot'
ps aux | grep -E '[p]ython.*(server|main|bot)'
```

Do not invent a systemd unit name; check `systemctl cat <ACTUAL_UNIT_NAME>` for the correct `WorkingDirectory` and interpreter. Persistent paths are relative to that working directory. Back up `.env`, `config.yaml`, `data/` (including models/feedback/threshold files) and service settings before changing deployments. **Never copy production tokens into source control.**

## Schwab order-limit verification (NOT modified in this branch)

The existing `api/hybrid_order_executor.py` computes `_submitted_limit_price()`: a normal LIMIT uses the signal limit, while marketable-limit intent applies a configured slippage cap. `/api/execution-diagnostics` displays submitted limit vs executed fill. This feature branch deliberately leaves the order-placement algorithm unchanged because it has **not** been checked against the actual failing Schwab order, current broker positions, or the new VPS logs.

Before accepting REAL operation, compare `orderType`, `price`, `quantity`, `instruction`, and order IDs against read-only Schwab order details and the audit for a failing order; confirm partial fills, cancellation states, working orders and protective exits are reconciled. Test PAPER before REAL; do not assert the order-limit problem is fixed based solely on this threshold upgrade.

## Manual API example

Dashboard handles CSRF automatically. For authorized code, `POST /api/ai/threshold/AAPL` body:
```json
{"model_version":"CURRENT_MODEL_VERSION_FROM_STATUS","threshold":0.60,"reason":"Operator review"}
```

Use `DELETE /api/ai/threshold/AAPL` to restore the loaded model threshold. A stale model version is rejected.
