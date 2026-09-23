import asyncio
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError
import cli
import mcp_server
from browser import Browser
from contracts import target_state_result
from extract import parse_discuz_ajax
from interact import set_favorite
from tests.test_extract import favorites_html

UID = 123456
FORMHASH = '06f891f1'
ADD_FORM = ('<?xml version="1.0" encoding="gbk"?>\n<root><![CDATA[<h3 class="flb"><em id="return_k_favorite">收藏</em></h3>'
            '<form method="post" id="favoriteform_%(tid)d" action="home.php?mod=spacecp&amp;ac=favorite&amp;type=thread&amp;id=%(tid)d&amp;spaceuid=0">'
            '<input type="hidden" name="favoritesubmit" value="true" /><input type="hidden" name="referer" value="https://www.1point3acres.com/next/daily-checkin" />'
            '<input type="hidden" name="formhash" value="' + FORMHASH + '" /><input type="hidden" name="handlekey" value="k_favorite" />'
            '<div class="c"><p>已有 <b>0</b> 人收藏</p><textarea name="description"></textarea></div></form>'
            '<script type="text/javascript">function succeedhandle_k_favorite(url, msg, values) {hideWindow(\'k_favorite\');}</script>]]></root>')
DELETE_FORM = ('<?xml version="1.0" encoding="gbk"?>\n<root><![CDATA[<h3 class="flb"><em id="return_delfavorite">取消收藏</em></h3>'
               '<form id="favoriteform_%(favid)d" method="post" action="home.php?mod=spacecp&amp;ac=favorite&amp;op=delete&amp;favid=%(favid)d&amp;type=all">'
               '<input type="hidden" name="referer" value="x" /><input type="hidden" name="deletesubmit" value="true" />'
               '<input type="hidden" name="formhash" value="' + FORMHASH + '" /><input type="hidden" name="handlekey" value="delfavorite" />'
               '<div class="c">您确定要删除此收藏吗？</div></form>]]></root>')
NOT_FAVORITABLE = ('<?xml version="1.0" encoding="gbk"?>\n<root><![CDATA[<h3 class="flb"><em>提示信息</em></h3><div class="c altw">'
                   '<div class="alert_error">抱歉，您指定的信息无法收藏<script type="text/javascript" reload="1">'
                   "if(typeof errorhandle_k_favorite=='function') {errorhandle_k_favorite('抱歉，您指定的信息无法收藏', {});}</script></div></div>]]></root>")
SUCCESS = ('<?xml version="1.0" encoding="gbk"?>\n<root><![CDATA[<script type="text/javascript" reload="1">'
           "if(typeof succeedhandle_k_favorite=='function') {succeedhandle_k_favorite('home.php?mod=space&do=favorite&view=me', '操作成功', {});}"
           "hideMenu();showDialog('操作成功', 'right', null, function () {});</script>]]></root>")
REPEATED = ('<?xml version="1.0" encoding="gbk"?>\n<root><![CDATA[<div class="alert_error">您已收藏过了<script type="text/javascript" reload="1">'
            "if(typeof errorhandle_k_favorite=='function') {errorhandle_k_favorite('您已收藏过了', {});}</script></div>]]></root>")
LIST_URL = f'https://www.1point3acres.com/bbs/home.php?mod=space&uid={UID}&do=favorite&view=me&type=thread'
PAGE2_URL = f'https://www.1point3acres.com/bbs/home.php?mod=space&uid={UID}&do=favorite&type=thread&page=2'


class FavoriteBrowser:
    """The site as the favorites flow sees it: a paged favorites list, two popups, and one form endpoint.

    `favorites` is the member's list newest first as (favid, tid). `apply` decides whether a posted form
    really changes the list, so a site that says ok while doing nothing can be simulated.
    """

    def __init__(self, favorites=(), *, per_page=20, popup=None, response=SUCCESS, apply=True, next_favid=7000000):
        self.favorites = list(favorites)
        self.per_page, self.popup, self.response, self.apply, self.next_favid = per_page, popup, response, apply, next_favid
        self.reads, self.posts = [], []
        self.sb = SimpleNamespace(sleep=lambda seconds: None)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def profile(self):
        return {'uid': UID, 'username': 'synthetic'}

    def read_html(self, url):
        self.reads.append(url)
        if 'do=favorite' in url and 'mod=space&' in url:
            page = int(url.split('page=')[1]) if 'page=' in url else 1
            rows = self.favorites[(page - 1) * self.per_page:page * self.per_page]
            more = len(self.favorites) > page * self.per_page
            return favorites_html([(favid, tid, '2026-9-9 09:00') for favid, tid in rows],
                                  next_url=PAGE2_URL.replace('page=2', f'page={page + 1}') if more else None,
                                  empty=not rows)
        if self.popup is not None:
            return self.popup
        if 'op=delete' in url:
            return DELETE_FORM % {'favid': int(url.split('favid=')[1].split('&')[0])}
        return ADD_FORM % {'tid': int(url.split('id=')[1].split('&')[0])}

    def submit_form(self, url, fields):
        self.posts.append((url, dict(fields)))
        if self.apply:
            if fields.get('favoritesubmit') == 'true':
                tid = int(url.split('id=')[1].split('&')[0])
                self.favorites.insert(0, (self.next_favid, tid))
            elif fields.get('deletesubmit') == 'true':
                favid = int(url.split('favid=')[1].split('&')[0])
                self.favorites = [row for row in self.favorites if row[0] != favid]
        return self.response


def run(browser, thread=1190345, favorited=True, **kwargs):
    with patch('interact.Browser', return_value=browser):
        return set_favorite(thread, favorited, **kwargs)


class FavoriteTargetStateTests(unittest.TestCase):
    """Favorite and unfavorite by target state; the favorites list is the truth before and after (#25)."""

    def test_adding_reads_the_state_submits_the_site_form_once_and_reads_back(self):
        browser = FavoriteBrowser([(6942247, 1134743)])
        result = run(browser, 1190345, True)
        self.assertEqual((result['status'], result['before'], result['after'], result['changed'], result['submitted']),
                         ('complete', False, True, True, True))
        self.assertEqual(result['favorite_id'], 7000000)
        self.assertEqual(result['site_message'], '操作成功')
        self.assertEqual(len(browser.posts), 1)
        url, fields = browser.posts[0]
        self.assertEqual(url, 'https://www.1point3acres.com/bbs/home.php?mod=spacecp&ac=favorite&type=thread&id=1190345&spaceuid=0')
        self.assertEqual(fields, {'favoritesubmit': 'true', 'referer': 'https://www.1point3acres.com/next/daily-checkin',
                                  'formhash': FORMHASH, 'handlekey': 'k_favorite', 'description': ''})
        self.assertEqual(result['error'], None)

    def test_removing_uses_the_favorite_id_the_list_gave_and_reads_back(self):
        browser = FavoriteBrowser([(6942247, 1190345), (6866152, 1173634)])
        result = run(browser, 'https://www.1point3acres.com/bbs/thread-1190345-1-1.html', False)
        self.assertEqual((result['status'], result['before'], result['after'], result['changed']), ('complete', True, False, True))
        url, fields = browser.posts[0]
        self.assertIn('op=delete&favid=6942247', url)
        self.assertEqual(fields['deletesubmit'], 'true')
        self.assertEqual(fields['formhash'], FORMHASH)
        self.assertEqual(browser.favorites, [(6866152, 1173634)])

    def test_a_state_already_at_the_goal_is_a_no_op_and_a_repeat_never_flips_it(self):
        browser = FavoriteBrowser([(6942247, 1190345)])
        for _ in range(2):
            result = run(browser, 1190345, True)
            self.assertEqual((result['status'], result['changed'], result['submitted']), ('complete', False, False))
            self.assertEqual(result['favorite_id'], 6942247)
        self.assertEqual(browser.posts, [])
        # The same for the other direction: two unfavorite requests cannot re-add anything.
        result = run(browser, 1190345, False)
        self.assertEqual((result['status'], result['changed']), ('complete', True))
        again = run(browser, 1190345, False)
        self.assertEqual((again['status'], again['changed'], again['submitted']), ('complete', False, False))
        self.assertEqual(len(browser.posts), 1)

    def test_the_site_saying_ok_is_not_success_when_the_read_back_disagrees(self):
        browser = FavoriteBrowser([], apply=False)
        result = run(browser, 1190345, True)
        self.assertEqual((result['status'], result['error'], result['submitted'], result['after']),
                         ('failed', 'target_state_not_reached', True, False))
        self.assertEqual(result['site_message'], '操作成功')

    def test_a_site_refusal_is_still_judged_by_the_read_back(self):
        # The list changed between the first read and the submission (another client favorited it); the
        # site refuses the repeat, and the read-back, not the refusal, says the goal is reached.
        class StaleListBrowser(FavoriteBrowser):
            def read_html(self, url):
                html = super().read_html(url)
                if 'mod=space&' in url and not self.favorites:
                    self.favorites.append((6000000, 1190345))
                return html
        browser = StaleListBrowser([], response=REPEATED, apply=False)
        result = run(browser, 1190345, True)
        self.assertEqual((result['status'], result['before'], result['after'], result['submitted'], result['changed']),
                         ('complete', False, True, True, True))
        self.assertEqual(result['site_message'], '您已收藏过了')
        self.assertEqual(len(browser.posts), 1)

    def test_an_unknown_state_never_turns_into_a_blind_submission(self):
        browser = FavoriteBrowser([(6942247, 1134743), (6866152, 1173634), (6831815, 1142076)], per_page=1)
        result = run(browser, 1190345, True, list_pages=2)
        self.assertEqual((result['status'], result['error'], result['before'], result['submitted']),
                         ('failed', 'favorite_state_unverified', None, False))
        self.assertEqual(result['favorites_pages_scanned'], 2)
        self.assertEqual(browser.posts, [])
        # With enough pages the same request finds the end of the list and acts.
        result = run(browser, 1190345, True, list_pages=4)
        self.assertEqual((result['status'], result['after']), ('complete', True))

    def test_a_thread_the_site_cannot_favorite_is_named_without_submitting(self):
        browser = FavoriteBrowser([], popup=NOT_FAVORITABLE)
        result = run(browser, 999999999, True)
        self.assertEqual((result['status'], result['error'], result['submitted']), ('failed', 'thread_not_favoritable', False))
        self.assertEqual(browser.posts, [])

    def test_a_lost_submission_is_attention_not_success_and_not_a_retry(self):
        class LostBrowser(FavoriteBrowser):
            def submit_form(self, url, fields):
                self.posts.append((url, dict(fields)))
                raise RuntimeError('form_submission_unconfirmed')
        browser = LostBrowser([])
        result = run(browser, 1190345, True)
        # The site may have acted after the request left: attention for a human, not a retry and not success.
        self.assertEqual((result['status'], result['error'], result['submitted'], result['after']),
                         ('needs_attention', 'form_submission_unconfirmed', True, None))
        self.assertEqual(len(browser.posts), 1)

    def test_invalid_input_never_opens_the_browser(self):
        with patch('interact.Browser', side_effect=AssertionError('must not open a browser')):
            for thread, favorited, pages in [('abc', True, 3), ('https://example.org/thread-1-1-1.html', True, 3),
                                             (1190345, 'yes', 3), (1190345, 1, 3), (1190345, True, 0), (1190345, True, 11)]:
                result = set_favorite(thread, favorited, list_pages=pages)
                self.assertEqual(result['status'], 'failed', (thread, favorited, pages))
                self.assertFalse(result['submitted'])
                self.assertIn(result['error'], {'invalid_thread_reference', 'unsupported_thread_target', 'invalid_target_state', 'invalid_list_budget'})

    def test_a_lapsed_login_is_a_failure_with_nothing_submitted(self):
        class LoggedOut(FavoriteBrowser):
            def profile(self):
                raise RuntimeError('login_required')
        browser = LoggedOut([])
        result = run(browser, 1190345, True)
        self.assertEqual((result['status'], result['error'], result['submitted']), ('failed', 'login_required', False))


class TargetStateContractTests(unittest.TestCase):
    def test_read_back_decides_the_status(self):
        cases = [
            (dict(before=False, after=True, submitted=True), ('complete', None, True)),
            (dict(before=True, after=True, submitted=False), ('complete', None, False)),
            (dict(before=False, after=False, submitted=True), ('failed', 'target_state_not_reached', False)),
            (dict(before=False, after=None, submitted=True), ('needs_attention', 'result_unverified', None)),
            (dict(before=None, after=None, submitted=False, error='favorite_state_unverified'), ('failed', 'favorite_state_unverified', None)),
            (dict(before=False, after=None, submitted=True, error='form_submission_unconfirmed'), ('needs_attention', 'form_submission_unconfirmed', None)),
        ]
        for arguments, expected in cases:
            result = target_state_result('favorite', 1, True, **arguments)
            self.assertEqual((result['status'], result['error'], result['changed']), expected, arguments)


class DiscuzAjaxParseTests(unittest.TestCase):
    def test_form_fields_and_action_come_from_the_popup(self):
        popup = parse_discuz_ajax(ADD_FORM % {'tid': 1190345})
        self.assertEqual(popup['form']['action'], 'https://www.1point3acres.com/bbs/home.php?mod=spacecp&ac=favorite&type=thread&id=1190345&spaceuid=0')
        self.assertEqual(popup['form']['fields']['formhash'], FORMHASH)
        self.assertEqual(popup['form']['fields']['favoritesubmit'], 'true')
        self.assertFalse(popup['failed'])

    def test_success_and_error_handlers_are_told_apart(self):
        success = parse_discuz_ajax(SUCCESS)
        self.assertEqual((success['succeeded'], success['failed'], success['message']), (True, False, '操作成功'))
        refused = parse_discuz_ajax(NOT_FAVORITABLE)
        self.assertEqual((refused['succeeded'], refused['failed'], refused['message'], refused['form']),
                         (False, True, '抱歉，您指定的信息无法收藏', None))
        self.assertEqual(refused['notice'], '抱歉，您指定的信息无法收藏')


class FormBoundaryTests(unittest.TestCase):
    def test_only_listed_discuz_handlers_can_be_posted_to(self):
        browser = Browser.__new__(Browser)
        browser.evaluate = lambda expression: self.fail('no request may be built for a refused target')
        for url in ['https://www.1point3acres.com/bbs/forum.php?mod=post&action=reply',
                    'https://www.1point3acres.com/bbs/home.php?mod=spacecp&ac=profile',
                    'http://www.1point3acres.com/bbs/home.php?mod=spacecp&ac=favorite',
                    'https://example.org/bbs/home.php?mod=spacecp&ac=favorite']:
            with self.assertRaisesRegex(ValueError, 'unsupported_form_target', msg=url):
                browser.submit_form(url, {'formhash': 'x'})
        with self.assertRaisesRegex(ValueError, 'form_token_missing'):
            browser.submit_form('https://www.1point3acres.com/bbs/home.php?mod=spacecp&ac=favorite&type=thread&id=1', {'favoritesubmit': 'true'})
        with self.assertRaisesRegex(ValueError, 'form_text_not_encodable'):
            browser.submit_form('https://www.1point3acres.com/bbs/home.php?mod=spacecp&ac=favorite&type=thread&id=1',
                                {'formhash': 'x', 'description': '\U0001F600'})

    def test_the_form_is_posted_once_as_gbk_with_the_ajax_flag_and_never_retried(self):
        browser = Browser.__new__(Browser)
        calls = []
        browser.evaluate = lambda expression: calls.append(expression) or {'transport_error': True}
        with self.assertRaisesRegex(RuntimeError, 'form_submission_unconfirmed'):
            browser.submit_form('https://www.1point3acres.com/bbs/home.php?mod=spacecp&ac=favorite&type=thread&id=1',
                                {'formhash': 'x', 'description': '收藏说明'})
        self.assertEqual(len(calls), 1)
        self.assertIn('id=1&inajax=1', calls[0])
        self.assertIn('%CA%D5%B2%D8%CB%B5%C3%F7', calls[0])  # 收藏说明 in GBK, as the site expects
        self.assertIn("method:'POST'", calls[0])


class EntryPointTests(unittest.TestCase):
    def test_cli_and_mcp_pass_the_target_state_through(self):
        payload = target_state_result('favorite', 1190345, False, before=True, after=False, submitted=True,
                                      favorite_id=None, site_message=None, favorites_pages_scanned=1, scope='own_account')
        with patch('cli.set_favorite', return_value=payload) as called, \
                patch('sys.argv', ['cli', 'unfavorite', '1190345', '--list-pages', '2']), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(), 0)
        self.assertEqual(called.call_args.args, ('1190345', False))
        self.assertEqual(called.call_args.kwargs, {'list_pages': 2})
        self.assertEqual(json.loads(out.getvalue())['after'], False)
        with patch('mcp_server.favorite_thread', return_value=payload) as tool_called:
            tool = asyncio.run(mcp_server.server.call_tool('set_favorite', {'thread': '1190345', 'favorited': False}))
        self.assertFalse(tool.is_error)
        self.assertEqual(tool_called.call_args.args, ('1190345', False))
        self.assertEqual(json.loads(tool.content[0].text)['action'], 'favorite')
        # A target state that is not a real boolean is rejected by the schema before anything runs.
        with patch('mcp_server.favorite_thread', side_effect=AssertionError('must not run')):
            for favorited in ['true', 1, None]:
                try:
                    rejected = asyncio.run(mcp_server.server.call_tool('set_favorite', {'thread': '1190345', 'favorited': favorited}))
                except ToolError as refusal:
                    self.assertIn('favorited', str(refusal))  # in-process calls raise; over stdio this is is_error
                else:
                    self.assertTrue(rejected.is_error, favorited)

    def test_a_failed_operation_exits_two_and_is_flagged_for_mcp(self):
        payload = target_state_result('favorite', None, True, error='invalid_thread_reference')
        with patch('cli.set_favorite', return_value=payload), patch('sys.argv', ['cli', 'favorite', 'abc']), \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(cli.main(), 2)
        with patch('mcp_server.favorite_thread', return_value=payload):
            tool = asyncio.run(mcp_server.server.call_tool('set_favorite', {'thread': 'abc', 'favorited': True}))
        self.assertTrue(tool.is_error)
