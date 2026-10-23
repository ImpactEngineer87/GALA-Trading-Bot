# -*- coding: utf-8 -*-
"""
=============================================================
MODULO: api/schwab_client.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCION:
    Cliente Schwab con OAuth2 de usuario.
    - Usa authorization_code para datos y trading
    - Auto-refresca el token antes de que expire
    - Falla de forma cerrada cuando Schwab requiere reautorizacion
=============================================================
"""

import math
import os
import sys
import threading
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import Optional
from dotenv import load_dotenv
from api.order_sync import valid_order_snapshot
from api.schwab_rate_guard import MARKET_DATA_GUARD
from api.schwab_oauth import (
    DEFAULT_REDIRECT_URI,
    OAUTH_ROTATION_LOCK,
    SchwabOAuthFailure,
    SchwabOAuthCooldown,
    refresh_oauth_token,
)

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
load_dotenv(ENV_PATH, override=False)

# Forzar UTF-8 en salida de consola (Windows fix)
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

BASE_URL   = "https://api.schwabapi.com/marketdata/v1"
TRADER_URL = "https://api.schwabapi.com/trader/v1"
TOKEN_URL  = "https://api.schwabapi.com/v1/oauth/token"


def _parse_token_expiry(raw_value) -> Optional[datetime]:
    """Normalize persisted timestamps to the local naive clock used by clients."""
    raw = str(raw_value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        expiry = datetime.fromisoformat(raw)
        if expiry.tzinfo is not None:
            expiry = expiry.astimezone().replace(tzinfo=None)
        return expiry
    except (TypeError, ValueError):
        return None


def _log(msg: str):
    """Print seguro para Windows (sin emojis que rompan cp1252)."""
    try:
        print(msg)
    except UnicodeEncodeError:
        # Reemplazar caracteres no-ASCII
        safe = msg.encode('ascii', errors='replace').decode('ascii')
        print(safe)


class SchwabAuthorizationRequired(RuntimeError):
    """La llamada no puede continuar hasta completar OAuth de usuario."""


class SchwabClient:
    """
    Cliente Schwab API con autenticacion automatica.

    Autenticacion:
      authorization_code -> token de usuario para datos y trading.

    Schwab no ofrece un fallback client_credentials para estos endpoints.
    Si el refresh token deja de ser valido, el cliente se desconecta y exige
    una nueva autorizacion explicita en vez de enviar Bearer None o repetir 401.

    Uso:
        cliente = SchwabClient()
        df = cliente.obtener_velas("BA")
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        # Gala10 en TOS usa las velas pre/after-market para mantener el estado
        # acumulado de EMA/MACD/ATR/memoria/cooldown. Esta opción controla los
        # DATOS solicitados; el horario en que se permite enviar órdenes se
        # controla de forma independiente en agents.agente3.
        self.include_extended_hours_data = bool(
            self.config.get("market", {}).get(
                "include_extended_hours_data", False))
        self.client_id     = os.getenv("SCHWAB_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("SCHWAB_CLIENT_SECRET", "").strip()
        self.access_token:  Optional[str]      = None
        self.refresh_token: Optional[str]      = None
        self.token_expiry:  Optional[datetime] = None
        self.auth_error: str = ""
        self.reauthorization_required = False
        self._auth_retry_after: Optional[datetime] = None
        self._auth_lock = threading.RLock()
        self._auth_warning_emitted = False
        self._rejected_access_token: Optional[str] = None
        # Todos los tokens aceptados por este cliente proceden del OAuth de usuario.
        # Arranca en False (fail-safe) hasta cargar o recibir uno.
        self.token_es_usuario = False
        self._session = requests.Session()
        self._paper = True   # True hasta que haya user auth completo

        if not self.client_id or not self.client_secret:
            _log("[WARN] Credenciales Schwab no configuradas en .env")
            self._marcar_reautorizacion(
                "Faltan SCHWAB_CLIENT_ID/SCHWAB_CLIENT_SECRET en .env.",
                clear_access=False,
                cooldown_seconds=0,
            )
        else:
            # Intentar cargar token guardado del .env
            saved = os.getenv("SCHWAB_ACCESS_TOKEN", "").strip()
            self.refresh_token = os.getenv("SCHWAB_REFRESH_TOKEN", "").strip() or None
            if saved:
                self.access_token  = saved
                self.token_expiry = _parse_token_expiry(
                    os.getenv("SCHWAB_TOKEN_EXPIRES_AT", "")
                )
                # Unknown expiry never grants extra validity to a saved bearer.
                # The first request must refresh, or require authorization.
                self.token_es_usuario = True
                self._paper = False
                self._session.headers.update({
                    "Authorization": f"Bearer {self.access_token}",
                    "Accept": "application/json",
                })
                _log("[OK] Token Schwab cargado desde .env")
            elif self.refresh_token:
                # An access token may be absent while refresh credentials remain
                # usable. Let the first authenticated operation recover it.
                self._paper = False
            else:
                self._marcar_reautorizacion(
                    "No hay token OAuth de usuario. Conecte Schwab mediante authorization_code.",
                    clear_access=False,
                    cooldown_seconds=0,
                )

    # ----------------------------------------------------------
    # AUTENTICACION
    # ----------------------------------------------------------

    def autenticar(self) -> bool:
        """
        Alias conservado por compatibilidad.

        La autorizacion inicial requiere interaccion del usuario mediante el
        flujo authorization_code del dashboard. No se intenta client_credentials
        porque no produce un token valido para estos endpoints de Schwab.
        """
        self._marcar_reautorizacion(
            "Schwab requiere OAuth authorization_code; abra la conexión del dashboard.",
            clear_access=not bool(self.access_token),
            cooldown_seconds=0,
        )
        return False

    def autenticar_con_token(self, access_token: str, refresh_token: str = "",
                             expires_in: int = 1800, *,
                             expires_at: Optional[datetime] = None):
        """
        Configura el cliente con tokens OAuth2 del usuario
        (flujo authorization_code -- para trading real).
        """
        if not access_token:
            self._marcar_reautorizacion(
                "El OAuth de Schwab no devolvio access_token.",
                cooldown_seconds=0,
            )
            return False
        self.access_token  = access_token
        self.refresh_token = refresh_token
        self.token_expiry = (
            _parse_token_expiry(expires_at)
            if expires_at is not None
            else datetime.now() + timedelta(seconds=max(30, int(expires_in) - 60))
        )
        self.token_es_usuario = True   # token de USUARIO → válido para trading
        self.reauthorization_required = False
        self.auth_error = ""
        self._auth_retry_after = None
        self._auth_warning_emitted = False
        self._rejected_access_token = None
        self._paper = False
        self._session.headers.update({
            "Authorization": f"Bearer {self.access_token}",
            "Accept":        "application/json",
        })
        _log("[OK] Schwab autenticado con token de usuario (trading habilitado)")
        return True

    def _marcar_reautorizacion(self, motivo: str, clear_access: bool = True,
                               cooldown_seconds: int = 60):
        """Marca el cliente como desconectado sin registrar ni exponer tokens."""
        with self._auth_lock:
            if clear_access:
                self.access_token = None
                self.token_expiry = None
                self._session.headers.pop("Authorization", None)
            self.token_es_usuario = False
            self.reauthorization_required = True
            self.auth_error = motivo
            self._auth_retry_after = (
                datetime.now() + timedelta(seconds=max(0, cooldown_seconds))
            )
            if not self._auth_warning_emitted:
                _log(f"[WARN] Schwab desconectado: {motivo}")
                self._auth_warning_emitted = True

    def _asegurar_token(self, forzar_refresh: bool = False) -> bool:
        """Devuelve True solo cuando existe un token OAuth utilizable."""
        with self._auth_lock:
            ahora = datetime.now()
            if (
                not forzar_refresh
                and self.access_token
                and self.token_expiry
                and ahora < self.token_expiry
            ):
                return True
            if (
                self._auth_retry_after
                and ahora < self._auth_retry_after
                and self.reauthorization_required
            ):
                return False
            if self.refresh_token:
                return self._refrescar_token()
            self._marcar_reautorizacion(
                "No hay refresh_token valido; complete OAuth authorization_code.",
                cooldown_seconds=0,
            )
            return False

    def _adoptar_oauth_del_proceso(self) -> bool:
        """Adopt the newest token/credentials persisted by another client instance."""
        self.client_id = os.getenv("SCHWAB_CLIENT_ID", self.client_id or "").strip()
        self.client_secret = os.getenv("SCHWAB_CLIENT_SECRET", self.client_secret or "").strip()
        latest_access = os.getenv("SCHWAB_ACCESS_TOKEN", "").strip()
        latest_refresh = os.getenv("SCHWAB_REFRESH_TOKEN", "").strip()
        latest_expiry = _parse_token_expiry(
            os.getenv("SCHWAB_TOKEN_EXPIRES_AT", "")
        )

        if latest_refresh:
            self.refresh_token = latest_refresh
        if (
            latest_access
            and latest_access != self._rejected_access_token
            and latest_expiry
            and datetime.now() < latest_expiry
        ):
            self.access_token = latest_access
            self.token_expiry = latest_expiry
            self.token_es_usuario = True
            self.reauthorization_required = False
            self.auth_error = ""
            self._auth_retry_after = None
            self._paper = False
            self._session.headers.update({
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
            })
            return True
        return False

    def _refrescar_token(self) -> bool:
        """Renew OAuth once, sharing the newest rotated token across clients."""
        with OAUTH_ROTATION_LOCK:
            # Another component may have refreshed while this client was idle.
            if self._adoptar_oauth_del_proceso():
                return True
            if not self.refresh_token or not self.client_id or not self.client_secret:
                self._marcar_reautorizacion(
                    "Faltan client_id/client_secret o refresh_token para renovar OAuth.",
                    cooldown_seconds=0,
                )
                return False
            try:
                d = refresh_oauth_token(
                    self.client_id,
                    self.client_secret,
                    self.refresh_token,
                    redirect_uri=DEFAULT_REDIRECT_URI,
                    timeout=15,
                )
                nuevo_access = d.get("access_token", "")
                self.access_token = nuevo_access
                self.refresh_token = d.get("refresh_token", self.refresh_token)
                expires_in = int(d.get("expires_in", 1800))
                self.token_expiry = datetime.now() + timedelta(
                    seconds=max(30, expires_in - 60)
                )
                self._session.headers.update({
                    "Authorization": f"Bearer {self.access_token}",
                    "Accept": "application/json",
                })
                self.token_es_usuario = True
                self.reauthorization_required = False
                self.auth_error = ""
                self._auth_retry_after = None
                self._auth_warning_emitted = False
                self._rejected_access_token = None
                self._paper = False

                self._save_env_token(ENV_PATH)
                _log("[OK] Schwab token renovado")
                return True
            except SchwabOAuthCooldown as exc:
                # Per-process OAuth rejection cooldown. Never bypass it from a
                # ticker worker; without a valid token REAL entries fail closed.
                self._marcar_reautorizacion(
                    "Schwab OAuth pausado tras HTTP %s; compruebe permisos o "
                    "reconecte desde el dashboard." % (exc.last_status or "?"),
                    cooldown_seconds=max(60, int(exc.remaining_seconds)),
                )
                return False
            except SchwabOAuthFailure as exc:
                diag = exc.diagnostic or {}
                oauth_error = str(diag.get("oauth_error") or "").lower()
                if oauth_error == "invalid_client":
                    motivo = "Schwab rechazó Client ID/Client Secret; complete OAuth con credenciales válidas."
                elif oauth_error == "invalid_grant":
                    motivo = "Schwab rechazó el refresh token; complete OAuth authorization_code nuevamente."
                else:
                    motivo = f"No se pudo renovar OAuth (HTTP {diag.get('http_status') or '?'}); reconecte Schwab."
                self._marcar_reautorizacion(motivo, cooldown_seconds=60)
                return False
            except requests.RequestException as exc:
                self._marcar_reautorizacion(
                    f"No se pudo renovar OAuth ({type(exc).__name__}); reconecte Schwab.",
                    cooldown_seconds=60,
                )
                return False
            except (TypeError, ValueError, KeyError) as exc:
                self._marcar_reautorizacion(
                    f"Respuesta OAuth inválida ({type(exc).__name__}); reconecte Schwab.",
                    cooldown_seconds=60,
                )
                return False

    def _recuperar_401(self, rejected_access_token: Optional[str] = None) -> bool:
        """Invalida el access token y realiza como maximo un refresh controlado."""
        with self._auth_lock:
            rejected = rejected_access_token or self.access_token
            self._rejected_access_token = rejected
            # Another thread may already have replaced the rejected bearer.
            if (
                self.access_token
                and self.access_token != rejected
                and self.token_expiry
                and datetime.now() < self.token_expiry
            ):
                return True
            self.access_token = None
            self.token_expiry = None
            self._session.headers.pop("Authorization", None)
            # Adoption may find a newer process token even if this instance
            # has no refresh token. The rejected bearer is never adopted again.
            return self._refrescar_token()

    def estado_autenticacion(self) -> dict:
        """Estado no sensible para diagnostico y dashboard."""
        ahora = datetime.now()
        segundos = None
        if self.token_expiry:
            segundos = max(0, int((self.token_expiry - ahora).total_seconds()))
        return {
            "connected": bool(
                self.access_token
                and self.token_expiry
                and ahora < self.token_expiry
                and self.token_es_usuario
            ),
            "has_access_token": bool(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
            "token_is_user_oauth": bool(self.token_es_usuario),
            "expires_in_seconds": segundos,
            "reauthorization_required": bool(self.reauthorization_required),
            "error": self.auth_error,
        }

    def _save_env_token(self, env_path: str):
        """Guarda el access/refresh token en .env para persistencia."""
        try:
            lines = []
            keys_written = set()
            if os.path.exists(env_path):
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        s = line.strip()
                        if "=" in s and not s.startswith("#"):
                            k = s.split("=", 1)[0]
                            if k == "SCHWAB_ACCESS_TOKEN":
                                lines.append(f"SCHWAB_ACCESS_TOKEN={self.access_token}\n")
                                keys_written.add(k)
                            elif k == "SCHWAB_REFRESH_TOKEN":
                                lines.append(f"SCHWAB_REFRESH_TOKEN={self.refresh_token}\n")
                                keys_written.add(k)
                            elif k == "SCHWAB_TOKEN_EXPIRES_AT":
                                expires = self.token_expiry.isoformat() if self.token_expiry else ""
                                lines.append(f"SCHWAB_TOKEN_EXPIRES_AT={expires}\n")
                                keys_written.add(k)
                            else:
                                lines.append(line if line.endswith("\n") else line + "\n")
                        else:
                            lines.append(line if line.endswith("\n") else line + "\n")
            if "SCHWAB_ACCESS_TOKEN" not in keys_written:
                lines.append(f"SCHWAB_ACCESS_TOKEN={self.access_token}\n")
            if "SCHWAB_REFRESH_TOKEN" not in keys_written and self.refresh_token:
                lines.append(f"SCHWAB_REFRESH_TOKEN={self.refresh_token}\n")
            if "SCHWAB_TOKEN_EXPIRES_AT" not in keys_written and self.token_expiry:
                lines.append(f"SCHWAB_TOKEN_EXPIRES_AT={self.token_expiry.isoformat()}\n")
            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.environ["SCHWAB_ACCESS_TOKEN"] = self.access_token or ""
            os.environ["SCHWAB_REFRESH_TOKEN"] = self.refresh_token or ""
            os.environ["SCHWAB_TOKEN_EXPIRES_AT"] = (
                self.token_expiry.isoformat() if self.token_expiry else ""
            )
        except Exception as e:
            _log(f"[WARN] No se pudo guardar token en .env: {e}")

    def _headers(self) -> dict:
        if not self._asegurar_token():
            raise SchwabAuthorizationRequired(
                self.auth_error or "Se requiere autorizacion OAuth de Schwab."
            )
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept":        "application/json",
        }

    def _get_autorizado(self, url: str, *, params: Optional[dict] = None,
                        timeout: int = 15):
        """GET con un solo refresh ante 401, sin repetir automáticamente un 403.

        Market-data requests share one bounded per-process request budget with
        dashboard reads. Trading/broker reconciliation GETs are not gated here.
        """
        if not self._asegurar_token():
            return None

        def get_once():
            market_data = url.startswith(BASE_URL + "/")
            if market_data:
                MARKET_DATA_GUARD.acquire()
            headers = self._headers()
            response = self._session.get(
                url, params=params, headers=headers, timeout=timeout
            )
            if market_data:
                MARKET_DATA_GUARD.observe(response, url)
            return response, headers["Authorization"][len("Bearer "):]

        r, used_access_token = get_once()
        if r.status_code != 401:
            # 403 can mean insufficient API permissions/app status, not OAuth
            # expiry. Let the caller report the error without rotating tokens.
            return r
        if not self._recuperar_401(used_access_token):
            return None
        return get_once()[0]

    # ----------------------------------------------------------
    # DATOS DE MERCADO -- VELAS OHLCV
    # ----------------------------------------------------------

    def obtener_velas(self, ticker: str, intervalo: str = "1m",
                      cantidad: int = 200) -> Optional[pd.DataFrame]:
        """
        Obtiene velas historicas reales desde Schwab.
        Requiere OAuth de usuario. Sin datos devuelve None; no genera precios.
        La simulación exige invocar explícitamente obtener_velas_demo.
        """
        if not self._asegurar_token():
            return None

        freq_map = {"1m": 1, "5m": 5, "15m": 15, "30m": 30}
        frequency = freq_map.get(intervalo, 1)

        params = {
            "symbol":                ticker,
            "periodType":            "day",
            "period":                5,
            "frequencyType":         "minute",
            "frequency":             frequency,
            "needExtendedHoursData": self.include_extended_hours_data,
        }

        try:
            url = f"{BASE_URL}/pricehistory"
            r = self._get_autorizado(url, params=params, timeout=15)
            if r is None:
                return None

            r.raise_for_status()
            datos = r.json()

            if "candles" not in datos or not datos["candles"]:
                _log(f"[WARN] Sin velas para {ticker} - mercado cerrado o sin datos")
                return None

            df = pd.DataFrame(datos["candles"])
            df["datetime"] = pd.to_datetime(df["datetime"], unit="ms", utc=True)
            df.set_index("datetime", inplace=True)
            df = df[["open", "high", "low", "close", "volume"]].astype(float)
            df.sort_index(inplace=True)
            _log(f"[OK] {ticker}: {len(df)} velas reales obtenidas de Schwab")
            return df.tail(cantidad)

        except Exception as e:
            _log(f"[ERROR] Error obteniendo velas para {ticker}: {e}")
            return None

    def _velas_fallback(self, ticker: str, cantidad: int = 200) -> pd.DataFrame:
        """
        Generador exclusivo para una petición explícita de demo.
        Nunca se usa como reemplazo automático de datos de Schwab.
        """
        import numpy as np
        _log(f"[WARN] [{ticker}] Usando datos sinteticos (mercado cerrado o sin conexion)")
        np.random.seed(hash(ticker) % 10000)
        fechas = pd.date_range(end=datetime.now(), periods=cantidad, freq="1min")
        precios_base = {
            "BA": 225.0, "AAPL": 192.0, "TSLA": 248.0, "NVDA": 118.0,
            "AMZN": 197.0, "META": 523.0, "AMD": 162.0, "RCL": 230.0, "UNH": 540.0,
        }
        base    = precios_base.get(ticker, 200.0)
        pasos   = np.random.choice([-1, 1], size=cantidad, p=[0.45, 0.55])
        cambios = pasos * (abs(np.random.randn(cantidad)) * 0.003 + 0.001) * base
        precios = np.maximum(base + np.cumsum(cambios), base * 0.8)
        ruido   = abs(np.random.randn(cantidad)) * base * 0.001
        frame = pd.DataFrame({
            "open":   precios - ruido * 0.5,
            "high":   precios + ruido * 1.5,
            "low":    precios - ruido * 1.5,
            "close":  precios,
            "volume": np.random.randint(80000, 600000, cantidad).astype(float),
        }, index=fechas)
        frame.attrs.update(source="EXPLICIT_DEMO_SYNTHETIC", synthetic=True)
        return frame

    # ----------------------------------------------------------
    # COTIZACION EN TIEMPO REAL
    # ----------------------------------------------------------

    def obtener_precio_actual(self, ticker: str) -> Optional[float]:
        """Obtiene el ultimo precio del ticker."""
        if not self._asegurar_token():
            return None
        try:
            r = self._get_autorizado(
                f"{BASE_URL}/quotes",
                params={"symbols": ticker},
                timeout=10,
            )
            if r is None:
                return None
            r.raise_for_status()
            data = r.json()
            quote = data.get(ticker, {}).get("quote", {})
            precio = quote.get("lastPrice") or quote.get("mark")
            if precio:
                _log(f"[OK] {ticker} precio actual: ${precio}")
            return precio
        except Exception as e:
            _log(f"[ERROR] Error obteniendo precio de {ticker}: {e}")
            return None

    def obtener_multiples_precios(self, tickers: list) -> dict:
        """Obtiene precios de multiples tickers en una sola llamada."""
        if not self._asegurar_token():
            return {t: 0.0 for t in tickers}
        try:
            symbols = ",".join(tickers)
            r = self._get_autorizado(
                f"{BASE_URL}/quotes",
                params={"symbols": symbols},
                timeout=10,
            )
            if r is None:
                return {t: 0.0 for t in tickers}
            r.raise_for_status()
            data = r.json()
            result = {}
            for t in tickers:
                q = data.get(t, {}).get("quote", {})
                result[t] = q.get("lastPrice") or q.get("mark") or 0.0
            return result
        except Exception as e:
            _log(f"[ERROR] Error obteniendo precios multiples: {e}")
            return {t: 0.0 for t in tickers}

    # ----------------------------------------------------------
    # BALANCE Y CUENTA (requiere user OAuth token)
    # ----------------------------------------------------------

    def obtener_balance(self, account_number: str) -> dict:
        """Obtiene balance de la cuenta (requiere user token)."""
        if self._paper:
            return {"equity": 0, "cash": 10000, "buying_power": 10000, "source": "PAPER"}
        if not self.refresh_token:
            return {}
        if not self._asegurar_token():
            return {}
        try:
            url = f"{TRADER_URL}/accounts/{account_number}"
            r = self._get_autorizado(url, timeout=10)
            if r is None:
                return {}
            r.raise_for_status()
            bal = r.json().get("securitiesAccount", {}).get("currentBalances", {})
            return {
                "source": "BROKER",
                "as_of_epoch": __import__('time').time(),
                "available_funds": bal.get("availableFunds", bal.get("cashAvailableForTrading")),
                "equity":       bal.get("equity",       0),
                "cash":         bal.get("cashBalance",   0),
                "buying_power": bal.get("buyingPower",   0),
            }
        except Exception as e:
            _log(f"[ERROR] Error obteniendo balance: {e}")
            return {}

    def obtener_cotizacion_ejecucion(self, ticker: str) -> dict:
        """Bid/ask and exchange timestamp, with no synthetic quote fallback."""
        if not self._asegurar_token():
            return {}
        try:
            r = self._get_autorizado(f'{BASE_URL}/quotes', params={'symbols': ticker}, timeout=5)
            if r is None:
                return {}
            r.raise_for_status()
            payload = r.json().get(ticker, {})
            q = payload.get('quote', {})
            return {'bid': q.get('bidPrice'), 'ask': q.get('askPrice'),
                    'quote_time': float(q.get('quoteTime', 0)) / 1000,
                    'delayed': payload.get('realtime') is not True}
        except Exception:
            return {}

    # ----------------------------------------------------------
    # HISTORIAL MULTI-TEMPORAL (para el sistema de 3 agentes SMC)
    # ----------------------------------------------------------

    def _pedir_pricehistory(self, params: dict) -> Optional[pd.DataFrame]:
        """
        Llama a /pricehistory con los parametros dados y devuelve un
        DataFrame OHLCV (o None si falla / sin datos). Reintenta una vez
        si el token expiro (401).
        """
        if not self._asegurar_token():
            return None
        url = f"{BASE_URL}/pricehistory"
        try:
            r = self._get_autorizado(url, params=params, timeout=20)
            if r is None:
                return None
            r.raise_for_status()
            datos = r.json()
            if "candles" not in datos or not datos["candles"]:
                return None
            df = pd.DataFrame(datos["candles"])
            df["datetime"] = pd.to_datetime(df["datetime"], unit="ms", utc=True)
            df.set_index("datetime", inplace=True)
            df = df[["open", "high", "low", "close", "volume"]].astype(float)
            df.sort_index(inplace=True)
            return df
        except Exception as e:
            _log(f"[ERROR] pricehistory {params.get('symbol')} {params.get('frequencyType')}: {e}")
            return None

    def _fetch_minutos_chunked(self, ticker: str, frequency: int, dias: int,
                               max_chunks: int = 40) -> Optional[pd.DataFrame]:
        """
        Descarga velas de minutos paginando en bloques de <=10 dias usando
        startDate/endDate (la API limita ~10 dias de minuto por llamada).
        Concatena, deduplica y ordena. Registra la cobertura REAL obtenida
        (sin truncar en silencio).
        """
        ventana_ms = 10 * 24 * 60 * 60 * 1000
        ahora_ms = int(datetime.now().timestamp() * 1000)
        objetivo_ms = ahora_ms - dias * 24 * 60 * 60 * 1000

        partes = []
        end = ahora_ms
        chunks = 0
        while end > objetivo_ms and chunks < max_chunks:
            start = max(objetivo_ms, end - ventana_ms)
            params = {
                "symbol": ticker,
                "periodType": "day",
                "frequencyType": "minute",
                "frequency": frequency,
                "startDate": start,
                "endDate": end,
                "needExtendedHoursData": self.include_extended_hours_data,
            }
            df = self._pedir_pricehistory(params)
            if df is not None and len(df):
                partes.append(df)
            else:
                # Sin mas datos hacia atras: la API ya no devuelve historico
                break
            end = start - 1
            chunks += 1

        if not partes:
            return None
        combinado = pd.concat(partes)
        combinado = combinado[~combinado.index.duplicated(keep="first")].sort_index()
        cobertura_dias = (combinado.index[-1] - combinado.index[0]).days if len(combinado) > 1 else 0
        _log(f"[OK] {ticker} {frequency}m: {len(combinado)} velas | cobertura real ~{cobertura_dias} dias "
             f"(solicitado {dias})")
        return combinado

    @staticmethod
    def _resample(df: pd.DataFrame, regla: str) -> Optional[pd.DataFrame]:
        """
        Reagrupa velas OHLCV a una temporalidad mayor (ej. 30m -> '1h'/'4h').

        Usa label='right'/closed='right' para que cada vela quede etiquetada en
        su instante de CIERRE (coherente con velas cerradas y sin adelantar
        información). El último bucket puede estar aún en formación; se conserva
        y el sistema lo trata como vela en curso (en_formacion=True).
        """
        if df is None or len(df) == 0:
            return None
        agg = df.resample(regla, label="right", closed="right").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna(subset=["open", "high", "low", "close"])
        return agg if len(agg) else None

    def obtener_historico(self, ticker: str, intervalo: str = "1D",
                          dias: int = 365) -> Optional[pd.DataFrame]:
        """
        Devuelve el historico de un ticker en la temporalidad pedida.

        Temporalidades soportadas:
            "1W"  -> semanal nativo
            "1D"  -> diario nativo (hasta 20 años)
            "4H"  -> resample desde 30m
            "1H"  -> resample desde 30m
            "30m","15m","5m","1m" -> minuto nativo (paginado)

        'dias' es la profundidad de historia deseada (ej. 365 = 1 año).
        """
        intervalo = intervalo.upper() if intervalo.lower() in ("1w", "1d", "4h", "1h") else intervalo.lower()

        if intervalo == "1D":
            anios = max(1, min(20, (dias // 365) + 1))
            return self._pedir_pricehistory({
                "symbol": ticker, "periodType": "year", "period": anios,
                "frequencyType": "daily", "frequency": 1,
                "needExtendedHoursData": False,
            })

        if intervalo == "1W":
            anios = max(1, min(20, (dias // 365) + 2))
            return self._pedir_pricehistory({
                "symbol": ticker, "periodType": "year", "period": anios,
                "frequencyType": "weekly", "frequency": 1,
                "needExtendedHoursData": False,
            })

        if intervalo in ("4H", "1H"):
            base_30m = self._fetch_minutos_chunked(ticker, 30, min(dias, 60))
            if base_30m is None:
                return None
            return self._resample(base_30m, "4h" if intervalo == "4H" else "1h")

        # Minuto nativo (30m / 15m / 5m / 1m)
        freq_map = {"30m": 30, "15m": 15, "5m": 5, "1m": 1}
        freq = freq_map.get(intervalo, 1)
        tope_dias = {"30m": 60, "15m": 40, "5m": 20, "1m": 10}.get(intervalo, 10)
        return self._fetch_minutos_chunked(ticker, freq, min(dias, tope_dias))

    def obtener_1m_reciente(self, ticker: str, dias: int = 2) -> Optional[pd.DataFrame]:
        """Fetch LIGERO de solo las últimas velas de 1m (últimos 'dias' días). Se usa
        para refrescar el precio y la señal en cada ciclo, sin re-descargar todas las
        temporalidades: mantiene el precio al día como la pantalla del bróker
        (ThinkOrSwim) y no satura la API cuando hay muchos tickers."""
        return self._fetch_minutos_chunked(ticker, 1, dias)

    def obtener_multitemporal(self, ticker: str, config: Optional[dict] = None):
        """
        Construye un DatosMultiTemporal con todas las temporalidades que
        necesitan los 3 agentes SMC. Reutiliza una sola descarga de 30m
        para derivar 1H y 4H (eficiencia).

        Retorna un objeto agents.contracts.DatosMultiTemporal.
        """
        from agents.contracts import DatosMultiTemporal

        cfg = config or {}
        dias_dia = cfg.get("ai", {}).get("dias_historia_schwab", 365)

        # No automatic synthetic data, including PAPER: broker outages stay unavailable.
        if not self._asegurar_token():
            raise SchwabAuthorizationRequired("Schwab sin OAuth; no hay datos de mercado verificables")

        velas = {}
        # Semanal y diario (nativos, 1 año+)
        velas["1W"] = self.obtener_historico(ticker, "1W", dias_dia)
        velas["1D"] = self.obtener_historico(ticker, "1D", dias_dia)

        # 30m una sola vez -> 1H y 4H
        base_30m = self._fetch_minutos_chunked(ticker, 30, 60)
        velas["4H"] = self._resample(base_30m, "4h") if base_30m is not None else None
        velas["1H"] = self._resample(base_30m, "1h") if base_30m is not None else None

        # Micro (ejecucion y auditoria)
        velas["15m"] = self.obtener_historico(ticker, "15m", 40)
        velas["5m"] = self.obtener_historico(ticker, "5m", 20)
        velas["1m"] = self.obtener_historico(ticker, "1m", 10)

        # Filtrar temporalidades sin datos
        velas = {k: v for k, v in velas.items() if v is not None and len(v) > 0}

        # Incomplete broker data must not manufacture replacement candles.
        if len(velas) < 3:
            raise RuntimeError(f"{ticker}: datos multi-temporal insuficientes; sin precios de sustitución")

        return DatosMultiTemporal(ticker=ticker, velas=velas)

    def _multitemporal_fallback(self, ticker: str):
        """
        Genera un DatosMultiTemporal sintetico coherente (mismo sesgo de
        deriva en todas las temporalidades) para desarrollo/demo cuando no
        hay datos reales disponibles. No apto para trading real.
        """
        import numpy as np
        from agents.contracts import DatosMultiTemporal

        bases = {"BA": 225.0, "AAPL": 313.0, "TSLA": 394.0, "NVDA": 118.0,
                 "AMD": 162.0, "AMZN": 197.0, "META": 523.0, "RCL": 230.0, "UNH": 540.0}
        base = bases.get(ticker, 200.0)
        seed = hash(ticker) % 100000
        deriva_signo = 1 if (seed % 2 == 0) else -1

        def serie(n, paso_pct, freq):
            rng = np.random.default_rng(seed + hash(freq) % 1000)
            deriva = deriva_signo * paso_pct * base
            ruido = rng.normal(0, paso_pct * base * 0.6, n)
            close = base + np.cumsum(np.full(n, deriva) + ruido)
            close = np.maximum(close, base * 0.4)
            amp = np.abs(rng.normal(0, paso_pct * base * 0.5, n)) + paso_pct * base * 0.3
            op = close - rng.normal(0, paso_pct * base * 0.3, n)
            return pd.DataFrame({
                "open": op, "high": close + amp, "low": close - amp,
                "close": close, "volume": rng.integers(80000, 600000, n).astype(float),
            }, index=pd.date_range(end=datetime.now(), periods=n, freq=freq))

        velas = {
            "1W": serie(60, 0.010, "1W"),
            "1D": serie(252, 0.006, "1D"),
            "4H": serie(300, 0.004, "4h"),
            "1H": serie(400, 0.002, "1h"),
            "15m": serie(300, 0.0012, "15min"),
            "5m": serie(300, 0.0008, "5min"),
            "1m": serie(300, 0.0004, "1min"),
        }
        # Marcado como sintético: el motor NO abrirá operaciones reales con estos datos
        return DatosMultiTemporal(ticker=ticker, velas=velas, sintetico=True)

    # ----------------------------------------------------------
    # ESTADO REAL DE LA CUENTA (para reconciliación tras reinicio)
    # ----------------------------------------------------------

    def obtener_account_hash(self, account_number: str) -> Optional[str]:
        """
        Resuelve el HASH cifrado de la cuenta, requerido por la API de trading
        de Schwab para /accounts/{hash}/orders y /positions. Los datos de mercado
        toleran el número plano, pero las órdenes NO: exigen este hash.
        """
        if not self._asegurar_token():
            return None
        try:
            r = self._get_autorizado(
                f"{TRADER_URL}/accounts/accountNumbers", timeout=10
            )
            if r is None:
                return None
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list) or not data:
                return None
            for a in data:
                if str(a.get("accountNumber", "")) == str(account_number):
                    return a.get("hashValue")
            # Nunca operar otra cuenta por aproximación: una cuenta configurada
            # que no coincide exige corrección manual.
            if account_number:
                _log("[ERROR] La cuenta configurada no coincide con ninguna cuenta OAuth")
                return None
            return data[0].get("hashValue")
        except Exception as e:
            _log(f"[ERROR] obtener_account_hash: {e}")
            return None

    def obtener_posiciones(self, account_number: str) -> Optional[list]:
        """
        Devuelve las posiciones abiertas REALES de la cuenta Schwab.
        Se usa al arrancar para reconciliar: si el VPS se reinició en mitad
        de una operación, el bot recupera lo que hay abierto en el broker en
        lugar de quedar ciego.

        Devuelve una LISTA (posiblemente vacía) si la consulta tuvo éxito, o
        None si NO se pudo consultar (sin cuenta o fallo de API). La distinción
        importa: una lista vacía significa 'cuenta plana' y autoriza limpiezas;
        None significa 'no sé' y NO debe interpretarse como plano.
        """
        if not account_number:
            return None
        if not self._asegurar_token():
            return None
        try:
            r = self._get_autorizado(
                f"{TRADER_URL}/accounts/{account_number}",
                params={"fields": "positions"},
                timeout=10,
            )
            if r is None:
                return None
            r.raise_for_status()
            acc = r.json().get("securitiesAccount", {})
            out = []
            for p in acc.get("positions", []):
                inst = p.get("instrument", {})
                longq = float(p.get("longQuantity", 0) or 0)
                shortq = float(p.get("shortQuantity", 0) or 0)
                if longq == 0 and shortq == 0:
                    continue
                # Keep account-reported P/L separate from GALA's strategy P/L.
                # Schwab may omit currentDayProfitLoss for an individual
                # security; do not substitute local/synthetic values or zero.
                raw_day_pnl = p.get("currentDayProfitLoss")
                day_pnl = None
                if raw_day_pnl not in (None, ""):
                    try:
                        parsed = float(raw_day_pnl)
                        if math.isfinite(parsed):
                            day_pnl = round(parsed, 2)
                    except (TypeError, ValueError, OverflowError):
                        pass
                out.append({
                    "symbol": inst.get("symbol", ""),
                    "tipo": "BUY" if longq > 0 else "SELL",
                    "cantidad": int(longq if longq > 0 else shortq),
                    "broker_quantity": longq if longq > 0 else shortq,
                    "precio": float(p.get("averagePrice", 0) or 0),
                    "broker_position_day_pnl": day_pnl,
                })
            return out
        except Exception as e:
            _log(f"[ERROR] obtener_posiciones: {e}")
            return None

    def obtener_ordenes_abiertas(self, account_number: str) -> Optional[list]:
        """Devuelve las órdenes de trabajo (WORKING) abiertas de la cuenta.

        El endpoint /accounts/{hash}/orders de Schwab EXIGE una ventana temporal
        (fromEnteredTime/toEnteredTime); sin ella devuelve 400 y nos quedábamos
        ciegos a las órdenes vivas. Se pide una ventana amplia (7 días) para
        cubrir cualquier orden DAY todavía en trabajo.
        """
        if not account_number:
            return None
        if not self._asegurar_token():
            return None
        ahora = datetime.now(timezone.utc)
        desde = ahora - timedelta(days=7)
        # Colchón de +1 día en el límite superior: si el reloj del VPS va atrasado,
        # evita que una orden recién puesta (con enteredTime del reloj de Schwab,
        # posterior a 'ahora') quede fuera de la ventana y no se vea.
        hasta = ahora + timedelta(days=1)
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        try:
            r = self._get_autorizado(
                f"{TRADER_URL}/accounts/{account_number}/orders",
                params={
                    "status": "WORKING",
                    "fromEnteredTime": desde.strftime(fmt),
                    "toEnteredTime": hasta.strftime(fmt),
                    "maxResults": 500,
                },
                timeout=10,
            )
            if r is None:
                return None
            r.raise_for_status()
            data = r.json()
            if not valid_order_snapshot(data):
                _log(
                    "[ERROR] obtener_ordenes_abiertas: respuesta de ordenes "
                    "invalida; estado del broker desconocido"
                )
                return None
            return data
        except Exception as e:
            cuerpo = ""
            resp = getattr(e, "response", None)
            if resp is not None:
                try:
                    cuerpo = f" | HTTP {resp.status_code} | {(resp.text or '').strip()[:300]}"
                except Exception:
                    cuerpo = ""
            _log(f"[ERROR] obtener_ordenes_abiertas: {e}{cuerpo}")
            return None  # None = no se pudo consultar (distinto de [] = sin órdenes)

    # ----------------------------------------------------------
    # COMPATIBILIDAD (alias viejo)
    # ----------------------------------------------------------

    def obtener_velas_demo(self, ticker: str, cantidad: int = 400) -> pd.DataFrame:
        """Alias para compatibilidad -- ahora intenta datos reales primero."""
        result = self.obtener_velas(ticker, cantidad=cantidad)
        return result if result is not None else self._velas_fallback(ticker, cantidad)

