"""
=============================================================
MÓDULO: ai_filter/trainer.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Entrenamiento del filtro de Inteligencia Artificial.
    Usa XGBoost para aprender qué señales MACD son reales
    y cuáles son falsas, basándose en el historial de velas.

    EL MODELO APRENDE A DETECTAR:
      - Señales en zonas de compresión (bajo ATR relativo)
      - Señales contra la tendencia dominante
      - Señales sin volumen institucional de respaldo
      - Señales en horarios de baja liquidez

    TIEMPO DE ENTRENAMIENTO:
      - Con 3 meses de velas de 1 minuto: ~5-15 minutos
      - Con 1 año de datos: ~30-60 minutos
      - El modelo se guarda y se reutiliza automáticamente

    DATOS NECESARIOS:
      - Historial de velas OHLCV de Schwab API (automático)
      - El bot genera el historial de señales durante el paper trading
=============================================================
"""

import os
import pickle
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Optional, Tuple
from pathlib import Path


# =============================================================
# ENTRENADOR DEL MODELO IA
# =============================================================

class EntrenadorIA:
    """
    Entrena un modelo XGBoost para filtrar señales falsas del MACD.

    El modelo usa las siguientes características (features):
      - atr_relativo       → ATR / Precio (indica volatilidad)
      - hist_fuerza        → Fuerza del histograma MACD
      - ema_slope          → Pendiente de la EMA de tendencia
      - rango_pct          → Rango de precio en las últimas 8 velas
      - volumen_relativo   → Volumen vs media de 20 velas
      - hora_mercado       → Codificación de la ventana temporal
      - cruces_recientes   → Número de cruces en las últimas 2 horas

    Etiqueta (target):
      - 1 = Señal BUENA (el precio siguió la dirección > 0.20% en 15 min)
      - 0 = Señal FALSA (el precio no siguió la dirección)

    Uso:
        entrenador = EntrenadorIA()
        entrenador.entrenar_desde_csv("data/historial_senales.csv")
        resultado = entrenador.predecir(features_dict)
    """

    MODELO_PATH = "data/modelo_ia.pkl"
    SCALER_PATH = "data/scaler_ia.pkl"
    FEATURES = [
        'atr_relativo', 'hist_fuerza', 'ema_slope',
        'rango_pct', 'volumen_relativo', 'hora_mercado', 'cruces_recientes'
    ]

    def __init__(self):
        self.modelo = None
        self.scaler = None
        self._cargar_modelo_si_existe()

    def _cargar_modelo_si_existe(self):
        """Carga el modelo entrenado si ya existe en disco."""
        if os.path.exists(self.MODELO_PATH):
            try:
                with open(self.MODELO_PATH, 'rb') as f:
                    self.modelo = pickle.load(f)
                with open(self.SCALER_PATH, 'rb') as f:
                    self.scaler = pickle.load(f)
                print(f"✅ Modelo IA cargado desde: {self.MODELO_PATH}")
            except Exception as e:
                print(f"⚠️  No se pudo cargar el modelo IA: {e}")

    def entrenar_desde_dataframe(self, df: pd.DataFrame,
                                  columna_target: str = 'es_buena_senal') -> dict:
        """
        Entrena el modelo con un DataFrame de señales históricas.

        El DataFrame debe tener las columnas de FEATURES y la columna target.

        Parámetros:
            df              → DataFrame con señales históricas etiquetadas
            columna_target  → Nombre de la columna con la etiqueta (0 o 1)

        Retorna:
            Diccionario con métricas: accuracy, precision, recall
        """
        try:
            from xgboost import XGBClassifier
            from sklearn.preprocessing import StandardScaler
            from sklearn.model_selection import train_test_split
            from sklearn.metrics import classification_report
        except ImportError:
            print("❌ Instala XGBoost: pip install xgboost scikit-learn")
            return {}

        # Filtrar filas con valores válidos
        df_limpio = df[self.FEATURES + [columna_target]].dropna()
        if len(df_limpio) < 50:
            print("⚠️  Datos insuficientes para entrenar. Mínimo 50 señales históricas.")
            return {}

        X = df_limpio[self.FEATURES].values
        y = df_limpio[columna_target].values

        # Verificar que haya al menos dos clases
        clases_unicas = np.unique(y)
        if len(clases_unicas) < 2:
            print(f"⚠️  El dataset solo tiene una clase ({int(clases_unicas[0])}). "
                  "Necesitas datos reales de Schwab para que haya señales buenas Y falsas.")
            print("   Ejecuta: python -m ai_filter.data_collector (sin --demo)")
            return {}

        # Normalizar features
        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(X)

        # Dividir en train/test (sin stratify si hay clases muy desbalanceadas)
        n_min = int(np.bincount(y.astype(int)).min())
        usar_stratify = n_min >= 2
        X_train, X_test, y_train, y_test = train_test_split(
            X_scaled, y, test_size=0.2, random_state=42,
            stratify=y if usar_stratify else None
        )

        # Entrenar XGBoost
        print(f"Entrenando modelo con {len(X_train)} senales...")
        self.modelo = XGBClassifier(
            n_estimators=200,
            max_depth=5,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            eval_metric='logloss',
            random_state=42,
        )
        self.modelo.fit(X_train, y_train, verbose=False)

        # Evaluar
        y_pred = self.modelo.predict(X_test)
        report = classification_report(y_test, y_pred, output_dict=True)
        accuracy = report['accuracy']

        # Guardar modelo
        Path("data").mkdir(exist_ok=True)
        with open(self.MODELO_PATH, 'wb') as f:
            pickle.dump(self.modelo, f)
        with open(self.SCALER_PATH, 'wb') as f:
            pickle.dump(self.scaler, f)

        metricas = {
            'accuracy': round(accuracy * 100, 1),
            'precision_buena': round(report.get('1', {}).get('precision', 0) * 100, 1),
            'recall_buena': round(report.get('1', {}).get('recall', 0) * 100, 1),
            'muestras_train': len(X_train),
            'muestras_test': len(X_test),
        }
        print(f"✅ Modelo entrenado | Accuracy: {metricas['accuracy']}%")
        return metricas

    def predecir(self, features: dict) -> Tuple[bool, float]:
        """
        Predice si una señal es buena o falsa.

        Parámetros:
            features → Diccionario con los valores de las 7 características

        Retorna:
            (es_buena: bool, probabilidad: float de 0.0 a 1.0)
        """
        if self.modelo is None or self.scaler is None:
            # Sin modelo entrenado → dejar pasar todas las señales
            return True, 0.5

        try:
            X = np.array([[features.get(f, 0) for f in self.FEATURES]])
            X_scaled = self.scaler.transform(X)
            prob = self.modelo.predict_proba(X_scaled)[0][1]
            es_buena = prob >= 0.60  # Umbral: 60% de confianza
            return es_buena, round(float(prob), 3)
        except Exception as e:
            print(f"⚠️  Error en predicción IA: {e}")
            return True, 0.5

    def construir_features_desde_vela(self, df: pd.DataFrame,
                                       hora_mercado: int = 930) -> dict:
        """
        Extrae automáticamente las features de la última vela del DataFrame.

        Parámetros:
            df           → DataFrame con indicadores calculados (strategy.calcular())
            hora_mercado → Hora en formato HHMM (ej: 942 = 09:42)

        Retorna:
            Diccionario de features listo para predecir()
        """
        if df.empty or len(df) < 20:
            return {f: 0 for f in self.FEATURES}

        ultima = df.iloc[-1]
        precio = float(ultima['close'])
        atr = float(ultima.get('atr', 0))

        # Calcular volumen relativo
        vol_actual = float(ultima.get('volume', 0))
        vol_medio = float(df['volume'].tail(20).mean()) if 'volume' in df else 1
        vol_relativo = vol_actual / vol_medio if vol_medio > 0 else 1.0

        # Rango de las últimas 8 velas
        highs = df['high'].tail(8)
        lows = df['low'].tail(8)
        rango_pct = ((highs.max() - lows.min()) / precio * 100) if precio > 0 else 0

        return {
            'atr_relativo': (atr / precio * 100) if precio > 0 else 0,
            'hist_fuerza': abs(float(ultima.get('hist_line', 0))),
            'ema_slope': abs(float(ultima.get('ema_slope', 0))),
            'rango_pct': round(rango_pct, 4),
            'volumen_relativo': round(vol_relativo, 3),
            'hora_mercado': hora_mercado,
            'cruces_recientes': 0,  # Se actualiza desde el motor temporal
        }
