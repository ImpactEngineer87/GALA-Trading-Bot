"""
=============================================================
MÓDULO: agents/backtest_trainer.py
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    PRE-ENTRENAMIENTO HISTÓRICO de los 3 Agentes SMC.

    Responde a: "el bot no puede empezar de cero". En lugar de arrancar
    con el meta-modelo en frío, este módulo REPRODUCE el historial de
    cada acción barra por barra, ejecuta el sistema de agentes en cada
    punto de decisión (SIN mirar el futuro), simula el resultado real de
    cada operación con las barras posteriores, y con ese dataset ENTRENA
    el meta-modelo ANTES de operar en vivo.

    Así el bot llega al primer día ya con un modelo entrenado sobre datos
    reales del propio activo, y sigue afinándose de forma dinámica.

    NOTA de alcance: Schwab entrega ~1 año en velas DIARIAS pero solo
    ~semanas de velas de MINUTO. El pre-entrenamiento intradía usa la
    profundidad real disponible (se registra en el log); el contexto
    macro (Agente 1) sí aprovecha el año completo diario/semanal.
=============================================================
"""

from datetime import datetime, time
from typing import List, Optional

import pandas as pd

from agents.contracts import DatosMultiTemporal
from agents.gala10_features import extraer_features_gala10
from agents.orchestrator import OrquestadorAgentes
from api.schwab_client import SchwabClient
from core.strategy import GALA10Strategy
from agents.training_report import guardar_reporte_modelos


class PreEntrenadorHistorico:
    """Reproduce el historial y pre-entrena el meta-modelo de los agentes."""

    def __init__(self, config: dict, schwab: Optional[SchwabClient] = None,
                 orquestador: Optional[OrquestadorAgentes] = None):
        self.config = config
        self.schwab = schwab or SchwabClient(config)
        # Un orquestador propio para no interferir con el del motor en vivo.
        # La auditoría recibe la hora histórica mediante hora_et_override, por
        # lo que debe conservar el MISMO filtro RTH que producción. Desactivarlo
        # entrenaría ejemplos pre/after-market que el bot real nunca ejecutaría.
        if orquestador is not None:
            self.orq = orquestador
        else:
            import copy
            cfg_bt = copy.deepcopy(config)
            # Generar etiquetas para TODAS las señales GALA10. Un modelo ya cargado
            # no puede vetar ejemplos antes de que entren al nuevo dataset.
            cfg_bt.setdefault("agents", {}).setdefault("entrenamiento", {})["activo"] = False
            self.orq = OrquestadorAgentes(cfg_bt)
        # Gala 10 = disparador también en el PRE-ENTRENAMIENTO: cada modelo por
        # ticker aprende a filtrar las señales de Gala 10 de ESE activo.
        self.estrategia = GALA10Strategy(config)
        bt = config.get("agents", {}).get("backtest", {})
        self.tf_decision = bt.get("timeframe_decision", "5m")
        self.stride = int(bt.get("stride", 3))              # 1 = cada barra
        self.max_decisiones = int(bt.get("max_decisiones_por_ticker", 400))
        self.max_barras_futuras = int(bt.get("max_barras_futuras", 120))
        self.warmup = int(bt.get("warmup", 60))
        self.hora_cierre = self._parse_hora(
            config.get("scheduler", {}).get("hora_fin", "15:45"),
            time(15, 45),
        )

    # =========================================================
    def pre_entrenar(self, tickers: List[str]) -> dict:
        """
        Reproduce el historial de cada ticker, acumula operaciones simuladas
        y entrena el meta-modelo. Devuelve un resumen con métricas.
        """
        total_ops = 0
        for ticker in tickers:
            try:
                ops = self._reproducir_ticker(ticker)
                total_ops += ops
                print(f"[BACKTEST] {ticker}: {ops} operaciones simuladas")
            except Exception as e:
                print(f"[BACKTEST] Error en {ticker}: {e}")

        print(f"[BACKTEST] Total operaciones acumuladas: {total_ops}")
        # Entrenar UN modelo XGBoost por ticker (individual por acción).
        metricas = self.orq.entrenador.entrenar_todos(tickers)
        if metricas:
            for t, m in metricas.items():
                print(f"[BACKTEST] Modelo {t} evaluado | muestras={m.get('muestras')} "
                      f"| accuracy={m.get('accuracy')}% | activado={m.get('activado')}")
            rutas = guardar_reporte_modelos(
                metricas,
                self.config.get("agents", {}).get("entrenamiento", {}).get(
                    "reportes_dir", "data/reportes"),
            )
            print(f"[BACKTEST] Reporte JSON: {rutas['json']}")
            print(f"[BACKTEST] Reporte CSV:  {rutas['csv']}")
        if not metricas:
            print(
                "[BACKTEST] No se pudo evaluar ningún modelo; revise las muestras, "
                "las clases ganadora/perdedora y el esquema de features."
            )
        elif not any(m.get("activado") for m in metricas.values()):
            print(
                "[BACKTEST] Todos los modelos fueron evaluados, pero ninguno superó "
                "las puertas mínimas de calidad. Permanecerán desactivados."
            )
        return {"operaciones": total_ops, "metricas": metricas}

    # =========================================================
    def _reproducir_ticker(self, ticker: str) -> int:
        """Camina el historial del ticker y registra operaciones simuladas."""
        # PRIORIDAD: histórico local EN BLOQUE (p. ej. FirstRate, ~2 años) si existe.
        # Con más historia la IA ve más ciclos/rangos/trampas → modelos sólidos.
        # Si no hay archivo local, cae a Schwab (~8 días) automáticamente.
        from agents.historico_loader import cargar_historico_local
        datos_full = cargar_historico_local(ticker, self.config)
        if datos_full is None:
            datos_full = self.schwab.obtener_multitemporal(ticker, self.config)
        if getattr(datos_full, "sintetico", False):
            print(f"[BACKTEST] {ticker}: sin datos reales (fallback sintético) — se omite")
            return 0

        micro = datos_full.tf(self.tf_decision)
        if micro is None or len(micro) < self.warmup + self.max_barras_futuras + 5:
            print(f"[BACKTEST] {ticker}: historial {self.tf_decision} insuficiente")
            return 0

        # OPTIMIZACIÓN CLAVE para históricos largos (2 años ~ 195k velas):
        # se precalcula la señal de Gala 10 UNA sola vez sobre TODO el 1m. Es causal
        # (cada barra usa solo su pasado), así que da lo mismo que recalcular sobre la
        # ventana <= t_now. Antes se recalculaba en CADA barra sobre una ventana
        # creciente (O(n^2)) y además el snapshot + los agentes corrían en cada barra
        # → inviable con 2 años. Ahora la señal es una lectura O(1) por barra, y el
        # snapshot + la auditoría SOLO se construyen cuando Gala 10 dispara.
        try:
            gala10_calc = self.estrategia.calcular(micro)
            sig_arr = gala10_calc["señal"].to_numpy()
        except Exception as e:
            print(f"[BACKTEST] {ticker}: no se pudo calcular la señal: {e}")
            return 0
        close_arr = micro["close"].to_numpy()

        n = len(micro)
        fin = n - self.max_barras_futuras - 1     # dejar barras futuras para el desenlace
        ops = 0
        decisiones = 0

        idx = self.warmup
        while idx < fin and decisiones < self.max_decisiones:
            senal_gala10 = sig_arr[idx]
            if senal_gala10 not in ("BUY", "SELL"):
                idx += self.stride
                continue
            # Solo cuando dispara Gala 10: instantánea (recortada) + auditoría.
            t_now = micro.index[idx]
            snap = self._snapshot(datos_full, t_now)
            precio = float(close_arr[idx])
            atr = self._atr_en(micro, idx)
            gala10_contexto = extraer_features_gala10(
                gala10_calc, posicion=idx, senal=senal_gala10,
                timestamp=t_now,
            )
            # Barras cerradas → en_formacion=False. GSCO=None (queda NaN; el modelo
            # lo aprende como faltante; en vivo sí se usa cuando la hoja tiene datos).
            decision = self.orq.auditar_gala10(
                snap, precio, senal_gala10, atr,
                ahora=self._as_dt(t_now), actual_en_formacion=False, gsco=None,
                gala10_contexto=gala10_contexto,
            )
            decisiones += 1

            if decision.operar and decision.propuesta is not None:
                res = self._simular_desenlace(micro, idx, decision)
                if res is not None:
                    exit_price, exit_bar = res
                    entrada = decision.propuesta.precio_entrada
                    sl = decision.propuesta.stop_loss
                    self.orq.feedback.abrir(decision, entrada, sl)
                    self.orq.feedback.cerrar(ticker, exit_price)
                    ops += 1
                    idx = max(exit_bar + 1, idx + 1)   # saltar al cierre real (no solapar)
                    continue
            idx += self.stride

        return ops

    # =========================================================
    def _senal_gala10(self, snap: DatosMultiTemporal) -> Optional[str]:
        """Señal de Gala 10 (core/strategy) sobre las barras del snapshot (sin futuro)."""
        df = snap.tf("1m")
        if df is None:
            df = snap.tf("5m")
        if df is None or len(df) < 35:
            return None
        try:
            s = self.estrategia.calcular(df)["señal"].iloc[-1]
            return s if s in ("BUY", "SELL") else None
        except Exception:
            return None

    def _atr_en(self, micro, idx: int) -> float:
        """ATR (Wilder) al cierre de la barra idx. Solo las últimas ~120 velas: el
        suavizado de Wilder (período 14) ya converge y evita recorrer todo el histórico."""
        try:
            from agents import smc_primitives as smc
            lo = max(0, idx - 120)
            serie = smc.atr_wilder(micro.iloc[lo:idx + 1],
                                   self.config.get("atr", {}).get("period", 14))
            return float(serie.iloc[-1]) if len(serie) else 0.0
        except Exception:
            return 0.0

    # =========================================================
    def _snapshot(self, datos: DatosMultiTemporal, t_now) -> DatosMultiTemporal:
        """Recorta cada temporalidad a las velas con índice <= t_now (sin futuro) y a
        las últimas 'cap' velas. Los agentes solo miran lo reciente (ATR, FVG, rango,
        swings), así que limitar la ventana hace cada auditoría O(cap) en vez de O(n)
        — imprescindible con 2 años de historia. El diario/semanal (pocos cientos de
        velas en 2 años) queda entero dentro de 'cap'."""
        cap = int(self.config.get("agents", {}).get("backtest", {}).get("snapshot_velas", 800))
        recorte = {}
        for tf, df in datos.velas.items():
            if df is None or len(df) == 0:
                continue
            sub = df.loc[:t_now]
            if len(sub) > cap:
                sub = sub.iloc[-cap:]
            if len(sub) > 0:
                recorte[tf] = sub
        return DatosMultiTemporal(ticker=datos.ticker, velas=recorte, sintetico=False)

    def _simular_desenlace(self, micro: pd.DataFrame, idx: int, decision):
        """
        Simula el resultado de la operación con las barras posteriores.
        Devuelve (precio_salida_efectivo, barra_de_salida) o None si una entrada
        LIMIT de retroceso nunca se llena (como en vivo: sin llenado no hay operación).

        Realismo:
          - Entrada a MERCADO (prop.fvg is None) llena en la barra de la señal.
          - Entrada LIMIT (FVG) SOLO llena si el precio TOCA 'entrada'; si no, None
            (antes se registraban 'ganadoras' fantasma de retrocesos que nunca ocurrían,
            sesgando el modelo).
          - La operación se CIERRA al terminar la jornada (como el kill switch de las
            15:45 en vivo): no se arrastra al otro día ni se dispara SL/TP con el salto
            nocturno entre días concatenados.
          - Si una barra toca SL y TP a la vez, prevalece el SL (conservador).
        """
        prop = decision.propuesta
        tipo = prop.tipo
        entrada = prop.precio_entrada
        sl = prop.stop_loss
        tp_final = prop.take_profit
        n = len(micro)

        # Replicar el MISMO plan 70/30 que usa GestorDeRiesgo en vivo. La
        # propuesta del orquestador solo contiene el objetivo FINAL; TP1 se
        # deriva del riesgo de la operación y de rb_primer_objetivo.
        cfg_parcial = self.config.get("salida_parcial", {})
        parcial_activa = bool(cfg_parcial.get("activo", True))
        pct_tp1 = float(cfg_parcial.get("pct_primer_objetivo", 0.70))
        pct_tp1 = min(1.0, max(0.0, pct_tp1))
        pct_resto = 1.0 - pct_tp1
        rb_tp1 = max(0.0, float(cfg_parcial.get("rb_primer_objetivo", 1.0)))
        riesgo = abs(float(entrada) - float(sl))
        if tipo == "BUY":
            tp1 = float(entrada) + riesgo * rb_tp1
            objetivo_valido = float(tp_final) > float(entrada)
            if objetivo_valido:
                tp1 = min(max(tp1, float(entrada) + 0.01),
                          float(tp_final) - 0.01)
        else:
            tp1 = float(entrada) - riesgo * rb_tp1
            objetivo_valido = float(tp_final) < float(entrada)
            if objetivo_valido:
                tp1 = max(min(tp1, float(entrada) - 0.01),
                          float(tp_final) + 0.01)
        parcial_activa = bool(
            parcial_activa and objetivo_valido and riesgo > 0.0
            and 0.0 < pct_tp1 < 1.0
        )
        if not parcial_activa:
            tp1 = float(tp_final)
            pct_tp1, pct_resto = 1.0, 0.0

        modo_resto = str(cfg_parcial.get("modo_resto", "breakeven")).lower()
        if parcial_activa and modo_resto == "breakeven":
            offset_pct = max(
                0.0, float(cfg_parcial.get("breakeven_offset_pct", 0.0)))
            offset = float(entrada) * (offset_pct / 100.0)
            sl_resto = (
                float(entrada) - offset
                if tipo == "BUY"
                else float(entrada) + offset
            )
        else:
            sl_resto = float(sl)

        # --- LLENADO ---
        if prop.fvg is None:
            fill = idx
        else:
            fill = None
            for j in range(idx + 1, min(n, idx + 1 + self.max_barras_futuras)):
                if (tipo == "BUY" and float(micro["low"].iloc[j]) <= entrada) or \
                   (tipo == "SELL" and float(micro["high"].iloc[j]) >= entrada):
                    fill = j
                    break
            if fill is None:
                return None

        try:
            fill_dia = micro.index[fill].date()
        except Exception:
            fill_dia = None

        # --- Recorrer barras DESDE el llenado (hasta SL/TP o fin de jornada) ---
        tp1_hit = False
        pnl = 0.0
        ultimo = fill
        for j in range(fill + 1, min(n, fill + 1 + self.max_barras_futuras)):
            if fill_dia is not None and micro.index[j].date() != fill_dia:
                break                      # cierre de jornada: no arrastrar al otro día
            # El dataset puede contener extended hours para reproducir el estado
            # exacto de Gala10. La operación simulada, igual que producción, debe
            # quedar plana al kill switch y no usar velas posteriores como futuro.
            if self._as_dt(micro.index[j]).time() >= self.hora_cierre:
                break
            ultimo = j
            hi = float(micro["high"].iloc[j])
            lo = float(micro["low"].iloc[j])
            if tipo == "BUY":
                sl_actual = sl_resto if tp1_hit else sl
                if lo <= sl_actual:
                    pnl += (
                        (sl_actual - entrada) * pct_resto
                        if tp1_hit else (sl_actual - entrada)
                    )
                    return (entrada + pnl, j)
                if parcial_activa and not tp1_hit and hi >= tp1:
                    pnl += (tp1 - entrada) * pct_tp1
                    tp1_hit = True
                elif (not parcial_activa or tp1_hit) and hi >= tp_final:
                    peso = pct_resto if tp1_hit else 1.0
                    pnl += (tp_final - entrada) * peso
                    return (entrada + pnl, j)
            else:  # SELL
                sl_actual = sl_resto if tp1_hit else sl
                if hi >= sl_actual:
                    pnl += (
                        (entrada - sl_actual) * pct_resto
                        if tp1_hit else (entrada - sl_actual)
                    )
                    return (entrada - pnl, j)
                if parcial_activa and not tp1_hit and lo <= tp1:
                    pnl += (entrada - tp1) * pct_tp1
                    tp1_hit = True
                elif (not parcial_activa or tp1_hit) and lo <= tp_final:
                    peso = pct_resto if tp1_hit else 1.0
                    pnl += (entrada - tp_final) * peso
                    return (entrada - pnl, j)

        # No se resolvió: cerrar al último cierre del día/ventana
        cierre = float(micro["close"].iloc[ultimo])
        if tipo == "BUY":
            pnl = (
                pnl + (cierre - entrada) * pct_resto
                if tp1_hit else (cierre - entrada)
            )
            return (entrada + pnl, ultimo)
        else:
            pnl = (
                pnl + (entrada - cierre) * pct_resto
                if tp1_hit else (entrada - cierre)
            )
            return (entrada - pnl, ultimo)

    @staticmethod
    def _as_dt(valor) -> datetime:
        """Convierte el índice de vela a hora ET (naive) para la feature 'hora'.

        Schwab entrega epoch-ms → el índice queda en UTC naive. En vivo la decisión
        se sella con hora ET (bot_engine usa _ahora_et). Para que la feature 'hora'
        del XGBoost tenga la MISMA convención en pre-entrenamiento y en vivo (y evitar
        el sesgo train/serve por zona horaria) convertimos aquí UTC→America/New_York.
        """
        try:
            t = pd.Timestamp(valor)
            if t.tzinfo is None:
                t = t.tz_localize("UTC")            # índice de Schwab: UTC naive
            t = t.tz_convert("America/New_York").tz_localize(None)
            return t.to_pydatetime()
        except Exception:
            return datetime(1970, 1, 1)

    @staticmethod
    def _parse_hora(valor, fallback: time) -> time:
        try:
            hh, mm = str(valor).split(":")[:2]
            return time(int(hh), int(mm))
        except Exception:
            return fallback


# =============================================================
# EJECUCIÓN DIRECTA:  python -m agents.backtest_trainer
# =============================================================
if __name__ == "__main__":
    import sys
    import yaml
    # Consola UTF-8 segura (VPS Windows puede usar cp1252)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Lista de tickers: config.market.tickers o los 20+ por defecto del recolector
    tickers = cfg.get("market", {}).get("tickers", []) or \
        ["BA", "AAPL", "TSLA", "NVDA", "AMZN", "META", "AMD", "RCL", "UNH"]

    print("\n" + "=" * 60)
    print("  PRE-ENTRENAMIENTO HISTÓRICO DE LOS 3 AGENTES SMC")
    print(f"  Tickers: {len(tickers)} | TF decisión: {cfg.get('agents',{}).get('backtest',{}).get('timeframe_decision','5m')}")
    print("=" * 60 + "\n")

    pre = PreEntrenadorHistorico(cfg)
    resultado = pre.pre_entrenar(tickers)
    if not resultado["metricas"]:
        sys.exit(1)