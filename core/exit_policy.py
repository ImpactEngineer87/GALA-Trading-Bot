"""Explicit REAL exit policy; PAPER keeps the configured partial exits."""


def real_exit_policy(config):
    value = (config.get("live_trading") or {}).get("real_exit_policy", "CONFIGURED")
    if value not in ("CONFIGURED", "FULL_POSITION"):
        raise ValueError("live_trading.real_exit_policy must be CONFIGURED or FULL_POSITION")
    return value


def effective_partial_enabled(config, *, paper):
    if not paper and real_exit_policy(config) == "FULL_POSITION":
        return False
    return bool((config.get("salida_parcial") or {}).get("activo", True))
