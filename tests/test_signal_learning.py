"""Offline regressions for causal signal labels and REAL/PAPER recording."""

import csv
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd

from agents import signal_learning_store
from agents.contracts import DecisionFinal, PropuestaOperacion
from agents.signal_learning_store import COLUMNS, SignalLearningStore
from engine.bot_engine import ChocoletraBot


BASELINE = datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)


def decision(*, signal_id="signal-1", side="BUY", baseline=BASELINE):
    decision_time = BASELINE + timedelta(minutes=1, seconds=3)
    if isinstance(baseline, datetime):
        decision_time = (
            baseline.replace(tzinfo=timezone.utc) if baseline.tzinfo is None else baseline
        ) + timedelta(minutes=1, seconds=3)
    value = DecisionFinal(
        ticker="AAPL", operar=False, tipo=side,
        propuesta=PropuestaOperacion(
            ticker="AAPL", tipo=side, precio_entrada=100.0,
            stop_loss=99.0 if side == "BUY" else 101.0,
            take_profit=102.0 if side == "BUY" else 98.0,
            ratio_rb=2.0,
        ),
        timestamp=decision_time.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None),
    )
    value.signal_id = signal_id
    value.signal_bar_timestamp = baseline
    return value


class SignalLearningTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / "signals.csv"
        self.store = SignalLearningStore(str(self.path))
        self.enterContext(patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Unexpected HTTP request"),
        ))
        self.enterContext(patch(
            "socket.socket.connect",
            side_effect=AssertionError("Unexpected network connection"),
        ))

    def record(self, value=None, **kwargs):
        return self.store.record_nonexecuted(
            value or decision(), execution_mode=kwargs.pop("execution_mode", "REAL"),
            source_kind="AI_VETO", disposition="BLOCKED", **kwargs,
        )

    def bar(self, *, when=None, high=100.5, low=99.5, close=100.0):
        return self.store.update_bar(
            "AAPL", high=high, low=low, close=close,
            bar_timestamp=when or BASELINE + timedelta(minutes=1),
        )

    def rows(self):
        with self.path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_utc_naive_candle_baseline_is_persisted(self):
        row = self.record(decision(baseline=BASELINE.replace(tzinfo=None)))
        self.assertEqual(row["signal_bar_utc"], BASELINE.isoformat())
        self.assertEqual(self.rows()[0]["signal_bar_utc"], BASELINE.isoformat())
        state = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["active"][row["observation_id"]]["signal_bar_utc"], BASELINE.isoformat())

    def test_legacy_naive_decision_time_is_new_york_not_vps_time(self):
        value = decision()
        del value.signal_bar_timestamp
        self.record(value)
        self.assertEqual(
            self.store.pending_after("AAPL"),
            datetime(2026, 10, 15, 14, 1, 3, tzinfo=timezone.utc),
        )

    def test_invalid_explicit_baseline_is_rejected(self):
        self.assertIsNone(self.record(decision(baseline="invalid")))
        self.assertFalse(self.path.exists())

    def test_signal_candle_must_have_closed_before_decision_features_are_recorded(self):
        value = decision()
        value.timestamp = datetime(2026, 10, 15, 10, 0, 30)
        self.assertIsNone(self.record(value))
        self.assertFalse(self.path.exists())

    def test_signal_and_older_candles_cannot_resolve_signal(self):
        row = self.record()
        for when in (BASELINE - timedelta(minutes=1), BASELINE):
            self.assertEqual(self.bar(when=when, high=103.0), [])
        self.assertEqual(self.store._active[row["observation_id"]]["bars_seen"], 0)
        self.assertEqual(self.bar(high=103.0)[0]["resolution"], "TP")

    def test_equivalent_and_out_of_order_timestamps_are_ignored(self):
        row = self.record()
        newest = BASELINE + timedelta(minutes=1)
        self.assertEqual(self.bar(when=newest), [])
        for when in (BASELINE + timedelta(seconds=30), newest.astimezone(timezone(timedelta(hours=-4)))):
            self.assertEqual(self.bar(when=when, low=98.0), [])
        self.assertEqual(self.store._active[row["observation_id"]]["bars_seen"], 1)

    def test_missing_bar_timestamp_cannot_supply_a_label(self):
        self.record()
        self.assertEqual(self.store.update_bar("AAPL", high=103, low=99.5, close=100), [])

    def test_invalid_brackets_are_not_recorded(self):
        for side, stop, target in (
            ("BUY", 101, 102), ("BUY", 99, 98), ("BUY", -1, 102),
            ("SELL", 99, 98), ("SELL", 101, 102), ("SELL", 101, 0),
        ):
            with self.subTest(side=side, stop=stop, target=target):
                value = decision(side=side)
                value.propuesta.stop_loss = stop
                value.propuesta.take_profit = target
                self.assertIsNone(self.record(value))
        self.assertFalse(self.path.exists())

    def test_invalid_candles_cannot_advance_or_resolve(self):
        row = self.record()
        for high, low, close in ((103, 104, 100), (103, 99.5, 104), (103, -1, 100), (float("nan"), 99.5, 100)):
            self.assertEqual(self.bar(high=high, low=low, close=close), [])
        self.assertEqual(self.store._active[row["observation_id"]]["bars_seen"], 0)

    def test_buy_and_sell_tp_sl_outcomes_have_correct_labels(self):
        cases = (("BUY", 103, 99.5, "TP", 1, 2.0), ("BUY", 100.5, 98, "SL", 0, -1.0),
                 ("SELL", 100.5, 97, "TP", 1, 2.0), ("SELL", 102, 99.5, "SL", 0, -1.0))
        for index, (side, high, low, resolution, winner, r_multiple) in enumerate(cases):
            with self.subTest(side=side, resolution=resolution):
                self.record(decision(signal_id=f"signal-{index}", side=side))
                result = self.bar(high=high, low=low)[0]
                self.assertEqual(result["resolution"], resolution)
                self.assertEqual(result["ganadora"], winner)
                self.assertEqual(result["r_multiple"], r_multiple)
                self.assertEqual(result["training_eligible"], 1)

    def test_ambiguous_timeout_and_policy_only_outcomes_are_not_trainable(self):
        self.record()
        self.assertEqual(self.bar(high=103, low=98)[0]["training_eligible"], 0)
        self.store.max_bars = 1
        self.record(decision(signal_id="timeout"))
        result = self.bar()[0]
        self.assertEqual(result["resolution"], "TIMEOUT")
        self.assertEqual(result["training_eligible"], 0)
        self.record(decision(signal_id="policy"), training_candidate=False)
        self.assertEqual(self.bar(high=103)[0]["training_eligible"], 0)

    def test_open_event_recovers_without_valid_sidecar(self):
        row = self.record()
        for content in (None, "{broken json"):
            with self.subTest(content=content):
                if content is None:
                    self.store.state_path.unlink(missing_ok=True)
                else:
                    self.store.state_path.write_text(content, encoding="utf-8")
                recovered = SignalLearningStore(str(self.path))
                self.assertIn(row["observation_id"], recovered._active)
                self.assertEqual(recovered.pending_after("AAPL"), BASELINE)
                self.assertEqual(len(recovered.update_bar(
                    "AAPL", high=103, low=99.5, close=100,
                    bar_timestamp=BASELINE + timedelta(minutes=1),
                )), 1)
                # Restore the OPEN-only ledger for the next recovery case.
                with self.path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerow(row)

    def test_resolved_csv_event_wins_over_stale_sidecar_and_dedupes_retry(self):
        row = self.record()
        stale_state = self.store.state_path.read_bytes()
        self.bar(high=103)
        self.store.state_path.write_bytes(stale_state)
        recovered = SignalLearningStore(str(self.path))
        self.assertEqual(recovered._active, {})
        self.assertIsNone(recovered.record_nonexecuted(
            decision(), execution_mode="REAL", source_kind="AI_VETO", disposition="BLOCKED",
        ))
        self.assertEqual(len(self.rows()), 2)

    def test_progress_and_monotonic_cursor_survive_restart(self):
        row = self.record()
        last = BASELINE + timedelta(minutes=1)
        self.bar(when=last)
        recovered = SignalLearningStore(str(self.path))
        self.assertEqual(recovered.pending_after("AAPL"), last)
        self.assertEqual(recovered._active[row["observation_id"]]["bars_seen"], 1)
        self.assertEqual(recovered.update_bar(
            "AAPL", high=103, low=99.5, close=100,
            bar_timestamp=BASELINE + timedelta(seconds=30),
        ), [])

    def test_missing_intraday_candle_censors_outcome_before_possible_tp(self):
        self.record()
        result = self.bar(when=BASELINE + timedelta(minutes=2), high=103)[0]
        self.assertEqual(result["resolution"], "DATA_GAP")
        self.assertEqual(result["label_quality"], "INCOMPLETE_CANDLE_HISTORY")
        self.assertEqual(result["training_eligible"], 0)
        self.assertEqual(result["ganadora"], "")

    def test_overnight_and_weekend_transitions_are_not_missing_candle_labels(self):
        cases = (
            (datetime(2026, 10, 15, 19, 59, tzinfo=timezone.utc), datetime(2026, 10, 16, 13, 30, tzinfo=timezone.utc)),
            (datetime(2026, 10, 16, 19, 59, tzinfo=timezone.utc), datetime(2026, 10, 19, 13, 30, tzinfo=timezone.utc)),
        )
        for index, (baseline, next_open) in enumerate(cases):
            self.record(decision(signal_id=f"overnight-{index}", baseline=baseline))
            result = self.bar(when=next_open, high=103)[0]
            self.assertEqual(result["resolution"], "TP")
            self.assertEqual(result["training_eligible"], 1)

    def test_next_day_open_cannot_hide_missing_prior_session_candles(self):
        self.record()
        next_open = datetime(2026, 10, 16, 13, 30, tzinfo=timezone.utc)
        result = self.bar(when=next_open, high=103)[0]
        self.assertEqual(result["resolution"], "DATA_GAP")
        self.assertEqual(result["training_eligible"], 0)

    def test_complete_prior_close_does_not_hide_missing_next_session_open(self):
        baseline = datetime(2026, 10, 15, 19, 59, tzinfo=timezone.utc)
        self.record(decision(baseline=baseline))
        result = self.bar(when=datetime(2026, 10, 16, 13, 31, tzinfo=timezone.utc), high=103)[0]
        self.assertEqual(result["resolution"], "DATA_GAP")
        self.assertEqual(result["training_eligible"], 0)

    def test_missing_whole_weekday_session_is_censored(self):
        baseline = datetime(2026, 10, 15, 19, 59, tzinfo=timezone.utc)
        self.record(decision(baseline=baseline))
        result = self.bar(when=datetime(2026, 10, 19, 13, 30, tzinfo=timezone.utc), high=103)[0]
        self.assertEqual(result["resolution"], "DATA_GAP")

    def test_after_hours_bar_cannot_hide_missing_regular_session_candles(self):
        self.record()
        result = self.bar(when=datetime(2026, 10, 15, 21, 0, tzinfo=timezone.utc), high=103)[0]
        self.assertEqual(result["resolution"], "DATA_GAP")

    def test_five_minute_signal_cannot_learn_from_predecision_one_minute_bars(self):
        value = decision()
        value.timestamp = datetime(2026, 10, 15, 10, 5, 3)
        opened = self.record(value, bar_duration_minutes=5)
        for minute in range(1, 5):
            self.assertEqual(self.store.update_bar(
                "AAPL", high=103, low=99.5, close=100,
                bar_timestamp=BASELINE + timedelta(minutes=minute), bar_duration_minutes=1,
            ), [])
        active = self.store._active[opened["observation_id"]]
        self.assertEqual(active["bars_seen"], 0)
        self.assertEqual(active["last_bar_ts"], "")
        self.assertEqual(active["bar_duration_minutes"], 5)
        result = self.store.update_bar(
            "AAPL", high=103, low=99.5, close=100,
            bar_timestamp=BASELINE + timedelta(minutes=5), bar_duration_minutes=1,
        )[0]
        self.assertEqual(result["resolution"], "TP")
        self.assertEqual(result["training_eligible"], 1)
        self.assertEqual(result["bars_seen"], 1)
        self.assertEqual(result["bar_duration_minutes"], 5)

    def test_configured_five_minute_candles_preserve_cadence_and_knowledge_time(self):
        value = decision()
        value.timestamp = datetime(2026, 10, 15, 10, 5, 3)
        self.record(value, bar_duration_minutes=5)
        when = BASELINE + timedelta(minutes=5)
        result = self.store.update_bar(
            "AAPL", high=103, low=99.5, close=100,
            bar_timestamp=when, bar_duration_minutes=5,
        )[0]
        self.assertEqual(result["resolution"], "TP")
        self.assertGreaterEqual(datetime.fromisoformat(result["resolved_utc"]), when + timedelta(minutes=5))

    def test_label_knowledge_time_cannot_precede_confirming_bar_close(self):
        baseline = datetime(2030, 10, 15, 14, 0, tzinfo=timezone.utc)
        self.record(decision(baseline=baseline))
        when = baseline + timedelta(minutes=1)
        result = self.bar(when=when, high=103)[0]
        self.assertGreaterEqual(datetime.fromisoformat(result["resolved_utc"]), when + timedelta(minutes=1))

    def test_failed_open_event_write_does_not_create_an_unlogged_observation(self):
        with patch.object(self.store, "_append", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.record()
        self.assertEqual(self.store._active, {})
        self.assertEqual(self.store._seen_keys, set())
        self.assertIsNotNone(self.record())

    def test_failed_resolution_write_does_not_consume_the_decisive_candle(self):
        row = self.record()
        with patch.object(self.store, "_append", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.bar(high=103)
        self.assertEqual(self.store._active[row["observation_id"]]["bars_seen"], 0)
        self.assertEqual(self.bar(high=103)[0]["resolution"], "TP")

    def fail_first_fsync(self):
        real_fsync = signal_learning_store.os.fsync
        calls = []

        def fsync(descriptor):
            calls.append(descriptor)
            if len(calls) == 1:
                raise OSError("fsync unavailable")
            return real_fsync(descriptor)

        return patch.object(signal_learning_store.os, "fsync", side_effect=fsync)

    def test_failed_open_fsync_rolls_back_csv_before_retry(self):
        with self.fail_first_fsync():
            with self.assertRaisesRegex(OSError, "fsync unavailable"):
                self.record()
        self.assertEqual(self.path.stat().st_size, 0)
        self.assertEqual(self.store._active, {})
        self.assertEqual(self.store._seen_keys, set())
        self.record()
        self.assertEqual(len(self.rows()), 1)

    def test_failed_resolution_fsync_preserves_open_and_retries_exact_candle(self):
        opened = self.record()
        original_csv = self.path.read_bytes()
        original_state = self.store.state_path.read_bytes()
        with self.fail_first_fsync():
            with self.assertRaisesRegex(OSError, "fsync unavailable"):
                self.bar(high=103)
        self.assertEqual(self.path.read_bytes(), original_csv)
        self.assertEqual(self.store.state_path.read_bytes(), original_state)
        self.assertEqual(self.store._active[opened["observation_id"]]["bars_seen"], 0)
        result = self.bar(high=103)[0]
        self.assertEqual(result["resolution"], "TP")
        self.assertEqual(len(self.rows()), 2)

    def test_failed_snapshot_fsync_preserves_existing_snapshot_and_cleans_temp(self):
        self.record()
        original = self.store.state_path.read_bytes()
        with patch.object(signal_learning_store.os, "fsync", side_effect=OSError("snapshot fsync unavailable")):
            with self.assertRaisesRegex(OSError, "snapshot fsync unavailable"):
                self.store._persist_state_locked()
        self.assertEqual(self.store.state_path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_failed_migration_fsync_preserves_original_schema_and_evidence(self):
        row = self.record()
        legacy_columns = [name for name in COLUMNS if name not in {"signal_bar_utc", "last_bar_ts", "bar_duration_minutes"}]
        with self.path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=legacy_columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerow(row)
        original = self.path.read_bytes()
        self.store = SignalLearningStore(str(self.path))
        with self.fail_first_fsync():
            with self.assertRaisesRegex(OSError, "fsync unavailable"):
                self.record(decision(signal_id="migration-retry"))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.record(decision(signal_id="migration-retry"))
        self.assertEqual(len(self.rows()), 2)

    def test_existing_csv_schema_is_migrated_without_losing_old_evidence(self):
        row = self.record()
        legacy_columns = [name for name in COLUMNS if name not in {"signal_bar_utc", "last_bar_ts", "bar_duration_minutes"}]
        with self.path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=legacy_columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerow(row)
        self.store = SignalLearningStore(str(self.path))
        self.record(decision(signal_id="new-signal"), execution_mode="PAPER")
        old, new = self.rows()
        for column in legacy_columns:
            self.assertEqual(old[column], str(row.get(column, "")))
        self.assertEqual(new["signal_id"], "new-signal")
        self.assertEqual(new["signal_bar_utc"], BASELINE.isoformat())
        self.assertEqual(new["execution_mode"], "PAPER")

    def test_failed_snapshot_keeps_existing_file_and_cleans_only_owned_temp(self):
        row = self.record()
        original = self.store.state_path.read_bytes()
        shared_temp = Path(str(self.store.state_path) + ".tmp")
        shared_temp.write_text("other writer", encoding="utf-8")
        self.store._active[row["observation_id"]]["unserializable"] = object()
        with self.assertRaises(TypeError):
            self.store._persist_state_locked()
        self.assertEqual(self.store.state_path.read_bytes(), original)
        self.assertEqual(shared_temp.read_text(encoding="utf-8"), "other writer")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [shared_temp])

    def test_concurrent_snapshots_use_distinct_complete_temporary_files(self):
        writers = [SignalLearningStore(str(self.path)), SignalLearningStore(str(self.path))]
        rendezvous = threading.Barrier(2)
        replacement_lock = threading.Lock()
        real_replace = signal_learning_store.os.replace
        observed = []

        def replace_together(source, target):
            path = Path(source)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("active", payload)
            with replacement_lock:
                observed.append(path)
            rendezvous.wait(timeout=5)
            with replacement_lock:
                return real_replace(source, target)

        with patch.object(signal_learning_store.os, "replace", side_effect=replace_together):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(writer._persist_state_locked) for writer in writers]
                for future in futures:
                    future.result(timeout=10)
        self.assertEqual(len(set(observed)), 2)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_engine_replays_skipped_candles_in_order_before_later_opposite_touch(self):
        self.record()
        bot = ChocoletraBot.__new__(ChocoletraBot)
        bot.signal_learning = self.store
        frame = pd.DataFrame(
            {"high": [100.5, 103.0, 100.5], "low": [98.0, 99.5, 98.0], "close": [100.0] * 3},
            index=[BASELINE + timedelta(minutes=2), BASELINE + timedelta(minutes=1), BASELINE],
        )
        result = bot._avanzar_aprendizaje_senales("AAPL", frame)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["resolution"], "TP")
        self.assertEqual(result[0]["bars_seen"], 1)

    def test_engine_observes_real_missing_model_signal_without_opening_trade(self):
        self._assert_pipeline_records_without_execution("REAL", model_missing=True)

    def test_engine_observes_paper_veto_signal_without_opening_trade(self):
        self._assert_pipeline_records_without_execution("PAPER", model_missing=False)

    def _assert_pipeline_records_without_execution(self, mode, *, model_missing):
        bot = ChocoletraBot.__new__(ChocoletraBot)
        frame = pd.DataFrame(
            {"high": [100.5] * 35, "low": [99.5] * 35, "close": [100.0] * 35},
            index=pd.date_range(end=BASELINE, periods=35, freq="min"),
        )
        datos = SimpleNamespace(sintetico=False, tf=Mock(return_value=frame))
        value = decision()
        value.operar = model_missing  # Even an erroneous approval must stay blocked.
        bot.signal_learning = self.store
        bot.veto_observations = SimpleNamespace(update_bar=Mock(return_value=[]))
        bot._lock = threading.RLock()
        bot._posiciones = {}
        bot._last_prices = {}
        bot._last_price_times = {}
        bot._gala10_signal_bar = {"AAPL": frame.index[-1]}
        bot._gala10_contexto = {}
        bot.agentes_activos = True
        bot.live_allow_short = False
        bot.requerir_modelo_aprobado_en_real = True
        bot._is_paper_ticker = Mock(return_value=mode == "PAPER")
        bot._obtener_mtf = Mock(return_value=datos)
        bot._datos_micro_confirmados = Mock(return_value=datos)
        bot._emit_candles = Mock()
        bot._recover_ticker_quarantine = Mock()
        bot._quarantine_real_ticker = Mock()
        bot._emit_log = Mock()
        bot._evaluar_limite_perdida_diaria = Mock(return_value=False)
        bot._gestionar_posiciones = Mock()
        bot._senal_estrategia = Mock(return_value="BUY")
        bot._atr_micro = Mock(return_value=1.0)
        bot._gsco_features = Mock(return_value={})
        bot._ahora_et = Mock(return_value=datetime(2026, 10, 15, 10, 1, 3))
        bot._ai_threshold_for_ticker = Mock(return_value=None)
        bot._effective_ai_threshold = Mock(return_value=None)
        bot._ai_decision_reason = Mock(return_value="AI_MODEL_UNAVAILABLE")
        bot._emit_signal = Mock(return_value={"signal_id": "pipeline-signal"})
        bot._registrar_no_op = Mock()
        bot._abrir_o_revertir = Mock()
        bot.ejecutor_hibrido = SimpleNamespace(secuencia_simbolo=lambda ticker: nullcontext())
        bot.orquestador = SimpleNamespace(
            entrenador=SimpleNamespace(modelo_disponible=Mock(return_value=False)),
            auditar_gala10=Mock(return_value=value),
        )
        bot._procesar_ticker("AAPL")
        bot._abrir_o_revertir.assert_not_called()
        self.assertFalse(value.operar)
        bot.orquestador.auditar_gala10.assert_called_once()
        self.assertEqual(len(self.store._active), 1)
        row = next(iter(self.store._active.values()))
        self.assertEqual(row["execution_mode"], mode)
        self.assertEqual(row["source_kind"], "MODEL_UNAVAILABLE")
        self.assertEqual(row["signal_bar_utc"], BASELINE.isoformat())
        self.assertEqual(row["signal_id"], "pipeline-signal")


if __name__ == "__main__":
    unittest.main()
