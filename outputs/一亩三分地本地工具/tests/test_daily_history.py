import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import cli
import daily
import library
from rules import site_day, daily_health
from tests.test_access_recovery import SubmissionBrowser


def result(run_id, *, verified=False, failed=False, started='2026-09-10T12:00:00+00:00'):
    return {'run_id': run_id, 'started_at': started, 'finished_at': started,
            'site_day': site_day(datetime.fromisoformat(started)), 'status_only': False,
            'status': 'failed' if failed else 'complete' if verified else 'needs_attention',
            'actions': [] if failed else [{'action': 'checkin',
                'status': 'reward_verified' if verified else 'not_done'}],
            'error': 'network_timeout' if failed else None,
            'reward_logs': [{'private': 'must not enter history'}],
            'before': {'username': 'test_member'},
            'recovery': {'read_retries': 1 if failed else 0, 'submission_checks': 0}}


class DailyHistoryTests(unittest.TestCase):
    def test_history_keeps_success_outside_limited_runs_and_deduplicates_same_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.sqlite'
            db = library.Library(path)
            try:
                first = result('first', verified=True)
                first.pop('status_only')
                first['actions'][0].update(status='reward_unconfirmed', completed=False)
                first['after'] = {'app_status': {'checkin': True}}
                first['reward_verified'] = {'checkin': True}
                db.save_daily(first, 123456)
                db.save_daily(first, 123456)
                db.save_daily(result('later', failed=True, started='2026-09-10T13:00:00+00:00'), 123456)
                db.save_daily(result('other-account', verified=True), 654321)
            finally:
                db.close()
            db = library.Library(path)
            try:
                history = db.daily_history(123456, '2026-09-10', 1)
                self.assertEqual([row['run_id'] for row in history['runs']], ['later'])
                self.assertTrue(history['truncated'])
                day = history['days'][0]
                self.assertEqual(day['run_count'], 2)
                self.assertEqual(day['actions'][0]['reward_run_id'], 'first')
                self.assertTrue(day['actions'][0]['reward_verified'])
                self.assertTrue(day['actions'][0]['completed'])
                self.assertIsNone(day['actions'][1]['completed'])
                self.assertNotIn('must not enter history', json.dumps(history))
                self.assertNotIn('test_member', json.dumps(history))
                self.assertEqual(db.stats()['threads'], 0)
                self.assertIsNone(db.daily_history(123456, '2026-09-10', 30)['runs'][-1]['status_only'])
            finally:
                db.close()

    def test_failure_then_recovery_and_site_day_boundary_keep_separate_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            db = library.Library(Path(directory) / 'history.sqlite')
            try:
                db.save_daily(result('failure', failed=True, started='2026-09-11T06:59:00+00:00'), 123456)
                db.save_daily(result('recovered', verified=True, started='2026-09-11T06:59:30+00:00'), 123456)
                db.save_daily(result('new-day', started='2026-09-11T07:00:00+00:00'), 123456)
                history = db.daily_history(123456, None, 30)
                self.assertEqual([row['site_day'] for row in history['days']], ['2026-09-11', '2026-09-10'])
                self.assertFalse(history['days'][0]['actions'][0]['reward_verified'])
                self.assertTrue(history['days'][1]['actions'][0]['reward_verified'])
                self.assertEqual(history['runs'][-1]['error'], 'network_timeout')
                self.assertEqual(site_day(datetime(2026, 9, 10, 16, 0, tzinfo=timezone.utc)), '2026-09-10')
            finally:
                db.close()

    def test_query_rejects_invalid_filters_before_opening_storage(self):
        with patch('library.Library') as storage:
            for date, limit in [('2026-02-30', 30), ('20260910', 30), ('2026-9-10', 30), ('２０２６-09-10', 30),
                                (None, True), (None, 0), (None, 201), (None, '30')]:
                with self.subTest(date=date, limit=limit):
                    self.assertEqual(library.get_daily_history(date, limit)['status'], 'failed')
            storage.assert_not_called()

    def test_daily_persists_results_and_cli_reads_them_without_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = SubmissionBrowser(completed=True, rewarded=True)
            with patch('daily.STATE', root), patch('daily.Browser', return_value=session), \
                    patch('daily.time.monotonic', side_effect=[0, 100]), patch('daily.ACCOUNT_UID', 123456):
                saved = daily.run_daily(supplied_answer='Answer', expected_question='Synthetic question')
            self.assertEqual(saved['status'], 'complete')
            self.assertTrue(saved['history_saved'])
            with patch('library.STATE', root), patch('library.ACCOUNT_UID', 123456), \
                    patch('library.Browser') as browser, patch('sys.argv', ['cli', 'daily-history', '--limit', '1']), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(cli.main(), 0)
                history = json.loads(output.getvalue())
                browser.assert_not_called()
            self.assertEqual(history['runs'][0]['run_id'], saved['run_id'])
            self.assertTrue(all(row['reward_verified'] for row in history['days'][0]['actions']))
            self.assertNotIn('reward_logs', history['runs'][0])

    def test_site_midnight_stops_before_submitting_and_storage_failure_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = SubmissionBrowser(completed=True, rewarded=True)
            with patch('daily.STATE', root), patch('daily.Browser', return_value=session), \
                    patch('daily.datetime') as clock, \
                    patch('daily.site_day', side_effect=['2026-09-10', '2026-09-11']):
                clock.now.return_value = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
                saved = daily.run_daily()
            self.assertEqual(saved['error'], 'site_day_changed')
            self.assertEqual(session.submissions, 0)
            with patch('daily.STATE', root), patch('daily.Browser', side_effect=RuntimeError('network_timeout')), \
                    patch('daily.Library', side_effect=OSError('synthetic private path')):
                failed = daily.run_daily()
            self.assertEqual(failed['status'], 'failed')
            self.assertFalse(failed['history_saved'])
            self.assertEqual(failed['error'], 'daily_history_write_failed')
            self.assertNotIn('synthetic private path', json.dumps(failed))

    def test_reward_read_failure_retains_completion_observed_after_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = SubmissionBrowser(completed=True, rewarded=True)
            with patch('daily.STATE', root), patch('daily.Browser', return_value=session), \
                    patch('daily.time.monotonic', side_effect=[0, 100]), patch('daily.ACCOUNT_UID', 123456), \
                    patch.object(session, 'credit_logs', side_effect=RuntimeError('network_timeout')):
                saved = daily.run_daily(supplied_answer='Answer', expected_question='Synthetic question')
            self.assertEqual(saved['status'], 'failed')
            db = library.Library(root / library.DATABASE_NAME)
            try:
                history = db.daily_history(123456, None, 1)
                quiz = history['runs'][0]['actions'][1]
                self.assertTrue(quiz['completed'])
                self.assertFalse(quiz['reward_verified'])
                self.assertEqual(history['runs'][0]['error'], 'network_timeout')
            finally:
                db.close()


NOON = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


def summarized_day(day, *, checkin=True, quiz=True):
    return {'site_day': day, 'run_count': 1, 'actions': [
        {'action': 'checkin', 'completed': checkin or None, 'reward_verified': checkin},
        {'action': 'quiz', 'completed': quiz or None, 'reward_verified': quiz}]}


def fresh_runs(now=NOON, hours=1):
    return [{'started_at': (now - timedelta(hours=hours)).isoformat()}]


class DailyHealthTests(unittest.TestCase):
    def test_finished_days_with_a_recent_run_are_ok(self):
        health = daily_health([summarized_day('2026-09-20'), summarized_day('2026-09-19')], fresh_runs(), now=NOON)
        self.assertEqual(health['verdict'], 'ok')
        self.assertEqual(health['consecutive_incomplete_days'], 0)
        self.assertEqual(health['reasons'], [])
        self.assertEqual(health['evaluated_through'], '2026-09-20')

    def test_one_bad_day_warns_and_a_second_escalates(self):
        one = daily_health([summarized_day('2026-09-20', quiz=False), summarized_day('2026-09-19')],
                           fresh_runs(), now=NOON)
        self.assertEqual(one['verdict'], 'warn')
        self.assertEqual(one['consecutive_incomplete_days'], 1)
        two = daily_health([summarized_day('2026-09-20', quiz=False),
                            summarized_day('2026-09-19', checkin=False), summarized_day('2026-09-18')],
                           fresh_runs(), now=NOON)
        self.assertEqual(two['verdict'], 'alert')
        self.assertEqual(two['incomplete_days'], ['2026-09-20', '2026-09-19'])
        self.assertEqual(two['reasons'], ['incomplete_days'])

    def test_days_with_no_rows_at_all_count_as_incomplete(self):
        # A dead scheduler leaves silence, not failure rows; silence must not read as success.
        health = daily_health([summarized_day('2026-09-18')], fresh_runs(), now=NOON)
        self.assertEqual(health['verdict'], 'alert')
        self.assertEqual(health['incomplete_days'], ['2026-09-20', '2026-09-19'])

    def test_quiet_invocations_keep_a_finished_day_healthy(self):
        # A finished day is done in one run; every later invocation exits without writing a run.
        # Judging liveness by run age therefore alerts on exactly the days that went perfectly.
        beat = (NOON - timedelta(hours=1)).isoformat()
        health = daily_health([summarized_day('2026-09-20')], fresh_runs(hours=9), beat, now=NOON)
        self.assertEqual(health['verdict'], 'ok')
        self.assertEqual(health['reasons'], [])
        self.assertEqual(health['last_fired_age_hours'], 1.0)

    def test_nothing_firing_alerts_even_when_every_day_finished(self):
        health = daily_health([summarized_day('2026-09-20')], fresh_runs(hours=9),
                              (NOON - timedelta(hours=9)).isoformat(), now=NOON)
        self.assertEqual(health['verdict'], 'alert')
        self.assertEqual(health['reasons'], ['runs_stale'])
        self.assertEqual(health['consecutive_incomplete_days'], 0)
        self.assertEqual(health['last_fired_age_hours'], 9.0)

    def test_empty_history_is_reported_rather_than_read_as_success(self):
        health = daily_health([], [], now=NOON)
        self.assertEqual(health['verdict'], 'alert')
        self.assertEqual(health['reasons'], ['no_history'])
        self.assertIsNone(health['last_fired_age_hours'])

    def test_today_is_not_counted_until_it_is_due(self):
        before_due = datetime(2026, 9, 20, 8, tzinfo=timezone.utc)
        health = daily_health([summarized_day('2026-09-19')], fresh_runs(before_due), now=before_due)
        self.assertEqual(health['evaluated_through'], '2026-09-19')
        self.assertEqual(health['verdict'], 'ok')


class WatcherExitCodeTests(unittest.TestCase):
    """An independent watcher reads the exit code; it cannot be asked to parse JSON or judge a trend."""

    def run_cli(self, health, *flags):
        payload = {'status': 'complete', 'runs': [], 'days': [], 'health': health,
                   'truncated': False, 'error': None}
        with patch('cli.get_daily_history', return_value=payload), \
                patch('sys.argv', ['cli', 'daily-history', *flags]), \
                patch('sys.stdout', new_callable=io.StringIO):
            return cli.main()

    def test_alert_exits_non_zero_only_when_the_watcher_asks_for_it(self):
        alert = {'verdict': 'alert', 'reasons': ['runs_stale']}
        self.assertEqual(self.run_cli(alert, '--fail-on-alert'), 3)
        self.assertEqual(self.run_cli(alert), 0)

    def test_healthy_and_merely_warning_days_do_not_wake_the_watcher(self):
        for verdict in ['ok', 'warn']:
            with self.subTest(verdict=verdict):
                self.assertEqual(self.run_cli({'verdict': verdict, 'reasons': []}, '--fail-on-alert'), 0)

    def test_a_query_that_could_not_run_stays_a_plain_failure(self):
        with patch('cli.get_daily_history', return_value={'status': 'failed', 'health': None,
                                                          'error': 'daily_history_unavailable'}), \
                patch('sys.argv', ['cli', 'daily-history', '--fail-on-alert']), \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(cli.main(), 2)
