"""Read-only entry exceptions with separate scopes and unchanged accounting."""


def exception_status(config, guard_path, account, *, now=None, root=None):
    if (config.get("live_trading") or {}).get("reviewed_manual_tsla_resume") is True:
        from core.reviewed_manual_tsla_resume import exception_status as manual_status
        # A failed manual-session review must not fall through to an older grant.
        return manual_status(config, guard_path, account, now=now, root=root)
    from core.historical_manual_resume import exception_status as historical_status
    status = historical_status(config, guard_path, account, now=now, root=root)
    if status.get("historical_resume_active") is True and status.get("entry_blocked") is False:
        return status
    if (config.get("live_trading") or {}).get("archived_accounting_resume") is True:
        from core.archived_accounting_resume import exception_status as archived_status
        return archived_status(config, guard_path, account, now=now, root=root)
    return status
