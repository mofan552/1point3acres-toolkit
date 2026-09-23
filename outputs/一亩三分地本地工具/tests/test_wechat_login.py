import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from itertools import count
from pathlib import Path
from unittest.mock import patch

import browser
import cli
import mcp_server
from settings import WECHAT_LOGIN_TIMEOUT, WECHAT_LOGIN_MIN_WAIT, WECHAT_LOGIN_MAX_WAIT
from tests.test_session_status import USER, response

FRAME = 'iframe[src^='


class WechatLoginTests(unittest.TestCase):
    """Scan login (#51): the official QR is shown by the site's own page inside the owned Chrome; the tool only
    waits, verifies the identity and tidies up. Everything here is synthetic: no page, no scan, no cookies."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.replace('browser.STATE', self.root / 'state')
        self.replace('browser.PROFILE', self.root / 'profile')
        self.replace('browser.WECHAT_QR_FILE', self.root / 'state' / 'wechat-qr.png')
        self.replace('browser.USERNAME', USER['username'])
        self.replace('browser.ACCOUNT_UID', USER['uid'])
        self.launch = self.replace('browser.subprocess.Popen')
        self.replace('browser.requests.Session')
        chrome = self.replace('browser.sb_cdp.Chrome')
        chrome.return_value.loop.run_until_complete.side_effect = lambda awaitable: awaitable.close()
        self.navigate = self.replace('browser.Browser.goto')
        self.window = self.replace('browser.Browser.set_window_visible')
        self.capture = self.replace('browser.Browser.capture_png', return_value=b'synthetic-png')
        self.cookies = self.replace('browser.Browser.clear_site_cookies', return_value=0)
        self.replace('secure.load_credentials', side_effect=AssertionError('No credential read'))
        self.evaluate = self.replace('browser.Browser.evaluate')
        self.hosts = iter([browser.AUTH_HOST, browser.AUTH_HOST, browser.SITE_HOST])
        self.identities = iter([response(None), response(USER)])
        self.frame_present = True
        self.evaluate.side_effect = self.page

    def replace(self, target, *args, **kwargs):
        replacement = patch(target, *args, **kwargs)
        self.addCleanup(replacement.stop)
        return replacement.start()

    def page(self, expression):
        if 'fetch(' in expression:
            return next(self.identities)
        if FRAME in expression:
            return self.frame_present
        if expression == 'location.hostname':
            return next(self.hosts)
        raise AssertionError('Unexpected browser expression: ' + expression)

    def qr_file(self):
        return self.root / 'state' / 'wechat-qr.png'

    def test_arguments_are_checked_before_any_browser_starts(self):
        cases = [dict(method='wechat_qr'), dict(method='wechat', wait_seconds=WECHAT_LOGIN_MIN_WAIT - 1),
                 dict(method='wechat', wait_seconds=WECHAT_LOGIN_MAX_WAIT + 1), dict(method='wechat', wait_seconds='60'),
                 dict(method='wechat', wait_seconds=True)]
        for arguments in cases:
            with self.subTest(**arguments):
                result = browser.session_login(**arguments)
                self.assertEqual(result['error'], 'unsupported_login_method' if arguments['method'] != 'wechat'
                                 else 'invalid_wait_seconds')
                self.assertEqual(result['status'], 'failed')
                self.assertTrue(result['configured'])
                self.assertFalse(result['login_attempted'])
                self.assertIsNone(result['wechat'])
        self.launch.assert_not_called()
        self.window.assert_not_called()
        self.assertEqual(WECHAT_LOGIN_MIN_WAIT <= WECHAT_LOGIN_TIMEOUT <= WECHAT_LOGIN_MAX_WAIT, True)

    def test_valid_session_is_reused_without_showing_a_code(self):
        self.identities = iter([response(USER)])
        result = browser.session_login(method='wechat')
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['session_usable'])
        self.assertFalse(result['login_attempted'])
        self.assertIsNone(result['wechat'])
        self.window.assert_not_called()
        self.capture.assert_not_called()
        self.assertEqual([call.args for call in self.navigate.call_args_list], [(browser.SITE,)])

    def test_missing_official_frame_stops_before_anything_is_shown(self):
        self.frame_present = False
        with patch('browser.time.monotonic', side_effect=count(step=100)):
            result = browser.session_login(method='wechat')
        self.assertEqual(result['error'], 'wechat_qr_not_shown')
        self.assertEqual(result['session_state'], 'unavailable')
        self.assertFalse(result['login_attempted'])
        self.assertIsNone(result['wechat']['displayed_at'])
        self.window.assert_not_called()
        self.capture.assert_not_called()
        self.assertFalse(self.qr_file().exists())
        self.assertEqual(self.navigate.call_args.args, (browser.AUTH_URL + browser.WECHAT_QR_PATH,))

    def test_code_is_shown_then_the_real_identity_decides(self):
        written = []
        original = Path.write_bytes

        def record(path, data):
            written.append((Path(path), data, Path(path).parent.is_dir()))
            return original(path, data)

        with patch.object(Path, 'write_bytes', record):
            result = browser.session_login(method='wechat', wait_seconds=30)
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['session_usable'])
        self.assertTrue(result['identity_matches'])
        self.assertTrue(result['login_attempted'])
        self.assertEqual([call.args for call in self.window.call_args_list], [(True,), (False,)])
        self.assertEqual(written, [(self.qr_file(), b'synthetic-png', True)])
        self.assertFalse(self.qr_file().exists())
        report = result['wechat']
        self.assertTrue(report['qr_file_removed'])
        self.assertEqual(report['qr_path'], str(self.qr_file()))
        self.assertIsNotNone(report['displayed_at'])
        self.assertIsNone(report['expires_at'])
        self.assertEqual(report['wait_limit'], 30)
        self.assertIsInstance(report['waited_seconds'], float)
        self.assertFalse(report['foreign_session_cleared'])
        self.cookies.assert_not_called()
        dumped = json.dumps(result)
        for secret in ['open.weixin.qq.com', 'state=', 'uuid', 'synthetic-png', 'cookie']:
            self.assertNotIn(secret, dumped)

    def test_named_file_and_second_wait_are_honoured(self):
        target = self.root / 'somewhere' / 'code.png'
        self.hosts = iter([browser.AUTH_HOST] * 5 + [browser.SITE_HOST])
        with patch('browser.Browser.capture_png', return_value=b'x') as capture:
            result = browser.session_login(method='wechat', wait_seconds=600, qr_path=str(target))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['wechat']['qr_path'], str(target))
        self.assertTrue(result['wechat']['qr_file_removed'])
        self.assertFalse(target.exists())
        self.assertEqual(capture.call_count, 1)

    def test_timeout_and_local_cancel_end_logged_out_and_tidy(self):
        for name, hosts, clock in [
                ('wechat_login_timeout', [browser.AUTH_HOST] * 50, count(step=5)),
                ('wechat_login_cancelled', [KeyboardInterrupt()], count(step=1))]:
            with self.subTest(name=name):
                self.setUp()
                self.hosts = iter(hosts)

                def page(expression):
                    if expression == 'location.hostname':
                        value = next(self.hosts)
                        if isinstance(value, BaseException):
                            raise value
                        return value
                    return self.page(expression)

                self.evaluate.side_effect = page
                with patch('browser.time.monotonic', side_effect=clock):
                    result = browser.session_login(method='wechat', wait_seconds=30)
                self.assertEqual(result['error'], name)
                self.assertEqual(result['session_state'], 'logged_out')
                self.assertEqual(result['status'], 'needs_attention')
                self.assertFalse(result['session_usable'])
                self.assertTrue(result['login_attempted'])
                self.assertEqual([call.args for call in self.window.call_args_list], [(True,), (False,)])
                self.assertFalse(self.qr_file().exists())
                self.assertTrue(result['wechat']['qr_file_removed'])
                self.cookies.assert_not_called()

    def test_navigation_gaps_do_not_end_the_wait(self):
        gaps = iter([RuntimeError('page_expression_failed'), browser.AUTH_HOST, RuntimeError('page_expression_failed'),
                     browser.SITE_HOST])

        def page(expression):
            if expression == 'location.hostname':
                value = next(gaps)
                if isinstance(value, Exception):
                    raise value
                return value
            return self.page(expression)

        self.evaluate.side_effect = page
        result = browser.session_login(method='wechat')
        self.assertEqual(result['status'], 'complete')

    def test_lost_link_during_the_wait_is_reported_as_such(self):
        def page(expression):
            if expression == 'location.hostname':
                raise browser.BrowserConnectionError('browser_connection_lost')
            return self.page(expression)

        self.evaluate.side_effect = page
        result = browser.session_login(method='wechat')
        self.assertEqual(result['error'], 'browser_connection_lost')
        self.assertEqual(result['session_state'], 'unavailable')
        self.assertFalse(self.qr_file().exists())

    def test_foreign_identity_is_logged_out_again(self):
        self.identities = iter([response(None), response({**USER, 'uid': 654321})])
        result = browser.session_login(method='wechat')
        self.assertEqual(result['error'], 'unexpected_account')
        self.assertEqual(result['session_state'], 'wrong_account')
        self.assertFalse(result['session_usable'])
        self.assertTrue(result['login_attempted'])
        self.cookies.assert_called_once()
        self.assertTrue(result['wechat']['foreign_session_cleared'])
        self.assertNotIn('654321', json.dumps(result))

    def test_display_failure_is_unavailable_and_still_tidies(self):
        for target, error in [('browser.Browser.set_window_visible', OSError('no window')),
                              ('browser.Browser.capture_png', RuntimeError('page_expression_failed'))]:
            with self.subTest(target=target):
                self.setUp()
                with patch(target, side_effect=[error, None]) as failing:
                    result = browser.session_login(method='wechat')
                self.assertEqual(result['error'], 'wechat_display_failed')
                self.assertEqual(result['session_state'], 'unavailable')
                self.assertFalse(result['login_attempted'])
                self.assertIsNone(result['wechat']['displayed_at'])
                self.assertFalse(self.qr_file().exists())
                if target.endswith('capture_png'):
                    self.assertEqual([call.args for call in self.window.call_args_list], [(True,), (False,)])
                else:
                    self.assertEqual(failing.call_count, 2)

    def test_password_path_and_diagnostic_are_unchanged(self):
        self.identities = iter([response(USER)])
        result = browser.session_login()
        self.assertEqual(result['status'], 'complete')
        self.assertIsNone(result['wechat'])
        self.window.assert_not_called()
        status = browser.session_status()
        self.assertNotIn('wechat', status)

    def test_cli_and_mcp_pass_the_arguments_through(self):
        with patch('cli.session_login', return_value={'status': 'failed', 'error': 'invalid_wait_seconds'}) as login, \
                patch('sys.argv', ['cli.py', 'session-login', '--method', 'wechat', '--wait', '5', '--qr-path', 'code.png']), \
                contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as hint:
            self.assertEqual(cli.main(), 2)
        self.assertEqual(login.call_args.kwargs, {'method': 'wechat', 'wait_seconds': 5, 'qr_path': 'code.png'})
        self.assertIn('微信', hint.getvalue())
        self.assertEqual(json.loads(output.getvalue())['error'], 'invalid_wait_seconds')
        with patch('cli.session_login', return_value={'status': 'complete'}) as login, \
                patch('sys.argv', ['cli.py', 'session-login']), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as hint:
            self.assertEqual(cli.main(), 0)
        self.assertEqual(login.call_args.kwargs, {'method': 'password', 'wait_seconds': WECHAT_LOGIN_TIMEOUT, 'qr_path': None})
        self.assertEqual(hint.getvalue(), '')
        payload = {'status': 'needs_attention', 'error': 'wechat_login_timeout', 'wechat': {'expires_at': None}}
        with patch('mcp_server.restore_session', return_value=payload) as tool_called:
            tool = asyncio.run(mcp_server.server.call_tool('session_login', {'method': 'wechat', 'wait_seconds': 45}))
        self.assertEqual(tool_called.call_args.kwargs, {'method': 'wechat', 'wait_seconds': 45, 'qr_path': None})
        self.assertTrue(tool.is_error)
        self.assertEqual(json.loads(tool.content[0].text), payload)
        with patch('mcp_server.restore_session', side_effect=AssertionError('must not run')):
            with self.assertRaises(Exception):
                asyncio.run(mcp_server.server.call_tool('session_login', {'method': 'wechat', 'wait_seconds': '45'}))


if __name__ == '__main__':
    unittest.main()
