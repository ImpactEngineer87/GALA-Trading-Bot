"""
=============================================================
MÓDULO: api/order_executor.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Ejecutor de órdenes para la API de Charles Schwab.

    REGLA IMPORTANTE (definida por el cliente):
      ❌ NO se usan órdenes Market (causan slippage)
      ✅ SIEMPRE se usan órdenes Limit

    En Paper Trading mode, las órdenes se simulan sin enviar
    nada al broker real.
=============================================================
"""

import os
import json
import requests
from typing import Optional
from dotenv import load_dotenv
from core.risk_manager import OrdenTradingCalculada
from api.schwab_transport import schwab_request

load_dotenv()


# =============================================================
# EJECUTOR DE ÓRDENES
# =============================================================

class EjecutorDeOrdenes:
    """
    Envía órdenes LIMIT a Charles Schwab API.

    En modo Paper Trading (config: paper_trading: true),
    todas las órdenes se simulan y se registran en log,
    pero NO se envían al broker real.

    Uso:
        ejecutor = EjecutorDeOrdenes(paper_trading=True)
        resultado = ejecutor.ejecutar(orden, account_number="12345678")
    """

    TRADER_URL = "https://api.schwabapi.com/trader/v1"

    def __init__(self, paper_trading: bool = True, access_token: str = ""):
        """
        Parámetros:
            paper_trading → True = modo demo (sin órdenes reales)
            access_token  → Token OAuth2 de Schwab
        """
        self.paper_trading = paper_trading
        self.access_token = access_token
        self._session = requests.Session()
        if access_token:
            self._session.headers.update({
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            })

        if self.paper_trading:
            print("⚠️  EJECUTOR EN MODO PAPER TRADING — Órdenes simuladas")

    def ejecutar(self, orden: OrdenTradingCalculada,
                 account_number: str) -> dict:
        """
        Ejecuta una orden LIMIT en la cuenta especificada.

        En paper trading: simula la orden y retorna estado ficticio.
        En modo real: envía la orden a Schwab API.

        Parámetros:
            orden          → OrdenTradingCalculada del risk_manager
            account_number → Número de cuenta Schwab

        Retorna:
            Dict con el resultado: {'status': 'OK'/'ERROR', 'order_id': ...}
        """
        if self.paper_trading:
            return self._simular_orden(orden)
        else:
            return self._enviar_orden_real(orden, account_number)

    def _simular_orden(self, orden: OrdenTradingCalculada) -> dict:
        """
        Simula la ejecución de una orden sin enviarla al broker.
        Útil durante las fases de desarrollo y prueba.
        """
        print(f"📋 [PAPER] Simulando orden: {orden}")
        return {
            "status": "SIMULADA",
            "order_id": f"PAPER-{orden.ticker}-{orden.tipo}",
            "precio_limite": orden.precio_limite,
            "stop_loss": orden.stop_loss,
            "trailing_stop_pct": orden.trailing_stop_pct,
            "cantidad": orden.cantidad,
        }

    def _enviar_orden_real(self, orden: OrdenTradingCalculada,
                           account_number: str) -> dict:
        """
        Envía una orden LIMIT real a la API de Charles Schwab.

        Estructura de la orden según documentación de Schwab API:
          - orderType: "LIMIT" (nunca MARKET)
          - session: "NORMAL"
          - duration: "DAY"
          - orderStrategyType: "SINGLE"
        """
        if orden.tipo == 'BUY':
            instruccion = "BUY"
            asset_type = "EQUITY"
        elif orden.tipo == 'SELL':
            instruccion = "SELL"
            asset_type = "EQUITY"
        else:
            return {"status": "ERROR", "mensaje": "Tipo de orden inválido"}

        cuerpo_orden = {
            "orderType": "LIMIT",
            "session": "NORMAL",
            "price": str(orden.precio_limite),
            "duration": "DAY",
            "orderStrategyType": "TRIGGER",
            "orderLegCollection": [
                {
                    "orderLegType": "EQUITY",
                    "legId": 1,
                    "instrument": {
                        "symbol": orden.ticker,
                        "assetType": asset_type,
                    },
                    "instruction": instruccion,
                    "positionEffect": "OPENING",
                    "quantity": orden.cantidad,
                }
            ],
            # Trailing stop como orden secundaria
            "childOrderStrategies": [
                {
                    "orderType": "TRAILING_STOP",
                    "stopPriceLinkBasis": "PERCENT",
                    "stopPriceOffset": orden.trailing_stop_pct,
                    "duration": "DAY",
                    "orderStrategyType": "SINGLE",
                    "orderLegCollection": [
                        {
                            "instruction": "SELL" if instruccion == "BUY" else "BUY",
                            "quantity": orden.cantidad,
                            "instrument": {
                                "symbol": orden.ticker,
                                "assetType": "EQUITY"
                            }
                        }
                    ]
                }
            ]
        }

        url = f"{self.TRADER_URL}/accounts/{account_number}/orders"
        try:
            respuesta = schwab_request(self._session.post, url, json=cuerpo_orden, timeout=10, recovery_safe=False)
            respuesta.raise_for_status()
            order_id = respuesta.headers.get("Location", "").split("/")[-1]
            print(f"✅ Orden enviada a Schwab | ID: {order_id} | {orden}")
            return {"status": "OK", "order_id": order_id}
        except requests.exceptions.RequestException as e:
            print(f"❌ Error enviando orden: {e}")
            return {"status": "ERROR", "mensaje": str(e)}

    def cancelar_orden(self, account_number: str, order_id: str) -> bool:
        """
        Cancela una orden pendiente en Schwab.

        Retorna True si se canceló correctamente, False en caso de error.
        """
        if self.paper_trading:
            print(f"📋 [PAPER] Cancelando orden simulada: {order_id}")
            return True

        url = f"{self.TRADER_URL}/accounts/{account_number}/orders/{order_id}"
        try:
            respuesta = schwab_request(self._session.delete, url, timeout=10, recovery_safe=False)
            respuesta.raise_for_status()
            print(f"✅ Orden {order_id} cancelada correctamente")
            return True
        except Exception as e:
            print(f"❌ Error cancelando orden {order_id}: {e}")
            return False
