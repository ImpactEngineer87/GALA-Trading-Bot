import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import server


class PersistentSignalHistoryTests(unittest.TestCase):
    def test_jsonl_history_keeps_valid_rows_when_tail_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "historial_senales.jsonl"
            rows = [
                {"signal_id": "one", "ticker": "AMD", "tipo": "BUY"},
                {"signal_id": "two", "ticker": "TSLA", "tipo": "SELL"},
            ]
            with patch.object(server, "_signal_history_path", return_value=str(path)):
                for row in rows:
                    server._append_signal_history(row)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write('{"signal_id":"interrupted"')
                self.assertEqual(server._read_signal_history(), rows)
                self.assertEqual(server._read_signal_history(ticker="AMD"), rows[:1])

    def test_signals_api_is_not_limited_to_old_200_row_memory_slice(self):
        rows = [
            {"signal_id": str(i), "ticker": "AMD", "tipo": "BUY"}
            for i in range(350)
        ]
        with patch.object(server, "_read_signal_history", return_value=rows), \
             server.app.test_request_context("/api/signals?limit=500"):
            response = server.get_signals()
        payload = response.get_json()
        self.assertTrue(payload["persistent"])
        self.assertEqual(payload["total"], 350)
        self.assertEqual(len(payload["signals"]), 350)


class SignalChartTests(unittest.TestCase):
    def test_chart_centers_on_exact_clicked_signal_and_returns_markers(self):
        now = server._ahora_ny_datetime().replace(second=0, microsecond=0)
        selected = {
            "signal_id": "selected-buy",
            "ticker": "AMD",
            "tipo": "BUY",
            "precio": 172.5,
            "epoch_ms": int(now.timestamp() * 1000),
            "timestamp_iso": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "timestamp": now.strftime("%H:%M:%S"),
        }
        later = {
            **selected,
            "signal_id": "later-sell",
            "tipo": "SELL",
            "precio": 173.0,
            "epoch_ms": int((now + timedelta(minutes=5)).timestamp() * 1000),
            "timestamp_iso": (now + timedelta(minutes=5)).isoformat(),
        }
        candles = [
            {
                "epoch_ms": int((now + timedelta(minutes=offset)).timestamp() * 1000),
                "timestamp_iso": (now + timedelta(minutes=offset)).isoformat(),
                "open": 172.0,
                "high": 174.0,
                "low": 171.5,
                "close": 172.5,
                "volume": 1000,
            }
            for offset in (-10, 0, 10)
        ]
        with patch.object(server, "_read_signal_history", return_value=[selected, later]), \
             patch.object(server, "_load_local_chart_candles", return_value=([], "")), \
             patch.object(server, "_fetch_chart_candles", return_value=candles) as fetch, \
             patch.object(server, "_store_chart_candles"), \
             server.app.test_request_context(
                 "/api/signals/chart?ticker=AMD&signal_id=selected-buy&window=60"
             ):
            response = server.get_signal_chart()
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["selected_signal"]["signal_id"], "selected-buy")
        self.assertEqual([s["signal_id"] for s in payload["signals"]], [
            "selected-buy", "later-sell",
        ])
        start_ny, end_ny = fetch.call_args.args[1:]
        self.assertEqual(start_ny, now - timedelta(minutes=60))
        self.assertEqual(end_ny, now + timedelta(minutes=60))

    def test_dashboard_wires_ticker_and_signal_clicks_to_chart(self):
        html = (Path(__file__).parents[1] / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("function openTickerChart", html)
        self.assertIn("function openSignalChart", html)
        self.assertIn("function openDateChart", html)
        self.assertIn('id="signalChartDate"', html)
        self.assertIn("params.set('date',S.selectedDate)", html)
        self.assertIn("/api/signals/chart?", html)
        self.assertIn("Historial persistente", html)
        self.assertNotIn('onclick="clearSignals()"', html)

        engine = (Path(__file__).parents[1] / "engine" / "bot_engine.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("and not datos.sintetico", engine)

        server_source = (Path(__file__).parents[1] / "server.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("_CANDLE_ARCHIVE_QUEUE.put_nowait", server_source)

    def test_dashboard_renders_japanese_candles_with_ohlc_wicks(self):
        html = (Path(__file__).parents[1] / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("const candleSvg=candles.map", html)
        self.assertIn('class="price-candle ${rising?', html)
        self.assertIn("y1=\"${yHigh.toFixed(1)}\"", html)
        self.assertIn("y2=\"${yLow.toFixed(1)}\"", html)
        self.assertIn("O ${open.toFixed(2)}", html)
        self.assertIn("H ${high.toFixed(2)}", html)
        self.assertIn("L ${low.toFixed(2)}", html)
        self.assertIn("C ${close.toFixed(2)}", html)
        self.assertIn("velas japonesas 1m", html)
        self.assertIn("Velas japonesas y señales históricas", html)
        self.assertNotIn("const path=candles.map", html)

    def test_dashboard_has_realtime_session_zoom_and_pan_controls(self):
        html = (Path(__file__).parents[1] / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="signalChartLiveButton"', html)
        self.assertIn("function openLiveChart", html)
        self.assertIn("S.chartMode='live'", html)
        self.assertIn("09:15–15:45", html)
        self.assertIn("function zoomSignalChart", html)
        self.assertIn("function showFullChartSession", html)
        self.assertIn("addEventListener('wheel'", html)
        self.assertIn("addEventListener('pointerdown'", html)
        self.assertIn("function updateSignalChartCrosshair", html)
        self.assertIn('id="signalChartCrosshair"', html)
        self.assertIn('id="crosshairPriceText"', html)
        self.assertIn('id="crosshairTimeText"', html)
        self.assertIn("const tickMinutes=", html)
        self.assertIn("fetchSignalChart({silent:true,preserveViewport:true})", html)
        self.assertIn("params.set('view',S.chartMode==='live'?'live':'session')", html)
        self.assertIn("data.active_position", html)
        self.assertIn("data.trade_events", html)
        self.assertIn("P&amp;L flotante", html)
        self.assertIn("SALIDA 70%", html)
        self.assertIn("Cierre final", html)
        self.assertNotIn(">±30m<", html)

    def test_date_click_requests_the_complete_new_york_day_from_local_data(self):
        ny_tz = server._ahora_ny_datetime().tzinfo
        start = datetime(2026, 8, 4, 9, 30, tzinfo=ny_tz)
        candles = [
            {
                "epoch_ms": int((start + timedelta(minutes=offset)).timestamp() * 1000),
                "timestamp_iso": (start + timedelta(minutes=offset)).isoformat(),
                "open": 172.0, "high": 173.0, "low": 171.5,
                "close": 172.5, "volume": 1000,
            }
            for offset in (0, 1, 2)
        ]
        with patch.object(server, "_read_signal_history", return_value=[]), \
             patch.object(
                 server, "_load_local_chart_candles",
                 return_value=(candles, "historico_csv"),
             ) as local_load, \
             patch.object(server, "_fetch_chart_candles") as broker_fetch, \
             server.app.test_request_context(
                 "/api/signals/chart?ticker=AMD&date=2026-08-04&view=date"
             ):
            response = server.get_signal_chart()
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["source"], "historico_csv")
        self.assertEqual(payload["selected_date"], "2026-08-04")
        self.assertEqual(payload["view"], "date")
        requested_start, requested_end = local_load.call_args.args[1:]
        self.assertEqual(requested_start.strftime("%Y-%m-%d %H:%M:%S"), "2026-08-04 00:00:00")
        self.assertEqual(requested_end.strftime("%Y-%m-%d %H:%M:%S"), "2026-08-04 23:59:59")
        broker_fetch.assert_not_called()

    def test_live_chart_merges_schwab_extended_hours_into_0915_1545_session(self):
        ny_tz = server._ahora_ny_datetime().tzinfo
        now = datetime(2026, 8, 6, 10, 31, 30, tzinfo=ny_tz)
        session_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        session_end = now.replace(hour=15, minute=45, second=0, microsecond=0)
        local_rows = [
            {
                "epoch_ms": int((session_start + timedelta(minutes=offset)).timestamp() * 1000),
                "timestamp_iso": (session_start + timedelta(minutes=offset)).isoformat(),
                "open": 100.0, "high": 101.0, "low": 99.0,
                "close": 100.0 + offset, "volume": 500,
            }
            for offset in (0, 1)
        ]
        broker_rows = [
            {**local_rows[1], "close": 102.25},
            {
                "epoch_ms": int(now.replace(second=0, microsecond=0).timestamp() * 1000),
                "timestamp_iso": now.replace(second=0, microsecond=0).isoformat(),
                "open": 102.0, "high": 103.0, "low": 101.5,
                "close": 102.75, "volume": 800,
            },
        ]
        with patch.object(server, "_ahora_ny_datetime", return_value=now), \
             patch.object(server, "_read_signal_history", return_value=[]), \
             patch.object(
                 server, "_load_local_chart_candles",
                 return_value=(local_rows, "local_archive"),
             ) as local_load, \
             patch.object(
                 server, "_fetch_chart_candles", return_value=broker_rows,
             ) as broker_fetch, \
             patch.object(server, "_store_chart_candles"), \
             server.app.test_request_context(
                 "/api/signals/chart?ticker=AMD&view=live"
             ):
            response = server.get_signal_chart()
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["live"])
        self.assertEqual(payload["live_status"], "connected")
        self.assertEqual(payload["source"], "schwab_live")
        self.assertEqual(payload["refresh_seconds"], 5)
        self.assertEqual(payload["session_start_epoch_ms"], int(session_start.timestamp() * 1000))
        self.assertEqual(payload["session_end_epoch_ms"], int(session_end.timestamp() * 1000))
        self.assertEqual(len(payload["candles"]), 3)
        self.assertEqual(payload["candles"][1]["close"], 102.25)
        requested_start, requested_end = local_load.call_args.args[1:]
        self.assertEqual(requested_start, session_start)
        self.assertEqual(requested_end, now)
        self.assertTrue(broker_fetch.call_args.kwargs["include_extended"])

    def test_live_chart_returns_engine_position_and_persistent_exit_markers(self):
        ny_tz = server._ahora_ny_datetime().tzinfo
        now = datetime(2026, 8, 6, 11, 0, 30, tzinfo=ny_tz)
        session_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        candles = [
            {
                "epoch_ms": int((now.replace(second=0, microsecond=0) + timedelta(minutes=offset)).timestamp() * 1000),
                "timestamp_iso": (now.replace(second=0, microsecond=0) + timedelta(minutes=offset)).isoformat(),
                "open": 101.0, "high": 102.0, "low": 100.5,
                "close": 101.5 + offset, "volume": 900,
            }
            for offset in (-1, 0)
        ]
        position = {
            "ticker": "AMD", "side": "BUY", "status": "PARTIAL",
            "entry_price": 100.0, "remaining_quantity": 6,
            "initial_quantity": 20, "stop_current": 100.0,
            "tp1_price": 103.5, "final_target": 104.5,
        }
        trade_events = [{
            "event_id": "partial", "kind": "PARTIAL_70",
            "epoch_ms": int(now.replace(second=0, microsecond=0).timestamp() * 1000),
            "price": 103.5, "quantity": 14, "pnl_usd": 49.0,
            "label": "SALIDA 70%",
        }]
        with patch.object(server, "_ahora_ny_datetime", return_value=now), \
             patch.object(server, "_read_signal_history", return_value=[]), \
             patch.object(server, "_load_local_chart_candles", return_value=(candles, "local_archive")), \
             patch.object(server, "_fetch_chart_candles", return_value=candles), \
             patch.object(server, "_store_chart_candles"), \
             patch.object(server, "_engine_positions_snapshot", return_value=[position]), \
             patch.object(server, "_chart_trade_events", return_value=trade_events), \
             server.app.test_request_context("/api/signals/chart?ticker=AMD&view=live"):
            payload = server.get_signal_chart().get_json()

        self.assertEqual(payload["active_position"]["ticker"], "AMD")
        self.assertEqual(payload["active_position"]["last_price"], 101.5)
        self.assertEqual(payload["active_position"]["floating_pnl"], 9.0)
        self.assertEqual(payload["trade_events"], trade_events)
        self.assertEqual(
            payload["session_start_epoch_ms"], int(session_start.timestamp() * 1000)
        )

    def test_sqlite_candle_archive_survives_and_filters_by_ticker(self):
        ny_tz = server._ahora_ny_datetime().tzinfo
        start = datetime(2026, 8, 4, 10, 0, tzinfo=ny_tz)
        candles = [
            {
                "epoch_ms": int((start + timedelta(minutes=offset)).timestamp() * 1000),
                "open": 100 + offset, "high": 101 + offset,
                "low": 99 + offset, "close": 100.5 + offset,
                "volume": 500 + offset,
            }
            for offset in range(3)
        ]
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(
                 server, "_candle_history_path",
                 return_value=str(Path(tmp) / "candles.sqlite3"),
             ):
            self.assertEqual(server._store_chart_candles("AMD", candles, "engine_1m"), 3)
            loaded = server._read_cached_chart_candles(
                "AMD", start - timedelta(minutes=1), start + timedelta(minutes=5)
            )
            other = server._read_cached_chart_candles(
                "TSLA", start - timedelta(minutes=1), start + timedelta(minutes=5)
            )
        self.assertEqual(len(loaded), 3)
        self.assertEqual([row["close"] for row in loaded], [100.5, 101.5, 102.5])
        self.assertEqual(other, [])

    def test_existing_training_csv_is_used_as_local_chart_data(self):
        ny_tz = server._ahora_ny_datetime().tzinfo
        start = datetime(2026, 8, 4, 10, 0, tzinfo=ny_tz)
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "AMD.csv"
            csv_path.write_text(
                "datetime,open,high,low,close,volume\n"
                "2026-08-04 14:00:00,100,101,99,100.5,500\n"
                "2026-08-04 14:01:00,100.5,102,100,101.5,600\n",
                encoding="utf-8",
            )
            with patch.object(
                server, "_local_history_settings", return_value=(tmp, "UTC")
            ):
                rows = server._read_training_history_candles(
                    "AMD", start - timedelta(minutes=1), start + timedelta(minutes=2)
                )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["timestamp_iso"][:16], "2026-08-04T10:00")
        self.assertEqual(rows[1]["close"], 101.5)


if __name__ == "__main__":
    unittest.main()
