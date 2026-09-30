"""
=============================================================
MÓDULO: gsco/sheets_client.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Cliente de Google Sheets para leer y escribir:
      - Hoja "Posteos_GSCO": posteos institucionales del día
      - Hoja "Memoria_Bot": historial de cruces clave del día

    Requiere:
      - google-auth, gspread instalados
      - Archivo de credenciales de servicio (service_account.json)
      - La hoja de Google Sheets compartida con el correo de servicio

    CONFIGURACIÓN EN .env (se acepta cualquiera de los dos nombres):
      GOOGLE_SHEET_ID=tu_spreadsheet_id   (equivalente: GOOGLE_SHEETS_ID)
      GOOGLE_CREDENTIALS_FILE=service_account.json
=============================================================
"""

import os
import json
from datetime import datetime, date
from typing import List, Optional, Dict
from dataclasses import dataclass


# =============================================================
# ESTRUCTURA DE UN POSTEO GSCO
# =============================================================

@dataclass
class PosteoGSCO:
    """
    Representa un posteo del emisor GSCO extraído de ThinkorSwim.

    Atributos:
        ticker    → Símbolo del activo (ej: 'BA')
        direccion → 'Bid' (alcista) o 'Ask' (bajista)
        fuente    → Siempre 'GSCO' en esta integración
        precio    → Precio del nivel mencionado
        hora      → Hora del posteo (DD/MM/YYYY HH:MM:SS)
    """
    ticker: str
    direccion: str    # 'Bid' o 'Ask'
    fuente: str       # 'GSCO'
    precio: float
    hora: str         # "DD/MM/YYYY HH:MM:SS"

    def es_alcista(self) -> bool:
        return self.direccion.lower() in ('bid', 'compra', 'buy', 'bull')

    def es_bajista(self) -> bool:
        return self.direccion.lower() in ('ask', 'venta', 'sell', 'bear')


# =============================================================
# CLIENTE GOOGLE SHEETS
# =============================================================

class SheetsClient:
    """
    Gestiona la lectura y escritura en Google Sheets.

    Funciona en dos modos:
      - Modo Real: Conectado a Google Sheets API (requiere credenciales)
      - Modo Demo: Usa datos locales simulados para desarrollo

    Uso:
        client = SheetsClient()
        posteos = client.obtener_posteos_gsco('BA')
    """

    def __init__(self, spreadsheet_id: str = "", credentials_file: str = "service_account.json"):
        # Se acepta CUALQUIERA de los dos nombres: el header documentaba
        # GOOGLE_SHEET_ID y el código leía GOOGLE_SHEETS_ID → si el operador
        # seguía la doc, la hoja quedaba en modo DEMO en silencio (GSCO nunca se
        # activaba). Ahora se leen ambos.
        self.spreadsheet_id = (spreadsheet_id or os.getenv("GOOGLE_SHEETS_ID")
                               or os.getenv("GOOGLE_SHEET_ID", ""))
        self.credentials_file = credentials_file
        self._gc = None
        self._sheet = None
        self._modo_demo = True

        if self.spreadsheet_id and os.path.exists(self.credentials_file):
            self._conectar()
        else:
            print("⚠️  SheetsClient en modo DEMO — usando datos simulados")
            print("   Para activar: configura GOOGLE_SHEETS_ID en .env")
            print("   y coloca service_account.json en la raíz del proyecto")

    def _conectar(self):
        """Conecta a Google Sheets usando credenciales de servicio."""
        try:
            import gspread
            from google.oauth2.service_account import Credentials

            scopes = [
                'https://www.googleapis.com/auth/spreadsheets',
                'https://www.googleapis.com/auth/drive',
            ]
            creds = Credentials.from_service_account_file(
                self.credentials_file, scopes=scopes
            )
            self._gc = gspread.authorize(creds)
            self._sheet = self._gc.open_by_key(self.spreadsheet_id)
            self._modo_demo = False
            print(f"✅ Conectado a Google Sheets: {self._sheet.title}")
        except Exception as e:
            print(f"❌ Error conectando a Sheets: {e}")
            self._modo_demo = True

    # ==========================================================
    # LEER POSTEOS GSCO
    # ==========================================================

    def obtener_posteos_gsco(self, ticker: str, solo_hoy: bool = True) -> List[PosteoGSCO]:
        """
        Lee los posteos del emisor GSCO para un ticker.

        Formato esperado en Google Sheets (hoja "Posteos_GSCO"):
          Columnas: Ticker | Bid/Ask | Fuente | Precio | Fecha/Hora

        Parámetros:
            ticker    → Símbolo del activo (ej: 'BA')
            solo_hoy  → Si True, filtra solo posteos del día actual

        Retorna:
            Lista de PosteoGSCO
        """
        if self._modo_demo:
            return self._posteos_demo(ticker)

        try:
            hoja = self._sheet.worksheet("Posteos_GSCO")
            filas = hoja.get_all_records()
            posteos = []
            hoy = date.today().strftime("%d/%m/%Y")

            for fila in filas:
                # Filtrar por ticker y fuente GSCO
                if str(fila.get("Ticker", "")).upper() != ticker.upper():
                    continue
                if str(fila.get("Fuente", "")).upper() != "GSCO":
                    continue
                # Filtrar por fecha si solo_hoy=True
                hora_str = str(fila.get("Fecha/Hora", ""))
                if solo_hoy and not hora_str.startswith(hoy):
                    continue

                try:
                    posteos.append(PosteoGSCO(
                        ticker=ticker.upper(),
                        direccion=str(fila.get("Bid/Ask", "")).strip(),
                        fuente="GSCO",
                        precio=float(str(fila.get("Precio", "0")).replace(",", ".")),
                        hora=hora_str,
                    ))
                except Exception:
                    continue

            return posteos
        except Exception as e:
            print(f"❌ Error leyendo posteos GSCO para {ticker}: {e}")
            return []

    # ==========================================================
    # LEER / ESCRIBIR MEMORIA DEL BOT
    # ==========================================================

    def guardar_cruce_en_memoria(self, ticker: str, hito: str, direccion: str,
                                  close: float, high: float, low: float):
        """
        Guarda un cruce clave en la hoja "Memoria_Bot".

        Formato: Fecha | Ticker | Hito | Dirección | Close | High | Low
        """
        if self._modo_demo:
            print(f"[DEMO Sheets] Guardando: {ticker} | {hito} | {direccion} | C:{close}")
            return

        try:
            hoja = self._sheet.worksheet("Memoria_Bot")
            fila = [
                datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                ticker.upper(),
                hito,
                direccion,
                round(close, 2),
                round(high, 2),
                round(low, 2),
            ]
            hoja.append_row(fila)
        except Exception as e:
            print(f"❌ Error guardando en Memoria_Bot: {e}")

    def obtener_memoria_del_dia(self, ticker: str) -> List[Dict]:
        """
        Lee todos los hitos guardados hoy para un ticker.
        """
        if self._modo_demo:
            return []

        try:
            hoja = self._sheet.worksheet("Memoria_Bot")
            filas = hoja.get_all_records()
            hoy = date.today().strftime("%d/%m/%Y")
            return [
                f for f in filas
                if str(f.get("Fecha", "")).startswith(hoy)
                and str(f.get("Ticker", "")).upper() == ticker.upper()
            ]
        except Exception as e:
            print(f"❌ Error leyendo Memoria_Bot: {e}")
            return []

    # ==========================================================
    # MODO DEMO — Datos simulados
    # ==========================================================

    def _posteos_demo(self, ticker: str) -> List[PosteoGSCO]:
        """Retorna posteos GSCO simulados para pruebas."""
        import random
        hoy = datetime.now().strftime("%d/%m/%Y")
        posteos = []
        for i in range(random.randint(1, 4)):
            hora = f"{hoy} {9 + i}:{random.randint(30, 59):02d}:00"
            posteos.append(PosteoGSCO(
                ticker=ticker,
                direccion=random.choice(["Bid", "Ask"]),
                fuente="GSCO",
                precio=round(220 + random.random() * 20, 2),
                hora=hora,
            ))
        return posteos
