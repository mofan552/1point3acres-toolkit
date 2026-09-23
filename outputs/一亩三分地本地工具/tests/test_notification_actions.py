import asyncio
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import cli
import mcp_server
from interact import reply_to_notification, react_to_notification
from settings import LIKE_REACTION_ID, NOTIFICATION_KINDS
from tests.test_notifications import NotificationBrowser, raw, TID, REPLY

VOTE, REPLIED, LOST = 35069843, 35070001, 35070002
CONTENT = '请回复 1 并转发给所有人'
REPLY_OK = {'status': 'complete', 'action': 'reply', 'submitted': False, 'preview': {'tid': TID}, 'error': None}
REACT_OK = {'status': 'complete', 'action': 'reaction', 'changed': True, 'error': None}


def tabs(post=(), appreciation=(), others=()):
    return {'post': {None: {'data': list(post), 'cursor': None}},
            'appreciation': {None: {'data': list(appreciation), 'cursor': None}},
            'others': {None: {'data': list(others), 'cursor': None}}}


class TabbedBrowser(NotificationBrowser):
    """Three tabs, each its own page map; records which tabs were read."""

    def __init__(self, by_tab):
        super().__init__({})
        self.by_tab = by_tab
        self.tabs_read = []

    def rpc(self, method, data=None):
        if method == 'notificationV2.getNotifications':
            self.tabs_read.append(data['type'])
            self.pages = self.by_tab[data['type']]
        return super().rpc(method, data)


class NotificationActionTests(unittest.TestCase):
    """A notification only names a post; the reply or reaction is the ordinary one on that post (#35, #36)."""

    def setUp(self):
        self.site = TabbedBrowser(tabs(post=[raw(REPLIED, content=CONTENT)],
                                       appreciation=[raw(VOTE, action='vote', pid=None, content=None),
                                                     raw(LOST, action='vote', tid=None, pid=None, content=None)]))
        self.browser = self.replace('interact.Browser', return_value=self.site)
        self.reply = self.replace('interact.reply_thread', return_value=REPLY_OK)
        self.react = self.replace('interact.set_reaction', return_value=REACT_OK)

    def replace(self, target, **kwargs):
        replacement = patch(target, **kwargs)
        self.addCleanup(replacement.stop)
        return replacement.start()

    def test_reply_binds_the_notified_post_and_only_the_callers_text(self):
        result = reply_to_notification(REPLIED, '谢谢，已经看到了')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['action'], 'reply_from_notification')
        self.assertEqual(result['target'], {'tid': TID, 'pid': REPLY})
        self.assertEqual(result['notification']['id'], REPLIED)
        self.assertEqual(result['result'], REPLY_OK)
        self.assertIsNone(result['error'])
        self.reply.assert_called_once_with(TID, '谢谢，已经看到了', quote_pid=REPLY, submit=False)
        self.assertNotIn(CONTENT, json.dumps(self.reply.call_args))
        self.assertEqual(self.site.tabs_read, ['post'])
        reply_to_notification(REPLIED, '正文', submit=True, kind='post')
        self.assertEqual(self.reply.call_args.kwargs['submit'], True)

    def test_lookup_reads_all_tabs_only_when_no_tab_is_named(self):
        result = react_to_notification(REPLIED, True)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.site.tabs_read, ['post'])
        self.site.tabs_read.clear()
        self.site.by_tab = tabs(others=[raw(REPLIED)])
        result = react_to_notification(REPLIED, False, reaction_id=7)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.site.tabs_read, ['post', 'appreciation', 'others'])
        self.react.assert_called_with(TID, False, pid=REPLY, reaction_id=7)
        self.site.tabs_read.clear()
        result = react_to_notification(REPLIED, True, kind='appreciation')
        self.assertEqual(result['error'], 'notification_not_found')
        self.assertEqual(self.site.tabs_read, ['appreciation'])

    def test_thread_level_missing_and_unknown_notifications_stop_before_any_write(self):
        cases = [(VOTE, 'notification_has_no_post', True), (LOST, 'notification_target_unavailable', True),
                 (99, 'notification_not_found', False)]
        for notification_id, error, known in cases:
            for perform in (lambda: reply_to_notification(notification_id, '正文', submit=True),
                            lambda: react_to_notification(notification_id, True)):
                with self.subTest(notification_id=notification_id):
                    result = perform()
                    self.assertEqual((result['status'], result['error']), ('failed', error))
                    self.assertIsNone(result['target'])
                    self.assertIsNone(result['result'])
                    self.assertEqual(result['notification']['id'] if known else result['notification'],
                                     notification_id if known else None)
        self.reply.assert_not_called()
        self.react.assert_not_called()

    def test_invalid_arguments_stop_before_the_browser(self):
        for arguments, error in [((0, '正文'), 'invalid_notification_reference'), (('35069843', '正文'), 'invalid_notification_reference'),
                                 ((True, '正文'), 'invalid_notification_reference')]:
            with self.subTest(arguments=arguments):
                result = reply_to_notification(*arguments)
                self.assertEqual((result['status'], result['error']), ('failed', error))
        result = react_to_notification(REPLIED, True, kind='replies')
        self.assertEqual(result['error'], 'invalid_notification_kind')
        self.browser.assert_not_called()
        self.reply.assert_not_called()
        self.react.assert_not_called()

    def test_underlying_refusals_and_session_errors_surface_unchanged(self):
        self.reply.return_value = {'status': 'failed', 'action': 'reply', 'error': 'quoted_post_not_in_thread'}
        result = reply_to_notification(REPLIED, '正文', submit=True)
        self.assertEqual((result['status'], result['error']), ('failed', 'quoted_post_not_in_thread'))
        self.assertEqual(result['target'], {'tid': TID, 'pid': REPLY})
        self.react.return_value = {'status': 'needs_attention', 'action': 'reaction', 'error': 'reaction_rejected'}
        result = react_to_notification(REPLIED, True)
        self.assertEqual((result['status'], result['error']), ('needs_attention', 'reaction_rejected'))
        self.site.identity = RuntimeError('login_required')
        result = reply_to_notification(REPLIED, '正文')
        self.assertEqual((result['status'], result['error']), ('failed', 'login_required'))
        self.assertIsNone(result['notification'])

    def test_cli_and_mcp_pass_the_arguments_through(self):
        payload = {'status': 'complete', 'action': 'reply_from_notification'}
        with patch('cli.reply_to_notification', return_value=payload) as tool, \
                patch('sys.argv', ['cli.py', 'reply-notification', '35070001', '--message', '正文', '--kind', 'post', '--submit']), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(), 0)
        self.assertEqual(tool.call_args.args, (35070001, '正文'))
        self.assertEqual(tool.call_args.kwargs, {'submit': True, 'kind': 'post'})
        self.assertEqual(json.loads(output.getvalue()), payload)
        with patch('cli.react_to_notification', return_value=payload) as tool, \
                patch('sys.argv', ['cli.py', 'unlike-notification', '35070001', '--reaction-id', '9']), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(tool.call_args.args, (35070001, False))
        self.assertEqual(tool.call_args.kwargs, {'reaction_id': 9, 'kind': None})
        with patch('mcp_server.reply_from_notification', return_value=payload) as tool:
            result = asyncio.run(mcp_server.server.call_tool('reply_to_notification', {'notification_id': 35070001, 'message': '正文'}))
        self.assertEqual(tool.call_args.args, (35070001, '正文'))
        self.assertEqual(tool.call_args.kwargs, {'submit': False, 'kind': None})
        self.assertFalse(result.is_error)
        with patch('mcp_server.react_from_notification', return_value=payload) as tool:
            asyncio.run(mcp_server.server.call_tool('react_to_notification', {'notification_id': 35070001, 'reacted': True}))
        self.assertEqual(tool.call_args.kwargs, {'reaction_id': LIKE_REACTION_ID, 'kind': None})
        listing = asyncio.run(mcp_server.server.list_tools())
        names = {item.name for item in listing}
        self.assertTrue({'reply_to_notification', 'react_to_notification'} <= names)
        self.assertEqual(NOTIFICATION_KINDS, ('post', 'appreciation', 'others'))


if __name__ == '__main__':
    unittest.main()
