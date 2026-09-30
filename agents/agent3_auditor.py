"""
=============================================================
MÓDULO: agents/agent3_auditor.py
PROYECTO: GALA6 Chocoletra Bot
AGENTE 3 — EL AUDITOR / FILTRO DE RIESGO
=============================================================
DESCRIPCIÓN:
    Última barrera antes de enviar la orden al broker. Revisa el
    microcontexto inmediato de la propuesta del Agente 2 y decide
    APROBAR o VETAR:

      1. VOLATILIDAD (ATR): ni muerta (mercado sin recorrido) ni
         desbocada (spike de noticia). Debe estar en un rango sano.
      2. HORARIO INSTITUCIONAL: solo operar dentro de la sesión
         regular (RTH). Opcionalmente vetar el bloque de mediodía.
      3. TRAMPA DE RANGO LATERAL: si las últimas velas están
         comprimidas en un rango estrecho, se veta para no quedar
         atrapado en amagos que frían el capital.

    Protege el capital: ante la duda, VETA.
=============================================================
"""

from datetime import datetime, time
from typing import Optional

import pandas as pd

from agents.contracts import (
    DatosMultiTemporal, PropuestaOperacion, VeredictoAuditor, Veredicto,
)
from agents import smc_primitives as smc


class AgenteAuditorRiesgo:
    """Agente 3 del sistema SMC: filtro de riesgo final."""

    def __init__(self, config: dict):
        cfg = config.get("agents", {}).get("agente3", {})
        self.atr_rel_min: float = cfg.get("atr_relativo_min", 0.08)
        self.atr_rel_max: float = cfg.get("atr_relativo_max", 1.20)
        self.velas_rango: int = cfg.get("velas_rango_lateral", 8)
        self.rango_min_pct: float = cfg.get("rango_lateral_min_pct", 0.18)
        self.solo_institucional: bool = cfg.get("horario_institucional_only", True)
        self.hora_inicio = self._parse_hora(cfg.get("hora_inicio_rth", "09:30"), time(9, 30))
        self.hora_fin = self._parse_hora(cfg.get("hora_fin_rth", "15:55"), time(15, 55))
        self.vetar_mediodia: bool = cfg.get("vetar_mediodia", False)
        self.atr_period: int = config.get("atr", {}).get("period", 14)

    # =========================================================
    # AUDITORÍA PRINCIPAL
    # =========================================================

    def auditar(self, datos: DatosMultiTemporal, propuesta: PropuestaOperacion,
                ahora: Optional[datetime] = None,
                hora_et_override: Optional[datetime] = None,
                actual_en_formacion: bool = False) -> VeredictoAuditor:
        """
        Audita una propuesta. Retorna VeredictoAuditor (APROBADA/VETADA).
        """
        razones = []

        # Micro DataFrame para volatilidad y rango (preferir la TF más baja)
        df_micro = self._df_micro(datos, actual_en_formacion)
        if df_micro is None or len(df_micro) < self.atr_period + 2:
            # No se pudo auditar el microcontexto. Se marca datos_suficientes=False
            # y horario NO confirmado para que el flujo Gala 10 (auditar_gala10) lo
            # trate como VETO DURO (fail-closed: ante la duda, no se opera).
            return VeredictoAuditor(
                veredicto=Veredicto.VETADA,
                motivo="Datos micro insuficientes para auditar",
                es_horario_institucional=False,
                datos_suficientes=False,
                razones=["Sin velas micro suficientes"],
            )

        # -----------------------------------------------------
        # 1) Volatilidad ATR
        # -----------------------------------------------------
        atr_rel = smc.atr_relativo_actual(df_micro, self.atr_period)
        vol_ok = self.atr_rel_min <= atr_rel <= self.atr_rel_max
        if atr_rel < self.atr_rel_min:
            razones.append(f"ATR relativo {atr_rel:.3f}% < mín {self.atr_rel_min}% (mercado muerto)")
        elif atr_rel > self.atr_rel_max:
            razones.append(f"ATR relativo {atr_rel:.3f}% > máx {self.atr_rel_max}% (spike/noticia)")
        else:
            razones.append(f"ATR relativo {atr_rel:.3f}% en rango sano")

        # -----------------------------------------------------
        # 2) Horario institucional
        # -----------------------------------------------------
        es_institucional, motivo_horario = self._chequear_horario(hora_et_override)
        if not es_institucional:
            razones.append(motivo_horario)
        else:
            razones.append(f"Horario OK ({motivo_horario})")

        # -----------------------------------------------------
        # 3) Trampa de rango lateral
        # -----------------------------------------------------
        es_rango, rango_pct = self._es_rango_lateral(df_micro)
        if es_rango:
            razones.append(f"Rango lateral {rango_pct:.3f}% < mín {self.rango_min_pct}% → trampa")
        else:
            razones.append(f"Rango {rango_pct:.3f}% suficiente (no lateral)")

        # -----------------------------------------------------
        # VEREDICTO: cualquier fallo → VETO
        # -----------------------------------------------------
        aprobada = vol_ok and es_institucional and (not es_rango)
        veredicto = Veredicto.APROBADA if aprobada else Veredicto.VETADA

        if aprobada:
            motivo = "Microcontexto limpio: volatilidad sana, horario válido, sin rango lateral"
        else:
            fallos = []
            if not vol_ok:
                fallos.append("volatilidad fuera de rango")
            if not es_institucional:
                fallos.append("fuera de horario institucional")
            if es_rango:
                fallos.append("trampa de rango lateral")
            motivo = "VETO: " + ", ".join(fallos)

        return VeredictoAuditor(
            veredicto=veredicto,
            motivo=motivo,
            atr_relativo=round(atr_rel, 4),
            es_horario_institucional=es_institucional,
            es_rango_lateral=es_rango,
            razones=razones,
        )

    # =========================================================
    # AUXILIARES
    # =========================================================

    def _df_micro(self, datos: DatosMultiTemporal,
                  actual_en_formacion: bool = False) -> Optional[pd.DataFrame]:
        """Elige la TF más baja disponible CON velas suficientes para auditar.
        Prefiere 5m→15m→1m→1H, pero salta las temporalidades demasiado cortas
        (menos de atr_period+2 velas) para no vetar por 'datos insuficientes'
        cuando otra TF sí tiene profundidad (p. ej. 1m al inicio de la sesión,
        donde el motor ya opera sobre 1m pero 5m aún tiene pocas velas)."""
        minimo = self.atr_period + 2
        primera = None
        for tf in ("5m", "15m", "1m", "1H"):
            df = datos.tf(tf)
            if df is None:
                continue
            if actual_en_formacion and len(df) > 1:
                df = df.iloc[:-1]
            if primera is None:
                primera = df          # respaldo: primera TF con datos (aunque corta)
            if len(df) >= minimo:
                return df             # TF preferida con profundidad suficiente
        return primera

    def _chequear_horario(self, hora_et_override: Optional[datetime]):
        """
        Verifica que el momento actual esté dentro de la sesión regular (RTH)
        en hora de Nueva York. Si solo_institucional es False, siempre pasa.
        """
        if not self.solo_institucional:
            return True, "filtro horario desactivado"

        ahora_et = self._ahora_et(hora_et_override)

        # Días laborables (lunes-viernes)
        if ahora_et.weekday() >= 5:
            return False, "fin de semana (mercado cerrado)"

        hora = ahora_et.time()
        if not (self.hora_inicio <= hora <= self.hora_fin):
            return False, f"fuera de RTH {self.hora_inicio.strftime('%H:%M')}-{self.hora_fin.strftime('%H:%M')} ET (ahora {hora.strftime('%H:%M')})"

        # Bloque de mediodía opcional (baja liquidez 12:00-13:00 ET)
        if self.vetar_mediodia and time(12, 0) <= hora < time(13, 0):
            return False, "bloque de mediodía (baja liquidez) vetado"

        return True, f"RTH {hora.strftime('%H:%M')} ET"

    def _ahora_et(self, override: Optional[datetime]) -> datetime:
        """Hora actual en la zona horaria del mercado (America/New_York)."""
        if override is not None:
            return override
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            # Fallback: reloj del servidor (se asume configurado en ET)
            return datetime.now()

    def _es_rango_lateral(self, df: pd.DataFrame):
        """
        Detecta compresión: rango de las últimas N velas como % del precio.
        Devuelve (es_lateral, rango_pct).
        """
        n = min(self.velas_rango, len(df))
        ventana = df.iloc[-n:]
        precio = float(df["close"].iloc[-1])
        if precio <= 0:
            return True, 0.0
        rango = float(ventana["high"].max() - ventana["low"].min())
        rango_pct = rango / precio * 100.0
        return (rango_pct < self.rango_min_pct), rango_pct

    @staticmethod
    def _parse_hora(valor: str, defecto: time) -> time:
        try:
            h, m = map(int, str(valor).split(":"))
            return time(h, m)
        except Exception:
            return defecto
