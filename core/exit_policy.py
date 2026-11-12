"""Explicit REAL exit policy; PAPER keeps the configured partial exits."""


def real_exit_policy(config):
    value = (config.get("live_trading") or {}).get("real_exit_policy", "CONFIGURED")
    if value not in ("CONFIGURED", "FULL_POSITION", "LINKED_70_30"):
        raise ValueError("live_trading.real_exit_policy must be CONFIGURED, FULL_POSITION or LINKED_70_30")
    return value


def linked_partial_enabled(config):
    return real_exit_policy(config) == 'LINKED_70_30' and effective_partial_enabled(config, paper=False)


def effective_partial_enabled(config, *, paper):
    if not paper and real_exit_policy(config) == "FULL_POSITION":
        return False
    return bool((config.get("salida_parcial") or {}).get("activo", True))
