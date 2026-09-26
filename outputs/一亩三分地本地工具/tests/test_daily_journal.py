import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import daily
from library import Library
from rules import site_day
from tests.test_access_recovery import SubmissionBrowser


class JournalTests(unittest.TestCase):
    def invoke(self, root, session):
        with patch('daily.STATE', root), patch('daily.ACCOUNT_UID', 123456), \
                patch('daily.Browser', return_value=session), patch('daily.time.monotonic', side_effect=[0, 100]):
            return daily.run_daily(supplied_answer='Answer', expected_question='Synthetic question')

    def test_killed_after_click_leaves_intent_and_restart_does_not_resubmit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = SubmissionBrowser(False, False)
            click = session.click_text

            def killed(label):
                if label == '提交答案':
                    db = Library(root / daily.DATABASE_NAME)
                    try:
                        days = db.daily_history(123456, site_day(), 10)['days']
                        self.assertTrue(days)
                        self.assertIsNotNone(days[0]['actions'][1]['unconfirmed_submission_run_id'])
                    finally:
                        db.close()
                    click(label)
                    raise KeyboardInterrupt('synthetic process interruption')
                click(label)

            with patch.object(session, 'click_text', side_effect=killed), self.assertRaises(KeyboardInterrupt):
                self.invoke(root, session)
            later = self.invoke(root, session)
            self.assertEqual(session.submissions, 1)
            self.assertEqual(later['error'], 'quiz_submission_unconfirmed')
            session.completed = session.rewarded = True
            self.assertEqual(self.invoke(root, session)['status'], 'complete')
            self.assertEqual(session.submissions, 1)

    def test_storage_failure_prevents_the_click(self):
        with tempfile.TemporaryDirectory() as directory:
            session = SubmissionBrowser(False, False)
            with patch.object(Library, 'save_daily', side_effect=OSError('synthetic failure')):
                result = self.invoke(Path(directory), session)
            self.assertEqual(session.submissions, 0)
            self.assertEqual(result['error'], 'daily_history_write_failed')

    def test_proven_no_click_removes_intent_and_allows_later_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = SubmissionBrowser(True, True)
            original = session.click_text
            def not_ready(label):
                if label == '提交答案':
                    raise RuntimeError('button_not_ready')
                original(label)
            with patch.object(session, 'click_text', side_effect=not_ready):
                self.assertEqual(self.invoke(root, session)['error'], 'button_not_ready')
            self.assertEqual(self.invoke(root, session)['status'], 'complete')
            self.assertEqual(session.submissions, 1)

    def test_temporary_storage_failure_does_not_block_later_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = SubmissionBrowser(True, True)
            original = Library.save_daily
            failed = False

            def fail_once(db, *args, **kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError('synthetic lock')
                return original(db, *args, **kwargs)

            with patch.object(Library, 'save_daily', fail_once):
                result = self.invoke(root, session)
            self.assertEqual(result['error'], 'daily_history_write_failed')
            self.assertEqual(session.submissions, 0)
            self.assertEqual(self.invoke(root, session)['status'], 'complete')
            self.assertEqual(session.submissions, 1)

    def test_midnight_during_checkpoint_prevents_click_and_clears_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = SubmissionBrowser(True, True)
            original = Library.save_daily
            current_day = site_day()

            def cross_midnight(db, *args, **kwargs):
                nonlocal current_day
                saved = original(db, *args, **kwargs)
                current_day = '2099-01-01'
                return saved

            with patch.object(Library, 'save_daily', cross_midnight), \
                    patch('daily.site_day', side_effect=lambda now=None: site_day(now) if now else current_day):
                result = self.invoke(root, session)
            self.assertEqual(result['error'], 'site_day_changed')
            self.assertEqual(session.submissions, 0)
            self.assertEqual(self.invoke(root, session)['status'], 'complete')
            self.assertEqual(session.submissions, 1)
