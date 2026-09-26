import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import settings
from browser import Browser
from governance import scan_sources


class LocalIdentityTests(unittest.TestCase):
    def test_missing_identity_allows_offline_use(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(settings.load_identity(Path(directory) / 'absent.json'), ('', 0, {}))

    def test_valid_identity_is_loaded_from_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'account.json'
            path.write_text(json.dumps({'username': 'test_member', 'uid': 123456}), encoding='utf-8')
            self.assertEqual(settings.load_identity(path), ('test_member', 123456, {}))

    def test_invalid_identity_fails_without_echoing_data(self):
        for data in [{'username': 'test_member', 'uid': True}, {'username': '', 'uid': 1},
                     {'username': 'test_member', 'uid': 0}, {'username': 'test_member', 'uid': '1'},
                     {'username': 'test_member', 'uid': 1, 'password': 'synthetic-only'}, []]:
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'account.json'
                path.write_text(json.dumps(data), encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError, '^invalid_local_account_config$'):
                    settings.load_identity(path)

    def test_malformed_json_fails_without_echoing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'account.json'
            path.write_text('{malformed', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, '^invalid_local_account_config$'):
                settings.load_identity(path)

    def test_unconfigured_browser_stops_before_launch(self):
        with patch('browser.USERNAME', ''), patch('browser.ACCOUNT_UID', 0), patch('browser.subprocess.Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, '^account_not_configured$'):
                Browser().__enter__()
            launch.assert_not_called()

    def test_empty_identity_is_not_a_copied_config_literal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'rules.py').write_text('text = ""\ncount = 0\n', encoding='utf-8')
            with patch.object(settings, 'USERNAME', ''), patch.object(settings, 'ACCOUNT_UID', 0):
                self.assertEqual(scan_sources(root, {'modules': {'rules': []}}), [])

    def test_configured_identity_copy_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'rules.py').write_text('username = "test_member"\nuid = 123456\n', encoding='utf-8')
            with patch.object(settings, 'USERNAME', 'test_member'), patch.object(settings, 'ACCOUNT_UID', 123456):
                self.assertEqual(len(scan_sources(root, {'modules': {'rules': []}})), 2)

    def test_short_identity_does_not_poison_source_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'rules.py').write_text('field = "data"\ncount = 1\n', encoding='utf-8')
            with patch.object(settings, 'USERNAME', 'data'), patch.object(settings, 'ACCOUNT_UID', 1):
                self.assertEqual(scan_sources(root, {'modules': {'rules': []}}), [])

    def test_hardcoded_identity_rejected_without_local_account(self):
        for source in ['username = "test_member"', 'ACCOUNT_UID = 123456',
                       'account = {"uid": 123456}', 'account["username"] = "test_member"']:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'rules.py').write_text(source, encoding='utf-8')
                with patch.object(settings, 'USERNAME', ''), patch.object(settings, 'ACCOUNT_UID', 0):
                    self.assertTrue(any('owned_configuration' in error for error in scan_sources(root, {'modules': {'rules': []}})))


class LocalScheduleTests(unittest.TestCase):
    """The schedule is machine-local: changing it must never mean editing tracked source (#65)."""

    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'account.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            return settings.load_identity(path)

    def test_schedule_overrides_ride_alongside_the_identity(self):
        identity = self.load({'username': 'test_member', 'uid': 123456,
                              'schedule_time': '07:05', 'schedule_timezone': 'America/New_York'})
        self.assertEqual(identity[2], {'schedule_time': '07:05', 'schedule_timezone': 'America/New_York'})
        self.assertEqual(settings.load_schedule(identity[2], '16:10', 'Asia/Shanghai'),
                         ('07:05', 'America/New_York'))

    def test_absent_overrides_keep_the_shipped_default(self):
        self.assertEqual(settings.load_schedule({}, '16:10', 'Asia/Shanghai'), ('16:10', 'Asia/Shanghai'))
        self.assertEqual(settings.load_schedule_mode({}), 'random')
        self.assertEqual(settings.load_schedule_mode({'schedule_time': '07:05'}), 'fixed')
        self.assertEqual(settings.load_schedule_mode({'schedule_time': '07:05', 'schedule_mode': 'random'}), 'random')
        with self.assertRaisesRegex(RuntimeError, '^invalid_local_schedule_config$'):
            settings.load_schedule_mode({'schedule_mode': []})

    def test_unknown_keys_are_still_rejected(self):
        with self.assertRaisesRegex(RuntimeError, '^invalid_local_account_config$'):
            self.load({'username': 'test_member', 'uid': 1, 'schedule_hour': 7})

    def test_a_bad_schedule_fails_loudly_instead_of_falling_back(self):
        for bad in [{'schedule_time': '7:05'}, {'schedule_time': '24:00'}, {'schedule_time': '16:60'},
                    {'schedule_time': 1610}, {'schedule_timezone': 'Mars/Olympus'},
                    {'schedule_timezone': ''}]:
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(RuntimeError, '^invalid_local_schedule_config$'):
                    settings.load_schedule(bad, '16:10', 'Asia/Shanghai')

    def test_summary_reports_both_clocks_and_flags_an_ambiguous_rrule(self):
        change = patch.object(settings, 'SCHEDULE_MODE', 'fixed')
        change.start()
        self.addCleanup(change.stop)
        # A bare RRULE does not say which clock it means; a whole-multiple offset hides the question.
        with patch.object(settings, 'SCHEDULE_TIME', '16:10'), \
                patch.object(settings, 'SCHEDULE_TIMEZONE', 'Asia/Shanghai'):
            shanghai = settings.daily_schedule_summary()
        self.assertEqual(shanghai['fires_local'], shanghai['fires_utc'])
        self.assertFalse(shanghai['rrule_clock_is_ambiguous'])
        with patch.object(settings, 'SCHEDULE_TIME', '16:10'), \
                patch.object(settings, 'SCHEDULE_TIMEZONE', 'Asia/Kolkata'):
            kolkata = settings.daily_schedule_summary()
        self.assertNotEqual(kolkata['fires_local'], kolkata['fires_utc'])
        self.assertTrue(kolkata['rrule_clock_is_ambiguous'])
