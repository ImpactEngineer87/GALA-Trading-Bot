"""
=============================================================
MÓDULO: ai_filter/data_collector.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Recolector de datos históricos desde la API de Charles Schwab
    para entrenar el modelo de IA (XGBoost).

    FLUJO:
      1. Conecta con la API de Schwab usando credenciales del .env
      2. Descarga datos históricos de 1 minuto para cada ticker
      3. Calcula la estrategia GALA6 (MACD, ATR, EMA)
      4. Genera un historial de señales con etiquetas (buena/falsa)
      5. Guarda el dataset en data/historial_senales.csv

    USO:
        python -m ai_filter.data_collector
        → Genera: data/historial_senales.csv
        → Luego puedes entrenar: python -m ai_filter.train_model
=============================================================
"""

import os
import sys
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

# Asegurar imports desde raíz del proyecto
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.strategy import GALA10Strategy
from api.schwab_client import SchwabClient
from engine.logger import log_info, log_error

# =============================================================
# CONFIGURACIÓN
# =============================================================

# 20+ acciones líquidas para el entrenamiento profundo (se puede sobrescribir
# desde config.yaml -> market.tickers)
TICKERS = ["BA", "AAPL", "TSLA", "NVDA", "AMZN", "META", "AMD", "RCL", "UNH",
           "MSFT", "GOOGL", "NFLX", "JPM", "BAC", "DIS", "INTC", "QCOM",
           "PYPL", "SBUX", "CAT"]
DIAS_HISTORIA = 365         # Días de historia a descargar por ticker (1 año objetivo)
MIN_MOVIMIENTO_VALIDO = 0.20  # % de movimiento mínimo para etiquetar señal como buena
VELAS_FUTURAS = 15          # Velas adelante para evaluar si la señal fue buena
OUTPUT_PATH = "data/historial_senales.csv"


# =============================================================
# RECOLECTOR PRINCIPAL
# =============================================================

class RecolectorDatos:
    """
    Descarga datos de Schwab y construye el dataset de entrenamiento para la IA.

    Proceso:
      1. Para cada ticker, descarga las velas de 1 minuto (hasta 5 días)
      2. Calcula los indicadores de la estrategia GALA6
      3. Detecta dónde hubo señales (BUY/SELL)
      4. Mira 15 velas hacia el futuro para ver si el precio se movió >= 0.20%
      5. Etiqueta la señal: 1 = buena, 0 = falsa
      6. Extrae las features relevantes para XGBoost
      7. Guarda todo en CSV
    """

    def __init__(self, config_path: str = "config.yaml"):
        import yaml
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

        self.estrategia = GALA10Strategy(self.config)
        self.schwab = SchwabClient(self.config)

        # Parámetros de historia desde config (1 año por defecto)
        ai_cfg = self.config.get("ai", {})
        self.dias = ai_cfg.get("dias_historia_schwab", DIAS_HISTORIA)
        self.intervalo = ai_cfg.get("intervalo_entrenamiento", "5m")
        # Permitir sobreescribir la lista de tickers desde config
        cfg_tickers = self.config.get("market", {}).get("tickers", [])
        self.tickers = [t.upper() for t in cfg_tickers] if cfg_tickers else list(TICKERS)

    def recolectar_todo(self, usar_demo: bool = False) -> pd.DataFrame:
        """
        Recolecta datos para todos los tickers y genera el dataset de entrenamiento.

        Parámetros:
            usar_demo → True para generar datos sintéticos (sin credenciales Schwab)

        Retorna:
            DataFrame con features y etiqueta lista para entrenar XGBoost
        """
        log_info(f"[IA] Iniciando recolección — {len(self.tickers)} tickers | "
                 f"{self.intervalo} | objetivo {self.dias} días")
        todos_los_registros = []

        for ticker in self.tickers:
            log_info(f"[IA] Procesando {ticker}...")
            try:
                if usar_demo:
                    df_velas = self.schwab.obtener_velas_demo(ticker, cantidad=400)
                else:
                    # Descarga profunda paginada (registra cobertura real por ticker)
                    df_velas = self.schwab.obtener_historico(
                        ticker, intervalo=self.intervalo, dias=self.dias)

                if df_velas is None or len(df_velas) < 50:
                    log_error(f"[IA] Sin datos para {ticker}", ticker)
                    continue

                registros = self._procesar_ticker(df_velas, ticker)
                todos_los_registros.extend(registros)
                log_info(f"[IA] {ticker} → {len(registros)} señales registradas")

            except Exception as e:
                log_error(f"[IA] Error en {ticker}: {e}", ticker)
                continue

        if not todos_los_registros:
            log_error("[IA] No se pudieron recolectar datos. ¿Están configuradas las credenciales de Schwab?")
            return pd.DataFrame()

        df_dataset = pd.DataFrame(todos_los_registros)
        Path("data").mkdir(exist_ok=True)
        df_dataset.to_csv(OUTPUT_PATH, index=False)
        log_info(f"[IA] Dataset guardado en {OUTPUT_PATH}")
        log_info(f"[IA] Total señales: {len(df_dataset)} | Buenas: {df_dataset['es_buena_senal'].sum()} | Falsas: {(df_dataset['es_buena_senal'] == 0).sum()}")
        return df_dataset

    def _procesar_ticker(self, df: pd.DataFrame, ticker: str) -> list:
        """
        Para un DataFrame de velas de un ticker:
          1. Calcula la estrategia GALA6
          2. Detecta señales BUY/SELL
          3. Etiqueta cada señal como buena (1) o falsa (0)
          4. Extrae las 7 features del modelo IA
        """
        df_calc = self.estrategia.calcular(df)
        registros = []

        # Encontrar filas con señales
        mascara_senal = df_calc['señal'].isin(['BUY', 'SELL'])
        indices_senal = df_calc.index[mascara_senal].tolist()

        for idx in indices_senal:
            pos = df_calc.index.get_loc(idx)

            # Necesitamos al menos VELAS_FUTURAS velas después de la señal para etiquetar
            if pos + VELAS_FUTURAS >= len(df_calc):
                continue

            fila_senal = df_calc.iloc[pos]
            fila_futura = df_calc.iloc[pos + VELAS_FUTURAS]

            precio_entrada = float(fila_senal['close'])
            precio_futuro = float(fila_futura['close'])
            tipo_senal = fila_senal['señal']

            # Calcular movimiento porcentual
            if tipo_senal == 'BUY':
                movimiento_pct = (precio_futuro - precio_entrada) / precio_entrada * 100
            else:  # SELL
                movimiento_pct = (precio_entrada - precio_futuro) / precio_entrada * 100

            # Etiqueta: 1 = buena (precio se movió >= MIN_MOVIMIENTO_VALIDO en la dirección)
            es_buena = 1 if movimiento_pct >= MIN_MOVIMIENTO_VALIDO else 0

            # Extraer features
            atr = float(fila_senal.get('atr', 0) or 0)
            volumen_actual = float(fila_senal.get('volume', 1) or 1)
            vol_medio = float(df_calc['volume'].iloc[max(0, pos-20):pos].mean() or 1)
            highs_8 = df_calc['high'].iloc[max(0, pos-8):pos]
            lows_8 = df_calc['low'].iloc[max(0, pos-8):pos]
            rango_pct = 0.0
            if not highs_8.empty and precio_entrada > 0:
                rango_pct = (highs_8.max() - lows_8.min()) / precio_entrada * 100

            # Codificar hora del mercado
            if hasattr(idx, 'hour'):
                hora_hhmm = idx.hour * 100 + idx.minute
            else:
                hora_hhmm = 1000

            registros.append({
                'ticker': ticker,
                'fecha': str(idx),
                'tipo_senal': tipo_senal,
                'precio_entrada': round(precio_entrada, 4),
                'movimiento_pct': round(movimiento_pct, 4),
                # ---- Features para el modelo IA ----
                'atr_relativo': round((atr / precio_entrada * 100) if precio_entrada > 0 else 0, 6),
                'hist_fuerza': abs(float(fila_senal.get('hist_line', 0) or 0)),
                'ema_slope': abs(float(fila_senal.get('ema_slope', 0) or 0)),
                'rango_pct': round(rango_pct, 4),
                'volumen_relativo': round(volumen_actual / vol_medio, 3),
                'hora_mercado': hora_hhmm,
                'cruces_recientes': 0,  # Se calcula en tiempo real; aquí se pone 0
                # ---- Target ----
                'es_buena_senal': es_buena,
            })

        return registros


# =============================================================
# EJECUCIÓN DIRECTA
# =============================================================

if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    import argparse
    parser = argparse.ArgumentParser(description="Recolector de datos para IA — Schwab API")
    parser.add_argument("--demo", action="store_true",
                        help="Usar datos sintéticos (sin credenciales Schwab)")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  RECOLECTOR DE DATOS — ENTRENAMIENTO IA")
    print("  Fuente: Charles Schwab API" if not args.demo else "  Fuente: Datos sintéticos (demo)")
    print("=" * 60 + "\n")

    recolector = RecolectorDatos()
    dataset = recolector.recolectar_todo(usar_demo=args.demo)

    if not dataset.empty:
        print(f"\n✅ Dataset generado con {len(dataset)} registros")
        print(f"   Guardado en: data/historial_senales.csv")
        print(f"\n   Distribución:")
        print(f"   - Señales BUENAS: {dataset['es_buena_senal'].sum()}")
        print(f"   - Señales FALSAS: {(dataset['es_buena_senal'] == 0).sum()}")
        print(f"\n   Siguiente paso: python -m ai_filter.train_model")
    else:
        print("❌ No se generaron datos. Revisa las credenciales de Schwab en .env")
