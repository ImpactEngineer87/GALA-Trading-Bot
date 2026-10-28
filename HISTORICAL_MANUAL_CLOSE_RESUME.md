# GALA10: reviewed manual-close historical exception (October 28, 2026)

The client reports manually intervening in a BA position on October 23, 2026. GALA cannot attribute that historical close to its own order ID. NKE has since been proven and archived; the existing guard still contains exactly three unresolved BA/META entries.

## Guarantees

- Original BA/META accounting records remain unresolved and are never deleted, fabricated or relabeled as verified for AI learning.
- The new entry exemption is DISABLED BY DEFAULT. It requires an explicit live_trading.historical_manual_close_resume: true config setting AND a separate root-owned operator grant.
- The grant binds the original accounting file SHA-256, the exact three unresolved keys, the configured Schwab account fingerprint, the old October 23 source session and the CURRENT NY calendar date.
- The grant is created only after a root-only, stopped-service, manual-scheduler, daily-loss/shutdown, AI-auto-promotion, current-day trade-history and fresh Schwab-flat preflight.
- A changed guard, any new discrepancy, a different broker account, invalid permissions, missing file, or a new NY day revokes the exemption immediately.
- Existing daily-loss kill switches, approved-model gates, working-order and position checks, broker account protection and STOP/TP management remain unchanged.
- The dashboard keeps reporting blocked=true and unresolved=3 for historical accounting even when entry_blocked=false is authorized for today's new entries. Current-day verified risk is separately displayed.

## Readiness and deployment

1. Keep the trading engine OFF using dashboard Stop; independently verify zero live broker positions AND zero working orders. Never blindly stop a bot managing live positions.
2. As root on the live VPS, run scripts/deploy_historical_manual_close_resume.sh with the EXACT pinned SHA of the reviewed candidate. It runs all 464 tests with network disabled BEFORE touching the service, checks live broker state and AI idle, creates private backups, fast-forwards, runs 464 tests as gala, and restarts only the web dashboard. The feature remains OFF.
3. An operator must separately coordinate client authorization. With engine OFF, stop gala.service and verify ActiveState=inactive, MainPID=0.
4. Back up config.yaml. Run .venv/bin/python scripts/authorize_historical_manual_close.py --ack 'I AUTHORIZE OCT23 HISTORICAL ONLY'. The tool writes data/historical_manual_close_resume.json with mode 0640 root:gala; it does NOT change config or start trading.
5. Under explicit operator review, edit ONLY live_trading.historical_manual_close_resume to true in config.yaml (preserve a byte-for-byte original backup); never disable the original accounting guard.
6. Start gala.service as dashboard only. Authenticate to /api/status; require bot.running=false, thread_alive=false, historical resume active, entry_blocked=false, blocked=true, unresolved=3. Reverify fresh broker flat and normal REAL preflight before the operator manually starts the engine.
7. Each following NY session requires a NEW explicitly reviewed grant after separate broker-flat verification and a stopped-service maintenance window; no silent renewal.

This is a narrow remedy for the exact verified NKE archive + known BA/META historical situation, not a general-purpose bypass or proof of why individual historical Schwab fills occurred. Never infer trading correctness from an absence of broker positions.

## Failure handling

If the suite, account or historical scope checks fail, STOP. Never force a new order, modify the guard file, delete old history, turn off daily-loss protection or promote AI models to work around the failure. Real-money execution should be enabled only after the explicit client/operator approval and fresh independent risk checks.

## Post-install fail-closed hotfix

After the first successful source installation, one further safety review identified that an in-process accounting storage/audit error must always block new entries, even if the October 23 historical guard file itself is unchanged. The follow-up candidate adds this explicit override and a fourteenth offline test. Install the latest pinned candidate with the same guarded installer (now supports previous installed source revision a2a30b6) before issuing any historical approval. The opt-in remains OFF by default.
