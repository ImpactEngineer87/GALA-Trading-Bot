# -*- coding: utf-8 -*-
"""Preflight Schwab estrictamente de solo lectura.

Este módulo valida OAuth, hash de cuenta, posiciones, órdenes abiertas y datos
de mercado. No importa ni instancia ningún ejecutor de órdenes y no contiene
llamadas POST/PUT/DELETE a endpoints de trading.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Sequence

import yaml

from api.schwab_client import SchwabClient


def _medir(fn):
    inicio = time.monotonic()
    valor = fn()
    return valor, int(round((time.monotonic() - inicio) * 1000.0))


def _simbolo_orden(orden: dict) -> str:
    try:
        leg = (orden.get("orderLegCollection") or [{}])[0]
        return str((leg.get("instrument") or {}).get("symbol", "")).upper()
    except Exception:
        return ""


def _percentil(valores: Sequence[int], percentil: float) -> int:
    """Percentil lineal pequeño, sin dependencia adicional."""
    ordenados = sorted(int(v) for v in valores)
    if not ordenados:
        return 0
    if len(ordenados) == 1:
        return ordenados[0]
    posicion = (len(ordenados) - 1) * float(percentil)
    inferior = int(posicion)
    superior = min(inferior + 1, len(ordenados) - 1)
    fraccion = posicion - inferior
    return int(round(
        ordenados[inferior]
        + (ordenados[superior] - ordenados[inferior]) * fraccion
    ))


def _resumen_latencias(valores: Sequence[int]) -> Dict:
    valores = [int(v) for v in valores]
    return {
        "samples": len(valores),
        "min_ms": min(valores) if valores else 0,
        "median_ms": int(round(statistics.median(valores))) if valores else 0,
        "p95_ms": _percentil(valores, 0.95),
        "max_ms": max(valores) if valores else 0,
        "all_ms": valores,
    }


def _guardar_reporte_latency(reporte: Dict, output_dir: str) -> str:
    """Guarda el benchmark sin incluir número de cuenta, tokens ni secretos."""
    os.makedirs(output_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destino = os.path.join(output_dir, f"broker_latency_{stamp}.json")
    latest = os.path.join(output_dir, "broker_latency_latest.json")
    temporal = destino + ".tmp"
    contenido = json.dumps(reporte, ensure_ascii=False, indent=2)
    with open(temporal, "w", encoding="utf-8") as fh:
        fh.write(contenido)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporal, destino)
    temporal_latest = latest + ".tmp"
    with open(temporal_latest, "w", encoding="utf-8") as fh:
        fh.write(contenido)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporal_latest, latest)
    return destino


def ejecutar_broker_check(
    config_path: str,
    account_number: str,
    ticker: str = "MSFT",
    client_factory: Optional[Callable[[dict], SchwabClient]] = None,
) -> Dict:
    """Ejecuta verificaciones GET y devuelve un resultado serializable."""
    with open(config_path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}

    factory = client_factory or SchwabClient
    cliente = factory(config)
    ticker = str(ticker or "MSFT").strip().upper()
    account_number = str(account_number or "").strip()
    pasos = {}
    errores = []

    auth_ok, lat = _medir(lambda: bool(cliente._asegurar_token()))
    pasos["oauth"] = {"ok": bool(auth_ok), "latency_ms": lat}
    if not auth_ok:
        errores.append("OAuth de usuario no disponible")

    account_hash = None
    if auth_ok and account_number:
        account_hash, lat = _medir(
            lambda: cliente.obtener_account_hash(account_number)
        )
        pasos["account_hash"] = {
            "ok": bool(account_hash),
            "latency_ms": lat,
            "hash_suffix": str(account_hash or "")[-6:],
        }
        if not account_hash:
            errores.append("no se pudo resolver el hash exacto de la cuenta")
    else:
        pasos["account_hash"] = {"ok": False, "latency_ms": 0, "hash_suffix": ""}
        if not account_number:
            errores.append("número de cuenta vacío")

    posiciones = None
    ordenes = None
    velas = None
    if account_hash:
        posiciones, lat = _medir(
            lambda: cliente.obtener_posiciones(account_hash)
        )
        pasos["positions"] = {
            "ok": posiciones is not None,
            "latency_ms": lat,
            "count": len(posiciones or []),
            "symbols": sorted(
                {str(p.get("symbol", "")).upper() for p in (posiciones or [])}
                - {""}
            ),
        }
        if posiciones is None:
            errores.append("no se pudieron consultar posiciones")

        ordenes, lat = _medir(
            lambda: cliente.obtener_ordenes_abiertas(account_hash)
        )
        pasos["open_orders"] = {
            "ok": ordenes is not None,
            "latency_ms": lat,
            "count": len(ordenes or []),
            "symbols": sorted({_simbolo_orden(o) for o in (ordenes or [])} - {""}),
        }
        if ordenes is None:
            errores.append("no se pudieron consultar órdenes abiertas")

    if auth_ok:
        velas, lat = _medir(
            lambda: cliente.obtener_historico(ticker, "1m", 2)
        )
        pasos["market_data_1m"] = {
            "ok": velas is not None and len(velas) > 0,
            "latency_ms": lat,
            "ticker": ticker,
            "rows": int(len(velas)) if velas is not None else 0,
        }
        if velas is None or len(velas) == 0:
            errores.append(f"sin velas reales de 1m para {ticker}")

    ok = not errores and all(p.get("ok") for p in pasos.values())

    print("\n" + "=" * 60)
    print("  SCHWAB BROKER CHECK — SOLO LECTURA")
    print("  No se envían, modifican ni cancelan órdenes")
    print("=" * 60)
    for nombre, paso in pasos.items():
        estado = "OK" if paso.get("ok") else "ERROR"
        extras = []
        if "count" in paso:
            extras.append(f"count={paso['count']}")
        if "rows" in paso:
            extras.append(f"rows={paso['rows']}")
        if paso.get("hash_suffix"):
            extras.append(f"hash=…{paso['hash_suffix']}")
        detalle = f" | {' | '.join(extras)}" if extras else ""
        print(
            f"[{estado}] {nombre}: {paso.get('latency_ms', 0)}ms{detalle}"
        )
    if errores:
        for error in errores:
            print(f"[ERROR] {error}")
    print(f"RESULTADO: {'APROBADO' if ok else 'BLOQUEADO'}")
    print("=" * 60 + "\n")

    return {"ok": ok, "steps": pasos, "errors": errores}


def ejecutar_broker_latency(
    config_path: str,
    account_number: str,
    ticker: str = "MSFT",
    samples: int = 10,
    output_dir: str = "data/reportes",
    client_factory: Optional[Callable[[dict], SchwabClient]] = None,
) -> Dict:
    """Mide llamadas Schwab GET con una sesión caliente y sin enviar órdenes.

    Se hace una pasada de calentamiento que no entra en las estadísticas. Después
    se miden posiciones, órdenes abiertas y cotización. El resultado caracteriza
    la conectividad de lectura; no pretende medir POST/cancel/fill de órdenes.
    """
    with open(config_path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}

    samples = max(1, min(int(samples), 50))
    ticker = str(ticker or "MSFT").strip().upper()
    account_number = str(account_number or "").strip()
    factory = client_factory or SchwabClient
    cliente = factory(config)
    errores = []

    auth_ok, auth_ms = _medir(lambda: bool(cliente._asegurar_token()))
    account_hash = None
    hash_ms = 0
    if auth_ok and account_number:
        account_hash, hash_ms = _medir(
            lambda: cliente.obtener_account_hash(account_number)
        )
    if not auth_ok:
        errores.append("OAuth de usuario no disponible")
    if not account_number:
        errores.append("número de cuenta vacío")
    elif auth_ok and not account_hash:
        errores.append("no se pudo resolver el hash exacto de la cuenta")

    reporte = {
        "ok": False,
        "read_only": True,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "requested_samples": samples,
        "warmup_excluded": True,
        "oauth_ms": auth_ms,
        "account_hash_ms": hash_ms,
        "account_hash_suffix": str(account_hash or "")[-6:],
        "metrics": {},
        "errors": errores,
    }

    if account_hash and auth_ok:
        endpoints = {
            "positions": lambda: cliente.obtener_posiciones(account_hash),
            "open_orders": lambda: cliente.obtener_ordenes_abiertas(account_hash),
            "quote": lambda: cliente.obtener_precio_actual(ticker),
        }
        validadores = {
            "positions": lambda valor: valor is not None,
            "open_orders": lambda valor: valor is not None,
            "quote": lambda valor: valor is not None and float(valor) > 0,
        }

        # Una pasada establece conexión HTTP/TLS, token y cachés. No se mezcla
        # con las muestras calientes que representan la operación normal.
        for nombre, fn in endpoints.items():
            try:
                valor, _ = _medir(fn)
                if not validadores[nombre](valor):
                    errores.append(f"warm-up {nombre} sin respuesta válida")
            except Exception as exc:
                errores.append(
                    f"warm-up {nombre} falló ({type(exc).__name__})"
                )

        latencias = {nombre: [] for nombre in endpoints}
        fallos = {nombre: 0 for nombre in endpoints}
        for _ in range(samples):
            for nombre, fn in endpoints.items():
                try:
                    valor, lat = _medir(fn)
                    if validadores[nombre](valor):
                        latencias[nombre].append(lat)
                    else:
                        fallos[nombre] += 1
                except Exception:
                    fallos[nombre] += 1

        for nombre in endpoints:
            resumen = _resumen_latencias(latencias[nombre])
            resumen["failures"] = fallos[nombre]
            resumen["ok"] = (
                resumen["samples"] == samples and fallos[nombre] == 0
            )
            reporte["metrics"][nombre] = resumen
            if not resumen["ok"]:
                errores.append(
                    f"{nombre}: {fallos[nombre]} fallo(s), "
                    f"{resumen['samples']}/{samples} muestras válidas"
                )

    reporte["ok"] = (
        not errores
        and len(reporte["metrics"]) == 3
        and all(m["ok"] for m in reporte["metrics"].values())
    )
    try:
        reporte["report_path"] = _guardar_reporte_latency(
            reporte, output_dir
        )
    except Exception as exc:
        reporte["ok"] = False
        error = f"no se pudo guardar el reporte ({type(exc).__name__})"
        reporte["errors"].append(error)
        errores.append(error)

    print("\n" + "=" * 68)
    print("  SCHWAB WARM LATENCY — SOLO LECTURA")
    print("  Warm-up excluido; no se envían, modifican ni cancelan órdenes")
    print("=" * 68)
    print(f"  OAuth inicial: {auth_ms}ms | hash de cuenta: {hash_ms}ms")
    for nombre, metrica in reporte["metrics"].items():
        estado = "OK" if metrica["ok"] else "ERROR"
        print(
            f"[{estado}] {nombre}: n={metrica['samples']} | "
            f"min={metrica['min_ms']}ms | mediana={metrica['median_ms']}ms | "
            f"p95={metrica['p95_ms']}ms | max={metrica['max_ms']}ms"
        )
    for error in errores:
        print(f"[ERROR] {error}")
    if reporte.get("report_path"):
        print(f"[REPORTE] {reporte['report_path']}")
    print(f"RESULTADO: {'APROBADO' if reporte['ok'] else 'BLOQUEADO'}")
    print("=" * 68 + "\n")
    return reporte
