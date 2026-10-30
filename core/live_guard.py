"""Fail-closed validation for deliberate real-money GALA10 sessions.

v2.9.2 validates only the tickers that are explicitly routed to REAL.  PAPER
tickers may continue running in the same engine and keep accumulating feedback.
The validator itself is side-effect free; broker callbacks must be read-only.
"""

from __future__ import annotations
from core.live_capacity import max_real_tickers
from core.exit_policy import effective_partial_enabled, real_exit_policy

from typing import Callable, Iterable, Optional, Tuple


def _ticker_list(values: Iterable[str]):
    return list(dict.fromkeys(
        str(value).strip().upper()
        for value in (values or [])
        if str(value).strip()
    ))


def _unsupported_real_partial_exit(cfg: dict, shares: int) -> bool:
    """A standalone TP1 + full STOP for >=3 shares over-closes at Schwab.

    GALA's risk manager defaults salida_parcial.activo to True. Keep this
    preflight consistent with its default and fail closed until a vetted
    broker-linked 70/30 protective-order strategy replaces the SINGLE orders.
    """
    return effective_partial_enabled(cfg, paper=False) and int(shares) >= 3


def validate_live_start(
    cfg: dict,
    *,
    get_env: Callable[[str, str], str],
    account_number: str = "",
    model_checker: Optional[Callable[[list], list]] = None,
    broker_checker: Optional[Callable[[str, str], dict]] = None,
    real_tickers: Optional[Iterable[str]] = None,
    ticker_settings: Optional[dict] = None,
) -> Tuple[bool, list, list]:
    """Validate REAL-routed tickers and return ``(ok, errors, real_tickers)``."""
    live_cfg = cfg.get("live_trading", {}) or {}
    risk_cfg = cfg.get("risk", {}) or {}
    sched_cfg = cfg.get("scheduler", {}) or {}
    ent_cfg = (cfg.get("agents", {}) or {}).get("entrenamiento", {}) or {}
    ks_cfg = cfg.get("kill_switch", {}) or {}
    daily_cfg = ks_cfg.get("daily_loss", {}) or {}

    errors = []
    configured_allowlist = _ticker_list(live_cfg.get("allowed_tickers", []))
    active_real = _ticker_list(
        configured_allowlist if real_tickers is None else real_tickers
    )
    max_tickers = max_real_tickers(live_cfg)
    max_shares = max(1, int(live_cfg.get("max_shares_per_order", 1)))
    per_ticker = bool(live_cfg.get("per_ticker_execution", False))

    # No ticker routed to REAL = nothing can reach the broker.  PAPER operation
    # remains safe even when the master mixed-mode switch is enabled.
    if not active_real:
        return True, [], []

    try:
        real_exit_policy(cfg)
    except ValueError as exc:
        return False, [str(exc)], active_real

    if len(active_real) > max_tickers:
        errors.append(
            f"hay {len(active_real)} tickers REAL; máximo permitido {max_tickers}"
        )
    if configured_allowlist:
        outside = [t for t in active_real if t not in configured_allowlist]
        if outside:
            errors.append(
                "ticker(s) REAL fuera del allowlist: " + ", ".join(outside)
            )

    if bool(live_cfg.get("require_manual_start", True)):
        if str(sched_cfg.get("modo", "manual")).lower() != "manual":
            errors.append("scheduler.modo debe ser manual cuando hay dinero real")

    arm_name = str(live_cfg.get("arm_env", "GALA10_LIVE_ARMED") or "").strip()
    arm_value = str(live_cfg.get("arm_value", "YES") or "YES").strip()
    if arm_name:
        actual = str(get_env(arm_name, "") or "").strip()
        if actual.upper() != arm_value.upper():
            errors.append(f"falta armado explícito: {arm_name}={arm_value}")

    if per_ticker:
        settings = ticker_settings or {}
        for ticker in active_real:
            row = settings.get(ticker) or {}
            try:
                shares = int(row.get("shares", 0) or 0)
            except (TypeError, ValueError):
                shares = 0
            if not 1 <= shares <= max_shares:
                errors.append(
                    f"{ticker}: shares REAL debe estar entre 1 y {max_shares}"
                )
            elif _unsupported_real_partial_exit(cfg, shares):
                errors.append(
                    f"{ticker}: REAL 70/30 no seguro: TP1 independiente "
                    "conflicta con STOP total Schwab; entrada bloqueada"
                )
    else:
        if str(risk_cfg.get("sizing_mode", "")).lower() != "fixed":
            errors.append("risk.sizing_mode debe ser fixed durante la validación REAL")
        configured_cap = int(risk_cfg.get("max_acciones_por_orden", 0) or 0)
        fixed_shares = int(risk_cfg.get("fixed_shares", 0) or 0)
        if configured_cap <= 0 or configured_cap > max_shares:
            errors.append(
                "risk.max_acciones_por_orden debe estar entre 1 y "
                f"{max_shares}"
            )
        if fixed_shares <= 0 or fixed_shares > max_shares:
            errors.append(f"risk.fixed_shares debe estar entre 1 y {max_shares}")
        elif _unsupported_real_partial_exit(cfg, fixed_shares):
            errors.append(
                "REAL 70/30 no seguro: TP1 independiente conflicta con STOP "
                "total Schwab; entrada bloqueada"
            )

    if not bool(ent_cfg.get("requerir_modelo_aprobado_en_real", True)):
        errors.append("requerir_modelo_aprobado_en_real debe permanecer true")
    auto_cfg = ent_cfg.get("automatic_retraining", {}) or {}
    if bool(auto_cfg.get("auto_activate_production", False)):
        errors.append("auto_activate_production debe permanecer false")

    if not bool(daily_cfg.get("enabled", True)):
        errors.append("kill_switch.daily_loss.enabled debe permanecer true")
    if not bool(ks_cfg.get("liquidar_al_apagar", True)):
        errors.append("kill_switch.liquidar_al_apagar debe permanecer true")
    if not bool(ks_cfg.get("liquidar_al_cierre_mercado", True)):
        errors.append("kill_switch.liquidar_al_cierre_mercado debe permanecer true")

    if not errors and bool(live_cfg.get("require_approved_models", True)):
        if model_checker is None:
            errors.append("validador de modelos PRODUCTION no disponible")
        else:
            try:
                missing = list(model_checker(active_real) or [])
                if missing:
                    errors.append(
                        "faltan modelos PRODUCTION aprobados/cargables: "
                        + ", ".join(str(x) for x in missing)
                    )
            except Exception as exc:
                errors.append(
                    "no se pudieron validar modelos PRODUCTION: "
                    + f"{type(exc).__name__}: {exc}"
                )

    if not errors and bool(live_cfg.get("require_broker_preflight", True)):
        account_number = str(account_number or "").strip()
        if not account_number:
            errors.append("SCHWAB_ACCOUNT_NUMBER está vacío")
        elif broker_checker is None:
            errors.append("broker preflight no disponible")
        else:
            for ticker in active_real:
                try:
                    check = broker_checker(account_number, ticker)
                    if not check.get("ok"):
                        errors.append(
                            f"{ticker}: broker preflight BLOQUEADO: "
                            + "; ".join(
                                check.get("errors") or ["verificación Schwab falló"]
                            )
                        )
                except Exception as exc:
                    errors.append(
                        f"{ticker}: broker preflight falló: "
                        + f"{type(exc).__name__}: {exc}"
                    )

    return (not errors), errors, active_real


def validate_live_start_partitioned(
    cfg: dict,
    *,
    get_env: Callable[[str, str], str],
    account_number: str = "",
    model_checker: Optional[Callable[[list], list]] = None,
    broker_checker: Optional[Callable[[str, str], dict]] = None,
    real_tickers: Optional[Iterable[str]] = None,
    ticker_settings: Optional[dict] = None,
    broker_retry_attempts: int = 1,
    broker_retry_delay_sec: float = 0.0,
) -> dict:
    """Validate mixed PAPER/REAL startup without letting one ticker stop all.

    Global safety failures (manual arm, scheduler, daily kill switch, etc.) still
    fail closed for the whole engine.  Ticker-local failures (allow-list,
    quantity, missing PRODUCTION model, or broker/candle preflight) are returned
    separately so the caller can quarantine only that ticker to PAPER.

    This function does *not* mutate configuration or execution routing.  The
    server decides whether to apply the safe PAPER fallback.
    """
    import time

    live_cfg = cfg.get("live_trading", {}) or {}
    risk_cfg = cfg.get("risk", {}) or {}
    sched_cfg = cfg.get("scheduler", {}) or {}
    ent_cfg = (cfg.get("agents", {}) or {}).get("entrenamiento", {}) or {}
    ks_cfg = cfg.get("kill_switch", {}) or {}
    daily_cfg = ks_cfg.get("daily_loss", {}) or {}

    configured_allowlist = _ticker_list(live_cfg.get("allowed_tickers", []))
    active_real = _ticker_list(
        configured_allowlist if real_tickers is None else real_tickers
    )
    ticker_errors = {ticker: [] for ticker in active_real}
    global_errors = []
    max_tickers = max_real_tickers(live_cfg)
    max_shares = max(1, int(live_cfg.get("max_shares_per_order", 1)))
    per_ticker = bool(live_cfg.get("per_ticker_execution", False))

    if not active_real:
        return {
            "start_allowed": True,
            "global_errors": [],
            "ticker_errors": {},
            "requested_real": [],
            "approved_real": [],
            "blocked_real": [],
        }

    try:
        real_exit_policy(cfg)
    except ValueError as exc:
        return {"start_allowed": False, "global_errors": [str(exc)],
                "ticker_errors": ticker_errors, "requested_real": active_real,
                "approved_real": [], "blocked_real": active_real}

    if len(active_real) > max_tickers:
        global_errors.append(
            f"hay {len(active_real)} tickers REAL; máximo permitido {max_tickers}"
        )

    if configured_allowlist:
        for ticker in active_real:
            if ticker not in configured_allowlist:
                ticker_errors[ticker].append("ticker REAL fuera del allowlist")

    if bool(live_cfg.get("require_manual_start", True)):
        if str(sched_cfg.get("modo", "manual")).lower() != "manual":
            global_errors.append(
                "scheduler.modo debe ser manual cuando hay dinero real"
            )

    arm_name = str(live_cfg.get("arm_env", "GALA10_LIVE_ARMED") or "").strip()
    arm_value = str(live_cfg.get("arm_value", "YES") or "YES").strip()
    if arm_name:
        actual = str(get_env(arm_name, "") or "").strip()
        if actual.upper() != arm_value.upper():
            global_errors.append(f"falta armado explícito: {arm_name}={arm_value}")

    if per_ticker:
        settings = ticker_settings or {}
        for ticker in active_real:
            row = settings.get(ticker) or {}
            try:
                shares = int(row.get("shares", 0) or 0)
            except (TypeError, ValueError):
                shares = 0
            if not 1 <= shares <= max_shares:
                ticker_errors[ticker].append(
                    f"shares REAL debe estar entre 1 y {max_shares}"
                )
            elif _unsupported_real_partial_exit(cfg, shares):
                ticker_errors[ticker].append(
                    "REAL 70/30 no seguro: TP1 independiente conflicta con "
                    "STOP total Schwab; entrada bloqueada"
                )
    else:
        if str(risk_cfg.get("sizing_mode", "")).lower() != "fixed":
            global_errors.append(
                "risk.sizing_mode debe ser fixed durante la validación REAL"
            )
        configured_cap = int(risk_cfg.get("max_acciones_por_orden", 0) or 0)
        fixed_shares = int(risk_cfg.get("fixed_shares", 0) or 0)
        if configured_cap <= 0 or configured_cap > max_shares:
            global_errors.append(
                "risk.max_acciones_por_orden debe estar entre 1 y "
                f"{max_shares}"
            )
        if fixed_shares <= 0 or fixed_shares > max_shares:
            global_errors.append(
                f"risk.fixed_shares debe estar entre 1 y {max_shares}"
            )
        elif _unsupported_real_partial_exit(cfg, fixed_shares):
            global_errors.append(
                "REAL 70/30 no seguro: TP1 independiente conflicta con STOP "
                "total Schwab; entrada bloqueada"
            )

    if not bool(ent_cfg.get("requerir_modelo_aprobado_en_real", True)):
        global_errors.append("requerir_modelo_aprobado_en_real debe permanecer true")
    auto_cfg = ent_cfg.get("automatic_retraining", {}) or {}
    if bool(auto_cfg.get("auto_activate_production", False)):
        global_errors.append("auto_activate_production debe permanecer false")

    if not bool(daily_cfg.get("enabled", True)):
        global_errors.append("kill_switch.daily_loss.enabled debe permanecer true")
    if not bool(ks_cfg.get("liquidar_al_apagar", True)):
        global_errors.append("kill_switch.liquidar_al_apagar debe permanecer true")
    if not bool(ks_cfg.get("liquidar_al_cierre_mercado", True)):
        global_errors.append("kill_switch.liquidar_al_cierre_mercado debe permanecer true")

    # Do not spend network calls if a global hard-safety condition is invalid.
    if not global_errors and bool(live_cfg.get("require_approved_models", True)):
        if model_checker is None:
            global_errors.append("validador de modelos PRODUCTION no disponible")
        else:
            try:
                missing = {
                    str(item).strip().upper()
                    for item in (model_checker(active_real) or [])
                    if str(item).strip()
                }
                for ticker in active_real:
                    if ticker in missing:
                        ticker_errors[ticker].append(
                            "falta modelo PRODUCTION aprobado/cargable"
                        )
            except Exception as exc:
                global_errors.append(
                    "no se pudieron validar modelos PRODUCTION: "
                    + f"{type(exc).__name__}: {exc}"
                )

    if not global_errors and bool(live_cfg.get("require_broker_preflight", True)):
        account_number = str(account_number or "").strip()
        if not account_number:
            global_errors.append("SCHWAB_ACCOUNT_NUMBER está vacío")
        elif broker_checker is None:
            global_errors.append("broker preflight no disponible")
        else:
            attempts = max(1, int(broker_retry_attempts or 1))
            delay = max(0.0, float(broker_retry_delay_sec or 0.0))
            for ticker in active_real:
                # A model/share/allow-list failure already quarantines the ticker;
                # there is no reason to hit Schwab for it during this startup.
                if ticker_errors.get(ticker):
                    continue
                last_errors = []
                last_exception = None
                passed = False
                for attempt in range(1, attempts + 1):
                    try:
                        check = broker_checker(account_number, ticker) or {}
                        if check.get("ok"):
                            passed = True
                            break
                        last_errors = list(
                            check.get("errors") or ["verificación Schwab falló"]
                        )
                    except Exception as exc:
                        last_exception = exc
                        last_errors = [
                            f"{type(exc).__name__}: {exc}"
                        ]
                    if attempt < attempts and delay > 0:
                        time.sleep(delay)
                if not passed:
                    detail = "; ".join(str(x) for x in last_errors if str(x))
                    if not detail and last_exception is not None:
                        detail = f"{type(last_exception).__name__}: {last_exception}"
                    ticker_errors[ticker].append(
                        "broker preflight BLOQUEADO: "
                        + (detail or "verificación Schwab falló")
                    )

    approved = [
        ticker for ticker in active_real
        if not ticker_errors.get(ticker)
    ]
    blocked = [ticker for ticker in active_real if ticker_errors.get(ticker)]
    return {
        "start_allowed": not global_errors,
        "global_errors": global_errors,
        "ticker_errors": ticker_errors,
        "requested_real": active_real,
        "approved_real": approved,
        "blocked_real": blocked,
    }
