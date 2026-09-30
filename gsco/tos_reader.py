"""
=============================================================
MÓDULO: gsco/tos_reader.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Extractor de posteos GSCO desde ThinkorSwim (TOS).

    MÉTODO DE EXTRACCIÓN:
      ThinkorSwim permite exportar datos del Nivel II y los
      posteos institucionales mediante:
        1. RTD (Real-Time Data) vía Excel DDE (Windows)
        2. Exportación a CSV automática con una extensión TOS
        3. Lectura de la carpeta de exportación de TOS

    Este módulo lee los CSV exportados por TOS, filtra los
    posteos del emisor GSCO y los envía a Google Sheets.

    CONFIGURACIÓN EN .env:
      TOS_EXPORT_FOLDER=C:/TOS/exports/
      TOS_GSCO_SHEET_NAME=Posteos_GSCO
=============================================================
"""

import os
import csv
import time
import threading
from datetime import datetime, date
from typing import List, Callable, Optional
from pathlib import Path

from gsco.sheets_client import SheetsClient, PosteoGSCO


# =============================================================
# LECTOR DE POSTEOS TOS
# =============================================================

class TOSReader:
    """
    Monitoriza la carpeta de exportaciones de ThinkorSwim
    y extrae automáticamente los posteos del emisor GSCO.

    Funciona en un hilo separado para no bloquear el bot.

    Uso:
        reader = TOSReader(sheets_client, export_folder="C:/TOS/exports/")
        reader.iniciar_monitoreo(tickers=["BA", "AAPL", "TSLA"])
    """

    TICKER_COLUMNAS = {
        "ticker": ["Ticker", "Symbol", "Activo"],
        "direccion": ["Bid/Ask", "Side", "Dirección", "Direction"],
        "fuente": ["Fuente", "Source", "Emisor"],
        "precio": ["Precio", "Price", "Nivel"],
        "hora": ["Fecha/Hora", "Time", "DateTime", "Timestamp"],
    }

    def __init__(self, sheets: SheetsClient, export_folder: str = ""):
        self.sheets = sheets
        self.export_folder = export_folder or os.getenv("TOS_EXPORT_FOLDER", "data/tos_exports/")
        self._activo = False
        self._hilo: Optional[threading.Thread] = None
        self._archivos_procesados = set()
        self._posteos_en_memoria: List[PosteoGSCO] = []

        # Crear carpeta si no existe
        Path(self.export_folder).mkdir(parents=True, exist_ok=True)

    # ==========================================================
    # INICIO / DETENCIÓN DEL MONITOREO
    # ==========================================================

    def iniciar_monitoreo(self, tickers: List[str], intervalo_seg: int = 30):
        """
        Inicia el monitoreo en un hilo separado.

        Cada {intervalo_seg} segundos revisa la carpeta de exportaciones
        en busca de nuevos archivos CSV con posteos GSCO.

        Parámetros:
            tickers        → Lista de tickers a filtrar
            intervalo_seg  → Intervalo de revisión en segundos
        """
        self._activo = True
        self._hilo = threading.Thread(
            target=self._loop_monitoreo,
            args=(tickers, intervalo_seg),
            daemon=True
        )
        self._hilo.start()
        print(f"👁️  TOS Reader activo — monitorizando: {', '.join(tickers)}")

    def detener(self):
        """Detiene el monitoreo."""
        self._activo = False
        print("🛑 TOS Reader detenido")

    def _loop_monitoreo(self, tickers: List[str], intervalo: int):
        """Bucle principal del hilo de monitoreo."""
        while self._activo:
            try:
                self._procesar_nuevos_archivos(tickers)
            except Exception as e:
                print(f"❌ Error en TOS Reader: {e}")
            time.sleep(intervalo)

    # ==========================================================
    # PROCESAMIENTO DE ARCHIVOS CSV
    # ==========================================================

    def _procesar_nuevos_archivos(self, tickers: List[str]):
        """
        Busca nuevos archivos CSV en la carpeta de exportaciones
        y procesa los que aún no han sido leídos.
        """
        carpeta = Path(self.export_folder)
        archivos_csv = list(carpeta.glob("*.csv"))

        for archivo in archivos_csv:
            if str(archivo) in self._archivos_procesados:
                continue

            nuevos_posteos = self._leer_csv_tos(archivo, tickers)
            if nuevos_posteos:
                print(f"📥 {len(nuevos_posteos)} nuevos posteos GSCO desde: {archivo.name}")
                for posteo in nuevos_posteos:
                    self._posteos_en_memoria.append(posteo)
                    # Guardar en Google Sheets
                    self.sheets.guardar_cruce_en_memoria(
                        ticker=posteo.ticker,
                        hito=f"GSCO {posteo.direccion}",
                        direccion=posteo.direccion,
                        close=posteo.precio,
                        high=posteo.precio,
                        low=posteo.precio,
                    )

            self._archivos_procesados.add(str(archivo))

    def _leer_csv_tos(self, archivo: Path, tickers: List[str]) -> List[PosteoGSCO]:
        """
        Lee un archivo CSV exportado desde ThinkorSwim y extrae
        los posteos del emisor GSCO para los tickers especificados.

        El CSV puede tener diferentes nombres de columnas según la
        versión de TOS — se detectan automáticamente.
        """
        posteos = []
        try:
            with open(archivo, 'r', encoding='utf-8-sig', errors='ignore') as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames:
                    return []

                # Detectar nombres de columnas
                cols = self._detectar_columnas(reader.fieldnames)
                if not all(cols.values()):
                    return []  # No se pudo mapear

                for fila in reader:
                    try:
                        fuente = str(fila.get(cols["fuente"], "")).strip().upper()
                        if fuente != "GSCO":
                            continue

                        ticker = str(fila.get(cols["ticker"], "")).strip().upper()
                        if ticker not in [t.upper() for t in tickers]:
                            continue

                        posteos.append(PosteoGSCO(
                            ticker=ticker,
                            direccion=str(fila.get(cols["direccion"], "Bid")).strip(),
                            fuente="GSCO",
                            precio=float(str(fila.get(cols["precio"], "0")).replace(",", ".")),
                            hora=str(fila.get(cols["hora"], datetime.now().strftime("%d/%m/%Y %H:%M:%S"))),
                        ))
                    except Exception:
                        continue
        except Exception as e:
            print(f"❌ Error leyendo CSV TOS '{archivo.name}': {e}")

        return posteos

    def _detectar_columnas(self, fieldnames: List[str]) -> dict:
        """
        Mapea los nombres de columnas del CSV a las columnas
        estándar del sistema.
        """
        campos = {k: None for k in self.TICKER_COLUMNAS}
        for campo, opciones in self.TICKER_COLUMNAS.items():
            for opcion in opciones:
                if opcion in fieldnames:
                    campos[campo] = opcion
                    break
        return campos

    # ==========================================================
    # ACCESO A POSTEOS EN MEMORIA
    # ==========================================================

    def obtener_posteos_hoy(self, ticker: str) -> List[PosteoGSCO]:
        """
        Retorna los posteos GSCO de hoy para un ticker,
        leídos de la memoria interna del lector.
        """
        hoy = date.today().strftime("%d/%m/%Y")
        return [
            p for p in self._posteos_en_memoria
            if p.ticker.upper() == ticker.upper()
            and p.hora.startswith(hoy)
        ]

    def simular_posteo(self, ticker: str, direccion: str, precio: float):
        """
        Añade un posteo manual para pruebas en modo demo.
        Útil para simular que GSCO publicó un nivel.
        """
        hora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        posteo = PosteoGSCO(
            ticker=ticker.upper(),
            direccion=direccion,
            fuente="GSCO",
            precio=precio,
            hora=hora,
        )
        self._posteos_en_memoria.append(posteo)
        print(f"📋 [DEMO] Posteo GSCO simulado: {ticker} | {direccion} @ ${precio}")
        return posteo
