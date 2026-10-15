"""Catch missing startup modules without constructing or running the bot."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class EngineImportTests(unittest.TestCase):
    def test_dashboard_and_engine_import_without_network_or_engine_start(self):
        repository = str(Path(__file__).resolve().parents[1])
        script = """
import sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
with patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected HTTP request')), \\
     patch('socket.socket.connect', side_effect=AssertionError('Unexpected network connection')):
    import engine.bot_engine
    import engine.scheduler
    import server
    assert callable(engine.bot_engine.ChocoletraBot)
    assert callable(engine.bot_engine.cancelar_orden_compatible)
    assert server._ENGINE is None
    assert not server.BOT_STATE['running']
print('Dashboard and engine import OK; bot remains stopped')
"""
        # Import-time logging goes into a disposable directory rather than the
        # operator's trading logs. The bot constructor is never invoked.
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", script, repository],
                cwd=directory, capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("bot remains stopped", result.stdout)


if __name__ == "__main__":
    unittest.main()
