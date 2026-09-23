import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mycdp
import browser
import cli
import mcp_server
from contracts import logout_result
from tests.test_session_status import USER, response

SITE_COOKIES = [('4Oaf_61d6_auth', '.1point3acres.com', '/'), ('cf_clearance', 'www.1point3acres.com', '/'),
                ('sid', 'auth.1point3acres.com', '/'), ('cuid', '.1p3a.com', '/')]
OTHER_COOKIES = [('theme', 'example.org', '/'), ('lang', '.notpoint3acres.com', '/'), ('x', '1point3acres.com.evil.example', '/')]


class CookieJar:
    """The CDP cookie store as the reset sees it: one list, deletions recorded and applied."""

    def __init__(self, cookies):
        self.cookies = [SimpleNamespace(name=name, domain=domain, path=path) for name, domain, path in cookies]
        self.deleted = []

    def send(self, command):
        # mycdp commands are generators; the first yielded dict names the method and its params.
        request = next(command)
        if request['method'] == 'Storage.getCookies':
            return list(self.cookies)
        if request['method'] == 'Network.deleteCookies':
            params = request['params']
            self.deleted.append((params['name'], params['domain'], params['path']))
            self.cookies = [c for c in self.cookies if (c.name, c.domain, c.path) != (params['name'], params['domain'], params['path'])]
            return None
        raise AssertionError(request['method'])


class SessionLogoutTests(unittest.TestCase):
    """A reset clears only the site's login in the dedicated profile and proves it, never logging back in (#17)."""

    def setUp(self):
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
        self.jar = CookieJar(SITE_COOKIES + OTHER_COOKIES)
        self.replace('browser.Browser._cdp', side_effect=self.jar.send)
        self.credentials = self.replace('secure.load_credentials', side_effect=AssertionError('a reset must never read credentials'))
        # Once the site cookies are gone the identity read is refused; before that it still answers.
        self.evaluate = self.replace('browser.Browser.evaluate',
                                     side_effect=lambda expression: response(None) if not any(
                                         c.domain.endswith('1point3acres.com') for c in self.jar.cookies) else response(USER))

    def replace(self, target, *args, **kwargs):
        replacement = patch(target, *args, **kwargs)
        self.addCleanup(replacement.stop)
        return replacement.start()

    def test_only_site_domains_are_cleared_and_the_logout_is_verified(self):
        result = browser.session_logout()
        self.assertEqual((result['status'], result['session_state'], result['cookies_removed'], result['verified']),
                         ('complete', 'logged_out', 4, True))
        self.assertEqual(sorted(name for name, _, _ in self.jar.deleted), sorted(name for name, _, _ in SITE_COOKIES))
        self.assertEqual([c.name for c in self.jar.cookies], ['theme', 'lang', 'x'])
        self.assertEqual(result['scope']['domains'], ['1point3acres.com', '1p3a.com'])
        self.assertFalse(result['login_restored'])
        self.assertFalse(result['session_usable'])
        self.assertEqual(result['error'], None)

    def test_a_repeated_reset_is_harmless_and_still_verified(self):
        browser.session_logout()
        again = browser.session_logout()
        self.assertEqual((again['status'], again['cookies_removed'], again['verified']), ('complete', 0, True))
        self.assertEqual(len(self.jar.deleted), 4)

    def test_a_reset_the_site_does_not_confirm_is_attention_not_success(self):
        self.evaluate.side_effect = lambda expression: response(USER)  # the site still knows the account
        result = browser.session_logout()
        self.assertEqual((result['status'], result['session_state'], result['error'], result['cookies_removed']),
                         ('needs_attention', 'unavailable', 'logout_unverified', 4))

    def test_a_busy_browser_fails_safely_without_touching_cookies(self):
        holder = browser.Browser(recover_login=False)
        holder.__enter__()
        try:
            result = browser.session_logout()
        finally:
            holder.__exit__(None, None, None)
        self.assertEqual((result['status'], result['error'], result['cookies_removed'], result['verified']),
                         ('failed', 'another_task_is_using_the_browser', None, False))
        self.assertEqual(self.jar.deleted, [])
        released = browser.session_logout()
        self.assertEqual(released['status'], 'complete')

    def test_the_verification_never_restores_the_login(self):
        with patch('browser.Browser.ensure_account', side_effect=AssertionError('a reset must not log back in')):
            result = browser.session_logout()
        self.assertEqual(result['status'], 'complete')
        self.credentials.assert_not_called()


class LogoutContractTests(unittest.TestCase):
    def test_reasons_map_to_states(self):
        scope = {'profile': 'p', 'domains': ['1point3acres.com']}
        done = logout_result(None, cookies_removed=2, verified=True, scope=scope)
        self.assertEqual((done['status'], done['session_state'], done['error']), ('complete', 'logged_out', None))
        unverified = logout_result(None, cookies_removed=2, verified=False, scope=scope)
        self.assertEqual((unverified['status'], unverified['error']), ('needs_attention', 'logout_unverified'))
        busy = logout_result(RuntimeError('another_task_is_using_the_browser'), cookies_removed=None, verified=False, scope=scope)
        self.assertEqual((busy['status'], busy['session_state']), ('failed', 'unavailable'))
        odd = logout_result(KeyError('x'), cookies_removed=None, verified=False, scope=scope)
        self.assertEqual((odd['status'], odd['error']), ('needs_attention', 'KeyError'))
        for result in (done, unverified, busy, odd):
            self.assertFalse(result['login_restored'])
            self.assertFalse(result['session_usable'])


class LogoutEntryPointTests(unittest.TestCase):
    def test_cli_and_mcp_expose_the_reset_and_refuse_arguments(self):
        payload = logout_result(None, cookies_removed=3, verified=True, scope={'profile': 'p', 'domains': ['1point3acres.com', '1p3a.com']})
        with patch('cli.session_logout', return_value=payload) as called, patch('sys.argv', ['cli', 'session-logout']), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(), 0)
        called.assert_called_once_with()
        self.assertEqual(json.loads(out.getvalue())['session_state'], 'logged_out')
        with patch('mcp_server.reset_session', return_value=payload):
            tool = asyncio.run(mcp_server.server.call_tool('session_logout', {}))
        self.assertFalse(tool.is_error)
        self.assertEqual(json.loads(tool.content[0].text)['cookies_removed'], 3)
        with patch('mcp_server.reset_session', side_effect=AssertionError('must not run')):
            try:
                extra = asyncio.run(mcp_server.server.call_tool('session_logout', {'relogin': True}))
            except Exception as refusal:
                self.assertIn('unsupported_logout_arguments', str(refusal))
            else:
                self.assertTrue(extra.is_error)
