"""
=============================================================
MÓDULO: agents/feedback_store.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Memoria de resultados de los agentes para el APRENDIZAJE
    CONTINUO. Cada vez que el sistema abre una operación, se
    guardan las features de la decisión; cuando la operación se
    cierra, se registra el resultado real (R múltiple / ganadora).

    Ese historial alimenta el meta-modelo de los agentes
    (agents/training.py), que se reentrena automáticamente cada N
    operaciones cerradas — así el sistema NO queda estático.

    Diseño thread-safe: el motor procesa varios tickers en paralelo,
    por lo que todas las escrituras están protegidas con un lock.
    Se mantiene UNA operación abierta por ticker (coherente con el
    guardián de entradas duplicadas del motor).
=============================================================
"""

import os
import csv
import json
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from agents.contracts import DecisionFinal
from agents.gala10_features import GALA10_FEATURE_NAMES
from agents.volume_features import VOLUME_FEATURE_NAMES
from agents.learning_dataset import (
    eligible_closed_rows, observation_identity, summarize_closed_trades,
)
from core.runtime_store import path_lock


import math

# Columnas persistidas en el CSV de feedback. GSCO va como feature OPCIONAL
# (puede quedar vacía = NaN cuando no hay posteos ese día); XGBoost la maneja.
LEGACY_COLUMNAS = [
    "timestamp", "ticker", "tipo",
    "confianza_macro", "ratio_rb", "atr_relativo", "fvg_tam_rel",
    "hueco_apertura", "confluencia", "hora", "es_rango_lateral",
    "gsco_match", "gsco_dist_pct",
    *VOLUME_FEATURE_NAMES,
    *GALA10_FEATURE_NAMES,
    "precio_entrada", "precio_salida", "r_multiple", "ganadora",
]

V2_9_COLUMNAS = [
    "feedback_id", "execution_id", "position_id", "signal_id",
    "reversal_id", "correlation_id", "entry_signal_id", "exit_signal_id",
    "entry_reversal_id", "exit_reversal_id", "entry_correlation_id",
    "exit_correlation_id", "modo", "entry_order_id",
    "exit_order_id", "position_side", "signal_side", "entry_action",
    "exit_action", "entry_state_before", "entry_state_after",
    "exit_state_before", "exit_state_after", "entry_confirmed",
    "exit_confirmed", "audit_linked", "history_linked",
    "slippage_linked", "execution_valid", "exclusion_reason",
    *LEGACY_COLUMNAS,
]

COLUMNAS = [*V2_9_COLUMNAS, "closed_utc", "risk_per_share"]


def _csv_num(x):
    """Devuelve '' para NaN/None (columna vacía en CSV → NaN al leer con pandas)."""
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return ""
    except Exception:
        return ""
    return x


def construir_features(decision: DecisionFinal) -> Dict[str, float]:
    """
    Extrae las features numéricas de una DecisionFinal. Se usa tanto
    para PUNTUAR con el meta-modelo (antes de operar) como para
    REGISTRAR el ejemplo de entrenamiento (al cerrar la operación).

    Mantener esta función como única fuente de verdad evita
    inconsistencias entre entrenamiento e inferencia.
    """
    dd = decision.direccion_dia
    prop = decision.propuesta
    ver = decision.veredicto

    confianza = float(dd.confianza) if dd is not None else 0.0
    ratio_rb = float(prop.ratio_rb) if prop is not None else 0.0
    atr_rel = float(ver.atr_relativo) if ver is not None else 0.0

    fvg_tam_rel = float(getattr(decision, "fvg_contexto_tam_rel", 0.0) or 0.0)
    # Compatibilidad con decisiones antiguas creadas por el flujo legado.
    if fvg_tam_rel == 0.0 and prop is not None and prop.fvg is not None and prop.precio_entrada > 0:
        fvg_tam_rel = prop.fvg.tamano / prop.precio_entrada * 100.0

    hueco = 0.0
    if dd is not None and dd.hueco_apertura_pct is not None:
        hueco = float(dd.hueco_apertura_pct)

    confluencia = 1.0 if decision.confluencia_estrategia else 0.0

    ts = decision.timestamp if decision.timestamp else datetime(1970, 1, 1)
    hora = float(ts.hour * 100 + ts.minute)

    es_rango = 1.0 if (ver is not None and ver.es_rango_lateral) else 0.0
    tipo_num = 1.0 if decision.tipo == "BUY" else 0.0

    # GSCO como feature OPCIONAL: la rellena el motor si hay un posteo válido en
    # la ventana horaria; si no hay dato, queda NaN y XGBoost decide con el resto.
    gsco_match = getattr(decision, "gsco_match", float("nan"))
    gsco_dist = getattr(decision, "gsco_dist_pct", float("nan"))

    features = {
        "tipo_num": tipo_num,
        "confianza_macro": round(confianza, 4),
        "ratio_rb": round(ratio_rb, 4),
        "atr_relativo": round(atr_rel, 4),
        "fvg_tam_rel": round(fvg_tam_rel, 4),
        "hueco_apertura": round(hueco, 4),
        "confluencia": confluencia,
        "hora": hora,
        "es_rango_lateral": es_rango,
        "gsco_match": gsco_match,
        "gsco_dist_pct": gsco_dist,
    }
    for nombre in VOLUME_FEATURE_NAMES:
        features[nombre] = getattr(decision, nombre, float("nan"))
    for nombre in GALA10_FEATURE_NAMES:
        features[nombre] = getattr(decision, nombre, float("nan"))
    return features


class FeedbackStore:
    """
    Almacén thread-safe de decisiones abiertas y resultados cerrados.
    """

    def __init__(self, ruta_csv: str = "data/agentes_feedback.csv"):
        self.ruta_csv = ruta_csv
        self._lock = path_lock(ruta_csv)
        # v2.9.3: las aperturas de aprendizaje ya no viven solo en RAM. El sidecar
        # permite cerrar correctamente una operación después de reiniciar el
        # server sin perder su observación PAPER/REAL ni el progreso x/50.
        self._open_state_path = Path(str(ruta_csv) + ".open.json")
        self._abiertas: Dict[str, dict] = {}
        self._closed_rows = []
        self._closed_by_identity = {}
        self._closed_by_feedback_id = {}
        with self._lock:
            self._verificar_esquema()
            self._reload_closed_locked()
            self._cargar_abiertas_persistidas()
            self._cerradas_totales = self._contar_filas()

    def _verificar_esquema(self):
        """Si ya existe un CSV con la cabecera de un esquema ANTERIOR (p. ej. de
        antes de añadir las columnas GSCO), se archiva a un '.old' y se arranca
        limpio. Así nunca se mezclan filas de esquemas distintos (que descuadrarían
        las columnas al leer con pandas y corromperían el entrenamiento por ticker)."""
        if not os.path.exists(self.ruta_csv):
            return
        try:
            with open(self.ruta_csv, "r", encoding="utf-8-sig", newline="") as f:
                primera = f.readline().strip()
            if primera == ",".join(COLUMNAS):
                return
            if primera == ",".join(V2_9_COLUMNAS):
                # Adding outcome availability/risk must preserve the verified
                # learning history and existing counters. Unknown historical
                # close times remain empty instead of being invented at upgrade.
                with open(self.ruta_csv, "r", encoding="utf-8-sig", newline="") as f:
                    rows = list(csv.DictReader(f))
                backup = self.ruta_csv + ".before_closed_utc"
                if not os.path.exists(backup):
                    shutil.copy2(self.ruta_csv, backup)
                self._rewrite_schema(rows)
                return
            if primera == ",".join(LEGACY_COLUMNAS):
                with open(self.ruta_csv, "r", encoding="utf-8-sig", newline="") as f:
                    rows = list(csv.DictReader(f))
                bak, i = self.ruta_csv + ".before_v2_9", 1
                while os.path.exists(bak):
                    bak = f"{self.ruta_csv}.before_v2_9_{i}"; i += 1
                shutil.copy2(self.ruta_csv, bak)
                temporary = self.ruta_csv + ".v2_9.tmp"
                with open(temporary, "w", encoding="utf-8", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=COLUMNAS)
                    writer.writeheader()
                    for old in rows:
                        migrated = {column: "" for column in COLUMNAS}
                        migrated.update(old)
                        migrated.update({
                            "feedback_id": uuid.uuid4().hex,
                            "position_side": (
                                "LONG" if str(old.get("tipo", "")).upper() == "BUY"
                                else "SHORT"
                            ),
                            "signal_side": str(old.get("tipo", "")).upper(),
                            "entry_action": (
                                "BUY" if str(old.get("tipo", "")).upper() == "BUY"
                                else "SELL_SHORT"
                            ),
                            "exit_action": (
                                "SELL" if str(old.get("tipo", "")).upper() == "BUY"
                                else "BUY_TO_COVER"
                            ),
                            "entry_state_before": "OUT",
                            "entry_state_after": (
                                "LONG" if str(old.get("tipo", "")).upper() == "BUY"
                                else "SHORT"
                            ),
                            "exit_state_before": (
                                "LONG" if str(old.get("tipo", "")).upper() == "BUY"
                                else "SHORT"
                            ),
                            "exit_state_after": "OUT",
                            "execution_valid": 0,
                            "exclusion_reason": "LEGACY_V2_8_UNLINKED",
                        })
                        writer.writerow(migrated)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary, self.ruta_csv)
                print(
                    f"[Feedback] CSV v2.8 preservado y migrado; filas antiguas "
                    f"quedan en cuarentena ({bak})"
                )
                return
            if primera:
                bak, i = self.ruta_csv + ".old", 1
                while os.path.exists(bak):
                    bak = f"{self.ruta_csv}.old{i}"; i += 1
                os.rename(self.ruta_csv, bak)
                print(f"[Feedback] CSV con esquema anterior → archivado en '{bak}', se arranca limpio")
        except Exception as e:
            # Continuing with a mismatched header would silently corrupt labels.
            raise RuntimeError(f"No se pudo verificar el esquema del feedback: {e}") from e

    def _rewrite_schema(self, rows) -> None:
        directory = os.path.dirname(self.ruta_csv) or "."
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=directory,
                prefix=".feedback-schema-", suffix=".tmp", delete=False,
            ) as handle:
                temporary = handle.name
                writer = csv.DictWriter(handle, fieldnames=COLUMNAS)
                writer.writeheader()
                for row in rows:
                    writer.writerow({column: row.get(column, "") for column in COLUMNAS})
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, os.stat(self.ruta_csv).st_mode & 0o777)
            os.replace(temporary, self.ruta_csv)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def _reload_closed_locked(self) -> None:
        if not os.path.exists(self.ruta_csv):
            self._closed_rows = []
        else:
            with open(self.ruta_csv, "r", encoding="utf-8-sig", newline="") as handle:
                # An interrupted append must never be mistaken for a committed
                # close merely because its leading identity columns were written.
                self._closed_rows = [
                    dict(row) for row in csv.DictReader(handle)
                    if None not in row and all(value is not None for value in row.values())
                ]
        self._closed_by_identity = {}
        self._closed_by_feedback_id = {}
        for row in self._closed_rows:
            identity = observation_identity(row)
            if identity is not None:
                self._closed_by_identity[identity] = row
            feedback_id = str(row.get("feedback_id", "") or "").strip()
            if feedback_id:
                self._closed_by_feedback_id[feedback_id] = row

    @staticmethod
    def _pending_identity(ticker: str, pending: dict):
        return observation_identity({
            **dict(pending.get("lifecycle") or {}), "ticker": ticker,
        })

    def _committed_close(self, ticker: str, pending: dict):
        identity = self._pending_identity(ticker, pending)
        if identity is not None and identity in self._closed_by_identity:
            return self._closed_by_identity[identity]
        return self._closed_by_feedback_id.get(str(pending.get("feedback_id", "") or ""))

    # ---------------------------------------------------------
    @staticmethod
    def _json_safe(value):
        """Convierte el estado abierto a tipos JSON sin depender de pickle."""
        if value is None or isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(k): FeedbackStore._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [FeedbackStore._json_safe(v) for v in value]
        try:
            numeric = float(value)
            return numeric if math.isfinite(numeric) else None
        except Exception:
            return str(value)

    def _persistir_abiertas_locked(self) -> None:
        self._open_state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": "4.1",
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "open": {},
        }
        for ticker, pending in self._abiertas.items():
            payload["open"][ticker] = self._json_safe({
                "feedback_id": pending.get("feedback_id"),
                "features": pending.get("features") or {},
                "precio_entrada": pending.get("precio_entrada"),
                "stop_loss": pending.get("stop_loss"),
                "riesgo_planificado": pending.get("riesgo_planificado"),
                "lifecycle": pending.get("lifecycle") or {},
                "tipo": pending.get("tipo"),
                "timestamp": pending.get("timestamp"),
                "closing_row": pending.get("closing_row"),
            })
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self._open_state_path.parent,
                prefix=f".{self._open_state_path.name}.", suffix=".tmp", delete=False,
            ) as fh:
                tmp = fh.name
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            if self._open_state_path.exists():
                os.chmod(tmp, self._open_state_path.stat().st_mode & 0o777)
            os.replace(tmp, self._open_state_path)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    def _cargar_abiertas_persistidas(self) -> None:
        if not self._open_state_path.exists():
            return
        try:
            raw = json.loads(self._open_state_path.read_text(encoding="utf-8-sig"))
            items = raw.get("open", {}) if isinstance(raw, dict) else {}
            if not isinstance(items, dict):
                return
            for ticker, pending in items.items():
                if not isinstance(pending, dict):
                    continue
                tk = str(ticker or "").strip().upper()
                tipo = str(pending.get("tipo", "") or "").strip().upper()
                if not tk or tipo not in ("BUY", "SELL"):
                    continue
                restored = {
                    "feedback_id": str(pending.get("feedback_id", "") or uuid.uuid4().hex),
                    "features": dict(pending.get("features") or {}),
                    "decision": None,
                    "precio_entrada": float(pending.get("precio_entrada") or 0.0),
                    "stop_loss": (
                        float(pending["stop_loss"])
                        if pending.get("stop_loss") is not None else None
                    ),
                    "riesgo_planificado": float(pending.get("riesgo_planificado") or 0.0),
                    "lifecycle": dict(pending.get("lifecycle") or {}),
                    "tipo": tipo,
                    "timestamp": str(pending.get("timestamp", "") or ""),
                    "closing_row": (
                        dict(pending["closing_row"])
                        if isinstance(pending.get("closing_row"), dict) else None
                    ),
                }
                # CSV append commits first; a crash before sidecar cleanup can
                # leave a stale open. Never replay that already committed label.
                if self._committed_close(tk, restored) is None:
                    self._abiertas[tk] = restored
            if self._abiertas:
                print(
                    "[Feedback] Aperturas de aprendizaje recuperadas: "
                    + ", ".join(sorted(self._abiertas))
                )
            # A close intent contains the already computed, verified outcome.
            # Replaying it only writes learning evidence; it never contacts a
            # broker or recomputes historical PnL using a new market price.
            for ticker, pending in list(self._abiertas.items()):
                row = pending.get("closing_row")
                if isinstance(row, dict):
                    try:
                        self.cerrar(ticker, row.get("precio_salida"), lifecycle=None)
                    except Exception as exc:
                        print(f"[Feedback] {ticker}: cierre pendiente de persistencia: {exc}")
        except Exception as exc:
            print(f"[Feedback] No se pudo recuperar estado abierto: {exc}")

    # ---------------------------------------------------------
    def abrir(self, decision: DecisionFinal, precio_entrada: float,
              stop_loss: Optional[float] = None,
              lifecycle: Optional[dict] = None):
        """Registra en memoria una operación recién abierta."""
        if not decision.tipo:
            return
        feats = construir_features(decision)
        lifecycle = dict(lifecycle or {})
        prop = decision.propuesta
        riesgo_planificado = float(prop.riesgo_por_accion) if prop is not None else 0.0
        timestamp = decision.timestamp.isoformat() if decision.timestamp else ""
        ticker = str(decision.ticker or "").strip().upper()
        if not ticker:
            return
        with self._lock:
            self._reload_closed_locked()
            pending = {
                "feedback_id": uuid.uuid4().hex,
                "features": feats,
                "decision": decision,
                "precio_entrada": float(precio_entrada),
                "stop_loss": float(stop_loss) if stop_loss is not None else None,
                "riesgo_planificado": riesgo_planificado,
                "lifecycle": lifecycle,
                "tipo": str(decision.tipo or "").upper(),
                "timestamp": timestamp,
            }
            if self._committed_close(ticker, pending) is not None:
                return
            existing = self._abiertas.get(ticker)
            if existing is not None:
                closing_row = existing.get("closing_row")
                if isinstance(closing_row, dict):
                    # Retry a prior disk failure before accepting this next
                    # position. Failure preserves the old evidence and surfaces.
                    self.cerrar(ticker, closing_row.get("precio_salida"), lifecycle=None)
                    existing = self._abiertas.get(ticker)
                    if self._committed_close(ticker, pending) is not None:
                        return
            if existing is not None:
                identity = self._pending_identity(ticker, pending)
                if identity is not None and identity == self._pending_identity(ticker, existing):
                    return
                raise ValueError(f"{ticker}: apertura pendiente de aprendizaje; no se sobrescribe")
            self._abiertas[ticker] = pending
            self._persistir_abiertas_locked()

    def cerrar(self, ticker: str, precio_salida: float,
               lifecycle: Optional[dict] = None) -> Optional[dict]:
        """
        Cierra la operación del ticker, calcula el R múltiple real y
        persiste la fila completa en el CSV. Devuelve el registro o None.
        """
        ticker = str(ticker or "").strip().upper()
        with self._lock:
            pend = self._abiertas.get(ticker)
            if pend is None:
                return None
            self._reload_closed_locked()
            committed = self._committed_close(ticker, pend)
            if committed is None:
                fila = pend.get("closing_row")
                if not isinstance(fila, dict):
                    fila = self._construir_cierre(ticker, precio_salida, pend, lifecycle)
                    if fila is not None:
                        pend["closing_row"] = fila
                if fila is None:
                    return None
                # Persist the immutable close intent before appending. The engine
                # has already confirmed this outcome and may release its position,
                # so recovery needs the outcome as well as the entry features.
                self._persistir_abiertas_locked()
                # Keep both RAM and durable open state until the CSV row has
                # been flushed. Any append error therefore remains retryable.
                self._persistir(fila)
                self._reload_closed_locked()
            else:
                # Retrying after a sidecar failure must make no second CSV row.
                # Re-flush an existing row before deleting its recovery evidence.
                with open(self.ruta_csv, "r+b") as handle:
                    os.fsync(handle.fileno())
                fila = dict(committed)
            self._cerradas_totales = len(eligible_closed_rows(self._closed_rows))
            self._abiertas.pop(ticker, None)
            try:
                self._persistir_abiertas_locked()
            except Exception:
                self._abiertas[ticker] = pend
                raise
            return fila

    def _construir_cierre(self, ticker: str, precio_salida: float, pend: dict,
                         lifecycle: Optional[dict]) -> Optional[dict]:

        decision: Optional[DecisionFinal] = pend.get("decision")
        prop = decision.propuesta if decision is not None else None
        tipo = str(
            (decision.tipo if decision is not None else pend.get("tipo")) or ""
        ).upper()
        if tipo not in ("BUY", "SELL"):
            return None
        entrada = pend["precio_entrada"]
        feats = pend["features"]
        opening = dict(pend.get("lifecycle") or {})
        closing = dict(lifecycle or {})

        # R múltiple realizado = beneficio / riesgo planificado, con signo.
        # El riesgo se toma de la entrada y el SL REALES de la orden (mismos
        # valores redondeados que operó el motor), para que numerador y
        # denominador sean consistentes. Si no está, se usa el de la propuesta.
        sl = pend.get("stop_loss")
        if sl is not None:
            riesgo = abs(entrada - sl)
        elif prop is not None:
            riesgo = prop.riesgo_por_accion
        else:
            riesgo = float(pend.get("riesgo_planificado") or 0.0)
        r_mult = 0.0
        if riesgo > 0:
            if tipo == "BUY":
                r_mult = (precio_salida - entrada) / riesgo
            else:  # SELL
                r_mult = (entrada - precio_salida) / riesgo
        ganadora = 1 if r_mult > 0 else 0

        expected_position = "LONG" if tipo == "BUY" else "SHORT"
        expected_entry_action = "BUY" if tipo == "BUY" else "SELL_SHORT"
        expected_exit_action = "SELL" if tipo == "BUY" else "BUY_TO_COVER"
        mode = str(opening.get("modo") or closing.get("modo") or "").upper()
        required_ids = all(
            str(opening.get(key, "") or "").strip()
            for key in ("execution_id", "position_id", "signal_id", "correlation_id")
        )
        direction_valid = (
            str(opening.get("position_side", "")).upper() == expected_position
            and str(opening.get("signal_side", "")).upper() == tipo
            and str(opening.get("entry_action", "")).upper() == expected_entry_action
            and str(closing.get("exit_action", "")).upper() == expected_exit_action
            and str(opening.get("state_before", "")).upper() == "OUT"
            and str(opening.get("state_after", "")).upper() == expected_position
            and str(closing.get("state_before", "")).upper() == expected_position
            and str(closing.get("state_after", "")).upper() == "OUT"
        )
        links_valid = all(bool(closing.get(key)) for key in (
            "audit_linked", "history_linked"
        )) and bool(opening.get("audit_linked")) and bool(opening.get("history_linked")) \
            and bool(opening.get("slippage_linked"))
        broker_valid = bool(opening.get("entry_confirmed")) and bool(
            closing.get("exit_confirmed")
        )
        order_ids_valid = bool(opening.get("entry_order_id")) and (
            mode == "PAPER" or bool(closing.get("exit_order_id"))
        )
        economic_valid = all(math.isfinite(float(value)) and float(value) > 0
                             for value in (entrada, precio_salida, riesgo))
        mode_valid = mode in ("REAL", "PAPER")
        execution_valid = int(
            required_ids and direction_valid and links_valid and broker_valid
            and order_ids_valid and economic_valid and mode_valid
        )
        reasons = []
        if not required_ids:
            reasons.append("MISSING_CORRELATION_IDS")
        if not direction_valid:
            reasons.append("DIRECTION_OR_STATE_MISMATCH")
        if not links_valid:
            reasons.append("LEDGER_LINK_MISSING")
        if not broker_valid:
            reasons.append("BROKER_CONFIRMATION_MISSING")
        if not order_ids_valid:
            reasons.append("ORDER_ID_MISSING")
        if not economic_valid:
            reasons.append("INVALID_PRICE_OR_RISK")
        if not mode_valid:
            reasons.append("EXECUTION_MODE_INVALID")
        entry_reversal_id = str(opening.get("reversal_id", "") or "")
        exit_reversal_id = str(closing.get("reversal_id", "") or "")

        fila = {
            "feedback_id": pend.get("feedback_id") or uuid.uuid4().hex,
            "execution_id": opening.get("execution_id", ""),
            "position_id": opening.get("position_id", ""),
            "signal_id": opening.get("signal_id", ""),
            "reversal_id": exit_reversal_id or entry_reversal_id,
            "correlation_id": opening.get("correlation_id", ""),
            "entry_signal_id": opening.get("signal_id", ""),
            "exit_signal_id": closing.get("signal_id", ""),
            "entry_reversal_id": entry_reversal_id,
            "exit_reversal_id": exit_reversal_id,
            "entry_correlation_id": opening.get("correlation_id", ""),
            "exit_correlation_id": closing.get("correlation_id", ""),
            "modo": mode,
            "entry_order_id": opening.get("entry_order_id", ""),
            "exit_order_id": closing.get("exit_order_id", ""),
            "position_side": expected_position,
            "signal_side": opening.get("signal_side", tipo),
            "entry_action": opening.get("entry_action", ""),
            "exit_action": closing.get("exit_action", ""),
            "entry_state_before": opening.get("state_before", ""),
            "entry_state_after": opening.get("state_after", ""),
            "exit_state_before": closing.get("state_before", ""),
            "exit_state_after": closing.get("state_after", ""),
            "entry_confirmed": int(bool(opening.get("entry_confirmed"))),
            "exit_confirmed": int(bool(closing.get("exit_confirmed"))),
            "audit_linked": int(
                bool(opening.get("audit_linked")) and bool(closing.get("audit_linked"))
            ),
            "history_linked": int(
                bool(opening.get("history_linked")) and bool(closing.get("history_linked"))
            ),
            "slippage_linked": int(bool(opening.get("slippage_linked"))),
            "execution_valid": execution_valid,
            "exclusion_reason": "" if execution_valid else ";".join(reasons),
            "timestamp": (
                decision.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                if decision is not None and decision.timestamp
                else str(pend.get("timestamp", "") or "").replace("T", " ")[:19]
            ),
            "ticker": ticker,
            "tipo": tipo,
            "confianza_macro": feats["confianza_macro"],
            "ratio_rb": feats["ratio_rb"],
            "atr_relativo": feats["atr_relativo"],
            "fvg_tam_rel": feats["fvg_tam_rel"],
            "hueco_apertura": feats["hueco_apertura"],
            "confluencia": int(feats["confluencia"]),
            "hora": int(feats["hora"]),
            "es_rango_lateral": int(feats["es_rango_lateral"]),
            "gsco_match": _csv_num(feats.get("gsco_match")),
            "gsco_dist_pct": _csv_num(feats.get("gsco_dist_pct")),
            **{
                nombre: _csv_num(feats.get(nombre))
                for nombre in VOLUME_FEATURE_NAMES
            },
            **{
                nombre: _csv_num(feats.get(nombre))
                for nombre in GALA10_FEATURE_NAMES
            },
            "precio_entrada": round(entrada, 4),
            "precio_salida": round(float(precio_salida), 4),
            "r_multiple": round(r_mult, 4),
            "ganadora": ganadora,
            "closed_utc": str(closing.get("closed_utc") or datetime.now(timezone.utc).isoformat()),
            "risk_per_share": round(float(riesgo), 8),
        }
        return fila

    def total_cerradas(self) -> int:
        with self._lock:
            self._cerradas_totales = self._contar_filas()
            return self._cerradas_totales

    def total_cerradas_ticker(self, ticker: str) -> int:
        """Cuenta las operaciones cerradas de UN ticker (para el reentrenamiento
        del modelo individual de ese activo)."""
        try:
            with self._lock:
                self._reload_closed_locked()
                summary = summarize_closed_trades(self._closed_rows)
                return int(summary.get(str(ticker or "").strip().upper(), {}).get(
                    "eligible_observations", 0
                ))
        except Exception:
            return 0

    def tiene_abierta(self, ticker: str) -> bool:
        with self._lock:
            return str(ticker or "").strip().upper() in self._abiertas

    # ---------------------------------------------------------
    def _persistir(self, fila: dict):
        """Escribe una fila en el CSV (con cabecera si es nuevo)."""
        carpeta = os.path.dirname(self.ruta_csv)
        if carpeta:
            os.makedirs(carpeta, exist_ok=True)
        # Chequeo de existencia y escritura de forma ATÓMICA bajo el lock, para
        # que dos hilos no escriban ambos la cabecera ni intercalen filas.
        with self._lock:
            original_size = os.path.getsize(self.ruta_csv) if os.path.exists(self.ruta_csv) else 0
            try:
                with open(self.ruta_csv, "a", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=COLUMNAS)
                    if original_size == 0:
                        writer.writeheader()
                    writer.writerow(fila)
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                # Roll back any partial line on a handled I/O error. The open
                # sidecar is still intact, so a later close can append cleanly.
                try:
                    with open(self.ruta_csv, "r+b") as f:
                        f.truncate(original_size)
                        f.flush()
                        os.fsync(f.fileno())
                except OSError:
                    pass
                raise

    def _contar_filas(self) -> int:
        if not os.path.exists(self.ruta_csv):
            return 0
        try:
            with self._lock:
                self._reload_closed_locked()
                return len(eligible_closed_rows(self._closed_rows))
        except Exception:
            return 0
