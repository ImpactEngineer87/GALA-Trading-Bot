import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from engine.scheduler import PlanificadorHorario


class SchedulerTests(unittest.TestCase):
    def _automatic(self, now_box, events, running):
        scheduler = PlanificadorHorario(
            {
                "scheduler": {
                    "modo": "automatico",
                    "hora_inicio": "09:15",
                    "hora_fin": "15:45",
                    "zona_horaria": "America/New_York",
                }
            },
            now_provider=lambda: now_box[0],
        )

        def start():
            events.append("start")
            running[0] = True
            return True

        def stop():
            events.append("stop")
            running[0] = False

        scheduler._cb_encender = start
        scheduler._cb_apagar = stop
        scheduler._cb_estado = lambda: running[0]
        return scheduler

    def test_manual_mode_never_starts_by_itself(self):
        scheduler = PlanificadorHorario({
            "scheduler": {
                "modo": "manual",
                "hora_inicio": "09:15",
                "hora_fin": "15:45",
            }
        })
        self.assertEqual(scheduler.evaluar_una_vez(), "manual")
        self.assertFalse(scheduler.activo)

    def test_automatic_uses_new_york_time_and_stops_at_end(self):
        # 13:15 UTC in July is 09:15 in New York.
        now_box = [datetime(2026, 7, 29, 13, 15, tzinfo=timezone.utc)]
        events = []
        running = [False]
        scheduler = self._automatic(now_box, events, running)

        self.assertEqual(scheduler.evaluar_una_vez(), "encendido")
        self.assertEqual(events, ["start"])

        now_box[0] = datetime(2026, 7, 29, 19, 45, tzinfo=timezone.utc)
        self.assertEqual(scheduler.evaluar_una_vez(), "apagado")
        self.assertEqual(events, ["start", "stop"])

    def test_manual_emergency_stop_suspends_same_session_only(self):
        ny = ZoneInfo("America/New_York")
        now_box = [datetime(2026, 7, 29, 10, 0, tzinfo=ny)]
        events = []
        running = [False]
        scheduler = self._automatic(now_box, events, running)

        self.assertEqual(scheduler.evaluar_una_vez(), "encendido")
        running[0] = False
        scheduler.suspender_sesion_actual()
        self.assertEqual(scheduler.evaluar_una_vez(), "suspendido")
        self.assertEqual(events, ["start"])

        now_box[0] = datetime(2026, 7, 30, 10, 0, tzinfo=ny)
        self.assertEqual(scheduler.evaluar_una_vez(), "encendido")
        self.assertEqual(events, ["start", "start"])

    def test_weekend_never_auto_starts(self):
        ny = ZoneInfo("America/New_York")
        now_box = [datetime(2026, 8, 1, 10, 0, tzinfo=ny)]
        events = []
        running = [False]
        scheduler = self._automatic(now_box, events, running)

        self.assertEqual(scheduler.evaluar_una_vez(), "sin_cambio")
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
