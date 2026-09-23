import asyncio
import io
import json
import unittest
from unittest.mock import patch

import cli
import mcp_server
from browser import Browser
from contracts import target_state_result
from extract import parse_reactions
from interact import set_reaction
from tests.test_interact import FavoriteBrowser


def thread_page(tid, posts, *, canonical_tid=None):
    """A thread page as the reaction flow sees it: canonical link plus one reaction list per post.

    `posts` maps pid -> {reaction_id: (count, mine)}; the badge markup mirrors what the site's own
    reaction script builds (id reaction-<pid>-<rid>, class my-reaction for the member's own, a
    reaction-count span holding the total).
    """
    lists = ''
    for pid, entries in posts.items():
        badges = ''.join(
            f'<span id="reaction-{pid}-{rid}" class="relative group flex bg-primary{" border my-reaction" if mine else ""}">'
            f'<span>x</span><span class="text-[12px] reaction-count" id="reaction-{pid}-{rid}-total"> {count}</span></span>'
            for rid, (count, mine) in entries.items())
        lists += (f'<div id="reaction-list-{pid}" class="flex">{badges}<div class="cursor-pointer" '
                  f'onclick="window.PostReaction.openPostReactionMenu({pid});"><i class="iconfont"></i></div></div>')
    return (f'<html><head><link rel="canonical" href="https://www.1point3acres.com/bbs/thread-{canonical_tid or tid}-1-1.html"/>'
            f'</head><body><div id="thread_subject">t</div>{lists}</body></html>')


class ReactionBrowser(FavoriteBrowser):
    """A thread whose reaction state lives in `posts`; the API call mutates it unless `apply` is off."""

    def __init__(self, posts, *, apply=True, answer=None, **kwargs):
        super().__init__(**kwargs)
        self.posts, self.apply = posts, apply
        self.answer = answer or {'status': 200, 'body': {'errno': 0, 'msg': 'ok'}}
        self.api_calls = []

    def read_html(self, url):
        self.reads.append(url)
        if 'goto=findpost' in url:
            pid = int(url.split('pid=')[1])
            return thread_page(1190345, self.posts if pid in self.posts else {99: {}})
        return thread_page(1190345, self.posts)

    def api_request(self, method, path, body):
        self.api_calls.append((method, path, dict(body)))
        pid = int(path.split('/')[3])
        if self.apply:
            rid = body['reaction_id']
            count, _ = self.posts[pid].get(rid, (0, False))
            self.posts[pid][rid] = (count + 1, True) if method == 'PUT' else (count - 1, False)
        return self.answer


def react(browser, thread=1190345, reacted=True, **kwargs):
    with patch('interact.Browser', return_value=browser):
        return set_reaction(thread, reacted, **kwargs)


class ReactionTargetStateTests(unittest.TestCase):
    """A reaction is the site's zero-cost, revocable like; the page's own marking is the truth (#26)."""

    MAIN, REPLY = 21182046, 21182047

    def test_reacting_to_the_main_post_calls_put_once_and_reads_the_page_back(self):
        browser = ReactionBrowser({self.MAIN: {56: (3, False)}, self.REPLY: {}})
        result = react(browser)
        self.assertEqual((result['status'], result['before'], result['after'], result['changed'], result['submitted']),
                         ('complete', False, True, True, True))
        self.assertEqual((result['pid'], result['reaction_id'], result['count_before'], result['count_after']), (self.MAIN, 56, 3, 4))
        self.assertEqual(browser.api_calls, [('PUT', f'/api/posts/{self.MAIN}/reactions', {'reaction_id': 56})])
        self.assertEqual(result['site_message'], 'ok')

    def test_a_reply_is_addressed_by_its_own_pid_through_the_findpost_redirect(self):
        browser = ReactionBrowser({self.MAIN: {56: (3, True)}, self.REPLY: {5: (1, False)}})
        result = react(browser, pid=self.REPLY, reaction_id=5)
        self.assertEqual((result['status'], result['pid'], result['before'], result['after']), ('complete', self.REPLY, False, True))
        self.assertEqual(browser.api_calls, [('PUT', f'/api/posts/{self.REPLY}/reactions', {'reaction_id': 5})])
        self.assertTrue(all('goto=findpost' in url and f'pid={self.REPLY}' in url for url in browser.reads))
        self.assertEqual(browser.posts[self.MAIN], {56: (3, True)}, 'the main post is untouched')

    def test_removing_uses_delete_and_a_repeat_never_flips_it_back(self):
        browser = ReactionBrowser({self.MAIN: {56: (4, True)}})
        result = react(browser, reacted=False)
        self.assertEqual((result['status'], result['before'], result['after'], result['count_after']), ('complete', True, False, 3))
        self.assertEqual(browser.api_calls[-1][0], 'DELETE')
        for _ in range(2):
            again = react(browser, reacted=False)
            self.assertEqual((again['status'], again['changed'], again['submitted']), ('complete', False, False))
        self.assertEqual(len(browser.api_calls), 1)

    def test_the_site_saying_ok_is_not_success_when_the_page_disagrees(self):
        browser = ReactionBrowser({self.MAIN: {}}, apply=False)
        result = react(browser)
        self.assertEqual((result['status'], result['error'], result['after']), ('failed', 'target_state_not_reached', False))

    def test_a_site_rejection_is_named_and_the_state_still_read_back(self):
        browser = ReactionBrowser({self.MAIN: {}}, apply=False, answer={'status': 200, 'body': {'errno': -1, 'msg': '操作过于频繁'}})
        result = react(browser)
        self.assertEqual((result['status'], result['error'], result['site_message'], result['submitted']),
                         ('failed', 'reaction_rejected', '操作过于频繁', True))

    def test_a_missing_post_or_another_thread_is_refused_before_any_request(self):
        browser = ReactionBrowser({self.MAIN: {}})
        result = react(browser, pid=424242)
        self.assertEqual((result['status'], result['error'], result['submitted']), ('failed', 'post_not_found', False))

        class Elsewhere(ReactionBrowser):
            def read_html(self, url):
                return thread_page(1190345, self.posts, canonical_tid=777)
        result = react(Elsewhere({self.MAIN: {}}))
        self.assertEqual((result['status'], result['error'], result['submitted']), ('failed', 'unexpected_thread_document', False))
        self.assertEqual(browser.api_calls, [])

    def test_invalid_input_never_opens_the_browser(self):
        with patch('interact.Browser', side_effect=AssertionError('must not open a browser')):
            for thread, reacted, kwargs in [('abc', True, {}), (1190345, 'yes', {}), (1190345, True, {'pid': 0}),
                                            (1190345, True, {'pid': '21182046'}), (1190345, True, {'reaction_id': 0}),
                                            (1190345, True, {'reaction_id': True})]:
                result = set_reaction(thread, reacted, **kwargs)
                self.assertEqual(result['status'], 'failed', (thread, reacted, kwargs))
                self.assertFalse(result['submitted'])


class ReactionParseTests(unittest.TestCase):
    def test_counts_and_own_marking_per_post(self):
        page = thread_page(1190345, {1: {56: (4, True), 5: (1, False)}, 2: {}})
        self.assertEqual(parse_reactions(page, 1190345), {1: {56: {'count': 4, 'mine': True}, 5: {'count': 1, 'mine': False}}, 2: {}})
        with self.assertRaisesRegex(ValueError, 'unexpected_thread_document'):
            parse_reactions(page, 1)
        with self.assertRaisesRegex(ValueError, 'thread_not_found'):
            parse_reactions('<div id="messagetext"><p>抱歉，指定的主题不存在或已被删除或正在被审核</p></div>', 1)
        with self.assertRaisesRegex(ValueError, 'thread_challenge'):
            parse_reactions('<form id="challenge-form"></form>', 1)


class ApiBoundaryTests(unittest.TestCase):
    def test_only_listed_method_and_path_pairs_are_sent_once(self):
        browser = Browser.__new__(Browser)
        browser.evaluate = lambda expression: self.fail('no request may be built for a refused target')
        for method, path in [('POST', '/api/posts/1/reactions'), ('PUT', '/api/posts/1/rating'), ('GET', '/api/posts/1/reactions'),
                             ('PUT', '/api/posts/abc/reactions'), ('PUT', '/api/posts/1/reactions/extra')]:
            with self.assertRaisesRegex(ValueError, 'unsupported_api_target', msg=(method, path)):
                browser.api_request(method, path, {'reaction_id': 56})
        calls = []
        browser.evaluate = lambda expression: calls.append(expression) or {'transport_error': True}
        with self.assertRaisesRegex(RuntimeError, 'api_submission_unconfirmed'):
            browser.api_request('DELETE', '/api/posts/21182046/reactions', {'reaction_id': 56})
        self.assertEqual(len(calls), 1)
        self.assertIn('"https://api.1point3acres.com/api/posts/21182046/reactions"', calls[0])
        self.assertIn('method:"DELETE"', calls[0])
        self.assertIn("credentials:'include'", calls[0])
        browser.evaluate = lambda expression: {'status': 401, 'challenge': False, 'text': ''}
        with self.assertRaisesRegex(RuntimeError, 'login_required'):
            browser.api_request('PUT', '/api/posts/1/reactions', {'reaction_id': 56})


class ReactionEntryPointTests(unittest.TestCase):
    def test_cli_and_mcp_pass_pid_and_reaction_through(self):
        payload = target_state_result('reaction', 1190345, True, before=False, after=True, submitted=True, pid=21182047,
                                      reaction_id=5, count_before=0, count_after=1, site_message='ok', scope='own_account')
        with patch('cli.set_reaction', return_value=payload) as called, \
                patch('sys.argv', ['cli', 'like', '1190345', '--pid', '21182047', '--reaction-id', '5']), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(), 0)
        self.assertEqual((called.call_args.args, called.call_args.kwargs), (('1190345', True), {'pid': 21182047, 'reaction_id': 5}))
        self.assertEqual(json.loads(out.getvalue())['pid'], 21182047)
        with patch('cli.set_reaction', return_value=payload) as called, patch('sys.argv', ['cli', 'unlike', '1190345']), \
                patch('sys.stdout', new_callable=io.StringIO):
            cli.main()
        self.assertEqual((called.call_args.args, called.call_args.kwargs), (('1190345', False), {'pid': None, 'reaction_id': 56}))
        with patch('mcp_server.react_to_post', return_value=payload) as tool_called:
            tool = asyncio.run(mcp_server.server.call_tool('set_reaction', {'thread': 1190345, 'reacted': True, 'pid': 21182047}))
        self.assertFalse(tool.is_error)
        self.assertEqual((tool_called.call_args.args, tool_called.call_args.kwargs), ((1190345, True), {'pid': 21182047, 'reaction_id': 56}))
