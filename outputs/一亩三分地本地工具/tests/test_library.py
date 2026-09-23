import asyncio
import io
import tempfile
import unittest
from pathlib import Path
import inspect
import json
import sqlite3
from unittest.mock import patch
import cli
import mcp_server
from library import Library, merge_pages, load_learned_answers, record_answer_outcome, export_library, organize_thread
from presentation import render_reader


class LibraryTests(unittest.TestCase):
    def test_pages_dedupe_overlapping_replies(self):
        first = {'tid': 123, 'title': 'Stripe', 'url': 'https://www.1point3acres.com/bbs/thread-123-1-1.html', 'expected_posts': 3, 'posts': [{'pid': 1, 'text': '主楼', 'restricted': False}, {'pid': 2, 'text': '回复', 'restricted': False}], 'next_url': 'page2'}
        second = dict(first, posts=[first['posts'][1], {'pid': 3, 'text': '最后回复', 'restricted': False}], next_url=None)
        result = merge_pages([first, second], 'Stripe')
        self.assertEqual(len(result['posts']), 3)
        self.assertTrue(result['complete'])

    def test_storage_update_keeps_one_thread_and_searches_chinese(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Library(Path(directory) / 'test.sqlite')
            page = {'tid': 123, 'title': 'Stripe OA', 'url': 'https://www.1point3acres.com/bbs/thread-123-1-1.html', 'expected_posts': 1, 'posts': [{'pid': 1, 'text': '滑动窗口实现', 'restricted': False}], 'next_url': None}
            row = merge_pages([page], 'Stripe')
            db.save(row)
            db.save(row)
            self.assertEqual(db.stats()['threads'], 1)
            self.assertEqual(len(db.search('滑动窗口')), 1)
            self.assertEqual(db.search("%' OR 1=1 --"), [])
            db.close()


class SearchFacetTests(unittest.TestCase):
    """Facets follow one contract rule and read the posting date, never the collection time (#24)."""

    URL = 'https://www.1point3acres.com/bbs/thread-%d-1-1.html'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'library.sqlite'
        self.db = Library(self.path)
        self.addCleanup(self.db.close)
        counters = {'views': 1, 'replies': 0, 'favorites': None}
        for tid, title, extra in [
                # Published 9-22 on the thread page although the listing showed 9-9: the page wins.
                (1, 'Synthetic SWE NG 电面', {'listed_date': '2026-9-9',
                    'stats': {**counters, 'fetched_at': '2026-09-23T00:00:00+00:00', 'published_at': '2026-9-22 13:21'}}),
                (2, 'Synthetic 面经', {'listed_date': '2026-9-9'}),
                # Collected on 9-15 but with no posting date at all.
                (3, 'Synthetic 无日期', {'stats': {**counters, 'fetched_at': '2026-09-15T00:00:00+00:00'}})]:
            page = {'tid': tid, 'title': title, 'url': self.URL % tid, 'expected_posts': 1, 'next_url': None,
                    'posts': [{'pid': 1, 'text': '滑动窗口 ' + title, 'restricted': False}], 'stats': extra.get('stats')}
            self.db.save({**merge_pages([page], 'Stripe'), **extra})

    def tids(self, query='', **facets):
        return [record['tid'] for record in self.db.search(query, **facets)]

    def test_role_and_level_match_exactly_and_unlabelled_is_a_real_value(self):
        self.assertEqual(self.tids(role='SWE'), [1])
        self.assertEqual(self.tids(level='New Grad'), [1])
        self.assertEqual(self.tids(role='未标注'), [3, 2])
        self.assertEqual(self.tids(role='swe'), [])
        self.assertEqual(self.tids(role='SWE', level='Senior'), [])
        self.assertEqual(self.tids('电面', role='SWE'), [1])
        self.assertEqual(self.tids('电面', role='未标注'), [])

    def test_date_bounds_are_inclusive_on_the_posting_date(self):
        self.assertEqual(self.tids(date_from='2026-09-22'), [1])
        self.assertEqual(self.tids(date_to='2026-09-09'), [2])
        self.assertEqual(self.tids(date_from='2026-09-09', date_to='2026-09-22'), [2, 1])
        self.assertEqual(self.tids(date_from='2026-09-23'), [])
        self.assertEqual(self.tids(date_to='2026-09-08'), [])

    def test_collection_time_never_satisfies_a_date_bound(self):
        # Thread 3 was collected on 9-15 and thread 1 on 9-23; neither moment is a posting date.
        self.assertEqual(self.tids(date_from='2026-09-10', date_to='2026-09-20'), [])
        self.assertEqual(self.tids(date_from='2026-09-23'), [])
        self.assertEqual(self.tids(), [3, 2, 1])

    def test_find_counts_matches_before_the_cut_and_echoes_the_filters(self):
        result = self.db.find('滑动', limit=1, role='未标注')
        self.assertEqual([record['tid'] for record in result['records']], [3])
        self.assertEqual((result['matched'], result['limit']), (2, 1))
        self.assertEqual(result['filters'], {'query': '滑动', 'company': 'Stripe', 'role': '未标注', 'level': None,
                                             'date_from': None, 'date_to': None, 'date_field': 'posting_date', 'include_ocr': False})

    def test_date_bounds_must_be_iso_dates(self):
        for bad in ['2026-9-9', '2026-02-30', 'yesterday', 20260909, '2026-09-09T00:00']:
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.db.find('', date_from=bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.db.find('', date_to=bad)
        self.assertEqual(self.db.find('', date_from='')['filters']['date_from'], None)

    def test_cli_and_mcp_pass_every_facet_through_to_the_same_rule(self):
        with patch('cli.Library', side_effect=lambda: Library(self.path)), \
                patch('sys.argv', ['cli', 'search', '', '--company', 'Stripe', '--role', 'SWE', '--level', 'New Grad',
                                   '--date-from', '2026-09-22', '--date-to', '2026-09-22']), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(), 0)
        payload = json.loads(out.getvalue())
        self.assertEqual([record['tid'] for record in payload['records']], [1])
        self.assertEqual(payload['matched'], 1)
        self.assertEqual(payload['filters']['date_field'], 'posting_date')
        with patch('mcp_server.Library', side_effect=lambda: Library(self.path)):
            tool = asyncio.run(mcp_server.server.call_tool('interviews_search', {
                'query': '', 'level': '未标注', 'date_from': '2026-09-01', 'date_to': '2026-09-09'}))
            self.assertFalse(tool.is_error)
            payload = json.loads(tool.content[0].text)
            self.assertEqual([record['tid'] for record in payload['records']], [2])
            self.assertEqual(payload['filters']['level'], '未标注')
            self.assertEqual(payload['stats']['threads'], 3)
            invalid = asyncio.run(mcp_server.server.call_tool('interviews_search', {'query': '', 'date_to': '2026-9-9'}))
            self.assertTrue(invalid.is_error)
            self.assertIn('invalid_date_to', invalid.content[0].text)
        with patch('cli.Library', side_effect=lambda: Library(self.path)), \
                patch('sys.argv', ['cli', 'search', '', '--date-from', '2026-9-9']), \
                patch('sys.stdout', new_callable=io.StringIO):
            with self.assertRaisesRegex(ValueError, 'invalid_date_from'):  # the entry point reports it as failed, exit 2
                cli.main()


class LearnedAnswerTests(unittest.TestCase):
    """The bank grows only from reward-backed evidence, and never inside tracked source (#66)."""

    def outcome(self, path, **kwargs):
        return record_answer_outcome('论坛大米有什么用？', '这些都可以', path, **kwargs)

    def test_a_confirmed_reward_teaches_the_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'learned-answers.json'
            self.outcome(path, rewarded=True, completed=True, response_seen=True)
            self.assertEqual(load_learned_answers(path)['verified'], {'论坛大米有什么用？': '这些都可以'})

    def test_an_answered_question_without_reward_is_recorded_as_wrong(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'learned-answers.json'
            self.outcome(path, rewarded=False, completed=True, response_seen=True)
            learned = load_learned_answers(path)
            self.assertEqual(learned['rejected'], {'论坛大米有什么用？': ['这些都可以']})
            self.assertEqual(learned['verified'], {})

    def test_a_request_the_site_never_answered_teaches_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'learned-answers.json'
            self.assertIsNone(self.outcome(path, rewarded=False, completed=False, response_seen=False))
            self.assertIsNone(self.outcome(path, rewarded=True, completed=True, response_seen=False))
            self.assertFalse(path.exists())

    def test_a_later_reward_clears_an_earlier_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'learned-answers.json'
            self.outcome(path, rewarded=False, completed=True, response_seen=True)
            self.outcome(path, rewarded=True, completed=True, response_seen=True)
            learned = load_learned_answers(path)
            self.assertEqual(learned['verified'], {'论坛大米有什么用？': '这些都可以'})
            self.assertNotIn('论坛大米有什么用？', learned['rejected'])

    def test_repeated_rejections_do_not_pile_up(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'learned-answers.json'
            for _ in range(3):
                self.outcome(path, rewarded=False, completed=True, response_seen=True)
            self.assertEqual(load_learned_answers(path)['rejected']['论坛大米有什么用？'], ['这些都可以'])

    def test_a_damaged_cache_never_stops_the_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'learned-answers.json'
            for broken in ['{malformed', '[]', '{"verified": 1, "rejected": {}}', '{}']:
                with self.subTest(broken=broken):
                    path.write_text(broken, encoding='utf-8')
                    self.assertEqual(load_learned_answers(path), {'verified': {}, 'rejected': {}})
            self.assertEqual(load_learned_answers(Path(directory) / 'absent.json'),
                             {'verified': {}, 'rejected': {}})

    def test_the_path_is_required_so_no_call_can_reach_real_state(self):
        # A default path resolved at import time made the test run write into the live state
        # directory; requiring it removes the hidden global rather than trusting callers.
        for function in [load_learned_answers, record_answer_outcome]:
            with self.subTest(function=function.__name__):
                self.assertIs(inspect.signature(function).parameters['path'].default,
                              inspect.Parameter.empty)

    def test_stored_shape_is_plain_json_for_offline_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'learned-answers.json'
            self.outcome(path, rewarded=True, completed=True, response_seen=True)
            self.assertEqual(set(json.loads(path.read_text(encoding='utf-8'))), {'verified', 'rejected'})
            self.assertFalse(path.with_suffix('.tmp').exists())


def synthetic_page(pid, next_url=None):
    return {'tid': 123, 'title': 'Stripe', 'url': 'https://www.1point3acres.com/bbs/thread-123-1-1.html',
            'expected_posts': 2, 'posts': [{'pid': pid, 'text': f'post {pid}', 'restricted': False}],
            'next_url': next_url}


class Tripwire:
    """A connection stand-in that fails on one chosen statement, in the middle of the transaction."""

    def __init__(self, connection, trip):
        self.connection, self.trip = connection, trip

    def execute(self, sql, *args):
        if self.trip in sql:
            raise sqlite3.OperationalError('injected mid-transaction fault')
        return self.connection.execute(sql, *args)

    def __enter__(self):
        return self.connection.__enter__()

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class AtomicPageSaveTests(unittest.TestCase):
    """Content, version and resume position move together or not at all (#19)."""

    NEXT = 'https://www.1point3acres.com/bbs/thread-123-2-1.html'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'library.sqlite'

    def snapshot(self):
        db = Library(self.path)
        try:
            record = db.get(123)
            versions = db.db.execute('SELECT count(*) FROM versions WHERE tid=123').fetchone()[0]
            return {'pids': [p['pid'] for p in record['posts']] if record else None,
                    'versions': versions, 'progress': db.progress(123)}
        finally:
            db.close()

    def test_one_commit_carries_content_version_and_position(self):
        db = Library(self.path)
        try:
            first = [synthetic_page(1, self.NEXT)]
            db.save_page(merge_pages(first, 'Stripe'), first)
        finally:
            db.close()
        self.assertEqual(self.snapshot(), {'pids': [1], 'versions': 1, 'progress': first})

    def test_a_fault_inside_the_transaction_rolls_all_three_back(self):
        first = [synthetic_page(1, self.NEXT)]
        db = Library(self.path)
        try:
            db.save_page(merge_pages(first, 'Stripe'), first)
            both = first + [synthetic_page(2)]
            db.db = Tripwire(db.db, 'progress')  # content and version already written inside the same transaction
            with self.assertRaises(sqlite3.OperationalError):
                db.save_page(merge_pages(both, 'Stripe'), both)
        finally:
            db.close()
        # Reopened from disk: the thread row, the version row and the resume position are all still page one.
        self.assertEqual(self.snapshot(), {'pids': [1], 'versions': 1, 'progress': first})

    def test_a_finished_record_clears_its_position_in_the_same_commit(self):
        first = [synthetic_page(1, self.NEXT)]
        both = first + [synthetic_page(2)]
        db = Library(self.path)
        try:
            db.save_page(merge_pages(first, 'Stripe'), first)
            db.save_page(merge_pages(both, 'Stripe'), both)
        finally:
            db.close()
        self.assertEqual(self.snapshot(), {'pids': [1, 2], 'versions': 2, 'progress': []})

    def test_an_incomplete_refresh_still_cannot_overwrite_a_complete_record(self):
        both = [synthetic_page(1, self.NEXT), synthetic_page(2)]
        db = Library(self.path)
        try:
            db.save_page(merge_pages(both, 'Stripe'), both)
            partial = [synthetic_page(1, self.NEXT)]
            db.save_page(merge_pages(partial, 'Stripe'), partial)
            self.assertEqual([p['pid'] for p in db.get(123)['posts']], [1, 2])
            self.assertEqual(db.progress(123), partial)  # the position advances, the published content does not regress
        finally:
            db.close()

    def test_queue_state_goes_through_the_library_not_raw_sql(self):
        db = Library(self.path)
        try:
            db.enqueue([{'tid': 123, 'url': 'https://www.1point3acres.com/bbs/thread-123-1-1.html'}], 'Stripe')
            db.set_queue_state(123, 'failed', 'network_timeout')
            self.assertEqual(db.db.execute('SELECT status, error FROM queue WHERE tid=123').fetchone(), ('failed', 'network_timeout'))
            db.set_queue_state(123, 'complete')
            self.assertEqual(db.db.execute('SELECT status, error FROM queue WHERE tid=123').fetchone(), ('complete', None))
        finally:
            db.close()


class LegacyRecordCompatTests(unittest.TestCase):
    """Records saved before #22 have no author/stats/media fields; every consumer must still work."""

    def test_old_records_export_and_render_without_the_new_fields(self):
        legacy = {'tid': 321, 'company': 'Stripe', 'title': '旧记录', 'url': 'https://www.1point3acres.com/bbs/thread-321-1-1.html',
                  'role': '未标注', 'level': '未标注', 'tags': [], 'summary': '旧', 'content_status': 'visible',
                  'pagination_complete': True, 'complete': True, 'expected_posts': 1, 'pages_fetched': 1, 'next_url': None,
                  'images_included': False, 'fetched_at': '2026-09-01T00:00:00+00:00', 'content_hash': 'x',
                  'posts': [{'pid': 1, 'text': '旧正文', 'restricted': False}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'library.sqlite'
            db = Library(path)
            try:
                db.save(legacy)
            finally:
                db.close()
            with patch('library.Library', side_effect=lambda: Library(path)):
                export_library(Path(directory) / 'out', company='Stripe')
            markdown = (Path(directory) / 'out' / '面经.md').read_text(encoding='utf-8')
            self.assertIn('旧正文', markdown)
            self.assertNotIn('作者：', markdown)
            payload = json.loads((Path(directory) / 'out' / '面经.json').read_text(encoding='utf-8'))
            self.assertIsNone(payload['records'][0].get('stats'))
            self.assertTrue(render_reader(payload))

    def test_new_records_carry_stats_through_merge_and_export(self):
        page = {'tid': 322, 'title': '新记录', 'url': 'https://www.1point3acres.com/bbs/thread-322-1-1.html', 'expected_posts': 1,
                'posts': [{'pid': 2, 'text': '新正文', 'restricted': False,
                           'author': {'uid': 7, 'name': '作者甲', 'anonymous': False}}],
                'next_url': None, 'stats': {'views': 5, 'replies': 0, 'favorites': None, 'fetched_at': 'T'}}
        record = merge_pages([page], 'Stripe')
        self.assertEqual(record['stats']['views'], 5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'library.sqlite'
            db = Library(path)
            try:
                db.save(record)
            finally:
                db.close()
            with patch('library.Library', side_effect=lambda: Library(path)):
                export_library(Path(directory) / 'out', company='Stripe')
            markdown = (Path(directory) / 'out' / '面经.md').read_text(encoding='utf-8')
            self.assertIn('作者：作者甲（uid 7）', markdown)


class OrganizeThreadTests(unittest.TestCase):
    """Outlines live beside the record, follow its content hash, and never touch it (#23)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'library.sqlite'

    def stored(self, tid=555):
        db = Library(self.path)
        try:
            return db.get(tid), db.outline_versions(tid)
        finally:
            db.close()

    def record(self, text, content_hash):
        return {'tid': 555, 'company': 'Synthetic Co', 'title': 'Synthetic 面经', 'url': 'https://www.1point3acres.com/bbs/thread-555-1-1.html',
                'role': '未标注', 'level': '未标注', 'tags': [], 'summary': '', 'content_status': 'visible',
                'pagination_complete': True, 'complete': True, 'expected_posts': 1, 'pages_fetched': 1, 'next_url': None,
                'images_included': False, 'fetched_at': 'T', 'content_hash': content_hash,
                'posts': [{'pid': 1, 'text': text, 'restricted': False, 'author': {'uid': 7, 'name': '楼主', 'anonymous': False}}]}

    def organize(self, **kwargs):
        with patch('library.Library', side_effect=lambda: Library(self.path)):
            return organize_thread('555', **kwargs)

    def test_outline_is_built_stored_and_reused_without_changing_the_record(self):
        db = Library(self.path)
        try:
            db.save(self.record('第一轮是 OA。', 'hash-1'))
        finally:
            db.close()
        before, _ = self.stored()
        first = self.organize()
        self.assertEqual((first['status'], first['reused'], first['error']), ('needs_attention', False, None))
        self.assertEqual([r['round'] for r in first['outline']['rounds']], ['OA'])
        self.assertIn('questions', first['outline']['missing'])
        again = self.organize()
        self.assertTrue(again['reused'])
        self.assertEqual(again['outline'], first['outline'])
        after, versions = self.stored()
        self.assertEqual(after, before)  # the original is never replaced by its summary
        self.assertEqual(versions, ['hash-1'])
        self.assertFalse(self.organize(refresh=True)['reused'])

    def test_a_changed_original_yields_a_new_outline_and_keeps_the_old_version(self):
        db = Library(self.path)
        try:
            db.save(self.record('第一轮是 OA。', 'hash-1'))
        finally:
            db.close()
        self.organize()
        db = Library(self.path)
        try:
            db.save(self.record('第一轮是 OA，然后电面。', 'hash-2'))
        finally:
            db.close()
        updated = self.organize()
        self.assertFalse(updated['reused'])
        self.assertEqual([r['round'] for r in updated['outline']['rounds']], ['OA', '电面'])
        self.assertEqual(updated['superseded_versions'], ['hash-1'])
        self.assertEqual(self.stored()[1], ['hash-1', 'hash-2'])

    def test_unknown_thread_and_bad_input_fail_cleanly(self):
        missing = self.organize()
        self.assertEqual((missing['status'], missing['error'], missing['outline']), ('failed', 'thread_not_in_library', None))
        with patch('library.Library', side_effect=lambda: Library(self.path)):
            self.assertEqual(organize_thread('555', refresh='yes')['error'], 'invalid_refresh_flag')
            self.assertEqual(organize_thread('https://evil.example.com/bbs/thread-1-1-1.html')['status'], 'failed')
