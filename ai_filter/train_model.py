"""
=============================================================
MÓDULO: ai_filter/train_model.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Script de entrenamiento del modelo de IA (XGBoost).

    PRERREQUISITO:
        Primero ejecutar el recolector de datos:
        python -m ai_filter.data_collector

    USO:
        python -m ai_filter.train_model
        → Carga: data/historial_senales.csv
        → Entrena XGBoost
        → Guarda: data/modelo_ia.pkl + data/scaler_ia.pkl

    TIEMPO ESTIMADO:
        - 3 meses de datos → ~5-10 minutos
        - 1 año de datos   → ~30-60 minutos

    QUE APRENDE EL MODELO:
        - Qué señales MACD terminaron siendo buenas (precio siguió > 0.20%)
        - Qué condiciones generaron señales falsas (rango, baja volatilidad, etc.)
        - Con datos REALES de Schwab, el modelo aprende el comportamiento
          específico de cada uno de los 9 activos monitoreados
=============================================================
"""

import sys
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ai_filter.trainer import EntrenadorIA
from engine.logger import log_info, log_error


def entrenar():
    """
    Carga el dataset generado por data_collector.py y entrena el modelo XGBoost.
    """
    dataset_path = "data/historial_senales.csv"

    if not Path(dataset_path).exists():
        print(f"\n❌ Archivo no encontrado: {dataset_path}")
        print("   Primero ejecuta: python -m ai_filter.data_collector")
        print("   (Necesitas credenciales de Schwab en el .env para datos reales)")
        return

    print("\n" + "=" * 60)
    print("  ENTRENAMIENTO DEL MODELO IA — GALA6 Chocoletra")
    print("  Algoritmo: XGBoost (Gradient Boosting)")
    print("=" * 60 + "\n")

    # Cargar dataset
    df = pd.read_csv(dataset_path)
    log_info(f"[IA] Dataset cargado: {len(df)} registros")

    # Resumen del dataset
    total = len(df)
    buenas = df['es_buena_senal'].sum()
    falsas = total - buenas
    print(f"   Total señales:    {total}")
    print(f"   Señales buenas:   {buenas} ({buenas/total*100:.1f}%)")
    print(f"   Señales falsas:   {falsas} ({falsas/total*100:.1f}%)")
    print(f"   Tickers:          {', '.join(df['ticker'].unique())}")
    print()

    if total < 50:
        print("⚠️  Muy pocos datos para entrenar. Se necesitan al menos 50 señales.")
        print("    Descarga más días de historia o añade más tickers.")
        return

    # Entrenar modelo
    entrenador = EntrenadorIA()
    metricas = entrenador.entrenar_desde_dataframe(df, columna_target='es_buena_senal')

    if metricas:
        print("\n" + "=" * 60)
        print("  RESULTADO DEL ENTRENAMIENTO")
        print("=" * 60)
        print(f"  Accuracy total:         {metricas.get('accuracy', 0):.1f}%")
        print(f"  Precision (señal buena): {metricas.get('precision_buena', 0):.1f}%")
        print(f"  Recall (señal buena):    {metricas.get('recall_buena', 0):.1f}%")
        print(f"  Muestras entrenamiento: {metricas.get('muestras_train', 0)}")
        print(f"  Muestras validación:    {metricas.get('muestras_test', 0)}")
        print()
        print("  Archivos guardados:")
        print("  → data/modelo_ia.pkl   (modelo entrenado)")
        print("  → data/scaler_ia.pkl   (normalizador)")
        print()
        print("  El bot ya usará el modelo automáticamente en el próximo inicio.")
        print("=" * 60)
    else:
        print("❌ El entrenamiento falló. Revisa los datos y las dependencias.")


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    entrenar()
