import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from random import Random
from unittest.mock import patch

import daily
import settings
from contracts import daily_history_record
from library import Library
from rules import choose_mood, choose_phrase, load_mood_phrases, site_day
from settings import CHECKIN_MOOD_WEIGHTS, CHECKIN_MOOD_DEFAULT, MOOD_PHRASES_FILE, MOOD_PHRASE_MAX_LENGTH
from tests.test_access_recovery import SubmissionBrowser

# The run's started_at is the real clock and save_daily checks it against the run's site day, so the
# day under test must be the real site day rather than the date these tests were written on.
TODAY = site_day()


def days_ago(count):
    return (date.fromisoformat(TODAY) - timedelta(days=count)).isoformat()


class PhrasePoolTests(unittest.TestCase):
    """The shipped pool is what gets published in the member's name; every line is checked (#67)."""

    def test_the_shipped_pool_has_every_mood_and_only_clean_short_unique_lines(self):
        pool = load_mood_phrases(MOOD_PHRASES_FILE)
        self.assertEqual(set(pool), set(CHECKIN_MOOD_WEIGHTS))
        total = 0
        for mood, phrases in pool.items():
            self.assertTrue(phrases, mood)
            for phrase in phrases:
                self.assertEqual(phrase, phrase.strip())
                self.assertLessEqual(len(phrase), MOOD_PHRASE_MAX_LENGTH)
                self.assertNotIn('http', phrase)
                self.assertNotIn('\n', phrase)
            total += len(phrases)
        self.assertEqual(total, len({p for phrases in pool.values() for p in phrases}), 'no line may repeat across moods')

    def test_a_broken_pool_is_refused_before_anything_is_said(self):
        good = json.loads(MOOD_PHRASES_FILE.read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pool.json'
            for change in [lambda p: p.pop('开心'), lambda p: p.__setitem__('开心', []),
                           lambda p: p['开心'].append(' 带空格 '), lambda p: p['开心'].append('x' * (MOOD_PHRASE_MAX_LENGTH + 1)),
                           lambda p: p['开心'].append(p['难过'][0]), lambda p: p.__setitem__('多余', ['x'])]:
                broken = json.loads(json.dumps(good))
                change(broken)
                path.write_text(json.dumps(broken, ensure_ascii=False), encoding='utf-8')
                with self.assertRaises(ValueError):
                    load_mood_phrases(path)
            path.write_text('{not json', encoding='utf-8')
            with self.assertRaises(ValueError):
                load_mood_phrases(path)


class PhraseChoiceTests(unittest.TestCase):
    POOL = {mood: [f'{mood}-{n}' for n in range(3)] for mood in CHECKIN_MOOD_WEIGHTS}

    def test_the_line_comes_from_the_chosen_moods_group_and_skips_recent_ones(self):
        rng = Random(7)
        for _ in range(50):
            self.assertTrue(choose_phrase('开心', self.POOL, rng=rng).startswith('开心-'))
        self.assertEqual(choose_phrase('难过', self.POOL, recent=['难过-0', '难过-1'], rng=rng), '难过-2')
        self.assertIsNone(choose_phrase('衰', self.POOL, recent=self.POOL['衰'], rng=rng))
        self.assertIsNone(choose_phrase(CHECKIN_MOOD_DEFAULT, self.POOL, rng=rng))

    def test_the_mood_draw_follows_the_weights_and_can_continue_a_streak(self):
        rng = Random(3)
        draws = [choose_mood(None, rng=rng) for _ in range(4000)]
        self.assertLess(draws.count('生气'), draws.count('开心'))
        self.assertGreater(sum(1 for d in draws if d == '没心情'), 0)
        continued = sum(1 for _ in range(1000) if choose_mood('奋斗', rng=rng) == '奋斗')
        self.assertGreater(continued, 200)


class MoodBrowser(SubmissionBrowser):
    """Records which mood button was clicked and what went into the textarea."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clicked, self.textarea, self.waits = [], None, []

    def account(self):
        return {'uid': 123456, 'username': 'test_member', 'rice': 10,
                'app_status': {'checkin': bool(self.clicked), 'question': True}}

    def credit_logs(self):
        import time
        return [{'uid': 123456, 'dateline': int(time.time()), 'extcredits1': 1, 'details': {'title': '签到奖励'}}]

    def wait_for(self, expression, **kwargs):
        self.waits.append(expression)
        return True

    def evaluate(self, expression):
        if ".click()" in expression:
            self.clicked.append(expression)
            return None
        if 'todaysay' in expression:
            self.textarea = json.loads(expression.split('.set.call(t,', 1)[1].split(');', 1)[0])
            return True
        return None

    def click_text(self, text):
        if text == '提交签到':
            self.submissions += 1


class CheckInMoodTests(unittest.TestCase):
    def test_failed_preparation_reuses_same_choice_after_restart(self):
        class RefusingBrowser(MoodBrowser):
            def evaluate(self, expression):
                if 'todaysay' in expression:
                    return False
                return super().evaluate(expression)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('daily.choose_mood', return_value='开心') as draw, \
                    patch('daily.choose_phrase', return_value='慢慢来，也挺好。') as phrase:
                first = self.run_checkin(RefusingBrowser(True, True), root, random_mood=True)
                self.assertEqual(first['error'], 'checkin_phrase_not_accepted')
                second = MoodBrowser(True, True)
                self.run_checkin(second, root, random_mood=True)
                self.assertEqual(second.textarea, '慢慢来，也挺好。')
                draw.assert_called_once_with(None)
                phrase.assert_called_once()

    def test_calendar_window_and_yesterday_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('daily.choose_mood', return_value='开心') as draw:
                self.run_checkin(MoodBrowser(True, True), root, random_mood=True,
                                 seeded=[(days_ago(2), '疲惫', '前天'), (days_ago(30), '衰', '边界'),
                                         (days_ago(31), '衰', '过期')])
                draw.assert_called_once_with(None)
            db = Library(root / settings.DATABASE_NAME)
            try:
                recent = db.recent_checkins(123456, 30, on=TODAY)
            finally:
                db.close()
            self.assertIn('边界', [r['phrase'] for r in recent])
            self.assertNotIn('过期', [r['phrase'] for r in recent])

    def test_exhausted_pool_uses_sites_blank_checkin_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            session = MoodBrowser(True, True)
            with patch('daily.choose_mood', return_value='开心'), patch('daily.choose_phrase', return_value=None):
                self.run_checkin(session, Path(directory), random_mood=True)
            self.assertEqual(session.submissions, 1)
            self.assertIsNone(session.textarea)
            self.assertIn(json.dumps(CHECKIN_MOOD_DEFAULT), session.clicked[0])

    def test_opt_out_overrides_a_saved_public_phrase(self):
        with tempfile.TemporaryDirectory() as directory, patch('daily.STATE', Path(directory)), \
                patch('daily.ACCOUNT_UID', 123456), patch('daily.CHECKIN_MOOD_RANDOM', True), \
                patch('daily.choose_mood', return_value='开心'):
            planned = daily._checkin_plan(TODAY)
            self.assertIsNotNone(planned['phrase'])
            with patch('daily.CHECKIN_MOOD_RANDOM', False):
                self.assertEqual(daily._checkin_plan(TODAY), {'mood': CHECKIN_MOOD_DEFAULT, 'phrase': None})

    def run_checkin(self, session, root, *, random_mood, seeded=()):
        db = Library(root / settings.DATABASE_NAME)
        for site_day, mood, phrase in seeded:
            db.save_daily({'run_id': f'seed-{site_day}', 'site_day': site_day, 'started_at': f'{site_day}T20:00:00+00:00',
                           'finished_at': f'{site_day}T20:01:00+00:00', 'status': 'complete',
                           'actions': [{'action': 'checkin', 'status': 'reward_verified', 'mood': mood, 'phrase': phrase}]}, 123456)
        db.close()
        with patch('daily.STATE', root), patch('daily.ACCOUNT_UID', 123456), patch('daily.Browser', return_value=session), \
                patch('daily.CHECKIN_MOOD_RANDOM', random_mood), \
                patch('daily.time.monotonic', side_effect=[0, 100, 200, 300]), patch('daily.site_day', return_value=TODAY):
            return daily.run_daily()

    def test_switched_off_the_check_in_picks_the_default_mood_and_says_nothing(self):
        # The default (on) is asserted in ConfigTests; the live account.json on a developer machine may
        # legitimately have it off, so the run itself is patched explicitly here.
        with tempfile.TemporaryDirectory() as directory:
            session = MoodBrowser(completed=True, rewarded=True)
            result = self.run_checkin(session, Path(directory), random_mood=False)
        self.assertEqual(len(session.clicked), 1)
        self.assertIn(json.dumps(CHECKIN_MOOD_DEFAULT), session.clicked[0])
        self.assertIsNone(session.textarea)
        self.assertTrue(any('!document.querySelector' in w for w in session.waits))
        entry = next(a for a in result['actions'] if a['action'] == 'checkin')
        self.assertEqual((entry['mood'], entry['phrase']), (CHECKIN_MOOD_DEFAULT, None))

    def test_on_by_default_the_check_in_clicks_the_drawn_mood_fills_its_line_and_records_both(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = MoodBrowser(completed=True, rewarded=True)
            with patch('daily.choose_mood', return_value='奋斗') as drawn:
                result = self.run_checkin(session, root, random_mood=True,
                                          seeded=[(days_ago(1), '疲惫', '今天走了两万步，腿都是酸的。')])
            drawn.assert_called_once_with('疲惫')  # yesterday's mood feeds the streak rule
            self.assertIn(json.dumps('奋斗'), session.clicked[0])
            pool = load_mood_phrases(MOOD_PHRASES_FILE)
            self.assertIn(session.textarea, pool['奋斗'])
            self.assertTrue(any('!!document.querySelector' in w for w in session.waits))
            entry = next(a for a in result['actions'] if a['action'] == 'checkin')
            self.assertEqual((entry['mood'], entry['phrase']), ('奋斗', session.textarea))
            db = Library(root / settings.DATABASE_NAME)
            try:
                recent = db.recent_checkins(123456, 30)
            finally:
                db.close()
            self.assertEqual([(r['site_day'], r['mood']) for r in recent], [(TODAY, '奋斗'), (days_ago(1), '疲惫')])

    def test_recent_phrases_are_kept_out_of_the_draw(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pool = load_mood_phrases(MOOD_PHRASES_FILE)
            used = pool['衰'][:-1]  # every line but one was said this month
            seeded = [(days_ago(count), '衰', phrase) for count, phrase in zip(range(19, 0, -1), used)]
            session = MoodBrowser(completed=True, rewarded=True)
            with patch('daily.choose_mood', return_value='衰'):
                self.run_checkin(session, root, random_mood=True, seeded=seeded)
            self.assertEqual(session.textarea, pool['衰'][-1])

    def test_a_line_the_page_did_not_take_stops_the_submission(self):
        class RefusingBrowser(MoodBrowser):
            def evaluate(self, expression):
                if 'todaysay' in expression:
                    return False
                return super().evaluate(expression)
        with tempfile.TemporaryDirectory() as directory:
            session = RefusingBrowser(completed=True, rewarded=True)
            with patch('daily.choose_mood', return_value='开心'):
                result = self.run_checkin(session, Path(directory), random_mood=True)
        self.assertEqual(session.submissions, 0)
        self.assertEqual(result['error'], 'checkin_phrase_not_accepted')


class HistoryShapeTests(unittest.TestCase):
    def test_the_record_keeps_mood_and_phrase_only_as_observed(self):
        record = daily_history_record({'actions': [{'action': 'checkin', 'status': 'reward_verified', 'mood': '开心', 'phrase': '一句话'},
                                                   {'action': 'quiz', 'status': 'reward_verified'}], 'run_id': 'r', 'site_day': '2026-09-22',
                                       'started_at': '2026-09-22T20:00:00+00:00', 'finished_at': '2026-09-22T20:01:00+00:00',
                                       'status': 'complete'})
        by_action = {a['action']: a for a in record['actions']}
        self.assertEqual((by_action['checkin']['mood'], by_action['checkin']['phrase']), ('开心', '一句话'))
        self.assertEqual((by_action['quiz']['mood'], by_action['quiz']['phrase']), (None, None))


class ConfigTests(unittest.TestCase):
    def test_the_switch_must_be_a_real_boolean_and_defaults_on(self):
        # Issue #15: random moods are on unless account.json says "checkin_mood_random": false.
        self.assertTrue(settings.mood_random_enabled({}))
        self.assertTrue(settings.mood_random_enabled({'checkin_mood_random': True}))
        self.assertFalse(settings.mood_random_enabled({'checkin_mood_random': False}))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'account.json'
            path.write_text(json.dumps({'username': 'm', 'uid': 1, 'checkin_mood_random': False}), encoding='utf-8')
            self.assertEqual(settings.load_identity(path)[2], {'checkin_mood_random': False})
            path.write_text(json.dumps({'username': 'm', 'uid': 1}), encoding='utf-8')
            self.assertEqual(settings.load_identity(path)[2], {})
            self.assertTrue(settings.mood_random_enabled(settings.load_identity(path)[2]))
            for bad in ['true', 1, None]:
                path.write_text(json.dumps({'username': 'm', 'uid': 1, 'checkin_mood_random': bad}), encoding='utf-8')
                with self.assertRaises(RuntimeError):
                    settings.load_identity(path)
