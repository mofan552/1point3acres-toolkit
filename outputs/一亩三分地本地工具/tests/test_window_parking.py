import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import browser
from settings import CHROME_BUNDLE_ID, WINDOW_ON_SCREEN
from tests.test_session_status import USER

Bounds = browser.mycdp.browser.Bounds
WindowState = browser.mycdp.browser.WindowState


class WindowVisibilityTests(unittest.TestCase):
    """The owned Chrome stays out of sight (#5). Windows keeps the off-screen coordinates; macOS cannot, so it
    minimizes and hands focus back. Everything is synthetic: no Chrome, no CDP socket, no OS calls."""

    def setUp(self):
        self.browser = browser.Browser(recover_login=False)
        self.browser.front_app = 'com.example.editor'
        self.set_bounds = self.replace('browser.mycdp.browser.set_window_bounds')
        self.replace('browser.mycdp.browser.get_window_for_target')
        self.bring_to_front = self.replace('browser.mycdp.page.bring_to_front')
        self.replace('browser.Browser._cdp', side_effect=lambda command: ('window-1', None))
        self.focus = self.replace('browser._give_focus_back', return_value=True)

    def replace(self, target, *args, **kwargs):
        replacement = patch(target, *args, **kwargs)
        self.addCleanup(replacement.stop)
        return replacement.start()

    def bounds_sent(self):
        return [call.args[1] for call in self.set_bounds.call_args_list]

    def test_macos_hides_by_minimizing_and_hands_focus_back(self):
        with patch('browser.MACOS', True), patch('browser.WINDOWS', False):
            self.browser.set_window_visible(False)
        self.assertEqual(self.bounds_sent(), [Bounds(window_state=WindowState.MINIMIZED)])
        self.focus.assert_called_once_with('com.example.editor')
        self.bring_to_front.assert_not_called()

    def test_showing_restores_the_state_before_moving_on_screen(self):
        for macos in (True, False):
            with self.subTest(macos=macos), patch('browser.MACOS', macos), patch('browser.WINDOWS', False):
                self.set_bounds.reset_mock()
                self.focus.reset_mock()
                self.browser.set_window_visible(True)
                self.assertEqual(self.bounds_sent(), [
                    Bounds(window_state=WindowState.NORMAL),
                    Bounds(left=WINDOW_ON_SCREEN[0], top=WINDOW_ON_SCREEN[1], width=1280, height=900)])
                self.bring_to_front.assert_called()
                self.focus.assert_not_called()

    def test_windows_keeps_the_off_screen_coordinates(self):
        with patch('browser.WINDOWS', True), patch('browser.MACOS', False):
            self.browser.set_window_visible(False)
        self.assertEqual(self.bounds_sent(), [Bounds(left=-20000, top=-20000)])
        self.focus.assert_not_called()

    def test_parking_is_best_effort(self):
        with patch('browser.Browser.set_window_visible', side_effect=RuntimeError('no window')):
            self.browser._park_window()  # A window left visible must not fail the run.


class FocusHandoffTests(unittest.TestCase):
    def queries(self, outputs):
        calls = []

        def fake_query(*command):
            calls.append(list(command))
            return outputs.get(command[1], '')
        return calls, fake_query

    def test_front_app_is_read_from_lsappinfo(self):
        calls, fake_query = self.queries({'front': 'ASN:0x0-0x1234:', 'info': '"CFBundleIdentifier"="com.example.editor"'})
        with patch('browser._query', fake_query):
            self.assertEqual(browser._front_app(), 'com.example.editor')
        self.assertEqual(calls[1], ['lsappinfo', 'info', '-only', 'bundleid', 'ASN:0x0-0x1234:'])
        for outputs in [{}, {'front': 'ASN:0x0-0x1234:'}]:
            with self.subTest(outputs=outputs), patch('browser._query', self.queries(outputs)[1]):
                self.assertEqual(browser._front_app(), '')

    def test_focus_goes_only_to_a_running_app_that_is_not_chrome(self):
        for bundle_id in ('', CHROME_BUNDLE_ID):
            with self.subTest(bundle_id=bundle_id), patch('browser._query') as query:
                self.assertFalse(browser._give_focus_back(bundle_id))
                query.assert_not_called()
        calls, fake_query = self.queries({'find': ''})
        with patch('browser._query', fake_query):
            self.assertFalse(browser._give_focus_back('com.example.quit'))
        self.assertEqual([command[0] for command in calls], ['lsappinfo'])  # Nothing is launched for an app that quit.
        calls, fake_query = self.queries({'find': 'ASN:0x0-0x1234-"Editor":'})
        with patch('browser._query', fake_query):
            self.assertTrue(browser._give_focus_back('com.example.editor'))
        self.assertEqual(calls[1], ['open', '-b', 'com.example.editor'])

    def test_queries_never_raise_and_do_not_use_the_chrome_launch_double(self):
        with patch('browser.subprocess.Popen') as launch:
            self.assertEqual(browser._query('lsappinfo', 'front').__class__, str)
            launch.assert_not_called()
        with patch('browser._SHELL', side_effect=OSError('no such command')):
            self.assertEqual(browser._query('lsappinfo', 'front'), '')
        process = SimpleNamespace(communicate=Mock(side_effect=TimeoutError()), kill=Mock())
        with patch('browser._SHELL', return_value=process):
            self.assertEqual(browser._query('lsappinfo', 'front'), '')
        process.kill.assert_called_once()


class LaunchTests(unittest.TestCase):
    """Only the launch arguments and the parking step differ per platform; the rest of the session is untouched."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for target, value in [('browser.STATE', root / 'state'), ('browser.PROFILE', root / 'profile'),
                              ('browser.USERNAME', USER['username']), ('browser.ACCOUNT_UID', USER['uid'])]:
            self.replace(target, value)
        self.launch = self.replace('browser.subprocess.Popen')
        self.replace('browser.requests.Session')
        chrome = self.replace('browser.sb_cdp.Chrome')
        chrome.return_value.loop.run_until_complete.side_effect = lambda awaitable: awaitable.close()
        self.replace('browser.Browser.goto')
        self.park = self.replace('browser.Browser._park_window')
        self.front = self.replace('browser._front_app', return_value='com.example.editor')

    def replace(self, target, *args, **kwargs):
        replacement = patch(target, *args, **kwargs)
        self.addCleanup(replacement.stop)
        return replacement.start()

    def arguments(self):
        return self.launch.call_args.args[0]

    def test_macos_launch_skips_off_screen_coordinates_and_parks(self):
        with patch('browser.MACOS', True), patch('browser.WINDOWS', False), patch('browser._hidden_window', return_value={}):
            with browser.Browser(recover_login=False) as session:
                self.assertEqual(session.front_app, 'com.example.editor')
        self.assertNotIn('--window-position=-20000,-20000', self.arguments())
        self.park.assert_called_once()

    def test_other_platforms_launch_as_before(self):
        with patch('browser.MACOS', False), patch('browser.WINDOWS', True), patch('browser._hidden_window', return_value={}), \
                patch('browser._lock'):
            with browser.Browser(recover_login=False) as session:
                self.assertEqual(session.front_app, '')
        self.assertIn('--window-position=-20000,-20000', self.arguments())
        self.park.assert_not_called()
        self.front.assert_not_called()


if __name__ == '__main__':
    unittest.main()
