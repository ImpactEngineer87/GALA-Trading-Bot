"""
=============================================================
MÓDULO: notifications/telegram_bot.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Sistema de alertas por Telegram.
    Envía mensajes formateados al chat del cliente con:
      - Señales BUY/SELL con precio, muro y contexto
      - Alertas de ventanas temporales (09:42, 10:30, 12:00, 12:30)
      - Correlación institucional GSCO
      - Señales Maestras (máxima prioridad)

    CONFIGURACIÓN EN .env:
      TELEGRAM_BOT_TOKEN=tu_token_del_bot
      TELEGRAM_CHAT_ID=tu_chat_id

    Para obtener el token:
      1. Hablar con @BotFather en Telegram
      2. Crear un nuevo bot con /newbot
      3. Copiar el token que te da BotFather

    Para obtener el Chat ID:
      1. Añadir el bot a tu grupo o chat
      2. Enviar un mensaje al bot
      3. Visitar: https://api.telegram.org/bot<TOKEN>/getUpdates
=============================================================
"""

import os
import requests
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


# =============================================================
# CLIENTE TELEGRAM
# =============================================================

class TelegramBot:
    """
    Envía alertas formateadas al chat de Telegram del cliente.

    En modo demo, las alertas se imprimen en consola en lugar
    de enviarse a Telegram.

    Uso:
        bot = TelegramBot()
        bot.enviar_alerta_buy("BA", 227.49, 228.10, 226.80, hora_clave=True)
    """

    BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._modo_demo = not (self.token and self.chat_id)

        if self._modo_demo:
            print("⚠️  Telegram en modo DEMO — alertas en consola")
            print("   Configura TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en .env")

    # ==========================================================
    # ENVÍO GENÉRICO
    # ==========================================================

    def enviar(self, mensaje: str, silencioso: bool = False) -> bool:
        """
        Envía un mensaje al chat de Telegram.

        Parámetros:
            mensaje    → Texto del mensaje (soporta HTML básico)
            silencioso → Si True, no hace sonar la notificación

        Retorna:
            True si se envió correctamente, False si hubo error
        """
        if self._modo_demo:
            hora = datetime.now().strftime("%H:%M:%S")
            print(f"\n{'='*55}")
            print(f"📱 [TELEGRAM DEMO] {hora}")
            print(mensaje)
            print('='*55)
            return True

        url = self.BASE_URL.format(token=self.token)
        payload = {
            "chat_id": self.chat_id,
            "text": mensaje,
            "parse_mode": "HTML",
            "disable_notification": silencioso,
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            print(f"❌ Error enviando Telegram: {e}")
            return False

    # ==========================================================
    # ALERTAS PREDEFINIDAS
    # ==========================================================

    def enviar_alerta_buy(self, ticker: str, precio: float, muro_high: float,
                           muro_low: float, hora_clave: bool = False,
                           contexto_gsco: str = "") -> bool:
        """
        Alerta de COMPRA validada (BUY con patas abiertas).
        """
        hora = datetime.now().strftime("%H:%M:%S")
        emoji_prioridad = "🔥 [PRIORIDAD ALTA — SEÑAL MAESTRA]" if hora_clave else "🟢"
        msg = (
            f"{emoji_prioridad}\n"
            f"<b>[COMPRA — {ticker}]</b>\n"
            f"Precio de entrada: <b>${precio:.2f}</b>\n"
            f"Muro de referencia: High ${muro_high:.2f} | Low ${muro_low:.2f}\n"
            f"Hora: {hora}"
        )
        if contexto_gsco:
            msg += f"\n{contexto_gsco}"
        return self.enviar(msg)

    def enviar_alerta_sell(self, ticker: str, precio: float, muro_high: float,
                            muro_low: float, hora_clave: bool = False,
                            contexto_gsco: str = "") -> bool:
        """
        Alerta de VENTA validada (SELL con patas abiertas).
        """
        hora = datetime.now().strftime("%H:%M:%S")
        emoji_prioridad = "🔥 [PRIORIDAD ALTA — SEÑAL MAESTRA]" if hora_clave else "🔴"
        msg = (
            f"{emoji_prioridad}\n"
            f"<b>[VENTA — {ticker}]</b>\n"
            f"Precio de entrada: <b>${precio:.2f}</b>\n"
            f"Muro de referencia: High ${muro_high:.2f} | Low ${muro_low:.2f}\n"
            f"Hora: {hora}"
        )
        if contexto_gsco:
            msg += f"\n{contexto_gsco}"
        return self.enviar(msg)

    def enviar_alerta_temporal(self, texto: str, prioridad: str = "media") -> bool:
        """
        Envía una alerta de ventana temporal (09:42, 10:30, etc.).
        Prioridad 'alta' = notificación inmediata.
        """
        silencioso = prioridad == "baja"
        return self.enviar(texto, silencioso=silencioso)

    def enviar_senal_maestra(self, ticker: str, direccion: str, precio: float,
                              hora_clave: str, nivel_gsco: float) -> bool:
        """
        Envía la Señal Maestra cuando confluyen los 4 factores.
        """
        emoji = "🟢" if direccion == "BULL" else "🔴"
        dir_texto = "COMPRA" if direccion == "BULL" else "VENTA"
        msg = (
            f"🔥 <b>[PRIORIDAD ALTA — SEÑAL MAESTRA]</b>\n\n"
            f"{emoji} <b>[{dir_texto} — {ticker}]</b>\n"
            f"Precio: <b>${precio:.2f}</b>\n"
            f"Hora clave: {hora_clave}\n"
            f"MACD confirmado: ✅\n"
            f"Via libre historica: ✅\n"
            f"GSCO Match: ✅ Nivel ${nivel_gsco:.2f}\n\n"
            f"<i>Alta probabilidad de movimiento direccional</i>"
        )
        return self.enviar(msg, silencioso=False)

    def enviar_inicio_sesion(self, tickers: list) -> bool:
        """Notifica el inicio del bot al chat de Telegram."""
        hora = datetime.now().strftime("%H:%M:%S")
        msg = (
            f"🤖 <b>GALA10 Chocoletra Bot — Iniciado</b>\n"
            f"Hora: {hora}\n"
            f"Activos: {', '.join(tickers)}\n"
            f"Modo: Paper Trading\n"
            f"Estado: ACTIVO"
        )
        return self.enviar(msg)

    def enviar_fin_sesion(self, resumen: dict) -> bool:
        """Envía resumen al final de la jornada."""
        msg = (
            f"🏁 <b>Sesión finalizada — GALA10 Chocoletra</b>\n\n"
            f"Señales BUY:  {resumen.get('buy', 0)}\n"
            f"Señales SELL: {resumen.get('sell', 0)}\n"
            f"Omitidas (Rango):    {resumen.get('omr', 0)}\n"
            f"Omitidas (Tendencia): {resumen.get('omt', 0)}\n"
            f"Alertas Telegram: {resumen.get('alertas', 0)}\n"
            f"Coincidencias GSCO: {resumen.get('gsco_match', 0)}"
        )
        return self.enviar(msg)
