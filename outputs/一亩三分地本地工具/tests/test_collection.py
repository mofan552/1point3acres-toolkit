import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cli
import mcp_server
import asyncio
from mcp.server.mcpserver.exceptions import ToolError
from library import Library, collect_stripe, collect_company, merge_pages, save_thread, export_library, get_thread_detail
from settings import SITE

URL = 'https://www.1point3acres.com/bbs/thread-123-1-1.html'
NEXT = URL.replace('-1-1.html', '-2-1.html')
LISTING = {'threads': [{'tid': 123, 'title': 'Stripe', 'url': URL}], 'next_url': None}


def page(pid, next_url=None, restricted=True):
    return {'tid': 123, 'title': 'Stripe', 'url': URL, 'expected_posts': 2,
            'posts': [{'pid': pid, 'text': 'content', 'restricted': restricted}], 'next_url': next_url}


class FakeBrowser:
    def __init__(self, fail_second=False):
        self.fail_second = fail_second
        self.thread_reads = []
        self.sb = self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def sleep(self, _):
        pass

    def account(self):
        return {'uid': 1}

    def read_html(self, url):
        if 'tag/' in url:
            return 'listing'
        self.thread_reads.append(url)
        if url == NEXT and self.fail_second:
            raise RuntimeError('page_two_unavailable')
        return page(2) if url == NEXT else page(1, NEXT)


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite'
        self.addCleanup(self.temp.cleanup)

    def run_collection(self, browser, **kwargs):
        with patch('library.Library', side_effect=lambda: Library(self.path)), \
                patch('library.Browser', return_value=browser), \
                patch('library.parse_listing', return_value=LISTING), \
                patch('library.parse_thread', side_effect=lambda html, url: html):
            return collect_stripe(**kwargs)

    def get_record(self):
        db = Library(self.path)
        try:
            return db.get(123)
        finally:
            db.close()

    def test_failed_refresh_preserves_published_replies(self):
        db = Library(self.path)
        try:
            db.save(merge_pages([page(1, NEXT), page(2)], 'Stripe'))
        finally:
            db.close()
        result = self.run_collection(FakeBrowser(fail_second=True), refresh=True)
        self.assertEqual(len(self.get_record()['posts']), 2)
        self.assertEqual(result['status'], 'failed')

    def test_incomplete_restricted_thread_resumes_missing_page(self):
        first = self.run_collection(FakeBrowser(fail_second=True))
        self.assertEqual(first['status'], 'failed')
        browser = FakeBrowser()
        result = self.run_collection(browser)
        self.assertEqual(browser.thread_reads, [NEXT])
        self.assertEqual(len(self.get_record()['posts']), 2)
        self.assertTrue(self.get_record()['pagination_complete'])
        self.assertEqual(result['status'], 'complete')

    def test_page_cap_is_pending_and_resumable(self):
        result = self.run_collection(FakeBrowser(), max_thread_pages=1)
        self.assertEqual(result['status'], 'needs_attention')
        browser = FakeBrowser()
        self.run_collection(browser)
        self.assertEqual(browser.thread_reads, [NEXT])

    def test_empty_selection_is_not_success(self):
        with patch('library.Library', side_effect=lambda: Library(self.path)), \
                patch('library.Browser', return_value=FakeBrowser()), \
                patch('library.parse_listing', return_value={'threads': [], 'next_url': None}):
            self.assertEqual(collect_stripe()['status'], 'needs_attention')

    def test_cli_collection_failure_has_nonzero_exit(self):
        with patch('sys.argv', ['cli.py', 'collect-stripe']), \
                patch('cli.collect_stripe', return_value={'status': 'failed'}), \
                patch('cli.export_library', return_value={}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 2)

    def test_mcp_failed_payload_has_error_flag(self):
        with patch('mcp_server.run_daily', return_value={'status': 'failed', 'error': 'automatic_login_failed'}):
            result = mcp_server.daily_status()
        self.assertTrue(result.is_error)
        self.assertEqual(json.loads(result.content[0].text)['error'], 'automatic_login_failed')


if __name__ == '__main__':
    unittest.main()


class SaveThreadTests(unittest.TestCase):
    """Keeping one chosen thread: explicit, idempotent, resumable, never a quiet relabel (#20)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite'
        self.addCleanup(self.temp.cleanup)

    def keep(self, browser, thread=URL, **kwargs):
        with patch('library.Library', side_effect=lambda: Library(self.path)), \
                patch('library.Browser', return_value=browser), \
                patch('library.parse_thread', side_effect=lambda html, url: html):
            return save_thread(thread, **kwargs)

    def stored(self):
        db = Library(self.path)
        try:
            return db.get(123), db.progress(123)
        finally:
            db.close()

    def test_saved_thread_is_searchable_and_exported_without_a_company(self):
        result = self.keep(FakeBrowser())
        self.assertEqual((result['status'], result['tid'], result['reused'], result['collection_status']), ('complete', 123, False, 'restricted'))
        self.assertIsNone(result['record']['company'])
        self.assertIsNone(result['company_source'])
        self.assertEqual(result['resume'], {'pages_saved': 0, 'next_url': None})
        db = Library(self.path)
        try:
            self.assertEqual([r['tid'] for r in db.search('content', company='')], [123])
            self.assertEqual(db.search('content'), [])  # the Stripe default filter does not claim it
        finally:
            db.close()
        with patch('library.Library', side_effect=lambda: Library(self.path)):
            exported = export_library(Path(self.temp.name) / 'out', company='')
        payload = json.loads((Path(self.temp.name) / 'out' / '面经.json').read_text(encoding='utf-8'))
        self.assertEqual([r['tid'] for r in payload['records']], [123])

    def test_a_complete_record_is_reused_without_opening_the_browser(self):
        self.keep(FakeBrowser())
        with patch('library.Library', side_effect=lambda: Library(self.path)), patch('library.Browser') as opened:
            again = save_thread(URL)
        opened.assert_not_called()
        self.assertTrue(again['reused'])
        self.assertEqual(again['status'], 'complete')
        db = Library(self.path)
        try:
            self.assertEqual(db.db.execute('SELECT count(*) FROM threads WHERE tid=123').fetchone()[0], 1)
        finally:
            db.close()

    def test_a_failed_page_leaves_a_resumable_position_and_no_false_completeness(self):
        first = self.keep(FakeBrowser(fail_second=True))
        self.assertEqual(first['status'], 'needs_attention')
        self.assertEqual(first['error'], 'page_two_unavailable')
        self.assertEqual(first['collection_status'], 'partial_pages')
        self.assertEqual(first['resume'], {'pages_saved': 1, 'next_url': NEXT})
        record, progress = self.stored()
        self.assertEqual([p['pid'] for p in record['posts']], [1])
        self.assertEqual(progress[-1]['next_url'], NEXT)
        browser = FakeBrowser()
        resumed = self.keep(browser)
        self.assertEqual(browser.thread_reads, [NEXT])  # picks up at page two, does not restart
        self.assertEqual(resumed['status'], 'complete')
        self.assertEqual([p['pid'] for p in resumed['record']['posts']], [1, 2])

    def test_an_incomplete_refresh_never_overwrites_the_complete_record(self):
        self.keep(FakeBrowser())
        refreshed = self.keep(FakeBrowser(fail_second=True), refresh=True)
        self.assertEqual(refreshed['status'], 'needs_attention')
        record, _ = self.stored()
        self.assertEqual([p['pid'] for p in record['posts']], [1, 2])

    def test_company_is_metadata_the_caller_vouches_for(self):
        labelled = self.keep(FakeBrowser(), company='Synthetic Inc')
        self.assertEqual((labelled['record']['company'], labelled['company_source']), ('Synthetic Inc', 'caller'))
        unlabelled_resave = self.keep(FakeBrowser(), refresh=True)
        self.assertEqual((unlabelled_resave['record']['company'], unlabelled_resave['company_source']), ('Synthetic Inc', 'caller'))
        relabelled = self.keep(FakeBrowser(), company='Other Co')
        self.assertTrue(relabelled['reused'])
        self.assertEqual(relabelled['record']['company'], 'Other Co')
        for bad in ['', '  ', ' padded']:
            with self.subTest(company=bad), patch('library.Browser') as opened:
                with patch('library.Library', side_effect=lambda: Library(self.path)):
                    result = save_thread(URL, company=bad)
                opened.assert_not_called()
                self.assertEqual(result['error'], 'invalid_company_label')

    def test_refused_targets_and_budgets_never_open_the_browser(self):
        for kwargs, thread, expected in [({}, 'https://evil.example.com/bbs/thread-1-1-1.html', None),
                                         ({'max_thread_pages': 0}, URL, 'invalid_thread_page_budget'),
                                         ({'max_thread_pages': 999}, URL, 'invalid_thread_page_budget')]:
            with self.subTest(thread=thread, kwargs=kwargs), patch('library.Browser') as opened:
                with patch('library.Library', side_effect=lambda: Library(self.path)):
                    result = save_thread(thread, **kwargs)
                opened.assert_not_called()
                self.assertEqual(result['status'], 'failed')
                if expected:
                    self.assertEqual(result['error'], expected)

    def test_cli_and_mcp_expose_the_save_and_snapshot_stays_read_only(self):
        payload = {'status': 'complete', 'tid': 123, 'reused': False, 'collection_status': 'visible', 'record': {'tid': 123},
                   'resume': {'pages_saved': 0, 'next_url': None}, 'company_source': 'caller',
                   'scope': 'currently_visible_content', 'error': None}
        with patch('cli.save_thread', return_value=payload) as called, \
                patch('sys.argv', ['cli', 'save-thread', URL, '--company', 'Synthetic Inc', '--refresh']), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(), 0)
        self.assertEqual(json.loads(out.getvalue())['tid'], 123)
        self.assertEqual((called.call_args.kwargs['company'], called.call_args.kwargs['refresh']), ('Synthetic Inc', True))
        with patch('mcp_server.keep_thread', return_value=payload):
            tool = asyncio.run(mcp_server.server.call_tool('save_thread', {'thread': URL}))
        self.assertEqual(json.loads(tool.content[0].text)['tid'], 123)
        with patch('library.Browser', side_effect=AssertionError('schema rejects must not browse')):
            for arguments in [{'thread': 123}, {'thread': URL, 'refresh': 'yes'}, {'thread': URL, 'max_thread_pages': True}]:
                with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                    asyncio.run(mcp_server.server.call_tool('save_thread', arguments))
        # The snapshot reader must not have grown a write path.
        with patch('library.Library', side_effect=AssertionError('get_thread_detail must not open the library')), \
                patch('library.Browser', return_value=FakeBrowser()), \
                patch('library.parse_thread', side_effect=lambda html, url: html):
            self.assertEqual(get_thread_detail(URL)['status'], 'complete')


def thread_url(tid):
    return f'https://www.1point3acres.com/bbs/thread-{tid}-1-1.html'


def found(*rows, error=None, truncated=None):
    return {'status': 'failed' if error else 'complete', 'query': 'q', 'threads': list(rows), 'pages_fetched': 1,
            'site_reported_total': len(rows), 'pagination_complete': True, 'next_url': None,
            'omitted_on_last_page': 0, 'truncated_reason': truncated, 'scope': 'currently_visible_content', 'error': error}


def hit(tid, title):
    return {'tid': tid, 'title': title, 'url': thread_url(tid), 'summary': '', 'author': None, 'listed_date': None}


class MultiBrowser(FakeBrowser):
    """Serves a one-page thread for any tid; a chosen tid fails instead."""

    def __init__(self, failing=()):
        super().__init__()
        self.failing = set(failing)

    def read_html(self, url):
        if 'tag/' in url:
            return 'listing'
        self.thread_reads.append(url)
        tid = int(url.split('thread-')[1].split('-')[0])
        if tid in self.failing:
            raise RuntimeError('thread_unavailable')
        return {'tid': tid, 'title': f'Thread {tid}', 'url': thread_url(tid), 'expected_posts': 1,
                'posts': [{'pid': tid * 10, 'text': f'body {tid}', 'restricted': False}], 'next_url': None}


class CollectCompanyTests(unittest.TestCase):
    """A caller-named company, attributed by evidence, never by the request (#21)."""

    COMPANY = 'Synthetic Co'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite'
        self.addCleanup(self.temp.cleanup)

    def collect(self, browser, search=None, listing_rows=None, **kwargs):
        patches = [patch('library.Library', side_effect=lambda: Library(self.path)),
                   patch('library.Browser', return_value=browser),
                   patch('library.parse_thread', side_effect=lambda html, url: html)]
        if search is not None:
            patches.append(patch('library.search_threads', return_value=search))
        if listing_rows is not None:
            patches.append(patch('library.parse_listing', return_value={'threads': listing_rows, 'next_url': None}))
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            return collect_company(self.COMPANY, **kwargs)

    def companies(self):
        db = Library(self.path)
        try:
            return {row['tid']: (row.get('company'), row.get('company_source'), row.get('discovery'))
                    for row in db.search('', company='', limit=50)}
        finally:
            db.close()

    def test_search_hits_are_attributed_only_when_the_title_names_the_company(self):
        search = found(hit(123, 'Synthetic Co 电面经验'), hit(124, '某公司面经，顺带提到 synthetic'), hit(123, '重复'))
        result = self.collect(MultiBrowser(), search=search)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['discovery'], {'source': 'site_search', 'query': self.COMPANY, 'truncated_reason': None,
                                               'candidates': 2, 'selected': 2})
        by_tid = {row['tid']: row for row in result['results']}
        self.assertEqual(sorted(by_tid), [123, 124])
        self.assertEqual((by_tid[123]['company_match'], by_tid[124]['company_match']), ('title', 'unconfirmed'))
        self.assertEqual(by_tid[123]['discovery'], {'source': 'site_search', 'query': self.COMPANY})
        stored = self.companies()
        self.assertEqual(stored[123][:2], (self.COMPANY, 'title'))
        self.assertEqual(stored[124][:2], (None, None))  # unconfirmed stays unlabelled
        self.assertEqual(stored[124][2], {'source': 'site_search', 'query': self.COMPANY})
        db = Library(self.path)
        try:
            self.assertEqual([r['tid'] for r in db.search('', company=self.COMPANY)], [123])
        finally:
            db.close()

    def test_a_company_tag_page_attributes_its_rows_and_stripe_is_the_same_path(self):
        listing = SITE + '/bbs/tag/synthetic-9-1.html'
        rows = [{'tid': 201, 'title': 'tag row', 'url': thread_url(201), 'listed_date': '2026-9-1'}]
        result = self.collect(MultiBrowser(), listing=listing, listing_rows=rows)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['results'][0]['company_match'], 'tag')
        self.assertEqual(self.companies()[201][:2], (self.COMPANY, 'tag'))
        with patch('library.Library', side_effect=lambda: Library(self.path)), \
                patch('library.Browser', return_value=FakeBrowser()), \
                patch('library.parse_listing', return_value=LISTING), \
                patch('library.parse_thread', side_effect=lambda html, url: html):
            stripe = collect_stripe()
        self.assertEqual(stripe['company'], 'Stripe')
        self.assertTrue(stripe['discovery']['url'].endswith('/bbs/tag/stripe-2126-1.html'))
        self.assertEqual(self.companies()[123][:2], ('Stripe', 'tag'))

    def test_one_failed_thread_never_lets_the_batch_claim_completion(self):
        search = found(hit(123, 'Synthetic Co A'), hit(124, 'Synthetic Co B'))
        result = self.collect(MultiBrowser(failing=[124]), search=search)
        self.assertEqual(result['status'], 'needs_attention')
        by_tid = {row['tid']: row for row in result['results']}
        self.assertEqual(by_tid[123]['status'], 'complete')
        self.assertEqual((by_tid[124]['status'], by_tid[124]['error']), ('failed', 'thread_unavailable'))
        self.assertNotIn(124, self.companies())
        self.assertIsNone(result['error'])

    def test_discovery_failure_and_bad_inputs_stop_before_the_browser(self):
        with patch('library.Library', side_effect=lambda: Library(self.path)), patch('library.Browser') as opened, \
                patch('library.search_threads', return_value=found(error='network_timeout')):
            result = collect_company(self.COMPANY)
        opened.assert_not_called()
        self.assertEqual((result['status'], result['error'], result['results']), ('failed', 'network_timeout', []))
        cases = [({'company': ''}, 'invalid_company_label'), ({'company': ' padded'}, 'invalid_company_label'),
                 ({'company': self.COMPANY, 'query': 'x', 'listing': SITE + '/bbs/tag/x-1-1.html'}, 'invalid_discovery_input'),
                 ({'company': self.COMPANY, 'listing': 'https://evil.example.com/bbs/tag/x-1-1.html'}, 'invalid_listing_reference'),
                 ({'company': self.COMPANY, 'listing': SITE + '/bbs/forum-472-1.html'}, 'invalid_listing_reference'),
                 ({'company': self.COMPANY, 'refresh': 'yes'}, 'invalid_collection_budget'),
                 ({'company': self.COMPANY, 'max_thread_pages': 0}, 'invalid_collection_budget')]
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs), patch('library.Browser') as opened, \
                    patch('library.search_threads', side_effect=AssertionError('must not search')):
                with patch('library.Library', side_effect=lambda: Library(self.path)):
                    result = collect_company(**kwargs)
                opened.assert_not_called()
                self.assertEqual((result['status'], result['error']), ('failed', expected))
                self.assertEqual(result['results'], [])

    def test_the_result_limit_bounds_the_selection(self):
        search = found(hit(123, 'Synthetic Co A'), hit(124, 'Synthetic Co B'), hit(125, 'Synthetic Co C'))
        result = self.collect(MultiBrowser(), search=search, limit=2)
        self.assertEqual(result['discovery']['selected'], 2)
        self.assertEqual(sorted(row['tid'] for row in result['results']), [123, 124])

    def test_cli_and_mcp_expose_the_generic_collection_and_stripe_stays(self):
        payload = {'status': 'complete', 'company': self.COMPANY, 'discovery': {'source': 'site_search', 'query': self.COMPANY},
                   'results': [], 'stats': {}, 'account': None, 'scope': 'currently_visible_content', 'error': None}
        with patch('cli.collect_company', return_value=dict(payload)) as called, patch('cli.export_library', return_value={}), \
                patch('sys.argv', ['cli', 'collect', self.COMPANY, '--query', 'synthetic 面经', '--limit', '3', '--refresh']), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(), 0)
        self.assertEqual(json.loads(out.getvalue())['company'], self.COMPANY)
        self.assertEqual((called.call_args.kwargs['query'], called.call_args.kwargs['limit'], called.call_args.kwargs['refresh']),
                         ('synthetic 面经', 3, True))
        with patch('mcp_server.gather_company', return_value=payload):
            tool = asyncio.run(mcp_server.server.call_tool('collect_company', {'company': self.COMPANY}))
        self.assertEqual(json.loads(tool.content[0].text)['discovery']['source'], 'site_search')
        with patch('library.Browser', side_effect=AssertionError('schema rejects must not browse')):
            for arguments in [{'company': 5}, {'company': self.COMPANY, 'limit': True}, {'company': self.COMPANY, 'refresh': 'no'}]:
                with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                    asyncio.run(mcp_server.server.call_tool('collect_company', arguments))
        tools = [tool.name for tool in asyncio.run(mcp_server.server.list_tools())]
        self.assertIn('stripe_collect', tools)
