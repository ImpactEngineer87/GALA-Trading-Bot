"""Fail-closed mixed-mode startup when REAL accounting is unresolved.

This is a PAPER continuity mechanism, NOT an approval to place REAL orders.
Do not edit routing, accounting records or operator approvals.
"""
from __future__ import annotations

import json
from pathlib import Path


def unresolved_real_accounting(config, root, account_number):
    """Return (quarantine_all_real, reason) from fresh on-disk evidence.

    Even an unreadable guard requires a REAL quarantine. An explicitly approved
    historical exception is the only permitted bypass, and the normal REAL
    preflight must still pass before a REAL ticker can enter.
    """
    from core.historical_manual_resume import exception_status
    from core.project_paths import resolve_runtime_path

    try:
        root = Path(root)
        configured = (config.get("execution_engine") or {}).get(
            "accounting_state_path", "data/real_accounting_reconciliation.json")
        path = Path(resolve_runtime_path(configured, project_root=root))
        if not path.is_file() or path.is_symlink():
            return True, "ACCOUNTING_GUARD_UNAVAILABLE"
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            return True, "ACCOUNTING_GUARD_INVALID"
        unresolved = state.get("unresolved")
        if not isinstance(unresolved, dict) or any(
            not isinstance(value, dict) for value in unresolved.values()
        ):
            return True, "ACCOUNTING_GUARD_INVALID"
        if not unresolved:
            return False, "ACCOUNTING_CLEAN"
        status = exception_status(
            config, path, account_number, root=root)
        if (status.get("historical_resume_active") is True
                and status.get("entry_blocked") is False):
            return False, "EXACT_HISTORICAL_EXCEPTION_APPROVED"
        return True, "REAL_ACCOUNTING_UNRESOLVED"
    except Exception:
        return True, "ACCOUNTING_GUARD_UNAVAILABLE"


def broker_confirmed_flat(config, account_number, client_factory=None):
    """Fresh Schwab GET confirmation; any failure is NOT flat.

    OAuth may refresh authentication tokens, but there are no order writes.
    Do not let an account with existing exposure silently become PAPER-only:
    existing REAL positions require live protective management.
    """
    import contextlib
    import io
    if not str(account_number or "").strip():
        return False
    if client_factory is None:
        from api.schwab_client import SchwabClient
        client_factory = SchwabClient
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            broker = client_factory(config)
            if not broker._asegurar_token():
                return False
            account_hash = broker.obtener_account_hash(str(account_number).strip())
            if not account_hash:
                return False
            positions = broker.obtener_posiciones(account_hash)
            orders = broker.obtener_ordenes_abiertas(account_hash)
        return (isinstance(positions, list) and len(positions) == 0
                and isinstance(orders, list) and len(orders) == 0)
    except Exception:
        return False
