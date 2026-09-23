import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import browser
import cli
import library


USER = {'username': 'test_member', 'uid': 123456}
GOTO = browser.Browser.goto


def response(user=None, *, status=200, challenge=False, error=None, text=None):
    item = {'error': {'json': error}} if error else {'result': {'data': {'json': user}}}
    return {'status': status, 'challenge': challenge,
            'text': json.dumps([item]) if text is None else text}


class SessionStatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.replace('browser.STATE', root / 'state')
        self.replace('browser.PROFILE', root / 'profile')
        self.replace('browser.USERNAME', USER['username'])
        self.replace('browser.ACCOUNT_UID', USER['uid'])
        self.launch = self.replace('browser.subprocess.Popen')
        self.replace('browser.requests.Session')
        chrome = self.replace('browser.sb_cdp.Chrome')
        chrome.return_value.loop.run_until_complete.side_effect = lambda awaitable: awaitable.close()
        self.navigate = self.replace('browser.Browser.goto')
        self.evaluate = self.replace('browser.Browser.evaluate', return_value=response(USER))
        self.credentials = self.replace('secure.load_credentials', side_effect=AssertionError('No credential read'))

    def replace(self, target, *args, **kwargs):
        replacement = patch(target, *args, **kwargs)
        self.addCleanup(replacement.stop)
        return replacement.start()

    def test_account_responses_distinguish_identity_auth_and_challenge(self):
        cases = [
            (response(USER), 'logged_in', True, True, 'complete', None),
            (response({**USER, 'uid': 654321}), 'wrong_account', False, False, 'needs_attention', 'unexpected_account'),
            (response(None), 'logged_out', None, False, 'needs_attention', 'login_required'),
            (response(status=401, text='Unauthorized'), 'logged_out', None, False, 'needs_attention', 'login_required'),
            (response(error={'message': 'Synthetic private detail', 'data': {'code': 'UNAUTHORIZED'}}),
             'logged_out', None, False, 'needs_attention', 'login_required'),
            (response(status=401, challenge=True, text='Challenge'),
             'challenge', None, False, 'needs_attention', 'api_challenge_not_resolved'),
            (response(status=503, text='Unavailable'), 'unavailable', None, False, 'failed', 'api_returned_non_json'),
            (response(USER, status=503), 'unavailable', None, False, 'failed', 'account_http_error'),
            (response(error={'message': 'Synthetic private detail'}),
             'unavailable', None, False, 'failed', 'account_request_rejected'),
            (response(error={'message': 'unexpected_account'}, status=500),
             'unavailable', None, False, 'failed', 'account_request_rejected'),
            (response(error={'message': 'account_not_configured'}, status=500),
             'unavailable', None, False, 'failed', 'account_request_rejected'),
        ]
        for reply, state, matches, usable, status, error in cases:
            with self.subTest(state=state, error=error):
                self.evaluate.return_value = reply
                result = browser.session_status()
                self.assertEqual(result, {'configured': True, 'session_state': state,
                    'identity_matches': matches, 'session_usable': usable, 'status': status, 'error': error})
        self.credentials.assert_not_called()
        self.assertTrue(all(call.args == (browser.SITE,) for call in self.navigate.call_args_list))

    def test_malformed_identity_is_unavailable_instead_of_auth_failure(self):
        for user in [[], '', {}, {'uid': USER['uid']}, {'username': USER['username']},
                     {**USER, 'uid': True}, {**USER, 'uid': '123456'}, {**USER, 'username': ''}]:
            with self.subTest(user=user):
                self.evaluate.return_value = response(user)
                result = browser.session_status()
                self.assertEqual(result['session_state'], 'unavailable')
                self.assertIsNone(result['identity_matches'])
                self.assertEqual(result['error'], 'account_response_invalid')
        self.credentials.assert_not_called()

    def test_missing_configuration_stops_before_launch(self):
        with patch('browser.USERNAME', ''), patch('browser.ACCOUNT_UID', 0):
            result = browser.session_status()
        self.assertEqual(result, {'configured': False, 'session_state': 'not_configured',
            'identity_matches': None, 'session_usable': False,
            'status': 'needs_attention', 'error': 'account_not_configured'})
        self.launch.assert_not_called()
        self.credentials.assert_not_called()

    def test_page_timeout_requires_current_challenge_markers(self):
        for marker, state, error in [(True, 'challenge', 'page_challenge_not_resolved'),
                                    (False, 'unavailable', 'automatic_page_or_verification_timeout')]:
            with self.subTest(marker=marker), patch.object(browser.Browser, 'goto', GOTO), \
                    patch.object(browser.Browser, 'wait_for', side_effect=RuntimeError('automatic_page_or_verification_timeout')):
                self.evaluate.return_value = marker
                result = browser.session_status()
                self.assertEqual(result['session_state'], state)
                self.assertEqual(result['error'], error)
                self.assertIsNone(result['identity_matches'])
        self.credentials.assert_not_called()

    def test_browser_lock_blocks_diagnostic_and_is_released(self):
        with browser.Browser(recover_login=False):
            result = browser.session_status()
            self.assertEqual(result['session_state'], 'unavailable')
            self.assertEqual(result['error'], 'another_task_is_using_the_browser')
            self.assertEqual(self.launch.call_count, 1)
        self.assertEqual(browser.session_status()['session_state'], 'logged_in')

    def test_default_browser_still_recovers_an_expired_session(self):
        self.credentials.side_effect = None
        self.credentials.return_value = {'username': USER['username'], 'password': 'synthetic-only'}
        self.prepare_login()
        with patch.object(browser.Browser, 'wait_for', return_value=True):
            with browser.Browser():
                pass
        self.credentials.assert_called_once()
        self.assertEqual(self.navigate.call_args_list[0].args, (browser.SITE + '/next/daily-checkin',))
        self.assertTrue(self.navigate.call_args_list[1].args[0].startswith(browser.AUTH_URL + '/login?'))

    def prepare_login(self, *, widget=False, rejected=False, identity=None, page_challenge=False, error=None):
        replies = iter([response(None), response(USER if identity is None else identity, error=error)])
        self.credentials.side_effect = None
        self.credentials.return_value = {'username': USER['username'], 'password': 'synthetic-only'}

        def evaluate(expression):
            if 'fetch(' in expression:
                return next(replies)
            if 'checkValidity()' in expression:
                return True
            if '.cf-turnstile' in expression:
                return widget
            if expression == 'location.hostname===' + json.dumps(browser.AUTH_HOST):
                return rejected
            if '_cf_chl_opt' in expression:
                return page_challenge
            if '.click()' in expression:
                return None
            raise AssertionError('Unexpected login browser expression')

        self.evaluate.side_effect = evaluate

    def test_remote_error_text_cannot_trigger_password_recovery(self):
        self.evaluate.return_value = response(error={'message': 'login required'})
        with browser.Browser(recover_login=False) as session:
            with self.assertRaises(browser.AccountRequestError):
                session.ensure_account()
        self.credentials.assert_not_called()

    def test_existing_detail_caller_redacts_account_error_after_login(self):
        self.prepare_login(error={'message': 'Synthetic private detail',
                                  'data': {'code': 'INTERNAL_SERVER_ERROR'}})
        with patch.object(browser.Browser, 'wait_for', return_value=True):
            result = library.get_thread_detail(123)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error'], 'account_request_rejected')
        self.assertNotIn('Synthetic private detail', json.dumps(result))

    def test_saved_credential_decode_errors_have_a_safe_specific_reason(self):
        for error in [json.JSONDecodeError('Synthetic private detail', 'private', 0),
                      UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'Synthetic private detail')]:
            with self.subTest(error=type(error).__name__):
                self.prepare_login()
                self.credentials.side_effect = error
                result = browser.session_login()
                self.assertEqual(result['error'], 'saved_credentials_invalid')
                self.assertEqual(result['session_state'], 'logged_out')
                self.assertFalse(result['login_attempted'])
                self.assertNotIn('private', json.dumps(result))

    def test_password_form_without_widget_does_not_wait_for_absent_token(self):
        self.prepare_login()

        def wait(expression, **kwargs):
            if 'cf-turnstile-response' in expression:
                raise RuntimeError('Absent verification widget must not block login')
            return True

        with patch.object(browser.Browser, 'wait_for', side_effect=wait):
            with browser.Browser(recover_login=False) as session:
                self.assertEqual(session.ensure_account()['uid'], USER['uid'])

    def test_explicit_login_reuses_valid_session_without_credentials(self):
        result = browser.session_login()
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['identity_matches'])
        self.assertFalse(result['login_attempted'])
        self.credentials.assert_not_called()
        self.assertEqual(self.navigate.call_args.args, (browser.SITE,))

    def test_explicit_login_verifies_recovered_identity_for_both_form_variants(self):
        for widget in (False, True):
            with self.subTest(widget=widget):
                self.prepare_login(widget=widget)
                with patch.object(browser.Browser, 'wait_for', return_value=True):
                    result = browser.session_login()
                self.assertEqual(result['session_state'], 'logged_in')
                self.assertTrue(result['session_usable'])
                self.assertTrue(result['login_attempted'])
                self.assertNotIn('synthetic-only', json.dumps(result))
                self.assertNotIn('/next/daily-checkin', self.navigate.call_args.args[0])

    def test_login_rejections_and_saved_credentials_fail_without_success(self):
        for failure, state, attempted in [('login_required_credentials_not_configured', 'logged_out', False),
                                          ('saved_credentials_invalid', 'logged_out', False),
                                          ('login_rejected', 'logged_out', True),
                                          ('unexpected_account', 'wrong_account', True)]:
            with self.subTest(failure=failure):
                self.prepare_login(rejected=failure == 'login_rejected',
                                   identity={**USER, 'uid': 654321} if failure == 'unexpected_account' else None)
                if failure == 'login_required_credentials_not_configured':
                    self.credentials.side_effect = RuntimeError(failure)
                elif failure == 'saved_credentials_invalid':
                    self.credentials.return_value = {'username': 'another_test_member', 'password': 'synthetic-only'}
                with patch.object(browser.Browser, 'wait_for', return_value=True):
                    result = browser.session_login()
                self.assertEqual(result['error'], failure)
                self.assertEqual(result['session_state'], state)
                self.assertFalse(result['session_usable'])
                self.assertEqual(result['login_attempted'], attempted)
                self.assertNotIn('synthetic-only', json.dumps(result))

    def test_login_challenge_and_unconfirmed_submission_are_distinct(self):
        for widget, challenged, error, attempted in [
                (True, False, 'login_challenge_not_resolved', False),
                (False, True, 'page_challenge_not_resolved', True),
                (False, False, 'login_submission_unconfirmed', True)]:
            with self.subTest(error=error):
                self.prepare_login(widget=widget, page_challenge=challenged)

                def wait(expression, **kwargs):
                    if 'cf-turnstile-response' in expression or '__toolkitLoginDocument' in expression:
                        raise RuntimeError('automatic_page_or_verification_timeout')
                    return True

                with patch.object(browser.Browser, 'wait_for', side_effect=wait):
                    result = browser.session_login()
                self.assertEqual(result['error'], error)
                self.assertFalse(result['session_usable'])
                self.assertEqual(result['login_attempted'], attempted)

    def test_unsupported_login_method_stops_before_browser_and_preserves_configuration(self):
        for configured in (False, True):
            with self.subTest(configured=configured), patch('browser.USERNAME', USER['username'] if configured else ''):
                result = browser.session_login(method='wechat_qr')
                self.assertEqual(result['error'], 'unsupported_login_method')
                self.assertEqual(result['configured'], configured)
                self.assertEqual(result['status'], 'failed')
                self.assertFalse(result['login_attempted'])
        with patch('sys.argv', ['cli.py', 'session-login', '--method', 'wechat_qr']), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(), 2)
        self.assertEqual(json.loads(output.getvalue())['error'], 'unsupported_login_method')
        self.launch.assert_not_called()

    def test_cli_uses_diagnostic_and_reports_not_ready(self):
        self.evaluate.return_value = response(None)
        with patch('sys.argv', ['cli.py', 'session-status']), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(), 2)
        self.assertEqual(json.loads(output.getvalue())['session_state'], 'logged_out')
        self.credentials.assert_not_called()
