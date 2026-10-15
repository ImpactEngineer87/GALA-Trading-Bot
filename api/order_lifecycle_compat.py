"""Adapt confirmed cancellation methods without repeating broker mutations.

A successful DELETE only acknowledges a request.  This adapter therefore uses
the executor's confirmed lifecycle method and never falls back to the legacy
boolean cancellation API.
"""

import inspect


_CANCEL_TERMINAL_STATES = {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}


def _unconfirmed(error):
    return {
        "confirmado": False,
        "broker_status": "DESCONOCIDO",
        "latency_ms": 0,
        "timeout": False,
        "filled": 0.0,
        "error": error,
        "solicitud_aceptada": False,
        "terminal_by_absence": False,
    }


def cancelar_orden_compatible(ejecutor, account_number, order_id, ticker=""):
    """Return confirmed lifecycle evidence, or an explicitly uncertain result.

    Older confirmed methods may omit ``ticker``.  Inspect their signature before
    calling: catching a TypeError from the method and retrying could submit a
    second cancellation after the first already reached the broker.
    """
    method = getattr(ejecutor, "cancelar_orden_confirmada", None)
    if not callable(method):
        return _unconfirmed(
            "Executor has no broker-confirmed cancellation method; "
            "unverified cancellation was not submitted."
        )

    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return _unconfirmed(
            "Cannot inspect the confirmed cancellation method; "
            "cancellation was not submitted."
        )

    kwargs = {"ticker": ticker}
    try:
        signature.bind(account_number, order_id, **kwargs)
    except TypeError:
        kwargs = {}
        try:
            signature.bind(account_number, order_id)
        except TypeError:
            return _unconfirmed(
                "Unsupported confirmed cancellation signature; "
                "cancellation was not submitted."
            )

    try:
        evidence = method(account_number, order_id, **kwargs)
    except Exception as exc:
        # The request may already have reached the broker.  Do not retry or use
        # another method; the engine must retain its state and reconcile it.
        return _unconfirmed(f"Cancellation could not be confirmed: {exc}")

    if not isinstance(evidence, dict):
        return _unconfirmed(
            "Confirmed cancellation method returned invalid lifecycle evidence."
        )

    result = _unconfirmed("")
    result.update(evidence)
    status = evidence.get("broker_status")
    status = status.strip().upper() if isinstance(status, str) else ""
    result["broker_status"] = status or "DESCONOCIDO"
    valid = isinstance(evidence.get("confirmado"), bool) and bool(status)
    terminal = status in _CANCEL_TERMINAL_STATES or (
        status == "NOT_WORKING" and evidence.get("terminal_by_absence") is True
    )
    result["confirmado"] = valid and evidence.get("confirmado") is True and terminal
    if not result["confirmado"] and not result.get("error"):
        result["error"] = (
            "Cancellation has not been confirmed by valid broker lifecycle evidence."
        )
    return result
