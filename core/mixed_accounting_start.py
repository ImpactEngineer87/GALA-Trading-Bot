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
    from datetime import datetime, timedelta, timezone
    from urllib.parse import quote
    from api.schwab_client import TRADER_URL

    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            broker = client_factory(config)
            if not broker._asegurar_token():
                return False
            account_hash = broker.obtener_account_hash(str(account_number).strip())
            if not account_hash:
                return False
            positions = broker.obtener_posiciones(account_hash)
            working_orders = broker.obtener_ordenes_abiertas(account_hash)
            # WORKING-only endpoints can miss PENDING_ACTIVATION and other
            # pending statuses. Require a second unfiltered snapshot.
            now = datetime.now(timezone.utc)
            fmt = "%Y-%m-%dT%H:%M:%S.000Z"
            response = broker._get_autorizado(
                TRADER_URL + "/accounts/" + quote(str(account_hash), safe="") + "/orders",
                params={
                    "fromEnteredTime": (now - timedelta(days=59)).strftime(fmt),
                    "toEnteredTime": (now + timedelta(days=1)).strftime(fmt),
                    "maxResults": 500,
                },
                timeout=20,
            )
        if (not isinstance(positions, list) or positions
                or not isinstance(working_orders, list) or working_orders
                or response is None or response.status_code != 200):
            return False
        orders = response.json()
        if not isinstance(orders, list) or len(orders) >= 500:
            return False  # Truncated order list is not proof of flatness.
        terminal = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED"}
        pending = list(orders)
        while pending:
            order = pending.pop()
            if not isinstance(order, dict) or order.get("status") not in terminal:
                return False
            children = order.get("childOrderStrategies") or []
            if not isinstance(children, list):
                return False
            pending.extend(children)
        return True
    except Exception:
        return False
