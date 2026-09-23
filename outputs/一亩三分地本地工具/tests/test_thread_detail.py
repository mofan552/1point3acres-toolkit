import asyncio
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import cli
import extract
import library
import mcp_server
from browser import Browser
from mcp.server.mcpserver.exceptions import ToolError
from settings import SITE, THREAD_PAGES, THREAD_PAGES_MAX


URL = SITE + '/bbs/thread-123-1-1.html'
NEXT = SITE + '/bbs/thread-123-2-1.html'


def page_html(pid, *, next_url=None, restricted=False):
    notice = '<div class="locked">本帖需要积分权限</div>' if restricted else ''
    next_link = f'<div class="pg"><a class="nxt" href="{next_url}">下一页</a></div>' if next_url else ''
    return (f'<h1 id="thread_subject">Synthetic forum thread</h1><div id="postlist">回复: 1'
            f'<div id="post_{pid}"><span id="authorposton{pid}">2026-01-01</span>'
            f'<div id="postmessage_{pid}">Visible content {pid}{notice}</div></div></div>{next_link}')


class DetailBrowser:
    def __init__(self, second_error=False, next_url=NEXT, restricted=False):
        self.reads = []
        self.second_error = second_error
        self.next_url = next_url
        self.restricted = restricted
        self.sb = self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def sleep(self, _):
        pass

    def read_html(self, url):
        self.reads.append(url)
        if url == URL:
            return page_html(11, next_url=self.next_url, restricted=self.restricted)
        if self.second_error:
            raise RuntimeError('page_two_unavailable')
        return page_html(12)


class ThreadDetailTests(unittest.TestCase):
    def read(self, browser, thread='123', **kwargs):
        with patch('library.Browser', return_value=browser), \
                patch('library.Library', side_effect=AssertionError('Detail reads must not open the library')):
            return library.get_thread_detail(thread, **kwargs)

    def test_supported_references_normalize_to_first_page(self):
        for value in [123, '123', URL, NEXT + '#pid12', SITE + '/bbs/forum.php?mod=viewthread&tid=123&page=2']:
            with self.subTest(value=value):
                location = extract.parse_thread_reference(value, first_page=True)
                self.assertEqual(location, {'tid': 123, 'page': 1, 'url': URL})

    def test_invalid_references_fail_before_browser(self):
        values = [True, 0, -1, '', '0', 'hello', 'https://example.org/bbs/thread-123-1-1.html',
                  URL.replace('https:', 'http:'), URL.replace('www.', 'user@www.'),
                  SITE + ':444/bbs/thread-123-1-1.html', SITE + '/bbs/thread-123-0-1.html',
                  SITE + '/bbs/forum.php?mod=viewthread&tid=123&tid=456',
                  SITE + '/bbs/forum.php?mod=post&tid=123', URL + '\nignored']
        with patch('library.Browser', side_effect=AssertionError('Browser must not open')):
            for value in values:
                with self.subTest(value=value):
                    result = library.get_thread_detail(value)
                    self.assertEqual(result['status'], 'failed')
                    self.assertIsNone(result['record'])

    def test_all_visible_pages_are_returned_without_company_inference_or_storage(self):
        browser = DetailBrowser()
        result = self.read(browser, NEXT)
        self.assertEqual(browser.reads, [URL, NEXT])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['scope'], 'currently_visible_content')
        self.assertEqual([post['pid'] for post in result['record']['posts']], [11, 12])
        self.assertTrue(result['record']['complete'])
        self.assertEqual(result['record']['url'], URL)
        self.assertIsNone(result['record']['company'])

    def test_permission_restrictions_are_not_claimed_as_full_content(self):
        result = self.read(DetailBrowser(restricted=True))
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['record']['pagination_complete'])
        self.assertFalse(result['record']['complete'])
        self.assertEqual(result['record']['content_status'], 'restricted')
        self.assertTrue(result['record']['posts'][0]['permission_notices'])

    def test_page_limit_returns_partial_result_and_continuation_link(self):
        browser = DetailBrowser()
        result = self.read(browser, max_thread_pages=1)
        self.assertEqual(browser.reads, [URL])
        self.assertEqual(result['status'], 'needs_attention')
        self.assertFalse(result['record']['pagination_complete'])
        self.assertEqual(result['record']['next_url'], NEXT)

    def test_failed_second_page_retains_first_page(self):
        result = self.read(DetailBrowser(second_error=True))
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual([post['pid'] for post in result['record']['posts']], [11])
        self.assertEqual(result['error'], 'page_two_unavailable')

    def test_foreign_wrong_thread_skipped_or_repeated_page_is_not_fetched(self):
        for target in [NEXT.replace('123-', '456-'), NEXT.replace('-2-1', '-3-1'), URL,
                       'https://example.org/bbs/thread-123-2-1.html']:
            with self.subTest(target=target):
                browser = DetailBrowser(next_url=target)
                result = self.read(browser)
                self.assertEqual(browser.reads, [URL])
                self.assertEqual(result['status'], 'needs_attention')
                self.assertFalse(result['record']['complete'])

    def test_no_readable_content_is_a_failure(self):
        browser = DetailBrowser()
        browser.read_html = lambda _: '<form id="challenge-form">Verification</form>'
        result = self.read(browser)
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['record'])

    def test_redirect_to_another_thread_or_page_is_rejected(self):
        for target in [URL.replace('123-', '456-'), NEXT, 'https://example.org/thread']:
            browser = DetailBrowser()
            browser.last_read = {'url': target}
            result = self.read(browser)
            self.assertEqual(result['status'], 'failed')
            self.assertIsNone(result['record'])
            self.assertEqual(browser.reads, [URL])

    def test_browser_keeps_final_url_for_caller_validation(self):
        browser = object.__new__(Browser)
        browser.evaluate = lambda _: {'status': 200, 'challenge': False, 'charset': 'utf-8',
                                     'url': NEXT, 'html': '<p>Synthetic</p>'}
        self.assertEqual(browser.read_html(URL), '<p>Synthetic</p>')
        self.assertEqual(browser.last_read['url'], NEXT)

    def test_html_thread_or_current_page_mismatch_is_rejected(self):
        for marker in [f'<link rel="canonical" href="{URL.replace("123-", "456-")}">',
                       '<div class="pg"><strong>2</strong></div>']:
            browser = DetailBrowser()
            browser.read_html = lambda _, marker=marker: marker + page_html(11).replace('回复: 1', '回复: 0')
            result = self.read(browser)
            self.assertEqual(result['status'], 'failed')
            self.assertIsNone(result['record'])

    def test_page_one_canonical_does_not_reject_valid_later_pages(self):
        browser = DetailBrowser()
        def read(url):
            number = 1 if url == URL else 2
            browser.last_read = {'url': url}
            return (f'<link rel="canonical" href="{URL}"><div class="pg"><strong>{number}</strong></div>'
                    + page_html(10 + number, next_url=NEXT if number == 1 else None))
        browser.read_html = read
        result = self.read(browser)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual([post['pid'] for post in result['record']['posts']], [11, 12])

    def test_challenge_navigation_records_final_response_url(self):
        browser = object.__new__(Browser)
        responses = iter([{'status': 403, 'challenge': True, 'charset': 'utf-8', 'url': URL, 'html': ''},
                          NEXT, '<p>Synthetic</p>'])
        browser.evaluate = lambda _: next(responses)
        with patch.object(browser, 'goto') as navigate:
            self.assertEqual(browser.read_html(URL), '<p>Synthetic</p>')
        navigate.assert_called_once_with(URL)
        self.assertEqual(browser.last_read['url'], NEXT)

    def test_invalid_second_page_keeps_valid_partial_result(self):
        browser = DetailBrowser()
        browser.read_html = lambda url: page_html(11, next_url=NEXT) if url == URL else page_html(0)
        result = self.read(browser)
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual([post['pid'] for post in result['record']['posts']], [11])
        self.assertFalse(result['record']['complete'])
        self.assertIsNotNone(result['error'])

    def test_invalid_next_link_cannot_be_mistaken_for_end_of_thread(self):
        for target in [URL, 'https://example.org/bbs/thread-123-2-1.html']:
            browser = DetailBrowser()
            browser.read_html = lambda _, target=target: page_html(11, next_url=target).replace('回复: 1', '回复: 0')
            result = self.read(browser)
            self.assertEqual(result['status'], 'needs_attention')
            self.assertFalse(result['record']['pagination_complete'])
            self.assertIsNotNone(result['error'])

    def test_listing_keeps_legacy_next_link_filtering(self):
        base = SITE + '/bbs/tag/synthetic-1-1.html'
        valid = SITE + '/bbs/tag/synthetic-1-2.html'
        for target, expected in [(base, None), ('https://example.org/list', None), (valid, valid)]:
            html = ('<table><tr><td>海外面经</td><td><a href="' + URL + '">Synthetic interview</a></td></tr></table>'
                    '<div class="pg"><a class="nxt" href="' + target + '">Next</a></div>')
            with self.subTest(target=target):
                listing = extract.parse_listing(html, base)
                self.assertEqual([item['tid'] for item in listing['threads']], [123])
                self.assertEqual(listing['next_url'], expected)

    def test_page_budget_is_validated_before_browser(self):
        with patch('library.Browser', side_effect=AssertionError('Browser must not open')):
            for value in [0, -1, True, 1.2, THREAD_PAGES_MAX + 1]:
                result = library.get_thread_detail('123', max_thread_pages=value)
                self.assertEqual(result['status'], 'failed')

    def test_cli_propagates_partial_status_and_shared_default(self):
        payload = {'status': 'needs_attention', 'record': None}
        with patch('sys.argv', ['cli.py', 'thread-detail', '123']), \
                patch('cli.get_thread_detail', return_value=payload) as read, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(), 2)
        read.assert_called_once_with('123', max_thread_pages=THREAD_PAGES)
        self.assertEqual(json.loads(output.getvalue()), payload)

    def test_mcp_propagates_partial_status(self):
        payload = {'status': 'needs_attention', 'record': {'tid': 123}}
        with patch('mcp_server.read_thread_detail', return_value=payload):
            result = mcp_server.get_thread_detail('123')
        self.assertTrue(result.is_error)
        self.assertEqual(json.loads(result.content[0].text), payload)

    def test_mcp_sdk_rejects_boolean_and_coerced_number_arguments(self):
        async def exercise():
            with patch('mcp_server.read_thread_detail', return_value={'status': 'complete'}) as read:
                for arguments in [{'thread': True}, {'thread': 1.0},
                                  {'thread': '123', 'max_thread_pages': True},
                                  {'thread': '123', 'max_thread_pages': 1.0},
                                  {'thread': '123', 'max_thread_pages': '1'}]:
                    with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                        await mcp_server.server.call_tool('get_thread_detail', arguments)
                read.assert_not_called()
        asyncio.run(exercise())
