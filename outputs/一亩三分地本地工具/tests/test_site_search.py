import asyncio
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import cli
import extract
import browser
from tests.test_extract import board_html as extract_board_html, profile_html, profile_threads_html, notice_html, favorites_html
import library
import mcp_server
from mcp.server.mcpserver.exceptions import ToolError
from settings import SITE


FIRST = SITE + '/bbs/search.php?mod=forum&searchid=88&orderby=lastpost&ascdesc=desc&searchsubmit=yes&kw=Stripe'
SECOND = SITE + '/bbs/search.php?mod=forum&searchid=88&orderby=lastpost&ascdesc=desc&searchsubmit=yes&page=2'


def results_html(query='Stripe', ids=(123, 124), *, total=3, page=1, next_url=SECOND):
    rows = ''.join(
        f'<li class="pbw" id="{tid}"><h3><a href="forum.php?mod=viewthread&amp;tid={tid}">'
        f'Synthetic <strong>{query}</strong> {tid}</a></h3><p class="xg1">1 个回复 - 2 次查看</p>'
        '<p>合成摘要</p><p><span>2026-01-01 10:00</span> - '
        '<span><a href="space-uid-999.html">Synthetic reader</a></span> - '
        '<span><a href="forum-145-1.html">Synthetic forum</a></span></p></li>' for tid in ids)
    next_anchor = f'<a class="nxt" href="{next_url}">下一页</a>' if next_url else ''
    return (f'<div class="sttl"><h2>结果: <em>找到 “<span class="emfont">{query}</span>” '
            f'相关内容 {total} 个</em></h2></div>'
            + (f'<div class="slst"><ul>{rows}</ul></div>' if rows else '')
            + (f'<div class="pg"><strong>{page}</strong>{next_anchor}</div>' if rows else ''))


class SearchBrowser:
    def __init__(self, first=None, second=None):
        self.first = first if first is not None else results_html()
        self.second = second if second is not None else results_html(ids=(124, 125), page=2, next_url=None)
        self.reads = []
        self.sb = self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def sleep(self, _):
        pass

    def read_html(self, url):
        self.reads.append(url)
        if len(self.reads) == 1:
            self.last_read = {'url': FIRST}
            return self.first
        if isinstance(self.second, Exception):
            raise self.second
        self.last_read = {'url': SECOND}
        return self.second


class SiteSearchTests(unittest.TestCase):
    def search(self, browser, query='Stripe', **kwargs):
        with patch('library.Browser', return_value=browser), \
                patch('library.Library', side_effect=AssertionError('Online search must not open the database')):
            return library.search_threads(query, **kwargs)

    def test_chinese_is_encoded_for_native_search_and_results_feed_detail(self):
        browser = SearchBrowser(first=results_html('面经', ids=(123,), total=1, next_url=None))
        result = self.search(browser, '  面经  ')
        self.assertEqual(browser.reads, [SITE + '/bbs/search.php?mod=forum&srchtxt=%C3%E6%BE%AD&searchsubmit=yes'])
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['pagination_complete'])
        row = result['threads'][0]
        self.assertEqual(row, {'tid': 123, 'title': 'Synthetic 面经 123',
            'url': SITE + '/bbs/thread-123-1-1.html', 'summary': '合成摘要',
            'author': 'Synthetic reader', 'listed_date': '2026-01-01 10:00'})
        self.assertEqual(extract.parse_thread_reference(row['url'])['tid'], 123)

    def test_contiguous_pages_deduplicate_without_reordering(self):
        browser = SearchBrowser()
        result = self.search(browser)
        self.assertEqual(browser.reads[1:], [SECOND])
        self.assertEqual([row['tid'] for row in result['threads']], [123, 124, 125])
        self.assertEqual(result['pages_fetched'], 2)
        self.assertEqual(result['site_reported_total'], 3)
        self.assertTrue(result['pagination_complete'])

    def test_bounds_do_not_silently_drop_unreturned_rows(self):
        browser = SearchBrowser()
        result = self.search(browser, limit=1)
        self.assertEqual(len(browser.reads), 1)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['truncated_reason'], 'result_limit')
        self.assertEqual(result['omitted_on_last_page'], 1)
        self.assertEqual(result['next_url'], SECOND)
        self.assertFalse(result['pagination_complete'])
        result = self.search(SearchBrowser(), list_pages=1)
        self.assertEqual(result['truncated_reason'], 'page_limit')

    def test_zero_requires_matching_query_and_explicit_zero_count(self):
        result = self.search(SearchBrowser(first=results_html(ids=(), total=0, next_url=None)))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['threads'], [])
        self.assertTrue(result['pagination_complete'])
        for html in [results_html(ids=(), total=3, next_url=None),
                     results_html('other', ids=(), total=0, next_url=None),
                     '<html><body>Unknown response</body></html>']:
            with self.subTest(html=html):
                result = self.search(SearchBrowser(first=html))
                self.assertEqual(result['status'], 'failed')
                self.assertIsNotNone(result['error'])

    def test_access_failures_cannot_be_empty_success(self):
        cases = [('<form id="challenge-form"></form>', 'search_access_challenge'),
                 ('<div id="messagetext">请先登录后使用搜索</div>', 'login_required'),
                 ('<div id="messagetext">两次搜索间隔不得少于 30 秒</div>', 'search_rate_limited'),
                 ('<div id="messagetext">搜索需要消耗积分，请购买</div>', 'search_payment_required')]
        for html, error in cases:
            with self.subTest(error=error):
                result = self.search(SearchBrowser(first=html))
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['threads'], [])
                self.assertEqual(result['error'], error)

    def test_missing_metadata_stays_unknown(self):
        html = ('<div class="sttl">找到 “<span class="emfont">Stripe</span>” 相关内容 1 个</div>'
                '<div class="slst"><li class="pbw" id="123"><h3><a href="thread-123-1-1.html">'
                'Synthetic Stripe</a></h3></li></div>')
        row = self.search(SearchBrowser(first=html))['threads'][0]
        self.assertIsNone(row['author'])
        self.assertIsNone(row['listed_date'])
        self.assertIsNone(row['summary'])
        without_date = results_html(ids=(123,), total=1, next_url=None).replace('<span>2026-01-01 10:00</span> - ', '')
        row = self.search(SearchBrowser(first=without_date))['threads'][0]
        self.assertEqual(row['author'], 'Synthetic reader')
        self.assertIsNone(row['listed_date'])

    def test_partial_failure_keeps_verified_rows(self):
        result = self.search(SearchBrowser(second=RuntimeError('thread_http_503')))
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual([row['tid'] for row in result['threads']], [123, 124])
        self.assertEqual(result['error'], 'thread_http_503')
        self.assertEqual(result['next_url'], SECOND)
        self.assertFalse(result['pagination_complete'])

    def test_untrusted_pagination_is_not_fetched(self):
        for target in [FIRST, SECOND.replace('88', '89'), SECOND.replace('page=2', 'page=3'),
                       SECOND.replace(SITE, 'https://example.org'),
                       SECOND.replace('mod=forum', 'mod=post'), SECOND + '&page=2',
                       SECOND + '&formhash=synthetic', SECOND.replace('https:', 'http:')]:
            with self.subTest(target=target):
                browser = SearchBrowser(first=results_html(next_url=target))
                result = self.search(browser)
                self.assertEqual(len(browser.reads), 1)
                self.assertEqual(result['status'], 'needs_attention')
                self.assertIsNotNone(result['error'])

    def test_redirected_or_repeated_document_is_not_accepted(self):
        cases = [results_html(page=1, next_url=None),
                 results_html(page=2, next_url=None).replace('<div class="pg"><strong>2</strong></div>', ''),
                 results_html(ids=(), total=0, next_url=None),
                 results_html(ids=(125,), total=4, page=2, next_url=None)]
        for html in cases:
            with self.subTest(html=html):
                result = self.search(SearchBrowser(second=html))
                self.assertEqual(result['status'], 'needs_attention')
                self.assertEqual(result['pages_fetched'], 1)
                self.assertEqual(result['site_reported_total'], 3)

    def test_invalid_arguments_do_not_open_browser(self):
        with patch('library.Browser', side_effect=AssertionError('No browser for invalid input')) as launch:
            for arguments in [{'query': ''}, {'query': True}, {'query': 'x' * 201}, {'query': 'x\n'},
                              {'query': '\x85Stripe\x85'},
                              {'query': '😀'}, {'query': 'Stripe', 'limit': 101},
                              {'query': 'Stripe', 'limit': True}, {'query': 'Stripe', 'limit': 0},
                              {'query': 'Stripe', 'list_pages': 11}, {'query': 'Stripe', 'list_pages': 1.5}]:
                result = library.search_threads(**arguments)
                self.assertEqual(result['status'], 'failed')
            launch.assert_not_called()

    def test_missing_next_link_cannot_hide_unread_reported_results(self):
        result = self.search(SearchBrowser(first=results_html(next_url=None)))
        self.assertEqual(result['status'], 'needs_attention')
        self.assertFalse(result['pagination_complete'])
        self.assertEqual([row['tid'] for row in result['threads']], [123, 124])

    def test_cli_partial_failure_and_mcp_unknown_filters_are_rejected(self):
        with patch('sys.argv', ['cli.py', 'site-search', 'Stripe']), \
                patch('library.Browser', return_value=SearchBrowser(second=RuntimeError('search_rate_limited'))), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(), 2)
        self.assertEqual(json.loads(output.getvalue())['status'], 'needs_attention')

        async def exercise():
            with patch('library.Browser', side_effect=AssertionError('Invalid filters must not search')):
                for arguments in [{'query': 'Stripe', 'company': 'other'}, {'query': 'Stripe', 'sort': 'views'},
                                  {'query': 'Stripe', 'limit': True}]:
                    with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                        await mcp_server.server.call_tool('search_threads', arguments)
        asyncio.run(exercise())

    def test_query_text_cannot_supply_the_result_total(self):
        query = '相关内容 5 个'
        browser = SearchBrowser(first=results_html(query, ids=(123,), total=1, next_url=None))
        result = self.search(browser, query)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['site_reported_total'], 1)

    def test_unique_counts_are_checked_before_limits(self):
        cases = [
            (SearchBrowser(first=results_html(total=1)), 1, 'failed', []),
            (SearchBrowser(first=results_html(ids=(123,), total=2),
                           second=results_html(ids=(124, 125), total=2, page=2, next_url=None)),
             2, 'needs_attention', [123]),
        ]
        for browser, limit, status, ids in cases:
            with self.subTest(status=status):
                result = self.search(browser, limit=limit)
                self.assertEqual(result['status'], status)
                self.assertEqual([row['tid'] for row in result['threads']], ids)
                self.assertEqual(result['error'], 'search_result_count_mismatch')

    def test_length_bound_applies_after_space_normalization(self):
        query = 'a' * 200
        browser = SearchBrowser(first=results_html(query, ids=(123,), total=1, next_url=None))
        result = self.search(browser, '  ' + query + '  ')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['query'], query)


BOARD_TWO = SITE + '/bbs/forum-472-2.html'


class BoardBrowser:
    """Serves board pages in order; records every URL so pagination can be asserted."""

    def __init__(self, *pages):
        self.pages = list(pages)
        self.reads = []
        self.sb = self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def sleep(self, _seconds):
        pass

    def read_html(self, url):
        self.reads.append(url)
        return self.pages[min(len(self.reads), len(self.pages)) - 1]


class BoardBrowseTests(unittest.TestCase):
    def browse(self, browser, board='472', **kwargs):
        with patch('library.Browser', return_value=browser),                 patch('library.Library', side_effect=AssertionError('Browsing must not open the database')):
            return library.browse_board(board, **kwargs)

    def page(self, rows, next_url=None):
        return extract_board_html(rows, next_url=next_url)

    def test_a_sticky_repeated_on_every_page_is_counted_once(self):
        # Discuz repeats stickies; without dedupe the same thread would inflate two pages of results.
        browser = BoardBrowser(self.page(((9, True), (1001, False)), BOARD_TWO),
                               self.page(((9, True), (1002, False))))
        result = self.browse(browser, list_pages=2)
        self.assertEqual([t['tid'] for t in result['threads']], [9, 1001, 1002])
        self.assertTrue(result['pagination_complete'])
        self.assertIsNone(result['truncated_reason'])

    def test_a_read_budget_is_never_reported_as_the_end_of_the_board(self):
        browser = BoardBrowser(self.page(((1001, False), (1002, False)), BOARD_TWO))
        limited = self.browse(browser, limit=1, list_pages=2)
        self.assertEqual(limited['truncated_reason'], 'result_limit')
        self.assertFalse(limited['pagination_complete'])
        self.assertEqual(limited['omitted_on_last_page'], 1)

        paged = self.browse(BoardBrowser(self.page(((1001, False),), BOARD_TWO)), list_pages=1)
        self.assertEqual(paged['truncated_reason'], 'page_limit')
        self.assertFalse(paged['pagination_complete'])
        self.assertEqual(paged['next_url'], BOARD_TWO)

    def test_pagination_that_leaves_the_board_is_refused(self):
        browser = BoardBrowser(self.page(((1001, False),), SITE + '/bbs/forum-98-2.html'))
        result = self.browse(browser, list_pages=2)
        self.assertEqual(result['error'], 'invalid_board_pagination')
        self.assertEqual(result['status'], 'needs_attention')

    def test_a_refused_target_or_budget_never_opens_a_browser(self):
        for kwargs, board in [({}, 'https://evil.example.com/bbs/forum-1-1.html'),
                              ({}, 'abc'), ({'limit': 0}, '472'), ({'limit': 10 ** 6}, '472'),
                              ({'list_pages': 0}, '472'), ({'list_pages': 99}, '472')]:
            with self.subTest(board=board, kwargs=kwargs):
                with patch('library.Browser') as opened:
                    result = library.browse_board(board, **kwargs)
                opened.assert_not_called()
                self.assertEqual(result['status'], 'failed')
                self.assertIn(result['error'], {'invalid_board_reference', 'invalid_board_budget'})
                self.assertEqual(result['threads'], [])

    def test_result_rows_can_be_handed_to_thread_detail(self):
        result = self.browse(BoardBrowser(self.page(((1001, False),))))
        self.assertEqual(extract.parse_thread_reference(result['threads'][0]['url'])['tid'], 1001)

    def test_a_repeated_sticky_must_not_consume_the_result_budget(self):
        # The result dict deduplicates by tid on its own, so asserting the tid list proves nothing
        # about the filter. Only the budget accounting shows whether duplicates were counted.
        browser = BoardBrowser(self.page(((9, True), (1001, False)), BOARD_TWO),
                               self.page(((9, True), (1002, False), (1003, False))))
        result = self.browse(browser, limit=3, list_pages=2)
        self.assertEqual([t['tid'] for t in result['threads']], [9, 1001, 1002])
        self.assertEqual(result['truncated_reason'], 'result_limit')
        self.assertEqual(result['omitted_on_last_page'], 1)

    def test_cli_and_mcp_expose_the_same_browse_result(self):
        payload = {'status': 'complete', 'board': 472, 'threads': [], 'pages_fetched': 1,
                   'pagination_complete': True, 'next_url': None, 'omitted_on_last_page': 0,
                   'truncated_reason': None, 'scope': 'currently_visible_content', 'error': None}
        with patch('cli.browse_board', return_value=payload) as called,                 patch('sys.argv', ['cli', 'browse-board', '472', '--limit', '5']),                 patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(), 0)
        self.assertEqual(json.loads(out.getvalue())['board'], 472)
        self.assertEqual(called.call_args.kwargs['limit'], 5)
        with patch('mcp_server.browse_board_impl', return_value=payload):
            tool = asyncio.run(mcp_server.server.call_tool('browse_board', {'board': '472'}))
        self.assertEqual(json.loads(tool.content[0].text)['board'], 472)

    def test_a_failed_browse_is_not_reported_as_success(self):
        payload = {'status': 'failed', 'board': None, 'threads': [], 'pages_fetched': 0,
                   'pagination_complete': False, 'next_url': None, 'omitted_on_last_page': 0,
                   'truncated_reason': None, 'scope': 'currently_visible_content',
                   'error': 'invalid_board_reference'}
        with patch('cli.browse_board', return_value=payload),                 patch('sys.argv', ['cli', 'browse-board', 'abc']),                 patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(cli.main(), 2)
        # A business failure comes back as a result flagged is_error, not as a raise.
        with patch('mcp_server.browse_board_impl', return_value=payload):
            tool = asyncio.run(mcp_server.server.call_tool('browse_board', {'board': 'abc'}))
        self.assertTrue(tool.is_error)
        self.assertEqual(json.loads(tool.content[0].text)['error'], 'invalid_board_reference')

    def test_arguments_the_schema_rejects_never_reach_the_site(self):
        async def exercise():
            with patch('library.Browser', side_effect=AssertionError('Invalid arguments must not browse')):
                for arguments in [{'board': '472', 'limit': True}, {'board': '472', 'list_pages': 'many'},
                                  {'board': 472}]:
                    with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                        await mcp_server.server.call_tool('browse_board', arguments)
        asyncio.run(exercise())


class ProfileBrowser:
    def __init__(self, user=None, error=None):
        self.user, self.error, self.entered = user, error, 0

    def __enter__(self):
        self.entered += 1
        if self.error:
            raise self.error
        return self

    def __exit__(self, *_):
        pass

    def profile(self):
        return self.user


class UnreadCountTests(unittest.TestCase):
    """Reading the counters is the identity read the tool already makes; nothing gets marked read (#33)."""

    def test_counts_come_from_the_identity_read_with_login_recovery(self):
        fake = ProfileBrowser({'uid': 1, 'username': 'x', 'newprompt': 2, 'newpm': 0})
        with patch('browser.Browser', return_value=fake) as opened:
            result = browser.get_unread_counts()
        opened.assert_called_once_with()  # default recover_login, same entry as every other read
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['session_state'], 'logged_in')
        self.assertEqual(result['counts'], {'prompt': 2, 'pm': 0, 'chat': None})
        self.assertEqual(result['missing'], ['chat'])
        self.assertIsNone(result['error'])

    def test_a_lapsed_login_is_a_failed_read_not_zero_notifications(self):
        with patch('browser.Browser', return_value=ProfileBrowser(error=RuntimeError('login_required'))):
            result = browser.get_unread_counts()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['session_state'], 'logged_out')
        self.assertEqual(result['error'], 'login_required')
        self.assertEqual(result['counts'], {'prompt': None, 'pm': None, 'chat': None})

    def test_cli_and_mcp_expose_the_read_and_its_failure_alike(self):
        ok = {'status': 'complete', 'session_state': 'logged_in', 'counts': {'prompt': 0, 'pm': 0, 'chat': 0},
              'missing': [], 'read_at': 'T', 'source': 'user.me', 'scope': 'currently_visible_content', 'error': None}
        bad = dict(ok, status='failed', session_state='logged_out', error='login_required',
                   counts={'prompt': None, 'pm': None, 'chat': None}, missing=['prompt', 'pm', 'chat'])
        for payload, code in [(ok, 0), (bad, 2)]:
            with self.subTest(status=payload['status']):
                with patch('cli.get_unread_counts', return_value=payload), \
                        patch('sys.argv', ['cli', 'unread']), \
                        patch('sys.stdout', new_callable=io.StringIO) as out:
                    self.assertEqual(cli.main(), code)
                self.assertEqual(json.loads(out.getvalue())['counts'], payload['counts'])
                with patch('mcp_server.read_unread_counts', return_value=payload):
                    tool = asyncio.run(mcp_server.server.call_tool('get_unread_count', {}))
                self.assertEqual(tool.is_error, code != 0)
                self.assertEqual(json.loads(tool.content[0].text)['missing'], payload['missing'])


class ProfilePagesBrowser:
    """Serves the space page first, then thread pages in order."""

    def __init__(self, space, *pages):
        self.space, self.pages, self.reads, self.sb = space, list(pages), [], self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def sleep(self, _seconds):
        pass

    def read_html(self, url):
        self.reads.append(url)
        if len(self.reads) == 1:
            return self.space
        return self.pages[min(len(self.reads) - 1, len(self.pages)) - 1]


class UserProfileTests(unittest.TestCase):
    NEXT = SITE + '/bbs/home.php?mod=space&uid=123456&do=thread&view=me&order=dateline&from=space&page=2'

    def browse(self, browser, user='123456', **kwargs):
        with patch('library.Browser', return_value=browser), \
                patch('library.Library', side_effect=AssertionError('Profile reads must not open the database')):
            return library.get_user_profile(user, **kwargs)

    def test_profile_and_threads_are_read_for_the_requested_member(self):
        browser = ProfilePagesBrowser(profile_html(), profile_threads_html(((1001, 'A', 1, 5), (1002, 'B', 0, 1))))
        result = self.browse(browser)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['profile']['username'], '合成成员')
        self.assertEqual([t['tid'] for t in result['threads']], [1001, 1002])
        self.assertEqual(result['sections'], ['threads'])
        self.assertTrue(result['pagination_complete'])
        self.assertTrue(browser.reads[0].endswith('space-uid-123456.html'))

    def test_a_page_owned_by_someone_else_is_an_identity_mismatch_not_a_substitute(self):
        result = self.browse(ProfilePagesBrowser(profile_html(uid=999), profile_threads_html()))
        self.assertEqual(result['error'], 'profile_identity_mismatch')
        self.assertEqual(result['threads'], [])
        self.assertIsNone(result['profile'])

    def test_a_read_budget_is_never_reported_as_the_end_of_the_list(self):
        browser = ProfilePagesBrowser(profile_html(), profile_threads_html(((1001, 'A', 1, 5), (1002, 'B', 0, 1)), next_url=self.NEXT))
        limited = self.browse(browser, limit=1, list_pages=2)
        self.assertEqual(limited['truncated_reason'], 'result_limit')
        self.assertFalse(limited['pagination_complete'])
        self.assertEqual(limited['omitted_on_last_page'], 1)
        paged = self.browse(ProfilePagesBrowser(profile_html(), profile_threads_html(next_url=self.NEXT)), list_pages=1)
        self.assertEqual(paged['truncated_reason'], 'page_limit')
        self.assertEqual(paged['next_url'], self.NEXT)

    def test_pagination_that_leaves_the_member_is_refused(self):
        other = SITE + '/bbs/home.php?mod=space&uid=777&do=thread&view=me&page=2'
        result = self.browse(ProfilePagesBrowser(profile_html(), profile_threads_html(next_url=other)), list_pages=2)
        self.assertEqual(result['error'], 'invalid_profile_pagination')

    def test_missing_private_and_refused_targets_never_substitute_data(self):
        for space, expected in [(notice_html('抱歉，您指定的用户空间不存在'), 'profile_not_found'),
                                (notice_html('由于该用户的隐私设置，您不能访问当前内容'), 'profile_restricted')]:
            with self.subTest(expected=expected):
                result = self.browse(ProfilePagesBrowser(space))
                self.assertEqual((result['status'], result['error'], result['threads']), ('failed', expected, []))
        for kwargs, user in [({}, 'abc'), ({}, 'https://evil.example.com/bbs/space-uid-1.html'),
                             ({'limit': 0}, '123456'), ({'list_pages': 99}, '123456')]:
            with self.subTest(user=user, kwargs=kwargs), patch('library.Browser') as opened:
                result = library.get_user_profile(user, **kwargs)
                opened.assert_not_called()
                self.assertIn(result['error'], {'invalid_profile_reference', 'invalid_profile_budget'})

    def test_cli_and_mcp_expose_the_same_profile_result(self):
        payload = {'status': 'complete', 'uid': 123456, 'profile': {'username': 'x'}, 'sections': ['threads'],
                   'threads': [], 'pages_fetched': 1, 'pagination_complete': True, 'next_url': None,
                   'omitted_on_last_page': 0, 'truncated_reason': None, 'scope': 'currently_visible_content', 'error': None}
        with patch('cli.get_user_profile', return_value=payload) as called, \
                patch('sys.argv', ['cli', 'user-profile', '123456', '--limit', '7']), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(), 0)
        self.assertEqual(json.loads(out.getvalue())['uid'], 123456)
        self.assertEqual(called.call_args.kwargs['limit'], 7)
        with patch('mcp_server.read_user_profile', return_value=payload):
            tool = asyncio.run(mcp_server.server.call_tool('user_profile', {'user': '123456'}))
        self.assertEqual(json.loads(tool.content[0].text)['uid'], 123456)
        with patch('library.Browser', side_effect=AssertionError('schema rejects must not browse')):
            for arguments in [{'user': 123456}, {'user': '1', 'limit': True}]:
                with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                    asyncio.run(mcp_server.server.call_tool('user_profile', arguments))


class OwnSpaceBrowser:
    """Serves my space page, my thread page, then my favorites page; identity comes from profile()."""

    def __init__(self, user, space, threads, favorites, saved=None, fail=None):
        self.user, self.fail, self.sb, self.reads = user, fail, self, []
        self.pages, self.saved = [space, threads, favorites], saved if saved is not None else {'forums': [], 'tags': []}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def sleep(self, _seconds):
        pass

    def profile(self):
        if self.fail:
            raise RuntimeError(self.fail)
        return self.user

    def rpc(self, method):
        assert method == 'favorite.getFavorites'
        return self.saved

    def read_html(self, url):
        self.reads.append(url)
        return self.pages[min(len(self.reads), len(self.pages)) - 1]


class MyProfileTests(unittest.TestCase):
    USER = {'uid': 123456, 'username': '合成成员'}

    def read(self, browser, **kwargs):
        with patch('library.Browser', return_value=browser), \
                patch('library.Library', side_effect=AssertionError('Profile reads must not open the database')):
            return library.get_my_profile(**kwargs)

    def test_identity_comes_from_the_session_and_every_real_section_is_returned(self):
        browser = OwnSpaceBrowser(self.USER, profile_html(), profile_threads_html((), empty=True),
                                  favorites_html(), saved={'forums': [{'id': 424, 'name': '&#128273;拉群结伴'}],
                                                           'tags': [{'id': 9066, 'name': '英国'}, {'name': 'no id'}]})
        result = self.read(browser)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['uid'], 123456)
        self.assertEqual(result['sections'], ['threads', 'favorites', 'favorite_forums', 'favorite_tags'])
        self.assertEqual(result['threads'], [])
        self.assertEqual([f['tid'] for f in result['favorites']['favorites']], [1134743])
        self.assertTrue(result['favorites']['pagination_complete'])
        self.assertEqual(result['favorite_forums'], [{'id': 424, 'name': '🔑拉群结伴'}])
        self.assertEqual(result['favorite_tags'], [{'id': 9066, 'name': '英国'}])
        self.assertTrue(browser.reads[0].endswith('space-uid-123456.html'))
        self.assertIn('do=favorite&view=me&type=thread', browser.reads[2])

    def test_a_browser_holding_another_account_stops_before_reading_anything(self):
        browser = OwnSpaceBrowser(self.USER, profile_html(), profile_threads_html(), favorites_html(), fail='unexpected_account')
        result = self.read(browser)
        self.assertEqual((result['status'], result['error'], result['uid'], result['profile']), ('failed', 'unexpected_account', None, None))
        self.assertEqual(browser.reads, [])
        self.assertEqual(result['favorites']['favorites'], [])
        self.assertIsNone(result['favorite_forums'])

    def test_favorites_pagination_that_leaves_my_own_list_is_refused(self):
        other = SITE + '/bbs/home.php?mod=space&uid=777&do=favorite&view=me&page=2'
        browser = OwnSpaceBrowser(self.USER, profile_html(), profile_threads_html((), empty=True), favorites_html(next_url=other))
        self.assertEqual(self.read(browser, list_pages=2)['error'], 'invalid_profile_pagination')

    def test_cli_and_mcp_expose_my_profile_without_a_uid_argument(self):
        payload = {'status': 'complete', 'uid': 123456, 'profile': {'username': 'x'}, 'sections': ['threads', 'favorites'],
                   'threads': [], 'pages_fetched': 1, 'pagination_complete': True, 'next_url': None,
                   'omitted_on_last_page': 0, 'truncated_reason': None,
                   'favorites': {'favorites': [], 'pages_fetched': 1, 'pagination_complete': True, 'next_url': None,
                                 'omitted_on_last_page': 0, 'truncated_reason': None},
                   'favorite_forums': [], 'favorite_tags': [], 'scope': 'currently_visible_content', 'error': None}
        with patch('cli.get_my_profile', return_value=payload) as called, \
                patch('sys.argv', ['cli', 'my-profile', '--limit', '4']), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(), 0)
        self.assertEqual(json.loads(out.getvalue())['uid'], 123456)
        self.assertEqual(called.call_args.kwargs['limit'], 4)
        with patch('mcp_server.read_my_profile', return_value=payload):
            tool = asyncio.run(mcp_server.server.call_tool('get_my_profile', {}))
        self.assertEqual(json.loads(tool.content[0].text)['sections'], ['threads', 'favorites'])
        with patch('library.Browser', side_effect=AssertionError('schema rejects must not browse')):
            with self.assertRaises(ToolError):
                asyncio.run(mcp_server.server.call_tool('get_my_profile', {'limit': True}))


class OrganizeWiringTests(unittest.TestCase):
    def test_cli_and_mcp_expose_organize_without_touching_the_site(self):
        payload = {'status': 'complete', 'tid': 555, 'reused': False, 'built_at': 'T',
                   'outline': {'tid': 555, 'rounds': [], 'questions': [], 'missing': []},
                   'superseded_versions': [], 'scope': 'stored_content_only', 'error': None}
        with patch('cli.organize_thread', return_value=payload) as called, \
                patch('sys.argv', ['cli', 'organize', '555', '--refresh']), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(), 0)
        self.assertEqual(json.loads(out.getvalue())['tid'], 555)
        self.assertTrue(called.call_args.kwargs['refresh'])
        with patch('mcp_server.outline_thread', return_value=payload), \
                patch('library.Browser', side_effect=AssertionError('organize must never open a browser')):
            tool = asyncio.run(mcp_server.server.call_tool('organize_thread', {'thread': '555'}))
        self.assertEqual(json.loads(tool.content[0].text)['scope'], 'stored_content_only')
        with self.assertRaises(ToolError):
            asyncio.run(mcp_server.server.call_tool('organize_thread', {'thread': 555}))
