import io
import itertools
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import cli
import daily
import rules
import settings
from contracts import BrowserConnectionError
from library import Library
from tests.test_access_recovery import SubmissionBrowser


NOW = datetime(2026, 9, 10, 17, tzinfo=timezone.utc)


def saved_run(run_id, flags, *, started=NOW):
    return {'run_id': run_id, 'started_at': started.isoformat(), 'finished_at': started.isoformat(),
            'site_day': rules.site_day(started), 'status': 'needs_attention', 'status_only': False,
            'actions': [{'action': key, 'status': 'reward_verified'} for key in flags]}


class DailyResumeTests(unittest.TestCase):
    def test_due_time_tracks_site_day_across_local_midnight_and_dst(self):
        cases = [('2026-09-10T07:30:00+00:00', '2026-09-10T08:10:00+00:00'),
                 ('2026-09-10T17:00:00+00:00', '2026-09-10T08:10:00+00:00'),
                 ('2026-01-10T08:05:00+00:00', '2026-01-10T08:10:00+00:00'),
                 ('2026-11-01T07:30:00+00:00', '2026-11-01T08:10:00+00:00')]
        for now, due in cases:
            with self.subTest(now=now):
                self.assertEqual(rules.daily_due_at(datetime.fromisoformat(now)), datetime.fromisoformat(due))
        self.assertEqual(settings.daily_schedule_rrule(), 'FREQ=DAILY;BYHOUR=0,4,8,12,16,20;BYMINUTE=10;BYSECOND=0')
        with tempfile.TemporaryDirectory() as directory, patch('daily.STATE', Path(directory)), \
                patch('daily.ACCOUNT_UID', 123456), patch('daily.datetime') as clock, patch('daily.Browser') as browser:
            clock.now.return_value = datetime.fromisoformat(cases[0][0])
            result = daily.resume_daily()
            self.assertEqual(result['decision'], 'not_due')
            self.assertEqual(result['attempts'], [])
            browser.assert_not_called()

    def test_verified_day_exits_offline_and_cli_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Library(root / daily.DATABASE_NAME)
            db.save_daily(saved_run('finished', ['checkin', 'quiz']), 123456)
            db.close()
            with patch('daily.STATE', root), patch('daily.ACCOUNT_UID', 123456), \
                    patch('daily.datetime') as clock, patch('daily.site_day', return_value='2026-09-10'), \
                    patch('daily.Browser') as browser, \
                    patch('sys.argv', ['cli', 'daily', '--resume']), patch('sys.stdout', new_callable=io.StringIO) as output:
                clock.now.return_value = NOW
                self.assertEqual(cli.main(), 0)
                result = json.loads(output.getvalue())
            self.assertEqual(result['decision'], 'already_complete')
            self.assertEqual(result['history']['actions'][1]['reward_run_id'], 'finished')
            self.assertEqual(result['attempts'], [])
            browser.assert_not_called()

    def test_old_day_and_other_account_cannot_suppress_current_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Library(root / daily.DATABASE_NAME)
            db.save_daily(saved_run('other', ['checkin', 'quiz']), 654321)
            db.save_daily(saved_run('yesterday', ['checkin', 'quiz'],
                          started=NOW.replace(day=9)), 123456)
            db.close()
            session = SubmissionBrowser(completed=True, rewarded=True)
            with patch('daily.STATE', root), patch('daily.ACCOUNT_UID', 123456), \
                    patch('daily.datetime') as clock, patch('daily.site_day', return_value='2026-09-10'), \
                    patch('daily.Browser', return_value=session), \
                    patch('daily.time.monotonic', side_effect=[0, 100]), \
                    patch('tests.test_access_recovery.time.time', return_value=NOW.timestamp()):
                clock.now.return_value = NOW
                result = daily.resume_daily(supplied_answer='Answer', expected_question='Synthetic question')
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['decision'], 'executed')
            self.assertEqual(len(result['attempts']), 1)
            self.assertEqual(session.submissions, 1)

    def test_history_completion_blocks_resubmit_when_online_flag_disagrees(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Library(root / daily.DATABASE_NAME)
            recorded = saved_run('completed-no-rice', [])
            recorded['actions'] = [{'action': 'quiz', 'status': 'reward_unconfirmed', 'completed': True}]
            db.save_daily(recorded, 123456)
            db.close()
            session = SubmissionBrowser(completed=False, rewarded=False)
            with patch('daily.STATE', root), patch('daily.ACCOUNT_UID', 123456), \
                    patch('daily.datetime') as clock, patch('daily.site_day', return_value='2026-09-10'), \
                    patch('daily.Browser', return_value=session), \
                    patch('tests.test_access_recovery.time.time', return_value=NOW.timestamp()):
                clock.now.return_value = NOW
                result = daily.resume_daily(supplied_answer='Answer', expected_question='Synthetic question')
            self.assertEqual(result['status'], 'needs_attention')
            self.assertEqual(result['error'], 'daily_history_conflict')
            self.assertEqual(len(result['attempts']), 1)
            self.assertEqual(session.submissions, 0)

    def test_network_recovery_restarts_once_and_keeps_each_real_attempt(self):
        # A lost browser link or an expired run deadline restarts once with a fresh Chrome, like a network timeout.
        for reason in ['network_timeout', 'browser_connection_lost', 'daily_run_timeout']:
            for recovers in [True, False]:
                with self.subTest(reason=reason, recovers=recovers), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    session = SubmissionBrowser(completed=True, rewarded=True)
                    session.submissions = 1  # Already completed online, absent from local history.
                    failure = RuntimeError(reason) if reason == 'network_timeout' else BrowserConnectionError(reason)
                    with patch('daily.STATE', root), patch('daily.ACCOUNT_UID', 123456), \
                            patch('daily.datetime') as clock, patch('daily.site_day', return_value='2026-09-10'), \
                            patch('daily.Browser', side_effect=[failure, session if recovers else failure]) as browser, \
                            patch('tests.test_access_recovery.time.time', return_value=NOW.timestamp()):
                        clock.now.return_value = NOW
                        result = daily.resume_daily()
                    self.assertEqual(browser.call_count, 2)
                    self.assertEqual(len(result['attempts']), 2)
                    self.assertEqual(result['status'], 'complete' if recovers else 'failed')
                    self.assertEqual(result['attempts'][0]['error'], reason)
                    self.assertEqual(session.submissions, 1)
                    db = Library(root / daily.DATABASE_NAME)
                    self.assertEqual(db.daily_history(123456, '2026-09-10', 1)['days'][0]['run_count'], 2)
                    db.close()

    def test_rollover_and_storage_failure_stop_before_another_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('daily.STATE', root), patch('daily.ACCOUNT_UID', 123456), \
                    patch('daily.datetime') as clock, patch('daily.site_day', side_effect=[
                        '2026-09-10', '2026-09-10', '2026-09-10', '2026-09-11']), \
                    patch('daily.Browser', side_effect=RuntimeError('network_timeout')) as browser:
                clock.now.return_value = NOW
                result = daily.resume_daily()
            self.assertEqual(result['error'], 'site_day_changed')
            self.assertEqual(len(result['attempts']), 1)
            self.assertEqual(browser.call_count, 1)
            with patch('daily.ACCOUNT_UID', 123456), patch('daily.datetime') as clock, \
                    patch('daily.Library', side_effect=OSError('synthetic private path')), patch('daily.Browser') as browser:
                clock.now.return_value = NOW
                failed = daily.resume_daily()
            self.assertEqual(failed['error'], 'daily_history_unavailable')
            self.assertNotIn('synthetic private path', json.dumps(failed))
            browser.assert_not_called()

    def test_missing_receipt_stays_read_only_until_later_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = SubmissionBrowser(completed=False, rewarded=False)
            original_account = session.account

            def unavailable_after_submit():
                if session.submissions:
                    raise RuntimeError('network_timeout')
                return original_account()

            with patch('daily.STATE', root), patch('daily.ACCOUNT_UID', 123456), \
                    patch('daily.datetime') as clock, patch('daily.site_day', return_value='2026-09-10'), \
                    patch('daily.Browser', return_value=session), \
                    patch('daily.time.monotonic', side_effect=itertools.cycle([0, 100])), \
                    patch('tests.test_access_recovery.time.time', return_value=NOW.timestamp()):
                clock.now.return_value = NOW
                with patch.object(session, 'account', side_effect=unavailable_after_submit):
                    first = daily.resume_daily(supplied_answer='Answer', expected_question='Synthetic question')
                self.assertEqual(first['error'], 'network_timeout')
                self.assertEqual(len(first['attempts']), 1)
                self.assertEqual(session.submissions, 1)
                # A missing receipt is ambiguous, even if the online flag still says undone.
                for _ in range(3):
                    later = daily.resume_daily(supplied_answer='Answer', expected_question='Synthetic question')
                    self.assertEqual(later['error'], 'quiz_submission_unconfirmed')
                    self.assertEqual(session.submissions, 1)
                    self.assertIsNotNone(later['history']['actions'][1]['unconfirmed_submission_run_id'])
                session.completed = True
                session.rewarded = True
                confirmed = daily.resume_daily()
                self.assertEqual(confirmed['status'], 'complete')
                self.assertEqual(session.submissions, 1)
            db = Library(root / daily.DATABASE_NAME)
            action = db.daily_history(123456, '2026-09-10', 1)['days'][0]['actions'][1]
            self.assertIsNone(action['unconfirmed_submission_run_id'])
            self.assertTrue(action['reward_verified'])
            db.close()

    def test_response_without_completion_stays_protected_and_later_reward_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = SubmissionBrowser(completed=False, rewarded=False)
            session.ids['/trpc/dailyQuestion.answer'] = 'synthetic-response'
            with patch('daily.STATE', root), patch('daily.ACCOUNT_UID', 123456), \
                    patch('daily.datetime') as clock, patch('daily.site_day', return_value='2026-09-10'), \
                    patch('daily.Browser', return_value=session), patch('daily.time.monotonic', return_value=0), \
                    patch('tests.test_access_recovery.time.time', return_value=NOW.timestamp()):
                clock.now.return_value = NOW
                first = daily.resume_daily(supplied_answer='Answer', expected_question='Synthetic question')
                self.assertEqual(first['attempts'][0]['actions'][1]['status'], 'submission_unconfirmed')
                second = daily.resume_daily(supplied_answer='Answer', expected_question='Synthetic question')
                self.assertEqual(second['error'], 'quiz_submission_unconfirmed')
                self.assertEqual(session.submissions, 1)
                session.rewarded = True
                rewarded = daily.resume_daily()
                self.assertTrue(rewarded['attempts'][0]['reward_verified']['quiz'])
                self.assertNotEqual(rewarded['status'], 'complete')
                db = Library(root / daily.DATABASE_NAME)
                action = db.daily_history(123456, '2026-09-10', 1)['days'][0]['actions'][1]
                self.assertIsNone(action['unconfirmed_submission_run_id'])
                self.assertFalse(action['completed'])
                db.close()
                still_waiting = daily.resume_daily()
                self.assertNotEqual(still_waiting['decision'], 'already_complete')
                self.assertEqual(session.submissions, 1)

    def test_blocked_action_still_lets_the_other_action_run(self):
        class BothPendingBrowser(SubmissionBrowser):
            def __init__(self):
                super().__init__(completed=True, rewarded=True)
                self.clicked = []
                self.ids['/trpc/dailyQuestion.answer'] = 'synthetic-response'

            def account(self):
                return {'uid': 123456, 'username': 'test_member', 'rice': 10,
                        'app_status': {'checkin': False, 'question': bool(self.submissions)}}

            def credit_logs(self):
                return [row for row in super().credit_logs() if row['details']['title'] == '每日答题']

            def click_text(self, text):
                self.clicked.append(text)
                super().click_text(text)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = BothPendingBrowser()
            db = Library(root / daily.DATABASE_NAME)
            db.save_daily({'run_id': 'lost', 'started_at': NOW.isoformat(), 'finished_at': NOW.isoformat(),
                           'site_day': rules.site_day(NOW), 'status': 'failed', 'status_only': False,
                           'actions': [{'action': 'checkin', 'status': 'submission_unconfirmed',
                                        'response_seen': True}]}, 123456)
            db.close()
            with patch('daily.STATE', root), patch('daily.ACCOUNT_UID', 123456), \
                    patch('daily.datetime') as clock, patch('daily.site_day', return_value='2026-09-10'), \
                    patch('daily.Browser', return_value=session), \
                    patch('daily.time.monotonic', side_effect=itertools.cycle([0, 100])), \
                    patch('tests.test_access_recovery.time.time', return_value=NOW.timestamp()):
                clock.now.return_value = NOW
                result = daily.run_daily(supplied_answer='Answer', expected_question='Synthetic question')
            # Check-in stays protected because the site already answered its request once,
            # but that must not cost the quiz its turn.
            self.assertEqual(result['actions'][0]['status'], 'submission_unconfirmed')
            self.assertNotIn('提交签到', session.clicked)
            self.assertEqual(session.submissions, 1)
            self.assertEqual(result['actions'][1]['status'], 'reward_verified')
            self.assertTrue(result['reward_verified']['quiz'])

    def test_button_known_not_clicked_does_not_block_later_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = SubmissionBrowser(completed=True, rewarded=True)
            original_click = session.click_text

            def not_ready(text):
                if text == '提交答案':
                    raise RuntimeError('button_not_ready')
                original_click(text)

            with patch('daily.STATE', root), patch('daily.ACCOUNT_UID', 123456), \
                    patch('daily.datetime') as clock, patch('daily.site_day', return_value='2026-09-10'), \
                    patch('daily.Browser', return_value=session), \
                    patch('daily.time.monotonic', side_effect=[0, 100]), \
                    patch('tests.test_access_recovery.time.time', return_value=NOW.timestamp()):
                clock.now.return_value = NOW
                with patch.object(session, 'click_text', side_effect=not_ready):
                    failed = daily.resume_daily(supplied_answer='Answer', expected_question='Synthetic question')
                self.assertEqual(failed['error'], 'button_not_ready')
                self.assertEqual(session.submissions, 0)
                recovered = daily.resume_daily(supplied_answer='Answer', expected_question='Synthetic question')
                self.assertEqual(recovered['status'], 'complete')
                self.assertEqual(session.submissions, 1)

    def test_observed_reward_survives_later_read_failure_or_missing_receipt(self):
        for fail in [True, False]:
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                session = SubmissionBrowser(completed=False, rewarded=True)
                session.ids['/trpc/dailyQuestion.answer'] = 'synthetic-response'
                receipts = [{'uid': 123456, 'dateline': int(NOW.timestamp()), 'extcredits1': 1,
                             'details': {'title': title}} for title in ['签到奖励', '每日答题']]
                calls = 0

                def read_receipts():
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        return receipts
                    if fail:
                        raise RuntimeError('network_timeout')
                    return receipts[:1]

                with patch('daily.STATE', root), patch('daily.ACCOUNT_UID', 123456), \
                        patch('daily.datetime') as clock, patch('daily.site_day', return_value='2026-09-10'), \
                        patch('daily.Browser', return_value=session), patch('daily.time.monotonic', return_value=0), \
                        patch.object(session, 'credit_logs', side_effect=read_receipts):
                    clock.now.return_value = NOW
                    result = daily.resume_daily(supplied_answer='Answer', expected_question='Synthetic question')
                self.assertEqual(session.submissions, 1)
                self.assertTrue(result['attempts'][0]['reward_verified']['quiz'])
                self.assertNotEqual(result['status'], 'complete')
                db = Library(root / daily.DATABASE_NAME)
                action = db.daily_history(123456, '2026-09-10', 1)['days'][0]['actions'][1]
                self.assertTrue(action['reward_verified'])
                self.assertFalse(action['completed'])
                db.close()


class LearnedBankInDailyTests(unittest.TestCase):
    """What this account proved on the site outranks the snapshot the repository ships (#66)."""

    def seed(self, root, learned):
        root.mkdir(parents=True, exist_ok=True)
        (root / 'learned-answers.json').write_text(json.dumps(learned, ensure_ascii=False), encoding='utf-8')

    def run_daily(self, root, answered=False, **kwargs):
        session = SubmissionBrowser(completed=True, rewarded=True)
        if answered:
            session.ids['/trpc/dailyQuestion.answer'] = 'synthetic-response'
        with patch('daily.STATE', root), patch('daily.ACCOUNT_UID', 123456), \
                patch('daily.Browser', return_value=session), \
                patch('daily.time.monotonic', side_effect=itertools.cycle([0, 100])):
            # No clock patch: verify_reward matches the receipt's site day against this run's own.
            return daily.run_daily(**kwargs), session

    def test_a_learned_answer_is_used_without_being_supplied_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.seed(root, {'verified': {'Synthetic question': 'Answer'}, 'rejected': {}})
            result, session = self.run_daily(root)
            self.assertEqual(session.submissions, 1)
            self.assertNotIn('answer_needed', [row['status'] for row in result['actions']])

    def test_an_answer_already_proven_wrong_is_refused_rather_than_resubmitted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.seed(root, {'verified': {}, 'rejected': {'Synthetic question': ['Answer']}})
            result, session = self.run_daily(root, supplied_answer='Answer',
                                             expected_question='Synthetic question')
            self.assertEqual(session.submissions, 0)
            quiz = [row for row in result['actions'] if row['action'] == 'quiz'][0]
            self.assertEqual(quiz['status'], 'answer_needed')
            self.assertEqual(quiz['question'], 'Synthetic question')

    def test_a_rewarded_run_writes_the_answer_back_for_next_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _ = self.run_daily(root, answered=True, supplied_answer='Answer',
                                       expected_question='Synthetic question')
            self.assertEqual(result['status'], 'complete')
            learned = json.loads((root / 'learned-answers.json').read_text(encoding='utf-8'))
            self.assertEqual(learned['verified'], {'Synthetic question': 'Answer'})
