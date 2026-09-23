import asyncio
import io
import json
import unittest
from unittest.mock import patch

import cli
import mcp_server
from browser import Browser
from contracts import publish_result
from extract import parse_thread_page, normalize_message
from interact import create_thread, reply_thread
from tests.test_extract import post_html, thread_html
from tests.test_interact import FavoriteBrowser
from tests.test_site_search import profile_threads_html

UID = 123456
TID = 1190345
FORUM = {'name': '新闻时事', 'fid': 472, 'status': 1, 'forum_field': {'thread_types': {}, 'thread_sorts': {}, 'post_groups': [12, 13]}}
TYPED_FORUM = {'name': '研究生申请', 'fid': 27, 'status': 1,
               'forum_field': {'thread_types': {'1': '申请总结', '7': '面试'}, 'thread_sorts': {'0': '无分类信息', '162': '申请日记'}, 'post_groups': [12]}}
DETAIL = {'tid': TID, 'subject': 'MIT 首次登顶', 'pid': 21182046, 'close_status': 0, 'fid': 472, 'forum_name': {'name': '新闻时事'},
          'message_bbcode': '2027年度排名出炉\n\n来源：侨报网'}


def page_with(tid, *posts, page=1):
    """A thread page as a site redirect lands on it: canonical link, pager position, and the posts."""
    return (f'<link rel="canonical" href="https://www.1point3acres.com/bbs/thread-{tid}-{page}-1.html"/>'
            f'<div class="pg"><strong>{page}</strong></div>' + thread_html(*posts))


class ComposeBrowser(FavoriteBrowser):
    """The site as posting sees it: a board, a thread, its pages, and the two write endpoints.

    `posts` is the list of (pid, body, quote) on the thread's only page; a submitted reply is appended
    unless `apply` is off. A submitted thread becomes `created` unless `apply` is off.
    """

    def __init__(self, *, forum=FORUM, detail=DETAIL, posts=(), apply=True, reply_perm=None, answer=None,
                 groupid=12, lose=False, own_threads=(), **kwargs):
        super().__init__(**kwargs)
        self.forum, self.detail, self.apply, self.lose = forum, detail, apply, lose
        self.posts = [dict(pid=pid, body=body, quote=quote, uid=77) for pid, body, quote in posts]
        self.reply_perm = reply_perm or {'status': 200, 'body': {'errno': 0, 'msg': 'OK'}}
        self.answer, self.groupid, self.own_threads = answer, groupid, list(own_threads)
        self.api_calls, self.rpc_calls, self.created = [], [], None
        self.next_pid = 21182200

    def profile(self):
        return {'uid': UID, 'username': 'synthetic', 'groupid': self.groupid}

    def rpc(self, method, data=None):
        self.rpc_calls.append((method, data))
        assert method == 'forum.get'
        return {'forum': self.forum} if self.forum and self.forum['fid'] == data['fid'] else {'forum': None}

    def api_get(self, path):
        if path.endswith('/reply-perm'):
            return self.reply_perm
        if path.startswith('/api/v3/threads/'):
            tid = int(path.rsplit('/', 1)[1])
            if self.created and tid == self.created['tid']:
                return {'status': 200, 'body': {'errno': 0, 'msg': 'OK', 'thread': self.created}}
            if self.detail and tid == self.detail['tid']:
                return {'status': 200, 'body': {'errno': 0, 'msg': 'OK', 'thread': self.detail}}
            return {'status': 200, 'body': {'errno': -1, 'msg': '主题不存在'}}
        raise AssertionError(path)

    def _render(self):
        rendered = []
        for post in self.posts:
            quote = (post['quote'], '被引用者', '被引用的话') if post['quote'] else None
            html = post_html(post['pid'], post['body'], quote=quote)
            if post['uid'] != 77:
                html = html.replace('space-uid-77', f"space-uid-{post['uid']}")
            rendered.append(html)
        return page_with(TID, *rendered)

    def read_html(self, url):
        self.reads.append(url)
        if 'do=thread' in url:
            return profile_threads_html(self.own_threads)
        return self._render()

    def api_request(self, method, path, body):
        self.api_calls.append((method, path, dict(body)))
        if self.lose:
            self.lose = False
            if self.apply:
                self._apply(path, body)
            raise RuntimeError('api_submission_unconfirmed')
        if self.answer is not None:
            return self.answer
        if self.apply:
            return self._apply(path, body)
        if path == '/api/threads':
            return {'status': 200, 'body': {'errno': 0, 'msg': 'OK', 'thread': {'tid': 999}}}
        return {'status': 200, 'body': {'errno': 0, 'msg': 'OK', 'data': {'pid': 424242}}}

    def _apply(self, path, body):
        if path == '/api/threads':
            self.created = {'tid': 1190999, 'subject': body['subject'], 'message_bbcode': body['message'], 'pid': 21190000,
                            'close_status': 0, 'forum_name': {'name': self.forum['name']}}
            return {'status': 200, 'body': {'errno': 0, 'msg': 'OK', 'thread': {'tid': 1190999}}}
        pid = self.next_pid
        self.next_pid += 1
        self.posts.append(dict(pid=pid, body=body['message'], quote=body.get('quote_pid'), uid=UID))
        return {'status': 200, 'body': {'errno': 0, 'msg': 'OK', 'data': {'pid': pid}}}


def post(browser, fid=472, subject='一个标题', message='正文内容', **kwargs):
    with patch('interact.Browser', return_value=browser):
        return create_thread(fid, subject, message, **kwargs)


def reply(browser, thread=TID, message='一条回复', **kwargs):
    with patch('interact.Browser', return_value=browser):
        return reply_thread(thread, message, **kwargs)


class ThreadPublishTests(unittest.TestCase):
    """A text thread is previewed without any write and published once, then read back (#27)."""

    def test_preview_reads_the_board_and_writes_nothing(self):
        browser = ComposeBrowser()
        result = post(browser)
        self.assertEqual((result['status'], result['submitted'], result['tid']), ('complete', False, None))
        self.assertEqual(result['preview']['board'], '新闻时事')
        self.assertEqual(result['preview']['checks'], {'board_open': True, 'may_post': True, 'type_valid': True,
                                                       'sort_valid': True, 'type_chosen': True})
        self.assertEqual(browser.api_calls, [])
        self.assertEqual(browser.rpc_calls, [('forum.get', {'fid': 472})])

    def test_submit_posts_the_previewed_content_once_and_confirms_by_reading_back(self):
        browser = ComposeBrowser()
        result = post(browser, submit=True)
        self.assertEqual((result['status'], result['submitted'], result['confirmed'], result['tid'], result['pid']),
                         ('complete', True, True, 1190999, 21190000))
        self.assertEqual(result['url'], 'https://www.1point3acres.com/bbs/thread-1190999-1-1.html')
        self.assertEqual(len(browser.api_calls), 1)
        method, path, payload = browser.api_calls[0]
        self.assertEqual((method, path), ('POST', '/api/threads'))
        self.assertEqual(payload, {'fid': 472, 'typeid': 0, 'sortid': 0, 'subject': '一个标题', 'message': '正文内容',
                                   'attachments': [], 'htmlon': False, 'anonymous': 0, 'usesig': 0, 'poll': None, 'magic_ids': []})

    def test_a_board_with_types_requires_one_and_rejects_unknown_ones_before_any_write(self):
        browser = ComposeBrowser(forum=TYPED_FORUM)
        result = post(browser, fid=27, submit=True)
        self.assertEqual((result['status'], result['error'], result['submitted']), ('failed', 'thread_type_required', False))
        self.assertEqual(result['preview']['available_types'], {1: '申请总结', 7: '面试'})
        self.assertEqual(result['preview']['available_sorts'], {162: '申请日记'})
        result = post(browser, fid=27, typeid=99, submit=True)
        self.assertEqual((result['status'], result['error']), ('failed', 'unknown_thread_type'))
        result = post(browser, fid=27, typeid=7, sortid=5, submit=True)
        self.assertEqual((result['status'], result['error']), ('failed', 'unknown_thread_sort'))
        self.assertEqual(browser.api_calls, [])
        result = post(browser, fid=27, typeid=7, sortid=162, submit=True)
        self.assertEqual((result['status'], result['preview']['type_label'], result['preview']['sort_label']), ('complete', '面试', '申请日记'))
        self.assertEqual(browser.api_calls[0][2]['typeid'], 7)

    def test_no_permission_closed_board_and_unknown_board_are_refused(self):
        self.assertEqual(post(ComposeBrowser(groupid=99), submit=True)['error'], 'post_permission_denied')
        self.assertEqual(post(ComposeBrowser(forum={**FORUM, 'status': 0}), submit=True)['error'], 'board_closed')
        self.assertEqual(post(ComposeBrowser(), fid=473, submit=True)['error'], 'board_not_found')

    def test_a_thread_held_for_review_is_pending_not_published_and_not_retried(self):
        browser = ComposeBrowser(answer={'status': 200, 'body': {'errno': -2, 'msg': 'OK'}})
        result = post(browser, submit=True)
        self.assertEqual((result['status'], result['error'], result['pending_review'], result['tid'], result['confirmed']),
                         ('needs_attention', 'pending_review', True, None, False))
        self.assertEqual(len(browser.api_calls), 1)

    def test_a_site_rejection_is_a_failure_with_the_site_message(self):
        browser = ComposeBrowser(answer={'status': 200, 'body': {'errno': -1, 'msg': '发帖间隔太短'}})
        result = post(browser, submit=True)
        self.assertEqual((result['status'], result['error'], result['site_message'], result['tid']),
                         ('failed', 'thread_rejected', '发帖间隔太短', None))

    def test_the_site_saying_ok_is_not_success_when_the_read_back_differs(self):
        browser = ComposeBrowser(apply=False)
        browser.detail = {'tid': 999, 'subject': '别的标题', 'message_bbcode': '别的正文', 'pid': 1}
        result = post(browser, submit=True)
        self.assertEqual((result['status'], result['error'], result['tid'], result['confirmed']),
                         ('needs_attention', 'published_content_differs', 999, False))

    def test_a_lost_response_looks_up_the_own_thread_list_instead_of_posting_again(self):
        browser = ComposeBrowser(lose=True)
        browser.own_threads = [(1190999, '新闻时事', 0, 1)]  # the fixture titles this row 合成标题 1190999
        result = post(browser, subject='合成标题 1190999', submit=True)
        self.assertEqual((result['status'], result['recovered'], result['tid'], result['confirmed']), ('complete', True, 1190999, True))
        self.assertEqual(len(browser.api_calls), 1)
        lost = ComposeBrowser(lose=True, apply=False)
        result = post(lost, submit=True)
        self.assertEqual((result['status'], result['error'], result['submitted']), ('needs_attention', 'api_submission_unconfirmed', True))
        self.assertEqual(len(lost.api_calls), 1)

    def test_invalid_input_never_opens_the_browser(self):
        with patch('interact.Browser', side_effect=AssertionError('must not open a browser')):
            cases = [dict(fid='472'), dict(fid=0), dict(subject=''), dict(subject='x' * 81), dict(message='  '),
                     dict(submit='yes'), dict(typeid='7'), dict(sortid=-1)]
            for case in cases:
                arguments = {'fid': 472, 'subject': '标题', 'message': '正文', **case}
                result = create_thread(arguments.pop('fid'), arguments.pop('subject'), arguments.pop('message'), **arguments)
                self.assertEqual((result['status'], result['submitted']), ('failed', False), case)


class ReplyPublishTests(unittest.TestCase):
    """A reply lands on the thread it was aimed at, quoting exactly the post it was aimed at (#29, #30)."""

    def test_preview_checks_the_thread_and_permission_without_writing(self):
        browser = ComposeBrowser(posts=[(21182046, '主楼', None), (21182047, '二楼的话', None)])
        result = reply(browser)
        self.assertEqual((result['status'], result['submitted'], result['tid']), ('complete', False, TID))
        self.assertEqual(result['preview'], {'tid': TID, 'subject': 'MIT 首次登顶', 'board': '新闻时事', 'quote': None,
                                             'message': '一条回复', 'anonymous': False})
        self.assertEqual(browser.api_calls, [])

    def test_submit_posts_once_and_confirms_the_new_post_on_its_page(self):
        browser = ComposeBrowser(posts=[(21182046, '主楼', None)])
        result = reply(browser, submit=True)
        self.assertEqual((result['status'], result['confirmed'], result['pid']), ('complete', True, 21182200))
        self.assertEqual(browser.api_calls, [('POST', f'/api/threads/{TID}/posts',
                                              {'message': '一条回复', 'attachments': [], 'anonymous': 0, 'usesig': 0, 'magic_ids': [], 'app': 'new_home'})])
        self.assertIn(f'goto=findpost&ptid={TID}&pid=21182200', result['url'])

    def test_a_quoted_reply_targets_that_post_and_the_relation_is_read_back(self):
        browser = ComposeBrowser(posts=[(21182046, '主楼', None), (21182047, '二楼的话', None)])
        preview = reply(browser, quote_pid=21182047)
        self.assertEqual(preview['preview']['quote'], {'pid': 21182047, 'author': '合成作者', 'excerpt': '二楼的话', 'page': 1})
        result = reply(browser, quote_pid=21182047, submit=True)
        self.assertEqual((result['status'], result['confirmed']), ('complete', True))
        self.assertEqual(browser.api_calls[-1][2]['quote_pid'], 21182047)

    def test_a_pid_outside_the_thread_is_refused_never_downgraded_to_a_plain_reply(self):
        browser = ComposeBrowser(posts=[(21182046, '主楼', None)])
        result = reply(browser, quote_pid=777777, submit=True)
        self.assertEqual((result['status'], result['error'], result['submitted']), ('failed', 'quoted_post_not_in_thread', False))
        self.assertEqual(browser.api_calls, [])

    def test_a_missing_quote_relation_after_posting_is_not_confirmed(self):
        class DropsQuote(ComposeBrowser):
            def _apply(self, path, body):
                return super()._apply(path, {**body, 'quote_pid': None})
        browser = DropsQuote(posts=[(21182046, '主楼', None), (21182047, '二楼', None)])
        result = reply(browser, quote_pid=21182047, submit=True)
        self.assertEqual((result['status'], result['error'], result['confirmed']), ('needs_attention', 'quote_relation_missing', False))

    def test_closed_threads_denied_permission_and_missing_threads_are_refused_before_any_write(self):
        closed = ComposeBrowser(detail={**DETAIL, 'close_status': 1})
        self.assertEqual(reply(closed, submit=True)['error'], 'thread_closed')
        denied = ComposeBrowser(reply_perm={'status': 200, 'body': {'errno': -1, 'msg': '您没有权限回复'}})
        result = reply(denied, submit=True)
        self.assertEqual((result['error'], result['site_message']), ('reply_permission_denied', '您没有权限回复'))
        self.assertEqual(reply(ComposeBrowser(), thread=424242, submit=True)['error'], 'thread_not_found')
        for browser in (closed, denied):
            self.assertEqual(browser.api_calls, [])

    def test_a_site_rejection_and_a_lost_response_are_never_retried(self):
        rejected = ComposeBrowser(posts=[(21182046, '主楼', None)], answer={'status': 200, 'body': {'errno': -1, 'msg': '回复太快'}})
        result = reply(rejected, submit=True)
        self.assertEqual((result['status'], result['error'], result['site_message']), ('failed', 'reply_rejected', '回复太快'))
        lost = ComposeBrowser(posts=[(21182046, '主楼', None)], lose=True)
        result = reply(lost, submit=True)
        self.assertEqual((result['status'], result['recovered'], result['confirmed'], result['pid']), ('complete', True, True, 21182200))
        self.assertEqual(len(lost.api_calls), 1)
        vanished = ComposeBrowser(posts=[(21182046, '主楼', None)], lose=True, apply=False)
        result = reply(vanished, submit=True)
        self.assertEqual((result['status'], result['error']), ('needs_attention', 'api_submission_unconfirmed'))
        self.assertEqual(len(vanished.api_calls), 1)

    def test_a_reply_by_someone_else_with_the_same_text_is_not_mine(self):
        browser = ComposeBrowser(posts=[(21182046, '主楼', None), (21182047, '一条回复', None)], lose=True, apply=False)
        result = reply(browser, submit=True)
        self.assertEqual(result['error'], 'api_submission_unconfirmed')


class PublishContractTests(unittest.TestCase):
    def test_read_back_and_review_decide_the_status(self):
        cases = [
            (dict(submitted=False, preview={}), ('complete', None)),
            (dict(submitted=True, preview={}, tid=1, confirmed=True), ('complete', None)),
            (dict(submitted=True, preview={}, tid=1, confirmed=False, error='published_content_differs'), ('needs_attention', 'published_content_differs')),
            (dict(submitted=True, preview={}, pending_review=True), ('needs_attention', 'pending_review')),
            (dict(submitted=True, preview={}), ('needs_attention', 'result_unverified')),
            (dict(submitted=True, preview={}, rejected=True, error='thread_rejected'), ('failed', 'thread_rejected')),
            (dict(submitted=False, preview={}, error='board_closed'), ('failed', 'board_closed')),
        ]
        for arguments, expected in cases:
            result = publish_result('thread', **arguments)
            self.assertEqual((result['status'], result['error']), expected, arguments)


class ThreadPageParseTests(unittest.TestCase):
    def test_the_pager_gives_the_page_and_the_canonical_gives_the_thread(self):
        page = parse_thread_page(page_with(TID, post_html(5, '第二页的楼'), page=2), TID)
        self.assertEqual((page['page'], [p['pid'] for p in page['posts']]), (2, [5]))
        with self.assertRaisesRegex(ValueError, 'unexpected_thread_document'):
            parse_thread_page(page_with(TID, post_html(5, 'x')), 1)
        with self.assertRaisesRegex(ValueError, 'unexpected_thread_document'):
            parse_thread_page(thread_html(post_html(5, 'x')), TID)
        self.assertEqual(normalize_message(' a \n\n b\t'), 'a b')


class ApiReadBoundaryTests(unittest.TestCase):
    def test_only_listed_read_paths_and_write_pairs(self):
        browser = Browser.__new__(Browser)
        browser.evaluate = lambda expression: self.fail('no request may be built for a refused target')
        for path in ['/api/threads', '/api/posts/1/reactions', '/api/v3/threads/abc', '/api/threads/1/posts']:
            with self.assertRaisesRegex(ValueError, 'unsupported_api_target', msg=path):
                browser.api_get(path)
        for method, path in [('POST', '/api/threads/1/reactions'), ('PUT', '/api/threads'), ('POST', '/api/threads/1/posts/2')]:
            with self.assertRaisesRegex(ValueError, 'unsupported_api_target', msg=(method, path)):
                browser.api_request(method, path, {})


class ComposeEntryPointTests(unittest.TestCase):
    def test_cli_and_mcp_default_to_preview_and_pass_submit_explicitly(self):
        payload = publish_result('thread', submitted=False, preview={'fid': 472})
        with patch('cli.create_thread', return_value=payload) as called, \
                patch('sys.argv', ['cli', 'post', '472', '--subject', '标题', '--message', '正文', '--typeid', '7']), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(), 0)
        self.assertEqual(called.call_args.args, (472, '标题', '正文'))
        self.assertEqual(called.call_args.kwargs, {'typeid': 7, 'sortid': None, 'submit': False, 'images': [], 'video': None})
        self.assertEqual(json.loads(out.getvalue())['submitted'], False)
        with patch('cli.reply_thread', return_value=payload) as called, \
                patch('sys.argv', ['cli', 'reply', '1190345', '--message', '回复', '--quote-pid', '21182047', '--submit']), \
                patch('sys.stdout', new_callable=io.StringIO):
            cli.main()
        self.assertEqual((called.call_args.args, called.call_args.kwargs), (('1190345', '回复'), {'quote_pid': 21182047, 'submit': True}))
        with patch('mcp_server.publish_thread', return_value=payload) as tool_called:
            tool = asyncio.run(mcp_server.server.call_tool('create_thread', {'fid': 472, 'subject': '标题', 'message': '正文'}))
        self.assertFalse(tool.is_error)
        self.assertEqual(tool_called.call_args.kwargs, {'typeid': None, 'sortid': None, 'submit': False, 'images': None, 'video': None})
        with patch('mcp_server.publish_reply', return_value=payload) as tool_called:
            tool = asyncio.run(mcp_server.server.call_tool('reply_thread', {'thread': 1190345, 'message': '回复', 'quote_pid': 5, 'submit': True}))
        self.assertFalse(tool.is_error)
        self.assertEqual(tool_called.call_args.kwargs, {'quote_pid': 5, 'submit': True})
