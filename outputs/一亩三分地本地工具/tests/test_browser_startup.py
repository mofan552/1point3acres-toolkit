import io
import itertools
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from settings import WINDOWS
from tests.test_reader_integration import start_reader_browser, _process_metrics


class UnreadableLog(io.BytesIO):
    def read(self, *_):
        raise OSError('synthetic private diagnostic failure')


class ReapCheckedLog(io.BytesIO):
    def read(self, *args):
        assert self.process.wait.called, 'Read the inherited log only after reaping its writer'
        return super().read(*args)


class BrowserStartupTests(unittest.TestCase):
    def test_spawn_failure_closes_temporary_diagnostic_file(self):
        log = io.BytesIO()
        with patch('tests.test_reader_integration.tempfile.TemporaryFile', return_value=log), \
                patch('tests.test_reader_integration.subprocess.Popen', side_effect=OSError('synthetic spawn failure')):
            with self.assertRaises(OSError):
                start_reader_browser(Path('fixture.html'), Path('synthetic-profile'))
        self.assertTrue(log.closed)

    def test_diagnostic_read_failure_does_not_mask_startup_failure_or_skip_reaping(self):
        log = UnreadableLog()
        process = SimpleNamespace(poll=lambda: 1, wait=Mock(), kill=Mock())
        with tempfile.TemporaryDirectory() as directory, \
                patch('tests.test_reader_integration.tempfile.TemporaryFile', return_value=log), \
                patch('tests.test_reader_integration.subprocess.Popen', return_value=process), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            with self.assertRaisesRegex(RuntimeError, 'Test Chrome exited before'):
                start_reader_browser(Path(directory) / 'fixture.html', Path(directory) / 'profile')
        process.wait.assert_called_once_with(timeout=5)
        process.kill.assert_not_called()
        self.assertNotIn('synthetic private diagnostic failure', output.getvalue())
        self.assertTrue(log.closed)

    def test_diagnostics_emit_fixed_classifications_without_raw_log(self):
        log = ReapCheckedLog(b'GPU process launch failed; synthetic-private-log-value')
        process = SimpleNamespace(poll=lambda: 1, wait=Mock(), kill=Mock())
        log.process = process
        with tempfile.TemporaryDirectory() as directory, \
                patch('tests.test_reader_integration.tempfile.TemporaryFile', return_value=log), \
                patch('tests.test_reader_integration.subprocess.Popen', return_value=process), \
                patch('tests.test_reader_integration._process_metrics', side_effect=RuntimeError('synthetic metric failure')), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            with self.assertRaisesRegex(RuntimeError, 'Test Chrome exited before'):
                start_reader_browser(Path(directory) / 'fixture.html', Path(directory) / 'profile')
        self.assertIn('gpu_process_launch_failed=True', output.getvalue())
        self.assertNotIn('synthetic-private-log-value', output.getvalue())
        process.wait.assert_called_once_with(timeout=5)
        metrics = _process_metrics(SimpleNamespace(_handle=-1))
        if WINDOWS:
            self.assertTrue(metrics['process_metrics_available'])
            self.assertGreaterEqual(metrics['main_process_cpu_seconds'], 0)
            self.assertGreaterEqual(metrics['main_process_read_bytes'], 0)
        else:
            self.assertFalse(metrics['process_metrics_available'])

    def test_existing_invalid_port_file_is_not_reported_as_late_ready(self):
        state = {'exit_code': None}
        process = SimpleNamespace(poll=lambda: state['exit_code'], wait=Mock(),
                                  kill=lambda: state.update(exit_code=1))
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / 'profile'
            profile.mkdir()
            (profile / 'DevToolsActivePort').write_text('invalid-port', encoding='ascii')
            with patch('tests.test_reader_integration.subprocess.Popen', return_value=process), \
                    patch('tests.test_reader_integration.time.monotonic', side_effect=itertools.count()), \
                    patch('tests.test_reader_integration.time.sleep'), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                with self.assertRaisesRegex(TimeoutError, 'Test Chrome debugging endpoint was not ready'):
                    start_reader_browser(Path(directory) / 'fixture.html', profile)
            self.assertIn('late_port_file_ready=False', output.getvalue())
            self.assertNotIn('late_port_file_ready_seconds=', output.getvalue())
            process.wait.assert_called_once_with(timeout=5)
