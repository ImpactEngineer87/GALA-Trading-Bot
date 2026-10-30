"""Post-session SHADOW training control; never routes trades or promotes models."""
import os
import re
import stat
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from core.runtime_store import path_lock

NY = ZoneInfo("America/New_York")


def policy(config):
    training = ((config.get("agents") or {}).get("entrenamiento") or {})
    auto = training.get("automatic_retraining") or {}
    at = str(auto.get("not_before_ny", "16:15"))
    end = str((config.get("scheduler") or {}).get("hora_fin", "15:45"))
    pattern = r"(?:[01]\d|2[0-3]):[0-5]\d"
    if not re.fullmatch(pattern, at) or not re.fullmatch(pattern, end):
        raise ValueError("Horario Auto IA inválido; use HH:MM")
    return {"enabled": auto.get("enabled", training.get("activo", True)) is True,
            "only_after_session": True, "not_before_ny": max("16:15", at, end),
            "timezone": "America/New_York", "training_target": "SHADOW",
            "auto_activate_production": False, "promotion": "MANUAL"}


def window(config, now):
    settings = policy(config)
    local = now.astimezone(NY)
    hour, minute = map(int, settings["not_before_ny"].split(":"))
    next_at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    while next_at.weekday() >= 5:
        next_at += timedelta(days=1)
    allowed = local.weekday() < 5 and local >= next_at
    return allowed, None if allowed else next_at.isoformat()


def update_enabled(path, enabled):
    """Atomically change only Auto AI settings, preserving private file mode."""
    if type(enabled) is not bool:
        raise ValueError("enabled debe ser true o false")
    path = Path(path)
    with path_lock(path):
        with path.open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        if not isinstance(config, dict):
            raise ValueError("config.yaml inválido")
        auto = config.setdefault("agents", {}).setdefault("entrenamiento", {}).setdefault("automatic_retraining", {})
        auto.update(enabled=enabled, only_after_session=True, auto_activate_production=False)
        settings = policy(config)
        mode = stat.S_IMODE(path.stat().st_mode)
        fd, temp = tempfile.mkstemp(prefix=path.name + ".auto-ai-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), mode)
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return settings


def training_busy(trainer):
    return trainer is not None and bool(getattr(trainer, "_eligible_batch_running", False)
                                        or getattr(trainer, "_entrenando", ()))


class AutoAIService:
    def __init__(self, get_config, get_trainer, engine_busy, tickers, *,
                 admission_lock=None, now=None, log=None):
        self.get_config, self.get_trainer = get_config, get_trainer
        self.engine_busy, self.tickers = engine_busy, tickers
        self.lock = admission_lock or threading.RLock()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.log = log or (lambda message: None)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._state_lock = threading.Lock()
        self._last_signature = None
        self._status = {"state": "NOT_CHECKED", "message": "Programación aún no comprobada"}

    def _set(self, state, message, **fields):
        with self._state_lock:
            self._status = {**self._status, "state": state, "message": message,
                            "last_check_utc": self.now().isoformat(), **fields}
            return dict(self._status)

    def snapshot(self):
        with self._state_lock:
            return dict(self._status)

    def allowed_now(self):
        try:
            cfg = self.get_config()
            return policy(cfg)["enabled"] and window(cfg, self.now())[0] and not self.engine_busy()
        except Exception:
            return False

    def tick(self):
        with self.lock:
            try:
                cfg = self.get_config()
                settings = policy(cfg)
                if not settings["enabled"]:
                    return self._set("OFF", "Auto IA apagado; entrenamiento manual disponible", next_window_ny=None)
                allowed, next_at = window(cfg, self.now())
                if self.engine_busy():
                    return self._set("WAIT_ENGINE", "Esperando motor detenido y sin posiciones locales pendientes", next_window_ny=next_at)
                if not allowed:
                    return self._set("WAIT_SESSION", "Esperando ventana posterior a la sesión NY", next_window_ny=next_at)
                trainer = self.get_trainer()
                trainer.auto_retrain_enabled = True
                trainer.auto_retrain_only_after_session = True
                trainer.auto_activate_production = False
                trainer.automatic_training_allowed = self.allowed_now
                if training_busy(trainer):
                    return self._set("TRAINING", "Worker SHADOW en curso", next_window_ny=None)
                eligible = trainer.retraining.eligible_tickers(self.tickers(cfg))
                if not eligible:
                    return self._set("WAIT_CLOSES", "Sin tickers con nuevos cierres elegibles suficientes", eligible=[], next_window_ny=None)
                files = []
                for name in ("feedback_path", "signal_learning_path"):
                    path = Path(getattr(trainer, name, "__missing_auto_ai_source__"))
                    info = path.stat() if path.is_file() else None
                    files.append((str(path), info.st_mtime_ns if info else None, info.st_size if info else None))
                signature = (self.now().astimezone(NY).date().isoformat(), tuple(eligible), tuple(files))
                if signature == self._last_signature:
                    return self._set("WAIT_NEW_DATA", "Lote ya intentado; esperando nuevos datos o la próxima sesión", eligible=eligible)
                queued = trainer.solicitar_reentreno_todos(eligible, solo_elegibles=True)
                if not queued:
                    return self._set("DEFERRED", "Worker ocupado o política cambió; se comprobará de nuevo", eligible=eligible)
                self._last_signature = signature
                self.log("[IA AUTO] Evaluación SHADOW encolada: " + ", ".join(eligible))
                return self._set("QUEUED", "Evaluación SHADOW en cola; producción requiere aprobación manual",
                                 eligible=eligible, last_queued_utc=self.now().isoformat())
            except Exception as exc:
                self.log("[IA AUTO] Comprobación fallida: " + type(exc).__name__)
                return self._set("ERROR", "Auto IA no pudo verificar configuración/datos; no se inicia entrenamiento")

    def start(self):
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="auto-ai-post-session", daemon=True)
            self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            self.tick()
            self._wake.wait(60)
            self._wake.clear()

    def wake(self):
        with self._state_lock:
            self._status = {"state": "NOT_CHECKED", "message": "Configuración guardada; esperando comprobación del worker"}
        self._wake.set()
