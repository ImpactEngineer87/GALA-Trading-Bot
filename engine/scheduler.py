"""
=============================================================
MÓDULO: engine/scheduler.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Control horario opcional — el operador elige:
      - manual: el motor se enciende/apaga desde el Dashboard.
      - automatico: el servidor enciende/apaga el motor según horario ET.

    El planificador verifica cada minuto si debe:
      - Encender el bot (inicio de sesión de mercado)
      - Apagar el bot (fin de sesión o pausa programada)
      - Ejecutar tareas de mantenimiento nocturno (actualizar imanes)

    HORARIO POR DEFECTO:
      - Inicio: 09:15 ET (pre-market opcional)
      - Fin:    16:00 ET (cierre del mercado)
      - Pausa:  12:00 – 12:15 ET (bloque de mediodía)

    CONFIGURACIÓN en config.yaml:
      scheduler:
        modo: "manual"
        hora_inicio: "09:15"
        hora_fin: "15:45"
        zona_horaria: "America/New_York"
=============================================================
"""

import sys
import time
import threading
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional, Callable
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))
from engine.logger import log_info, log_error


# =============================================================
# KILL SWITCH / PLANIFICADOR
# =============================================================

class PlanificadorHorario:
    """
    Controla el encendido y apagado automático del bot según el mercado.

    Uso:
        planificador = PlanificadorHorario(config)
        planificador.iniciar(
            callback_encender=bot.iniciar,
            callback_apagar=bot.detener
        )
    """

    MODOS_VALIDOS = {"manual", "automatico"}

    def __init__(
        self,
        config: dict,
        now_provider: Optional[Callable[[], datetime]] = None,
    ):
        sched_cfg = config.get("scheduler", {})
        modo = str(sched_cfg.get("modo", "manual")).strip().lower()
        if modo == "automatic":
            modo = "automatico"
        self.modo = modo if modo in self.MODOS_VALIDOS else "manual"
        self.activo = self.modo == "automatico"
        self.zona_horaria = str(
            sched_cfg.get("zona_horaria", "America/New_York")
        )
        try:
            self._tz = ZoneInfo(self.zona_horaria)
        except Exception:
            self.zona_horaria = "America/New_York"
            self._tz = ZoneInfo(self.zona_horaria)
        self.intervalo_revision_seg = max(
            1.0,
            min(float(sched_cfg.get("intervalo_revision_seg", 5)), 60.0),
        )
        self._now_provider = now_provider
        self._parsear_horas(sched_cfg)
        self._corriendo = False
        self._hilo: Optional[threading.Thread] = None
        self._bot_encendido = False
        self._cb_estado: Optional[Callable[[], bool]] = None
        self._suspendido_fecha = None
        self._wake = threading.Event()
        self._start_retry_after = 0.0
        self._start_failures = 0
        self._start_retry_date = None
        self._start_retry_sec = max(
            60.0, float(sched_cfg.get("start_retry_sec", 300)))

    def _parsear_horas(self, cfg: dict):
        """Convierte strings HH:MM a objetos time."""
        def _to_time(s: str, default: str) -> dtime:
            try:
                h, m = map(int, s.split(":"))
                return dtime(h, m)
            except Exception:
                h, m = map(int, default.split(":"))
                return dtime(h, m)

        self.hora_inicio = _to_time(cfg.get("hora_inicio", "09:15"), "09:15")
        self.hora_fin = _to_time(cfg.get("hora_fin", "15:45"), "15:45")
        if self.hora_inicio >= self.hora_fin:
            self.hora_inicio = _to_time("09:15", "09:15")
            self.hora_fin = _to_time("15:45", "15:45")

    def _ahora(self) -> datetime:
        ahora = self._now_provider() if self._now_provider else datetime.now(self._tz)
        if ahora.tzinfo is None:
            return ahora.replace(tzinfo=self._tz)
        return ahora.astimezone(self._tz)

    def _es_horario_activo(self) -> bool:
        """Verifica si el momento actual está dentro del horario de mercado."""
        ahora = self._ahora().time().replace(tzinfo=None)
        return self.hora_inicio <= ahora < self.hora_fin

    def _es_dia_laborable(self) -> bool:
        """Los mercados solo operan de lunes a viernes."""
        return self._ahora().weekday() < 5  # 0=lunes, 4=viernes

    def iniciar(
        self,
        callback_encender: Callable,
        callback_apagar: Callable,
        callback_estado: Optional[Callable[[], bool]] = None,
    ):
        """
        Inicia el loop del planificador en un hilo separado.

        Parámetros:
            callback_encender → Función a llamar para encender el bot
            callback_apagar   → Función a llamar para apagar el bot
        """
        if self.modo != "automatico":
            log_info(
                "[Scheduler] Modo MANUAL — el motor se controla desde el Dashboard"
            )
            return

        if self._hilo is not None and self._hilo.is_alive():
            return

        self._corriendo = True
        self._cb_encender = callback_encender
        self._cb_apagar = callback_apagar
        self._cb_estado = callback_estado
        self._wake.clear()

        log_info("[Scheduler] Modo AUTOMÁTICO activo")
        log_info(
            f"[Scheduler] Horario: {self.hora_inicio.strftime('%H:%M')} — "
            f"{self.hora_fin.strftime('%H:%M')} {self.zona_horaria}"
        )

        self._hilo = threading.Thread(
            target=self._loop,
            daemon=True,
            name="ChocoletraScheduler",
        )
        self._hilo.start()

    def detener(self, apagar_bot: bool = False):
        """Detiene el planificador; no apaga el motor salvo petición explícita."""
        self._corriendo = False
        self._wake.set()
        if apagar_bot and self._estado_bot() and hasattr(self, "_cb_apagar"):
            self._cb_apagar()
        log_info("[Scheduler] Planificador detenido")

    def _estado_bot(self) -> bool:
        if self._cb_estado is not None:
            try:
                return bool(self._cb_estado())
            except Exception:
                pass
        return bool(self._bot_encendido)

    def suspender_sesion_actual(self):
        """Evita que un apagado manual de emergencia se revierta automáticamente."""
        self._suspendido_fecha = self._ahora().date()
        log_info(
            "[Scheduler] Encendido automático suspendido hasta la próxima sesión"
        )

    def reanudar(self):
        self._suspendido_fecha = None
        self._start_retry_after = 0.0
        self._start_failures = 0
        self._wake.set()

    def estado(self) -> dict:
        ahora = self._ahora()
        suspendido = self._suspendido_fecha == ahora.date()
        return {
            "modo": self.modo,
            "activo": self._corriendo,
            "hora_inicio": self.hora_inicio.strftime("%H:%M"),
            "hora_fin": self.hora_fin.strftime("%H:%M"),
            "zona_horaria": self.zona_horaria,
            "en_horario": bool(
                self._es_dia_laborable() and self._es_horario_activo()
            ),
            "suspendido_hoy": suspendido,
            "hora_actual": ahora.strftime("%H:%M:%S"),
        }

    def evaluar_una_vez(self):
        """Aplica una transición; es determinista y reutilizable por tests."""
        if self.modo != "automatico":
            return "manual"

        ahora = self._ahora()
        if self._start_retry_date != ahora.date():
            # A failed start belongs to its session. Do not carry a monotonic
            # cooldown into the next trading day (or a corrected clock date).
            self._start_retry_date = ahora.date()
            self._start_retry_after = 0.0
            self._start_failures = 0
        if (
            self._suspendido_fecha is not None
            and self._suspendido_fecha != ahora.date()
        ):
            self._suspendido_fecha = None

        es_activo = self._es_horario_activo() and self._es_dia_laborable()
        encendido = self._estado_bot()

        if es_activo and self._suspendido_fecha == ahora.date():
            return "suspendido"

        if es_activo and not encendido:
            if time.monotonic() < self._start_retry_after:
                return "esperando_reintento"
            log_info(
                f"[Scheduler] {ahora.strftime('%H:%M:%S')} ET — ENCENDIENDO BOT"
            )
            # Starting can perform OAuth, account resolution and broker
            # reconciliation. A blocked/failed start must not repeat every 5s.
            self._start_retry_after = time.monotonic() + self._start_retry_sec
            try:
                resultado = self._cb_encender()
            except Exception:
                self._start_failures += 1
                self._start_retry_after = time.monotonic() + min(
                    3600.0, self._start_retry_sec * (2 ** min(self._start_failures - 1, 6)))
                raise
            self._bot_encendido = resultado is not False
            if self._bot_encendido:
                self._start_failures = 0
                self._start_retry_after = 0.0
            else:
                self._start_failures += 1
                self._start_retry_after = time.monotonic() + min(
                    3600.0, self._start_retry_sec * (2 ** min(self._start_failures - 1, 6)))
            return "encendido" if self._bot_encendido else "bloqueado"

        if not es_activo and encendido:
            log_info(
                f"[Scheduler] {ahora.strftime('%H:%M:%S')} ET — "
                "APAGANDO BOT (fin de horario)"
            )
            self._cb_apagar()
            self._bot_encendido = False
            return "apagado"

        self._bot_encendido = encendido
        return "sin_cambio"

    def _loop(self):
        """Loop principal independiente del motor de trading."""
        while self._corriendo:
            try:
                self.evaluar_una_vez()
            except Exception as e:
                log_error(f"[Scheduler] Error en loop: {e}")
            self._wake.wait(self.intervalo_revision_seg)
            self._wake.clear()


# =============================================================
# EJECUCIÓN DIRECTA — TEST DEL SCHEDULER
# =============================================================

if __name__ == "__main__":
    import yaml

    with open("config.yaml", 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    def encender():
        print("[TEST] Bot ENCENDIDO")

    def apagar():
        print("[TEST] Bot APAGADO")

    planificador = PlanificadorHorario(config)
    planificador.iniciar(callback_encender=encender, callback_apagar=apagar)

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        planificador.detener()
