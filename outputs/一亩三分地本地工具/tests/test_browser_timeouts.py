"""Bounded CDP calls, the daily-run deadline and a hang-proof exit (#97).

A lost browser link or a machine that slept mid-run must end the run within a known time with a registered
error and free the profile, never leave the process blocked forever while the scheduler waits on it."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import browser
from contracts import BrowserConnectionError, RunStatus, SessionState, daily_history_record, session_result


USER = {'username': 'test_member', 'uid': 123456}
GOTO = browser.Browser.goto


def never_answers(calls=None):
    """A driver call whose reply never arrives: it awaits a future nobody sets, exactly today's hang."""
    async def send(command):
        if calls is not None:
            calls.append(command)
        await asyncio.get_running_loop().create_future()
    return send


def fake_sb(loop, send):
    page = SimpleNamespace(send=send, websocket=object(), listener=SimpleNamespace(running=True))
    return SimpleNamespace(loop=loop, page=page, driver=SimpleNamespace(connection=page),
                           sleep=Mock(), solve_captcha=Mock())


class LoopCase(unittest.TestCase):
    """A real event loop, so timeouts and a stop from another thread behave as they do in production."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.close_loop)

    def close_loop(self):
        for _ in range(5):  # A stop the watchdog queued ends one run early; the next run completes.
            try:
                self.loop.run_until_complete(asyncio.sleep(0))
                break
            except RuntimeError:
                continue
        pending = [task for task in asyncio.all_tasks(self.loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def detached(self, send):
        """A Browser that never entered: no lock, no Chrome, only the driver seam under test."""
        session = browser.Browser(recover_login=False)
        session.sb = fake_sb(self.loop, send)
        return session


class BoundedCallTests(LoopCase):
    def test_a_call_the_driver_never_answers_ends_within_the_bound(self):
        # Today's failure: run_until_complete waited forever on a reply that never came.
        session = self.detached(never_answers())
        started = time.monotonic()
        with patch('browser.CDP_CALL_TIMEOUT', 0.05), \
                self.assertRaisesRegex(BrowserConnectionError, '^browser_connection_lost$'):
            session.evaluate('1')
        self.assertLess(time.monotonic() - started, 1)

    def test_a_driver_error_report_is_a_page_error_and_a_legitimate_none_is_kept(self):
        # The driver answers None for any protocol error and reconnects on the next call: reporting it as a lost
        # link would abort the polling that carries a run through page redirects. Cookie deletion also answers None.
        async def send(command):
            return None
        session = self.detached(send)
        with self.assertRaisesRegex(RuntimeError, '^page_expression_failed$') as context:
            session.evaluate('1')
        self.assertNotIsInstance(context.exception, BrowserConnectionError)
        self.assertIsNone(session._cdp(object()))


class WaitForTests(unittest.TestCase):
    def test_wait_for_stops_at_once_when_the_link_is_gone_but_keeps_polling_page_errors(self):
        # Swallowed, a dead link would only surface as a page timeout after the whole wait.
        session = browser.Browser(recover_login=False)
        session.sb = SimpleNamespace(sleep=Mock(), solve_captcha=Mock())
        session.evaluate = Mock(side_effect=BrowserConnectionError('browser_connection_lost'))
        with self.assertRaisesRegex(BrowserConnectionError, '^browser_connection_lost$'):
            session.wait_for('x', timeout=5)
        session.evaluate.assert_called_once()
        session.sb.sleep.assert_not_called()
        session.evaluate = Mock(side_effect=RuntimeError('page_expression_failed'))
        with self.assertRaisesRegex(RuntimeError, '^automatic_page_or_verification_timeout$'):
            session.wait_for('x', timeout=0.01, allow_solver=False)
        self.assertGreater(session.evaluate.call_count, 0)


class EnteredCase(LoopCase):
    """A Browser that really enters: lock file in a temp dir, Chrome launch and driver replaced, goto stubbed."""

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for target, value in [('browser.STATE', root / 'state'), ('browser.PROFILE', root / 'profile'),
                              ('browser.USERNAME', USER['username']), ('browser.ACCOUNT_UID', USER['uid'])]:
            self.replace(target, value)
        self.replace('browser.subprocess.Popen')
        self.replace('browser.requests.Session')
        chrome = self.replace('browser.sb_cdp.Chrome')
        chrome.return_value.loop.run_until_complete.side_effect = lambda awaitable: awaitable.close()
        self.replace('browser.Browser.goto')

    def replace(self, target, *args, **kwargs):
        replacement = patch(target, *args, **kwargs)
        self.addCleanup(replacement.stop)
        return replacement.start()

    @staticmethod
    def fake_process(waits=(0,)):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = list(waits)
        return process

    def assert_lock_free(self):
        with browser.Browser(recover_login=False):
            pass  # Entering again proves the lock was released, or it would fail another_task_is_using_the_browser.

    @staticmethod
    def settles(condition, within=2):
        """The watchdog acts from its own thread and releases the blocked caller (loop stop) before its next
        step (killing Chrome): an assertion on that next step waits for it, bounded, instead of racing it."""
        deadline = time.monotonic() + within
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.01)
        return condition()


class DeadlineTests(EnteredCase):
    def test_the_deadline_releases_a_blocked_call_kills_chrome_and_refuses_further_work(self):
        # The process-wide hang, and the trap where a late stop would break the next loop run instead.
        calls = []
        session = browser.Browser(recover_login=False, deadline=0.2)
        with patch('browser.CDP_CALL_TIMEOUT', 10), session:
            session.sb = fake_sb(self.loop, never_answers(calls))
            session.process = self.fake_process()
            started = time.monotonic()
            with self.assertRaisesRegex(BrowserConnectionError, '^daily_run_timeout$'):
                session.evaluate('1')  # Only the deadline can release this: the call bound is far away.
            self.assertLess(time.monotonic() - started, 5)
            self.assertTrue(session.expired)
            self.assertTrue(self.settles(lambda: session.process.kill.called), 'the watchdog kills Chrome after the release')
            sent = len(calls)
            with self.assertRaisesRegex(BrowserConnectionError, '^daily_run_timeout$'):
                session.evaluate('1')
            with patch.object(browser.Browser, 'goto', GOTO), \
                    self.assertRaisesRegex(BrowserConnectionError, '^daily_run_timeout$'):
                session.goto(browser.SITE)
            self.assertEqual(len(calls), sent)
        self.assertEqual(len(calls), sent)  # Past the deadline not even the graceful close touches the loop.
        session.watchdog.join(2)
        self.assertFalse(session.watchdog.is_alive())
        self.assert_lock_free()


class ExitTests(EnteredCase):
    def test_exit_with_a_hanging_close_still_ends_chrome_and_frees_the_lock(self):
        # Otherwise an orphaned Chrome keeps the profile and the next run fails chrome_profile_busy_or_start_failed.
        session = browser.Browser(recover_login=False)
        session.__enter__()
        session.sb = fake_sb(self.loop, never_answers())
        expired = browser.subprocess.TimeoutExpired('chrome', 1)
        session.process = self.fake_process([expired, expired, 0])
        started = time.monotonic()
        with patch('browser.BROWSER_SHUTDOWN_TIMEOUT', 0.05):
            session.__exit__(None, None, None)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual([name for name, *_ in session.process.mock_calls],
                         ['wait', 'terminate', 'wait', 'kill', 'wait'])
        self.assert_lock_free()


class RegistrationTests(unittest.TestCase):
    def test_the_new_reasons_stay_unavailable_and_keep_their_name_in_history(self):
        # Unregistered, they would collapse into session_status_unavailable / daily_run_failed and hide the cause.
        for name in ['browser_connection_lost', 'daily_run_timeout']:
            with self.subTest(name=name):
                result = session_result(BrowserConnectionError(name))
                self.assertEqual(result['session_state'], SessionState.UNAVAILABLE)
                self.assertEqual(result['status'], RunStatus.FAILED)
                self.assertEqual(result['error'], name)
                record = daily_history_record({'run_id': 'synthetic', 'started_at': '2026-09-22T00:00:00+00:00',
                                               'finished_at': '2026-09-22T00:01:00+00:00', 'site_day': '2026-09-22',
                                               'status_only': False, 'status': RunStatus.FAILED, 'error': name,
                                               'actions': [], 'before': {}, 'after': {}, 'reward_verified': {}})
                self.assertEqual(record['error'], name)


if __name__ == '__main__':
    unittest.main()
