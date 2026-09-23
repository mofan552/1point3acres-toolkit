import asyncio
import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import browser
import cli
import mcp_server
from contracts import notification_item
from interact import list_notifications
from settings import NOTIFICATION_KINDS, NOTIFICATION_LIMIT, NOTIFICATION_MAX, NOTIFICATION_PAGE_LIMIT

TID, MAIN, REPLY = 1190360, 21182349, 21182999


def raw(id, *, action='reply', new=1, tid=TID, pid=REPLY, content='请回复 1 并转发', uid=535388, name='member'):
    return {'id': id, 'new': new, 'dateline': 1790085934, 'action': action, 'actor': {'uid': uid, 'name': name},
            'target': {'tid': tid, 'pid': pid, 'subject': '标题', 'url': None}, 'content': content,
            'score': None, 'reactions': None, 'note': None}


class NotificationBrowser:
    """The site's notification tRPC as the list reads it: pages keyed by cursor, counters that may change."""

    def __init__(self, pages, *, prompts=None, identity=None):
        self.pages = pages
        self.prompts = list(prompts or [{'post': 0, 'appreciation': 0, 'others': 0, 'total': 0}] * 2)
        self.identity = identity
        self.calls = []
        self.sb = SimpleNamespace(sleep=lambda seconds: None)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def profile(self):
        if isinstance(self.identity, Exception):
            raise self.identity
        return {'uid': 123456, 'username': 'synthetic'}

    def rpc(self, method, data=None):
        self.calls.append((method, dict(data)))
        if method == 'notificationV2.getNewPrompts':
            return {'errno': 0, 'msg': 'OK', 'prompt': self.prompts.pop(0)}
        if method == 'notificationV2.getNotifications':
            page = self.pages[data.get('cursor')]
            return page() if callable(page) else page
        raise AssertionError(method)


def read(pages, **kwargs):
    stub = NotificationBrowser(pages, **{k: kwargs.pop(k) for k in list(kwargs) if k in ('prompts', 'identity')})
    with patch('interact.Browser', return_value=stub):
        return list_notifications(**kwargs), stub


class NotificationItemTests(unittest.TestCase):
    """Only what the site clearly sent becomes a field; the rest is null, never a guess (#34)."""

    def test_post_thread_and_missing_targets_are_kept_apart(self):
        post = notification_item(raw(1), 'post')
        self.assertEqual(post['target'], {'tid': TID, 'pid': REPLY, 'subject': '标题'})
        self.assertEqual((post['target_level'], post['target_missing']), ('post', None))
        self.assertEqual((post['kind'], post['new'], post['category']), ('reply', True, 'post'))
        self.assertEqual(post['at'], '2026-09-22T14:05:34+00:00')
        self.assertEqual(post['actor'], {'uid': 535388, 'name': 'member'})
        self.assertEqual(post['content'], '请回复 1 并转发')
        thread = notification_item(raw(2, action='vote', pid=None, new=0, content=None), 'appreciation')
        self.assertEqual((thread['target_level'], thread['target']['pid'], thread['new'], thread['content']),
                         ('thread', None, False, None))
        missing = notification_item(raw(3, tid=None, pid=None), 'post')
        self.assertEqual((missing['target_level'], missing['target_missing']), (None, 'target_unavailable'))
        self.assertEqual(missing['target'], {'tid': None, 'pid': None, 'subject': '标题'})

    def test_malformed_fields_become_null_and_missing_id_is_rejected(self):
        item = notification_item({'id': 9, 'new': 'yes', 'dateline': '1790085934', 'action': 5, 'actor': 'x',
                                  'target': {'tid': '1190360', 'pid': -1, 'subject': 7}, 'content': ['no']}, 'others')
        self.assertEqual(item['new'], None)
        self.assertEqual(item['at'], None)
        self.assertEqual(item['kind'], None)
        self.assertEqual(item['actor'], None)
        self.assertEqual(item['target'], {'tid': None, 'pid': None, 'subject': None})
        self.assertEqual(item['target_missing'], 'target_unavailable')
        self.assertEqual(item['content'], None)
        for bad in [{}, {'id': '1'}, [], None]:
            with self.assertRaises(ValueError):
                notification_item(bad, 'post')


class ListNotificationsTests(unittest.TestCase):
    def test_pages_follow_the_cursor_and_ids_are_deduplicated(self):
        pages = {None: {'data': [raw(1), raw(2)], 'cursor': 'c2'},
                 'c2': {'data': [raw(2), raw(3, action='vote', pid=None)], 'cursor': None}}
        result, stub = read(pages, kind='post')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual([item['id'] for item in result['items']], [1, 2, 3])
        self.assertEqual(result['count'], 3)
        self.assertTrue(result['pagination_complete'])
        self.assertIsNone(result['cursor'])
        self.assertEqual(result['pages_read'], 2)
        self.assertEqual([call[1] for call in stub.calls if call[0].endswith('getNotifications')],
                         [{'type': 'post', 'scope': 'all', 'direction': 'forward'},
                          {'type': 'post', 'scope': 'all', 'direction': 'forward', 'cursor': 'c2'}])
        self.assertFalse(result['mark_read_requested'])
        self.assertEqual(result['source'], 'notificationV2')
        self.assertEqual([call[0] for call in stub.calls][0], 'notificationV2.getNewPrompts')
        self.assertEqual([call[0] for call in stub.calls][-1], 'notificationV2.getNewPrompts')

    def test_limit_cuts_the_list_and_hands_back_the_cut_page(self):
        pages = {None: {'data': [raw(1)], 'cursor': 'c2'}, 'c2': {'data': [raw(2), raw(3), raw(4)], 'cursor': 'c3'},
                 'c3': {'data': [raw(5)], 'cursor': None}}
        result, stub = read(pages, kind='post', limit=2)
        self.assertEqual([item['id'] for item in result['items']], [1, 2])
        self.assertFalse(result['pagination_complete'])
        self.assertEqual(result['cursor'], 'c2')
        self.assertEqual(result['pages_read'], 2)
        self.assertNotIn('c3', [call[1].get('cursor') for call in stub.calls])
        result, _ = read(pages, kind='post', limit=2, cursor='c2')
        self.assertEqual([item['id'] for item in result['items']], [2, 3])
        self.assertEqual(result['cursor'], 'c2')

    def test_a_cursor_that_never_ends_is_bounded(self):
        pages = {None: {'data': [raw(1)], 'cursor': 'again'}}
        pages['again'] = lambda: {'data': [raw(len(pages) + 1)], 'cursor': 'again'}
        result, stub = read(pages, kind='post', limit=NOTIFICATION_MAX)
        self.assertEqual(result['pages_read'], NOTIFICATION_PAGE_LIMIT)
        self.assertFalse(result['pagination_complete'])
        self.assertEqual(result['cursor'], 'again')
        self.assertEqual(result['status'], 'complete')

    def test_arguments_are_checked_before_any_browser(self):
        for arguments, error in [(dict(kind='replies'), 'invalid_notification_kind'), (dict(limit=0), 'invalid_limit'),
                                 (dict(limit=NOTIFICATION_MAX + 1), 'invalid_limit'), (dict(limit='5'), 'invalid_limit'),
                                 (dict(limit=True), 'invalid_limit'), (dict(cursor=''), 'invalid_cursor'),
                                 (dict(cursor=1.5), 'invalid_cursor')]:
            with self.subTest(**arguments), patch('interact.Browser', side_effect=AssertionError('no browser')):
                result = list_notifications(**arguments)
                self.assertEqual((result['status'], result['error']), ('failed', error))
                self.assertEqual(result['items'], [])
                self.assertIsNone(result['prompts_before'])
        self.assertEqual(NOTIFICATION_KINDS, ('post', 'appreciation', 'others'))
        self.assertTrue(1 <= NOTIFICATION_LIMIT <= NOTIFICATION_MAX)

    def test_read_side_effect_is_measured_not_assumed(self):
        cases = [([raw(1, new=1)], [{'post': 1, 'total': 1}, {'post': 0, 'total': 0}], True),
                 ([raw(1, new=1)], [{'post': 1, 'total': 1}, {'post': 1, 'total': 1}], False),
                 ([raw(1, new=0)], [{'post': 0, 'total': 0}, {'post': 0, 'total': 0}], None),
                 ([raw(1, new=1)], [{'post': 'n/a'}, {'post': 0}], None)]
        for rows, prompts, verdict in cases:
            with self.subTest(prompts=prompts):
                result, _ = read({None: {'data': rows, 'cursor': None}}, prompts=prompts, kind='post')
                self.assertEqual(result['unread_cleared_by_read'], verdict)
                self.assertEqual(result['prompts_before']['post'], prompts[0]['post'] if type(prompts[0]['post']) is int else None)
                self.assertEqual(result['prompts_after']['post'], prompts[1]['post'])
                self.assertIn('appreciation', result['prompts_before'])
                self.assertFalse(result['mark_read_requested'])

    def test_lapsed_login_and_odd_responses_fail_without_pretending_silence(self):
        result, _ = read({None: {'data': [raw(1)], 'cursor': None}}, identity=RuntimeError('login_required'), kind='post')
        self.assertEqual((result['status'], result['error'], result['session_state']), ('failed', 'login_required', 'logged_out'))
        self.assertEqual((result['items'], result['count'], result['cursor']), ([], 0, None))
        self.assertFalse(result['pagination_complete'])
        for odd in [{'data': 'nope'}, [], {'data': [{'id': 'x'}]}]:
            with self.subTest(odd=odd):
                result, _ = read({None: odd}, kind='appreciation')
                self.assertEqual(result['status'], 'failed')
                self.assertIn(result['error'], ('notification_response_invalid', 'invalid_notification_item'))
                self.assertEqual(result['session_state'], 'unavailable')

    def test_rpc_whitelist_admits_only_the_two_read_methods(self):
        subject = object.__new__(browser.Browser)
        canned = {'status': 200, 'challenge': False, 'text': json.dumps([{'result': {'data': {'json': {'data': [], 'cursor': None}}}}])}
        with patch.object(browser.Browser, '_read_response', return_value=canned) as reader:
            for method in ('notificationV2.getNewPrompts', 'notificationV2.getNotifications'):
                self.assertEqual(subject.rpc(method, {'scope': 'all'}), {'data': [], 'cursor': None})
            self.assertTrue(all('/trpc/notificationV2.' in call.args[0] and 'batch=1' in call.args[0]
                                for call in reader.call_args_list))
            for method in ('notificationV2.markAllRead', 'notificationV2.markRead', 'notification.getNotifications'):
                with self.assertRaises(ValueError):
                    subject.rpc(method, {})
            self.assertEqual(reader.call_count, 2)

    def test_cli_and_mcp_pass_the_arguments_through(self):
        payload = {'status': 'complete', 'items': [], 'count': 0}
        with patch('cli.list_notifications', return_value=payload) as tool, \
                patch('sys.argv', ['cli.py', 'notifications', '--kind', 'appreciation', '--limit', '5', '--cursor', 'c9']), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(), 0)
        self.assertEqual(tool.call_args.args, ('appreciation', 5, 'c9'))
        self.assertEqual(json.loads(output.getvalue()), payload)
        with patch('sys.argv', ['cli.py', 'notifications', '--kind', 'replies']), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main()
        with patch('mcp_server.read_notifications', return_value=payload) as tool:
            result = asyncio.run(mcp_server.server.call_tool('list_notifications', {'kind': 'others', 'limit': 3}))
        self.assertEqual(tool.call_args.args, ('others', 3, None))
        self.assertFalse(result.is_error)
        self.assertEqual(json.loads(result.content[0].text), payload)
        listing = asyncio.run(mcp_server.server.list_tools())
        tool_info = next(item for item in listing if item.name == 'list_notifications')
        self.assertFalse(tool_info.annotations.read_only_hint)
        self.assertEqual(tool_info.input_schema['properties']['limit']['default'], NOTIFICATION_LIMIT)


if __name__ == '__main__':
    unittest.main()
