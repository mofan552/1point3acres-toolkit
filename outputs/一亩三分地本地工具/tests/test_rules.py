import unittest
from datetime import datetime, timezone
import settings
from rules import choose_answer, choose_mood, site_day, verify_reward, build_outline


class RuleTests(unittest.TestCase):
    def test_answer_uses_text_not_position(self):
        self.assertEqual(choose_answer(" 问题？ ", {"a4": "正确", "a1": "错误"}, {"问题？": "正确"}), "a4")

    def test_unknown_or_ambiguous_answer_is_not_guessed(self):
        self.assertIsNone(choose_answer("未知", {"a1": "一个"}, {}))
        self.assertIsNone(choose_answer("问题", {"a1": "一样", "a2": "一样"}, {"问题": "一样"}))

    def test_site_day_respects_los_angeles_midnight(self):
        self.assertEqual(site_day(datetime(2026, 9, 10, 6, 59, tzinfo=timezone.utc)), "2026-09-09")
        self.assertEqual(site_day(datetime(2026, 9, 10, 7, 0, tzinfo=timezone.utc)), "2026-09-10")
        self.assertEqual(site_day(datetime(2026, 1, 10, 7, 59, tzinfo=timezone.utc)), "2026-01-09")

    def test_reward_needs_correct_user_action_and_day(self):
        now = datetime(2026, 9, 9, 22, 17, tzinfo=timezone.utc)
        row = {"uid": 123456, "dateline": int(now.timestamp()), "extcredits1": 1, "details": {"title": "签到奖励"}}
        self.assertTrue(verify_reward(123456, "checkin", [row], now))
        self.assertFalse(verify_reward(1, "checkin", [row], now))
        self.assertFalse(verify_reward(123456, "quiz", [row], now))
        self.assertFalse(verify_reward(123456, "checkin", [dict(row, dateline=row["dateline"] - 86400)], now))
        self.assertFalse(verify_reward(123456, "checkin", [dict(row, extcredits1=0)], now))
        self.assertFalse(verify_reward(123456, "checkin", [], now))


if __name__ == "__main__":
    unittest.main()


class FakeRandom:
    """Fixed draws so a probabilistic rule can be asserted exactly."""

    def __init__(self, *values):
        self.values = list(values)

    def random(self):
        return self.values.pop(0)


class MoodTests(unittest.TestCase):
    def test_a_run_continues_when_the_draw_falls_inside_persistence(self):
        self.assertEqual(choose_mood('难过', FakeRandom(0.0)), '难过')
        self.assertEqual(choose_mood('难过', FakeRandom(settings.CHECKIN_MOOD_PERSISTENCE / 2)), '难过')

    def test_a_run_ends_and_then_the_weights_decide(self):
        first = next(iter(settings.CHECKIN_MOOD_WEIGHTS))
        self.assertEqual(choose_mood('难过', FakeRandom(settings.CHECKIN_MOOD_PERSISTENCE, 0.0)), first)

    def test_missing_or_unknown_previous_mood_never_consumes_the_persistence_draw(self):
        first = next(iter(settings.CHECKIN_MOOD_WEIGHTS))
        self.assertEqual(choose_mood(None, FakeRandom(0.0)), first)
        self.assertEqual(choose_mood('不是一个心情', FakeRandom(0.0)), first)

    def test_every_weight_band_selects_its_own_mood(self):
        total = sum(settings.CHECKIN_MOOD_WEIGHTS.values())
        cumulative = 0
        for mood, weight in settings.CHECKIN_MOOD_WEIGHTS.items():
            low, cumulative = cumulative / total, cumulative + weight
            with self.subTest(mood=mood):
                self.assertEqual(choose_mood(None, FakeRandom(low)), mood)
                self.assertEqual(choose_mood(None, FakeRandom(cumulative / total - 1e-9)), mood)

    def test_the_distribution_is_deliberately_skewed_rather_than_uniform(self):
        # Uniform i.i.d. draws are the thing this rule exists to avoid; keep the skew and the streaks.
        weights = settings.CHECKIN_MOOD_WEIGHTS
        self.assertTrue(all(weight > 0 for weight in weights.values()))
        self.assertGreaterEqual(max(weights.values()), 3 * min(weights.values()))
        self.assertIn(settings.CHECKIN_MOOD_DEFAULT, weights)
        self.assertTrue(0 < settings.CHECKIN_MOOD_PERSISTENCE < 1)

    def test_default_source_is_unseeded_and_only_yields_known_moods(self):
        drawn = {choose_mood() for _ in range(200)}
        self.assertTrue(drawn.issubset(set(settings.CHECKIN_MOOD_WEIGHTS)))
        self.assertGreater(len(drawn), 1)

    def test_the_vocabulary_matches_what_the_site_actually_offers(self):
        """Read off the check-in page on 2026-09-22, not copied from a feature description.

        The first version of this list carried 惊呆, which the site does not have, because it was
        taken from prose instead of the page. This pins provenance; it cannot notice the site
        changing. At runtime a mood whose button is absent fails loudly on the wait, not silently.
        """
        self.assertEqual(set(settings.CHECKIN_MOOD_WEIGHTS),
                         {'开心', '难过', '郁闷', '无聊', '生气', '疲惫', '奋斗', '慵懒', '衰', '没心情'})
        self.assertNotIn('惊呆', settings.CHECKIN_MOOD_WEIGHTS)


def interview_record(op_text, replies=(), *, role='未标注', level='未标注', restricted=False):
    posts = [{'pid': 1, 'text': op_text, 'restricted': restricted, 'author': {'uid': 7, 'name': '楼主', 'anonymous': False}}]
    for index, (uid, text) in enumerate(replies, start=2):
        posts.append({'pid': index, 'text': text, 'restricted': False,
                      'author': {'uid': uid, 'name': f'用户{uid}', 'anonymous': uid is None}})
    return {'tid': 555, 'company': 'Synthetic Co', 'title': 'Synthetic 面经', 'role': role, 'level': level,
            'content_status': 'restricted' if restricted else 'visible', 'pagination_complete': True,
            'content_hash': 'hash-1', 'posts': posts}


class OutlineTests(unittest.TestCase):
    """Rounds and questions must be quotable from a specific post, never invented or misattributed (#23)."""

    OP = ('第一轮是 OA，两道题。\n'
          '题目是给定一个日志列表，要求返回出现次数最多的 K 个。\n'
          '[内容受限，当前账户未获得该部分正文]\n'
          '然后是电面，问了一道系统设计。\n'
          '本帖最后由 楼主 于 2026-8-24 编辑\n'
          '求加米')

    def test_every_entry_quotes_its_own_post_verbatim(self):
        record = interview_record(self.OP, replies=[(9, '感觉后面应该还有 onsite，题目可能是 LRU。')])
        outline = build_outline(record)
        texts = {post['pid']: post['text'] for post in record['posts']}
        for entry in outline['rounds'] + outline['questions']:
            with self.subTest(entry=entry):
                self.assertIn(entry['excerpt'], texts[entry['pid']])  # nothing paraphrased, nothing invented
                self.assertEqual(entry['extraction'], 'rule')
        self.assertEqual([(r['round'], r['pid']) for r in outline['rounds'] if r['attribution'] == 'author'],
                         [('OA', 1), ('电面', 1), ('系统设计', 1)])

    def test_a_reply_is_never_the_authors_confirmed_experience(self):
        record = interview_record(self.OP, replies=[(9, '感觉后面应该还有 onsite，题目可能是 LRU。'),
                                                   (7, '补充：onsite 是三轮 coding。')])
        outline = build_outline(record)
        by_pid = {}
        for entry in outline['rounds']:
            by_pid.setdefault(entry['pid'], set()).add((entry['attribution'], entry['certainty']))
        self.assertEqual(by_pid[2], {('reply', 'speculated')})       # a guess by someone else
        self.assertEqual(by_pid[3], {('author', 'stated')})          # the poster returning to add facts
        self.assertTrue(all(e['attribution'] == 'author' for e in outline['rounds'] if e['pid'] == 1))
        # A reply merely chatting about "问题" without any round is not a question entry.
        chatter = interview_record('第一轮是 OA。', replies=[(9, '我觉得她就是有问题，请问你后面有 move forward 吗')])
        self.assertEqual([q['pid'] for q in build_outline(chatter)['questions']], [])

    def test_missing_is_declared_not_filled(self):
        outline = build_outline(interview_record('大家好，分享一下经历。', restricted=True))
        self.assertEqual(outline['rounds'], [])
        self.assertEqual(outline['questions'], [])
        self.assertIsNone(outline['role'])
        self.assertEqual(outline['missing'], ['rounds', 'questions', 'role', 'level', 'restricted_text'])
        self.assertEqual(outline['method'], 'rule_based_review_required')
        typed = build_outline(interview_record('第一轮是 OA。', role='SWE', level='Senior'))
        self.assertEqual((typed['role'], typed['level']), ('SWE', 'Senior'))
        self.assertNotIn('role', typed['missing'])

    def test_cjk_adjacent_round_words_are_found_and_the_result_is_deterministic(self):
        record = interview_record('Stripe OA真的是代码量巨大+ 题目又臭又长\n给定一周的时间轴，要求返回前 K 个窗口')
        first, second = build_outline(record), build_outline(record)
        self.assertEqual([r['round'] for r in first['rounds']], ['OA'])
        self.assertEqual([q['round'] for q in first['questions']], ['OA', 'OA'])
        self.assertEqual(first, second)
        self.assertEqual(first['restricted_segments'], 0)
