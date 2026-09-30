"""
=============================================================
MÓDULO: agents/massive_downloader.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Descarga histórico de MINUTOS de Massive (ex Polygon.io) y lo guarda como
    CSV por acción en data/historico/, listo para PRE_ENTRENAR.

    Importante: el endpoint de agregados limita a 50.000 agregados base por
    consulta. Massive recomienda consultar minuto/hora en ventanas de un mes.
    Por eso este módulo divide el período completo en ventanas de 30 días y,
    dentro de cada ventana, sigue next_url hasta terminar la paginación.

    - Endpoint: https://api.massive.com/v2/aggs/ticker/{T}/range/1/minute/{from}/{to}
    - Clave: MASSIVE_API_KEY en .env (o config ai.massive_api_key).
    - Conserva extended hours cuando market.include_extended_hours_data=true.
    - Guarda datetime en UTC-naive (ai.historico_tz = "UTC").
=============================================================
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, List, Optional, Set, Tuple, Union

import pandas as pd
import requests

BASE = "https://api.massive.com"
VENTANA_DIAS = 30
MAX_REINTENTOS = 6


def _api_key(config: dict) -> str:
    return (
        os.getenv("MASSIVE_API_KEY")
        or (config or {}).get("ai", {}).get("massive_api_key", "")
        or ""
    ).strip()


def _archivo_tiene_extended(ruta: str) -> bool:
    """Detecta si un CSV existente contiene al menos una barra fuera de RTH."""
    try:
        muestra = pd.read_csv(ruta, usecols=["datetime"])
        ts = pd.to_datetime(muestra["datetime"], errors="coerce", utc=True)
        et = ts.dt.tz_convert("America/New_York")
        es_rth = (
            (et.dt.weekday < 5)
            & ((et.dt.hour > 9) | ((et.dt.hour == 9) & (et.dt.minute >= 30)))
            & (et.dt.hour < 16)
        )
        return bool((~es_rth & ts.notna()).any())
    except Exception:
        return False


def _parse_fecha(valor: Union[str, date]) -> date:
    if isinstance(valor, date):
        return valor
    return datetime.strptime(valor, "%Y-%m-%d").date()


def _iterar_ventanas(
    desde: Union[str, date],
    hasta: Union[str, date],
    dias: int = VENTANA_DIAS,
) -> Iterable[Tuple[str, str]]:
    """Genera ventanas inclusivas, continuas y sin huecos."""
    if dias < 1:
        raise ValueError("dias debe ser >= 1")

    cursor = _parse_fecha(desde)
    fin_total = _parse_fecha(hasta)
    if cursor > fin_total:
        raise ValueError("desde no puede ser posterior a hasta")

    while cursor <= fin_total:
        fin = min(cursor + timedelta(days=dias - 1), fin_total)
        yield cursor.isoformat(), fin.isoformat()
        cursor = fin + timedelta(days=1)


def _descargar_rango(
    ticker: str,
    key: str,
    desde: str,
    hasta: str,
    sesion: requests.Session,
) -> List[dict]:
    """Descarga una ventana y sigue next_url hasta terminarla.

    Lanza RuntimeError ante un fallo definitivo para evitar guardar un CSV
    parcial como si fuera completo.
    """
    url = f"{BASE}/v2/aggs/ticker/{ticker}/range/1/minute/{desde}/{hasta}"
    params = {
        "apiKey": key,
        "sort": "asc",
        "limit": 50000,
        "adjusted": "true",
    }
    barras: List[dict] = []
    paginas_vistas: Set[str] = set()

    while url:
        if url in paginas_vistas:
            raise RuntimeError(f"bucle de paginación detectado en {url}")
        paginas_vistas.add(url)

        ultimo_error: Optional[Exception] = None
        respuesta = None

        for intento in range(1, MAX_REINTENTOS + 1):
            try:
                respuesta = sesion.get(url, params=params, timeout=60)

                if respuesta.status_code == 429:
                    espera = min(60, 5 * intento)
                    time.sleep(espera)
                    continue

                if 500 <= respuesta.status_code < 600:
                    espera = min(30, 2 * intento)
                    time.sleep(espera)
                    continue

                respuesta.raise_for_status()
                break
            except Exception as exc:
                ultimo_error = exc
                if intento < MAX_REINTENTOS:
                    time.sleep(min(30, 2 * intento))
                else:
                    respuesta = None

        if respuesta is None:
            raise RuntimeError(
                f"falló {ticker} {desde}..{hasta} después de "
                f"{MAX_REINTENTOS} intentos: {ultimo_error}"
            )

        try:
            datos = respuesta.json()
        except Exception as exc:
            raise RuntimeError(
                f"respuesta JSON inválida para {ticker} {desde}..{hasta}: {exc}"
            ) from exc

        if datos.get("status") not in (None, "OK", "DELAYED"):
            raise RuntimeError(
                f"estado API inesperado para {ticker} {desde}..{hasta}: "
                f"{datos.get('status')} {datos.get('error') or datos.get('message') or ''}"
            )

        barras.extend(datos.get("results") or [])
        url = datos.get("next_url")
        params = {"apiKey": key} if url else {}
        if url:
            time.sleep(0.3)

    return barras


def _descargar_periodo_por_ventanas(
    ticker: str,
    key: str,
    desde: str,
    hasta: str,
    sesion: requests.Session,
    ventana_dias: int = VENTANA_DIAS,
) -> List[dict]:
    """Descarga el período completo en ventanas mensuales."""
    ventanas = list(_iterar_ventanas(desde, hasta, ventana_dias))
    total: List[dict] = []

    for indice, (inicio, fin) in enumerate(ventanas, start=1):
        lote = _descargar_rango(ticker, key, inicio, fin, sesion)
        total.extend(lote)
        print(
            f"[MASSIVE] {ticker}: ventana {indice}/{len(ventanas)} "
            f"{inicio}..{fin} -> {len(lote):,} velas "
            f"(acumulado {len(total):,})",
            flush=True,
        )

    return total


def descargar_ticker(
    ticker: str,
    key: str,
    anios: float = 2.0,
    carpeta: str = "data/historico",
    forzar: bool = False,
    incluir_extended: bool = False,
) -> Optional[str]:
    """Descarga ~anios de velas de 1m de un ticker y guarda el CSV en UTC.

    Si existe y no se fuerza, se omite. Al forzar, el archivo anterior se
    respalda y el nuevo CSV se escribe de forma atómica únicamente después de
    completar todas las ventanas.
    """
    ticker = str(ticker).upper().strip()
    os.makedirs(carpeta, exist_ok=True)
    ruta = os.path.join(carpeta, f"{ticker}.csv")

    if not forzar and os.path.exists(ruta) and os.path.getsize(ruta) > 1000:
        if incluir_extended and not _archivo_tiene_extended(ruta):
            print(
                f"[MASSIVE] {ticker}: el CSV existente es solo RTH y no coincide "
                "con Gala10/TOS extended. Use forzar=True para reemplazarlo."
            )
            return None
        print(
            f"[MASSIVE] {ticker}: ya está descargado "
            "(se omite; bórrelo o use forzar=True para volver a descargar)"
        )
        return ruta

    hoy = datetime.now(timezone.utc).date()
    desde = (hoy - timedelta(days=int(anios * 365) + 5)).isoformat()
    hasta = hoy.isoformat()

    sesion = requests.Session()
    try:
        barras = _descargar_periodo_por_ventanas(
            ticker, key, desde, hasta, sesion
        )
    except Exception as exc:
        print(f"[MASSIVE] {ticker}: descarga incompleta; no se reemplaza el CSV: {exc}")
        return None
    finally:
        sesion.close()

    if not barras:
        print(f"[MASSIVE] {ticker}: sin datos (¿clave, plan o ticker inválido?)")
        return None

    df = pd.DataFrame(barras)
    requeridas_api = {"t", "o", "h", "l", "c", "v"}
    faltantes_api = sorted(requeridas_api.difference(df.columns))
    if faltantes_api:
        print(f"[MASSIVE] {ticker}: faltan campos API: {faltantes_api}")
        return None

    df["datetime"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.rename(
        columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    df = df[["datetime", "open", "high", "low", "close", "volume"]]
    df = df.dropna(subset=["datetime", "open", "high", "low", "close"])

    et = df["datetime"].dt.tz_convert("America/New_York")
    laborable = et.dt.weekday < 5
    rth = laborable & (
        ((et.dt.hour > 9) | ((et.dt.hour == 9) & (et.dt.minute >= 30)))
        & (et.dt.hour < 16)
    )
    df = df[laborable].copy() if incluir_extended else df[rth].copy()
    if df.empty:
        nombre_sesion = "extended" if incluir_extended else "RTH"
        print(f"[MASSIVE] {ticker}: sin velas {nombre_sesion} tras filtrar")
        return None

    df["datetime"] = df["datetime"].dt.tz_convert("UTC").dt.tz_localize(None)
    df = df.drop_duplicates(subset=["datetime"]).sort_values("datetime")

    # Evita aceptar silenciosamente una descarga truncada por la derecha.
    ultimo_dia = df["datetime"].iloc[-1].date()
    retraso_dias = (hoy - ultimo_dia).days
    if retraso_dias > 10:
        print(
            f"[MASSIVE] {ticker}: cobertura truncada; última vela {ultimo_dia}, "
            f"{retraso_dias} días antes del final solicitado. No se guarda."
        )
        return None

    respaldo = None
    if forzar and os.path.exists(ruta):
        sello = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        respaldo = f"{ruta}.backup_{sello}"
        shutil.copy2(ruta, respaldo)
        print(f"[MASSIVE] {ticker}: respaldo previo -> {respaldo}")

    temporal = f"{ruta}.tmp"
    try:
        df.to_csv(temporal, index=False)
        os.replace(temporal, ruta)
    except Exception:
        if os.path.exists(temporal):
            os.remove(temporal)
        raise

    dias = (df["datetime"].iloc[-1] - df["datetime"].iloc[0]).days
    nombre_sesion = "extended" if incluir_extended else "RTH"
    print(
        f"[MASSIVE] {ticker}: {len(df):,} velas {nombre_sesion} 1m | "
        f"cobertura ~{dias} días -> {ruta}"
    )
    return ruta


def descargar_todos(tickers: List[str], config: dict, anios: float = 2.0) -> dict:
    """Descarga el histórico de todos los tickers. Devuelve {ticker: ruta}."""
    key = _api_key(config)
    if not key:
        print("[MASSIVE] Falta la API key. Ponga MASSIVE_API_KEY en el .env.")
        return {}

    cfg_ai = (config or {}).get("ai", {})
    carpeta = cfg_ai.get("historico_dir", "data/historico")
    forzar = bool(cfg_ai.get("forzar_descarga", False))
    incluir_extended = bool(
        (config or {}).get("market", {}).get("include_extended_hours_data", False)
    )

    resultado = {}
    for ticker in tickers:
        try:
            ruta = descargar_ticker(
                ticker,
                key,
                anios=anios,
                carpeta=carpeta,
                forzar=forzar,
                incluir_extended=incluir_extended,
            )
            if ruta:
                resultado[str(ticker).upper()] = ruta
        except Exception as exc:
            print(f"[MASSIVE] {ticker}: fallo inesperado: {exc}")

    print(
        f"[MASSIVE] Descargados {len(resultado)}/{len(tickers)} tickers "
        f"en {carpeta}"
    )
    return resultado
