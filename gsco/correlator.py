"""
=============================================================
MÓDULO: gsco/correlator.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Motor de correlación institucional.

    ANTES de enviar cualquier alerta de Telegram, el bot ejecuta
    este módulo para cruzar la señal MACD con los posteos GSCO
    del día y añadir contexto institucional a la alerta.

    LÓGICA:
      - Alerta MACD alcista → busca Bid de GSCO cercano al precio
      - Alerta MACD bajista → busca Ask de GSCO cercano al precio
      - Si hay match → añadir contexto y subir prioridad
      - Si 4 factores confluyen → SEÑAL MAESTRA 🔥
=============================================================
"""

from typing import Optional, List, Dict
from gsco.sheets_client import SheetsClient, PosteoGSCO


# =============================================================
# RESULTADO DE LA CORRELACIÓN
# =============================================================

class ResultadoCorrelacion:
    """
    Resultado del análisis de correlación MACD + GSCO.
    """
    def __init__(self):
        self.hay_match: bool = False
        self.posteo_coincidente: Optional[PosteoGSCO] = None
        self.diferencia_precio: Optional[float] = None
        self.es_senal_maestra: bool = False
        self.texto_adicional: str = ""

    def __str__(self):
        if not self.hay_match:
            return "[Sin coincidencia GSCO]"
        return (f"[GSCO Match] {self.posteo_coincidente.direccion} @ "
                f"${self.posteo_coincidente.precio:.2f} ({self.posteo_coincidente.hora})")


# =============================================================
# MOTOR DE CORRELACIÓN
# =============================================================

class MotorDeCorrelacion:
    """
    Cruza las señales MACD con los posteos institucionales de GSCO.

    Uso:
        motor = MotorDeCorrelacion(sheets_client)
        resultado = motor.correlacionar(
            ticker='BA',
            direccion_macd='BULL',
            precio_actual=227.49,
            es_hora_clave=True,
            via_libre_historica=True
        )
    """

    # Porcentaje máximo de diferencia de precio para considerar match (0.30%)
    MAX_DIFERENCIA_PCT = 0.30

    def __init__(self, sheets: SheetsClient):
        self.sheets = sheets

    def correlacionar(
        self,
        ticker: str,
        direccion_macd: str,    # 'BULL' o 'BEAR'
        precio_actual: float,
        es_hora_clave: bool = False,
        via_libre_historica: bool = False,
    ) -> ResultadoCorrelacion:
        """
        Ejecuta la correlación completa MACD + GSCO.

        Parámetros:
            ticker              → Símbolo del activo
            direccion_macd      → Dirección de la señal MACD ('BULL' o 'BEAR')
            precio_actual       → Precio de cierre de la vela de confirmación
            es_hora_clave       → True si el cruce ocurre en 09:42, 10:30, 12:00 o 12:30
            via_libre_historica → True si el precio supera los niveles históricos de 8 días

        Retorna:
            ResultadoCorrelacion con el análisis completo
        """
        resultado = ResultadoCorrelacion()

        # Obtener posteos GSCO del día para este ticker
        posteos = self.sheets.obtener_posteos_gsco(ticker, solo_hoy=True)
        if not posteos:
            return resultado

        # Buscar posteo que coincida en dirección y precio
        posteo_match = self._buscar_coincidencia(posteos, direccion_macd, precio_actual)

        if posteo_match:
            resultado.hay_match = True
            resultado.posteo_coincidente = posteo_match
            resultado.diferencia_precio = abs(posteo_match.precio - precio_actual)
            resultado.texto_adicional = (
                f"\n+ COINCIDE con posteo previo de GSCO ({posteo_match.direccion} "
                f"@ ${posteo_match.precio:.2f} — {posteo_match.hora}). Alta probabilidad."
            )

            # Verificar si es SEÑAL MAESTRA (4 factores simultáneos)
            if es_hora_clave and via_libre_historica:
                resultado.es_senal_maestra = True

        return resultado

    def _buscar_coincidencia(
        self,
        posteos: List[PosteoGSCO],
        direccion_macd: str,
        precio_actual: float,
    ) -> Optional[PosteoGSCO]:
        """
        Busca el posteo GSCO más reciente que coincida en:
          - Dirección (Bid si MACD alcista, Ask si MACD bajista)
          - Precio cercano al actual (dentro del MAX_DIFERENCIA_PCT)
        """
        direccion_gsco = "Bid" if direccion_macd == "BULL" else "Ask"

        candidatos = []
        for posteo in posteos:
            # Verificar dirección compatible
            if direccion_macd == "BULL" and not posteo.es_alcista():
                continue
            if direccion_macd == "BEAR" and not posteo.es_bajista():
                continue
            # Verificar proximidad de precio
            if precio_actual > 0:
                diff_pct = abs(posteo.precio - precio_actual) / precio_actual * 100
                if diff_pct <= self.MAX_DIFERENCIA_PCT:
                    candidatos.append((diff_pct, posteo))

        if not candidatos:
            return None

        # Retornar el más cercano al precio actual
        candidatos.sort(key=lambda x: x[0])
        return candidatos[0][1]

    def enriquecer_alerta(self, texto_alerta: str, correlacion: ResultadoCorrelacion) -> str:
        """
        Añade el contexto institucional GSCO a un texto de alerta existente.

        Si es Señal Maestra, añade el encabezado 🔥.
        """
        if correlacion.es_senal_maestra:
            return (
                f"🔥 [PRIORIDAD ALTA — SEÑAL MAESTRA]\n"
                f"{texto_alerta}"
                f"{correlacion.texto_adicional}"
            )
        elif correlacion.hay_match:
            return f"{texto_alerta}{correlacion.texto_adicional}"
        else:
            return texto_alerta
