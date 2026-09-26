import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import browser
import daily
from contracts import session_result


def account_response():
    return {'status': 200, 'challenge': False,
            'text': json.dumps([{'result': {'data': {'json': {'uid': 123456}}}}])}


class AccessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.browser = browser.Browser(recover_login=False)
        self.browser.sb = SimpleNamespace(sleep=Mock())
        self.browser.evaluate = Mock()

    def test_transient_transport_failure_retries_only_the_read(self):
        for reason in ['network_timeout', 'network_unavailable']:
            with self.subTest(reason=reason):
                self.setUp()
                self.browser.evaluate.side_effect = [{'transport_error': reason}, account_response()]
                self.assertEqual(self.browser.rpc('user.me'), {'uid': 123456})
                self.assertEqual(self.browser.evaluate.call_count, 2)
                self.assertEqual(self.browser.read_retries, 1)

    def test_persistent_transport_failure_stops_with_specific_diagnostic(self):
        for reason in ['network_timeout', 'network_unavailable', 'browser_read_failed']:
            with self.subTest(reason=reason):
                self.setUp()
                self.browser.evaluate.return_value = {'transport_error': reason}
                with self.assertRaisesRegex(RuntimeError, '^' + reason + '$'):
                    self.browser.rpc('user.me')
                self.assertEqual(self.browser.evaluate.call_count, 1 if reason == 'browser_read_failed' else 2)
                self.assertEqual(session_result(RuntimeError(reason))['error'], reason)

    def test_challenge_auth_and_business_rejection_are_not_network_retries(self):
        cases = [(dict(account_response(), challenge=True), 'api_challenge_not_resolved'),
                 (dict(account_response(), status=401), 'login_required'),
                 ({'status': 500, 'challenge': False, 'text': json.dumps([{'error': {
                     'json': {'message': 'Synthetic private detail'}}}])}, 'account_request_rejected')]
        for response, reason in cases:
            with self.subTest(reason=reason):
                self.setUp()
                self.browser.evaluate.return_value = response
                with self.assertRaisesRegex(RuntimeError, '^' + reason + '$'):
                    self.browser.rpc('user.me')
                self.browser.evaluate.assert_called_once()
        with self.assertRaisesRegex(ValueError, 'unsupported_read_method'):
            self.browser.rpc('dailyQuestion.answer')

    def test_html_read_uses_the_same_transport_recovery(self):
        url = browser.SITE + '/bbs/thread-123-1-1.html'
        self.browser.evaluate.side_effect = [{'transport_error': 'network_timeout'},
            {'status': 200, 'challenge': False, 'charset': 'utf-8', 'url': url, 'html': 'synthetic content'}]
        self.assertEqual(self.browser.read_html(url), 'synthetic content')
        self.assertEqual(self.browser.read_retries, 1)


class SubmissionBrowser:
    check_active = browser.Browser.check_active
    expired = False
    def __init__(self, completed, rewarded):
        self.completed = completed
        self.rewarded = rewarded
        self.submissions = 0
        self.ids = {}
        self.responses = []
        self.solver_calls = self.read_retries = 0
        self.sb = SimpleNamespace(sleep=Mock(), solve_captcha=Mock())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def account(self):
        return {'uid': 123456, 'username': 'test_member', 'rice': 10,
                'app_status': {'checkin': True, 'question': bool(self.submissions and self.completed)}}

    def credit_logs(self):
        titles = ['签到奖励', '每日答题'] if self.rewarded else ['签到奖励']
        return [{'uid': 123456, 'dateline': int(time.time()), 'extcredits1': 1,
                 'details': {'title': title}} for title in titles]

    def goto(self, url):
        pass

    def rpc(self, method):
        return {'question': {'qc': 'Synthetic question', 'a1': 'Answer', 'a2': 'Wrong'}}

    def wait_for(self, expression, **kwargs):
        return True

    def click_text(self, text):
        if text == '提交答案':
            self.submissions += 1


class SubmissionRecoveryTests(unittest.TestCase):
    def run_daily(self, session):
        with tempfile.TemporaryDirectory() as directory, patch('daily.STATE', Path(directory)), \
                patch('daily.Browser', return_value=session), patch('daily.time.monotonic', side_effect=[0, 100]):
            return daily.run_daily(supplied_answer='Answer', expected_question='Synthetic question')

    def test_missing_response_checks_state_and_reward_without_replaying_submission(self):
        session = SubmissionBrowser(completed=True, rewarded=True)
        result = self.run_daily(session)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['actions'][-1]['status'], 'reward_verified')
        self.assertEqual(result['recovery'], {'read_retries': 0, 'submission_checks': 1})
        self.assertEqual(self.run_daily(session)['status'], 'complete')
        self.assertEqual(session.submissions, 1)

    def test_missing_response_cannot_succeed_without_completion_or_rice(self):
        for completed, rewarded, status in [(False, True, 'failed'), (True, False, 'needs_attention')]:
            with self.subTest(completed=completed, rewarded=rewarded):
                session = SubmissionBrowser(completed, rewarded)
                result = self.run_daily(session)
                self.assertEqual(result['status'], status)
                self.assertEqual(result['recovery']['submission_checks'], 1)
                self.assertEqual(session.submissions, 1)
                if not completed:
                    self.assertEqual(result['error'], 'quiz_submission_unconfirmed')


class ReadWhitelistTests(unittest.TestCase):
    def test_the_favorites_read_is_allowed_and_writes_still_are_not(self):
        from browser import Browser
        with patch.object(Browser, '_read_response', return_value={'status': 200, 'challenge': False,
                                                                     'text': '[{"result":{"data":{"json":{"forums":[],"tags":[]}}}}]'}):
            b = Browser.__new__(Browser)
            self.assertEqual(b.rpc('favorite.getFavorites'), {'forums': [], 'tags': []})
        for method in ['favorite.add', 'favorite.delete', 'reward.checkin', 'thread.post']:
            with self.subTest(method=method), self.assertRaisesRegex(ValueError, 'unsupported_read_method'):
                Browser.__new__(Browser).rpc(method)
