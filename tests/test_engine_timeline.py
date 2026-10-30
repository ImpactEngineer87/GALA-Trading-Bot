"""Historical timeline uses retained evidence, UTC/NY conversion and no broker."""
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import audit_engine_timeline as timeline


class EngineTimelineTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = self.root/'dashboard_server.log'

    def test_utc_midnight_is_assigned_to_the_correct_new_york_session(self):
        self.log.write_text('[2026-10-24T00:15:00+00:00] [NY 20:15:00] INFO: [Motor] SOLICITUD DE ARRANQUE desde dashboard\n')
        rows, available = timeline.read_server_log(self.log, date(2026, 10, 23))
        self.assertTrue(available)
        self.assertEqual(rows[0]['timestamp_ny'], '2026-10-23T20:15:00-04:00')
        self.assertEqual(timeline.read_server_log(self.log, date(2026, 10, 24))[0], [])

    def test_request_acceptance_and_initialization_are_distinct(self):
        self.log.write_text('\n'.join([
            '[2026-10-23T16:00:00Z] [NY 12:00:00] INFO: [Motor] SOLICITUD DE ARRANQUE desde dashboard',
            '[2026-10-23T16:00:01Z] [NY 12:00:01] INFO: [Motor] ARRANCADO desde dashboard (modo: REAL)',
            '[2026-10-23T16:00:05Z] [NY 12:00:05] INFO: Motor iniciado en modo MIXED | 17 activos']))
        result, _ = timeline.read_server_log(self.log, date(2026, 10, 23))
        self.assertEqual([r['event'] for r in result], ['START_REQUESTED', 'START_ACCEPTED', 'ENGINE_INITIALIZATION_LOG'])
        self.assertEqual(result[-1]['mode'], 'MIXED')

    def test_naive_malformed_and_other_date_logs_do_not_invent_a_start(self):
        self.log.write_text('[2026-10-23T16:00:00] [NY 12:00:00] INFO: Motor iniciado en modo REAL\n'
                            'bad date [Motor] ARRANCADO desde dashboard\n')
        self.assertEqual(timeline.read_server_log(self.log, date(2026, 10, 23))[0], [])

    def test_failures_are_exported_as_codes_without_private_error_text(self):
        self.log.write_text('[2026-10-23T16:00:00Z] [NY 12:00:00] ERROR: [Motor] Terminó con error: token=SECRET account=123456\n')
        rows, _ = timeline.read_server_log(self.log, date(2026, 10, 23))
        self.assertEqual(rows[0]['event'], 'THREAD_FAILED')
        self.assertNotIn('SECRET', json.dumps(rows))
        self.assertNotIn('123456', json.dumps(rows))

    def test_manual_stop_and_unresolved_exposure_do_not_imply_flat(self):
        self.assertEqual(timeline.classify('[Motor] DETENIDO desde dashboard, PERO QUEDA EXPOSICIÓN REAL: private')[0], 'STOP_EXPOSURE_UNRESOLVED')
        self.assertEqual(timeline.classify('[Motor] DETENIDO desde dashboard — broker plano confirmado')[0], 'STOP_CONFIRMED_FLAT')
        self.assertEqual(timeline.classify('BA Contabilidad REAL pendiente: missing ID')[0], 'REAL_ACCOUNTING_BLOCK')

    def test_service_exit_is_not_automatically_classified_as_oom_or_engine_crash(self):
        ts = int(datetime(2026, 10, 23, 16, tzinfo=timezone.utc).timestamp()*1_000_000)
        rows = timeline.read_journal([json.dumps({'__REALTIME_TIMESTAMP': str(ts),
            'MESSAGE': 'gala.service: Main process exited, code=killed, status=9/KILL'})], date(2026, 10, 23))
        self.assertEqual(rows[0]['event'], 'SERVICE_PROCESS_EXIT')

    def test_journal_boundaries_respect_new_york_dst(self):
        command = timeline.journal_command(date(2026, 11, 1))
        self.assertEqual(command[command.index('--since')+1], '2026-11-01 04:00:00 UTC')
        self.assertEqual(command[command.index('--until')+1], '2026-11-02 05:00:00 UTC')
        self.assertEqual(command[:3], ['journalctl', '-u', 'gala'])

    def test_missing_logs_mean_unknown_and_cli_makes_no_other_requests(self):
        output = io.StringIO()
        with patch.object(timeline, 'ROOT', self.root), patch.object(
                timeline.subprocess, 'run', return_value=Mock(returncode=1, stdout='private', stderr='private')) as process, redirect_stdout(output):
            self.assertEqual(timeline.main(['--date', '2026-10-23', '--log', str(self.log), '--journal']), 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result['server_log_available'])
        self.assertFalse(result['journal_available'])
        self.assertIsNone(result['first_start_request_observed_ny'])
        self.assertEqual(result['broker_requests'], 0)
        self.assertNotIn('private', output.getvalue())
        self.assertEqual(process.call_args.args[0][0], 'journalctl')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cli_preserves_log_bytes_and_reports_only_observed_start(self):
        self.log.write_text('[2026-10-23T16:00:00Z] [NY 12:00:00] INFO: [Motor] ARRANCADO desde dashboard (modo: REAL)\n')
        before = self.log.read_bytes(), self.log.stat().st_mtime_ns
        output = io.StringIO()
        with patch.object(timeline, 'ROOT', self.root), redirect_stdout(output):
            timeline.main(['--date', '2026-10-23', '--log', str(self.log)])
        result = json.loads(output.getvalue())
        self.assertIsNotNone(result['first_start_acceptance_observed_ny'])
        self.assertIsNone(result['first_engine_initialization_log_observed_ny'])
        self.assertEqual((self.log.read_bytes(), self.log.stat().st_mtime_ns), before)

    def test_restart_loop_output_keeps_first_and_last_evidence_without_a_large_dump(self):
        baseline = datetime(2026, 10, 23, 16, tzinfo=timezone.utc)
        self.log.write_text('\n'.join(
            f'[{(baseline+timedelta(seconds=i)).isoformat()}] [NY 12:00:00] INFO: [Motor] HILO INICIADO (modo: REAL)'
            for i in range(100)))
        output = io.StringIO()
        with patch.object(timeline, 'ROOT', self.root), redirect_stdout(output):
            timeline.main(['--date', '2026-10-23', '--log', str(self.log)])
        result = json.loads(output.getvalue())
        self.assertEqual(result['matching_records'], 100)
        self.assertEqual(len(result['events']), 40)
        self.assertEqual(result['omitted_records'], 60)
        self.assertEqual(result['events'][0]['timestamp_utc'], baseline.isoformat())
        self.assertEqual(result['events'][-1]['timestamp_utc'], (baseline+timedelta(seconds=99)).isoformat())


if __name__ == '__main__':
    unittest.main()
