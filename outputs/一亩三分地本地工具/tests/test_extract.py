import unittest
from extract import (parse_listing, parse_thread, parse_board, parse_board_reference,
                     parse_profile, parse_profile_reference, parse_profile_threads, parse_favorites)


class ExtractTests(unittest.TestCase):
    def test_login_or_maintenance_listing_is_rejected(self):
        for html in ['<form id="login"><input name="password"></form>', '<h1>Maintenance</h1>']:
            with self.subTest(html=html), self.assertRaises(ValueError):
                parse_listing(html, 'https://www.1point3acres.com/bbs/tag/stripe-2126-1.html')

    def test_list_dedupes_summary_links_and_excludes_non_interviews(self):
        html = '<table><tr><td><a href="thread-123-1-1.html">Stripe 电面</a><a href="thread-123-1-1.html">摘要</a></td><td>海外面经</td><td>2026-9-9</td></tr><tr><td><a href="thread-124-1-1.html">Stripe 工资</a></td><td>晒工资</td></tr></table><div class="pg"><a class="nxt" href="tag/stripe-2126-2.html">下一页</a></div>'
        result = parse_listing(html, "https://www.1point3acres.com/bbs/tag/stripe-2126-1.html")
        self.assertEqual([(p["tid"], p["title"]) for p in result["threads"]], [(123, "Stripe 电面")])
        self.assertTrue(result["next_url"].endswith("stripe-2126-2.html"))

    def test_permission_notice_marks_missing_content(self):
        html = '<span id="thread_subject">Stripe 电面</span><div id="postlist">回复: 0<div id="post_8"><div id="postmessage_8">前文<div class="locked">本帖隐藏的内容需要积分高于 188 才可浏览</div>后文<script>bad()</script></div></div></div>'
        result = parse_thread(html, "https://www.1point3acres.com/bbs/thread-123-1-1.html")
        self.assertEqual(result["content_status"], "restricted")
        self.assertIn("内容受限", result["posts"][0]["text"])
        self.assertNotIn("bad()", result["posts"][0]["text"])
        self.assertFalse(result["complete"])

    def test_next_page_prevents_false_complete(self):
        html = '<span id="thread_subject">Stripe</span><div id="postlist">回复: 2<div id="postmessage_1">正文</div></div><div class="pg"><a class="nxt" href="thread-123-2-1.html">下一页</a></div>'
        result = parse_thread(html, "https://www.1point3acres.com/bbs/thread-123-1-1.html")
        self.assertEqual(result["expected_posts"], 3)
        self.assertTrue(result["next_url"].endswith("thread-123-2-1.html"))
        self.assertFalse(result["complete"])

    def test_challenge_html_never_becomes_a_post(self):
        with self.assertRaises(ValueError):
            parse_thread('<title>Just a moment</title><form id="challenge-form"></form>', 'https://www.1point3acres.com/bbs/thread-123-1-1.html')


if __name__ == "__main__":
    unittest.main()


def board_html(rows=((1001, False), (1002, False)), *, next_url=None):
    body = ''
    for tid, sticky in rows:
        kind = 'stickthread' if sticky else 'normalthread'
        body += (f'<tbody id="{kind}_{tid}"><tr><td>'
                 f'<a href="thread-{tid}-1-1.html" class="icn">图标</a>'
                 f'<a href="thread-{tid}-1-1.html" class="s xst">合成标题 {tid}</a></td>'
                 f'<td class="by"><cite><a href="space-uid-9.html">合成作者</a></cite></td>'
                 f'<td class="num"><a href="thread-{tid}-1-1.html" class="xi2">7</a></td></tr></tbody>')
    nxt = f'<div class="pg"><a class="nxt" href="{next_url}">下一页</a></div>' if next_url else ''
    return f'<div id="threadlist"><table id="threadlisttableid">{body}</table></div>{nxt}'


class BoardParseTests(unittest.TestCase):
    URL = 'https://www.1point3acres.com/bbs/forum-472-1.html'

    def test_four_outcomes_are_told_apart_by_structure(self):
        cases = [('<div id="challenge-form"></div>', 'board_challenge'),
                 ('<form id="login"><input name="password"></form>', 'board_requires_login'),
                 ('<h1>随便一个页面</h1>', 'board_not_recognized')]
        for html, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ValueError, '^' + expected + '$'):
                    parse_board(html, self.URL)
        # A board that really is empty is a result, not an error.
        self.assertEqual(parse_board('<div id="threadlist"></div>', self.URL),
                         {'threads': [], 'next_url': None})

    def test_identity_comes_from_the_row_not_the_first_link(self):
        # The icon anchor points at the same thread but carries no title; the title is a.s.xst.
        result = parse_board(board_html(((1001, False),)), self.URL)
        self.assertEqual([(t['tid'], t['title']) for t in result['threads']], [(1001, '合成标题 1001')])
        self.assertEqual(result['threads'][0]['url'],
                         'https://www.1point3acres.com/bbs/thread-1001-1-1.html')
        self.assertEqual(result['threads'][0]['author'], '合成作者')
        self.assertEqual(result['threads'][0]['replies'], 7)

    def test_stickies_are_marked_so_a_caller_can_tell_them_apart(self):
        result = parse_board(board_html(((9, True), (1001, False))), self.URL)
        self.assertEqual([(t['tid'], t['sticky']) for t in result['threads']], [(9, True), (1001, False)])

    def test_next_page_is_reported_when_the_site_offers_one(self):
        nxt = 'forum-472-2.html'
        self.assertTrue(parse_board(board_html(next_url=nxt), self.URL)['next_url'].endswith(nxt))
        self.assertIsNone(parse_board(board_html(), self.URL)['next_url'])


class BoardReferenceTests(unittest.TestCase):
    def test_a_plain_number_or_this_site_s_own_listing_is_accepted(self):
        self.assertEqual(parse_board_reference('472')['board'], 472)
        self.assertEqual(parse_board_reference(98)['board'], 98)
        self.assertEqual(
            parse_board_reference('https://www.1point3acres.com/bbs/forum-98-3.html')['url'],
            'https://www.1point3acres.com/bbs/forum-98-1.html')

    def test_anything_else_is_refused_including_another_host(self):
        for value in ['', '  ', 'abc', '-1', '../../etc/passwd',
                      'https://evil.example.com/bbs/forum-1-1.html',
                      'https://www.1point3acres.com/bbs/thread-1-1-1.html']:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, '^invalid_board_reference$'):
                    parse_board_reference(value)


def profile_html(uid=123456, name='合成成员', description=''):
    return (f'<div id="space"><h2 id="spaceinfoshow"><strong id="spacename">{name}的个人空间</strong>'
            f'<span id="spacedescription">{description}</span></h2>'
            f'<div id="profile_content"><div class="hm"><p><a href="space-uid-{uid}.html"><img/></a></p>'
            f'<h2 class="mbn"><a href="space-uid-{uid}.html">{name}</a></h2></div></div></div>')


def profile_threads_html(rows=((1001, '新闻时事', 2, 108),), *, next_url=None, empty=False):
    body = '<tr class="th"><td class="icn"> </td><th>主题</th><td class="frm">版块/群组</td><td class="num">回复/查看</td><td class="by"><cite>最后发帖</cite></td></tr>'
    if empty:
        body += '<tr><td colspan="5"><p class="emp">还没有相关的帖子</p></td></tr>'
    for tid, board, replies, views in rows:
        body += (f'<tr><td class="icn"><a href="forum.php?mod=viewthread&amp;tid={tid}"><img/></a></td>'
                 f'<th><a href="thread-{tid}-1-1.html">合成标题 {tid}</a></th>'
                 f'<td><a class="xg1" href="forum-472-1.html">{board}</a></td>'
                 f'<td class="num"><a class="xi2" href="thread-{tid}-1-1.html">{replies}</a><em>{views}</em></td>'
                 f'<td class="by"><cite><a href="space-username-x.html">回帖者</a></cite></td></tr>')
    nxt = f'<div class="pg"><a class="nxt" href="{next_url}">下一页</a></div>' if next_url else ''
    return f'<div id="ct"><div class="mn"><table>{body}</table></div>{nxt}</div>'


def notice_html(text):
    return f'<div id="ct"><div class="alert_error" id="messagetext"><p>{text}</p></div></div>'


class ProfileReferenceTests(unittest.TestCase):
    def test_uid_and_both_site_profile_links_are_accepted(self):
        for value in ['123456', 123456, 'https://www.1point3acres.com/bbs/space-uid-123456.html',
                      'https://www.1point3acres.com/bbs/home.php?mod=space&uid=123456&do=thread&view=me']:
            with self.subTest(value=value):
                ref = parse_profile_reference(value)
                self.assertEqual(ref['uid'], 123456)
                self.assertTrue(ref['threads_url'].endswith('uid=123456&do=thread&view=me&from=space'))

    def test_other_hosts_threads_and_junk_are_refused(self):
        for value in ['', 'abc', '0', 'https://evil.example.com/bbs/space-uid-1.html',
                      'https://www.1point3acres.com/bbs/thread-1-1-1.html',
                      'https://www.1point3acres.com/bbs/home.php?mod=forum&uid=5']:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, '^invalid_profile_reference$'):
                    parse_profile_reference(value)


class ProfileParseTests(unittest.TestCase):
    URL = 'https://www.1point3acres.com/bbs/space-uid-123456.html'

    def test_outcomes_are_told_apart_by_structure(self):
        cases = [('<div id="challenge-form"></div>', 'profile_challenge'),
                 (notice_html('抱歉，您指定的用户空间不存在'), 'profile_not_found'),
                 (notice_html('由于该用户的隐私设置，您不能访问当前内容'), 'profile_restricted'),
                 ('<form id="login"><input name="password"></form>', 'profile_requires_login'),
                 ('<h1>随便一个页面</h1>', 'profile_not_recognized')]
        for html, expected in cases:
            with self.subTest(expected=expected):
                for parser in (parse_profile, parse_profile_threads):
                    with self.assertRaisesRegex(ValueError, '^' + expected + '$'):
                        parser(html, self.URL)

    def test_profile_fields_come_only_from_the_page(self):
        result = parse_profile(profile_html(description='一句简介'), self.URL)
        self.assertEqual(result, {'uid': 123456, 'username': '合成成员', 'space_title': '合成成员的个人空间',
                                  'description': '一句简介'})
        self.assertIsNone(parse_profile(profile_html(), self.URL)['description'])

    def test_thread_rows_use_the_title_cell_not_the_icon_link(self):
        result = parse_profile_threads(profile_threads_html(), self.URL)
        self.assertEqual(result['threads'], [{'tid': 1001, 'title': '合成标题 1001',
            'url': 'https://www.1point3acres.com/bbs/thread-1001-1-1.html', 'board': '新闻时事',
            'replies': 2, 'views': 108}])
        self.assertIsNone(result['next_url'])

    def test_an_empty_member_list_is_a_result_and_next_page_is_kept(self):
        self.assertEqual(parse_profile_threads(profile_threads_html((), empty=True), self.URL)['threads'], [])
        nxt = 'home.php?mod=space&uid=123456&do=thread&view=me&page=2'
        self.assertTrue(parse_profile_threads(profile_threads_html(next_url=nxt), self.URL)['next_url'].endswith(nxt))


def favorites_html(items=((6942247, 1134743, '2026-6-27 18:12'),), *, next_url=None, empty=False):
    body = ''.join(
        f'<li class="bbda" id="fav_{favid}"><a class="y" href="home.php?mod=spacecp&amp;ac=favorite&amp;op=delete&amp;favid={favid}">删除</a>'
        f'<input name="favorite[]" type="checkbox" value="{favid}"/><span><img alt="thread"/></span>'
        f'<a href="thread-{tid}-1-1.html">合成收藏 {tid}</a> <span class="xg1">{when}</span></li>'
        for favid, tid, when in items)
    if empty:
        body = '<li><p class="emp">还没有相关的收藏</p></li>'
    nxt = f'<div class="pg"><a class="nxt" href="{next_url}">下一页</a></div>' if next_url else ''
    return f'<div id="ct"><div class="mn"><form id="delform"><ul id="favorite_ul">{body}</ul></form></div>{nxt}</div>'


class FavoritesParseTests(unittest.TestCase):
    URL = 'https://www.1point3acres.com/bbs/home.php?mod=space&uid=123456&do=favorite&view=me&type=thread'

    def test_rows_carry_both_the_favorite_id_and_the_shared_thread_id(self):
        result = parse_favorites(favorites_html(), self.URL)
        self.assertEqual(result['favorites'], [{'tid': 1134743, 'title': '合成收藏 1134743',
            'url': 'https://www.1point3acres.com/bbs/thread-1134743-1-1.html',
            'favorite_id': 6942247, 'favorited_at': '2026-6-27 18:12'}])

    def test_empty_gate_and_unrecognised_pages(self):
        self.assertEqual(parse_favorites(favorites_html((), empty=True), self.URL)['favorites'], [])
        for html, expected in [('<div id="challenge-form"></div>', 'profile_challenge'),
                               (notice_html('抱歉，您指定的用户空间不存在'), 'profile_not_found'),
                               ('<h1>不是收藏页</h1>', 'favorites_not_recognized')]:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ValueError, '^' + expected + '$'):
                    parse_favorites(html, self.URL)
        nxt = 'home.php?mod=space&uid=123456&do=favorite&view=me&type=thread&page=2'
        self.assertTrue(parse_favorites(favorites_html(next_url=nxt), self.URL)['next_url'].endswith(nxt))


THREAD_URL = 'https://www.1point3acres.com/bbs/thread-123-1-1.html'


def post_html(pid, body, *, author=('space-uid-77.html', '合成作者'), anonymous=None, stamp='2026-9-22 08:20:24',
              quote=None, ratings=None, attachments='', videos=''):
    if anonymous:
        byline = f'<div class="authi"><img class="authicn"/> {anonymous} <em id="authorposton{pid}"><span title="{stamp}">刚刚</span></em></div>'
    else:
        byline = (f'<div class="authi"><img class="authicn"/> <a class="xi2" href="{author[0]}">{author[1]}</a> '
                  f'<span><meta content="2026-9-22 00:32" itemprop="datePublished"/><time datetime="2026-9-22 09:47" itemprop="dateModified">'
                  f'<span title="{stamp}">刚刚</span></time></span></div>')
    quoted = (f'<div class="quote"><blockquote><font><a href="forum.php?mod=redirect&amp;goto=findpost&amp;pid={quote[0]}&amp;ptid=123">'
              f'<font color="#999">{quote[1]} 发表于 2026-9-21 15:57</font></a></font><br/>{quote[2]}</blockquote></div>') if quote else ''
    rated = ratings or ''
    # Message before byline on purpose: the author rule is "the byline", not "the first member link",
    # and only this order lets a test tell the two apart.
    return (f'<div id="post_{pid}"><div id="postmessage_{pid}">{quoted}{body}{attachments}{videos}</div>'
            f'<div class="pi">{byline}</div>{rated}</div>')


def rating_html(participants, rice, rows):
    body = ''.join(f'<tr><td>{"<a href=\"space-uid-" + str(uid) + ".html\">" + name + "</a>" if uid else name}</td>'
                   f'<td class="xi1"> + {amount}</td><td class="xg1">{reason}</td></tr>' for uid, name, amount, reason in rows)
    return (f'<table class="ratl"><tr><th class="xw1"><a href="#">参与人数 <span class="xi1">{participants}</span></a></th>'
            f'<th class="xw1">大米 <i><span class="xi1">+{rice}</span></i></th><th>理由</th></tr><tbody class="ratl_l">{body}</tbody></table>')


def thread_html(*posts, views=None, replies=2, favorites=None):
    counters = ''
    if views is not None:
        counters = f'<div class="hm ptn"><span class="xg1">查看:</span> <span class="xi1">{views}</span><span class="pipe">|</span> <span class="xg1">回复:</span> <span class="xi1">{replies}</span></div>'
    fav = f'<span id="favoritenumber" style="display:none">{favorites}</span>' if favorites is not None else ''
    return (f'<span id="thread_subject">合成主题</span>{counters}{fav}<div id="postlist">回复: {replies}'
            + ''.join(posts) + '</div>')


class ThreadMetadataTests(unittest.TestCase):
    """Author, stats and media are what the page carries; nothing is guessed or zero-filled (#22)."""

    def test_the_poster_is_the_byline_not_the_quoted_or_mentioned_member(self):
        html = thread_html(post_html(8, '正文', quote=(5, '被引用者', '被引用的话 <a href="space-uid-99.html">被提到的人</a>')))
        post = parse_thread(html, THREAD_URL)['posts'][0]
        self.assertEqual(post['author'], {'uid': 77, 'name': '合成作者', 'anonymous': False})
        self.assertEqual(post['quotes'], [{'pid': 5, 'author': '被引用者', 'text': '被引用者 发表于 2026-9-21 15:57\n被引用的话\n被提到的人'}])

    def test_each_post_keeps_its_own_time_not_the_threads(self):
        html = thread_html(post_html(8, '一楼', stamp='2026-9-22 00:32:38'),
                           post_html(9, '二楼', stamp='2026-9-22 08:20:24'),
                           post_html(10, '匿名楼', anonymous='匿名用户-Y092N', stamp='2026-9-22 13:21:15'))
        result = parse_thread(html, THREAD_URL)
        self.assertEqual([p['published_at'] for p in result['posts']],
                         ['2026-9-22 00:32:38', '2026-9-22 08:20:24', '2026-9-22 13:21:15'])
        self.assertEqual(result['posts'][2]['author'], {'uid': None, 'name': '匿名用户-Y092N', 'anonymous': True})
        # The schema.org metas in the byline describe the thread and land on the thread, once.
        self.assertEqual((result['stats']['published_at'], result['stats']['last_post_at']), ('2026-9-22 00:32', '2026-9-22 09:47'))
        self.assertNotIn('edited_at', result['posts'][0])

    def test_counters_are_absent_not_zero_when_the_page_omits_them(self):
        bare = parse_thread(thread_html(post_html(8, '正文')), THREAD_URL)['stats']
        self.assertEqual((bare['views'], bare['replies'], bare['favorites']), (None, None, None))
        self.assertTrue(bare['fetched_at'])
        full = parse_thread(thread_html(post_html(8, '正文'), views=1411, replies=9, favorites=3), THREAD_URL)['stats']
        self.assertEqual((full['views'], full['replies'], full['favorites']), (1411, 9, 3))

    def test_ratings_are_parsed_only_when_the_page_shows_them(self):
        rated = post_html(8, '正文', ratings=rating_html(2, 3, [(None, '匿名用户-SKVB0', 2, '赞一个'), (50412, 'mengyi2008', 1, '')]))
        posts = parse_thread(thread_html(rated, post_html(9, '无评分')), THREAD_URL)['posts']
        self.assertEqual(posts[0]['ratings'], {'participants': 2, 'rice': 3, 'entries': [
            {'uid': None, 'name': '匿名用户-SKVB0', 'rice': 2, 'reason': '赞一个'},
            {'uid': 50412, 'name': 'mengyi2008', 'rice': 1, 'reason': None}]})
        self.assertIsNone(posts[1]['ratings'])

    def test_media_sources_are_recorded_and_restricted_ones_are_not_claimed(self):
        attachments = ('<div class="pattl"><span class="attnm"><a href="forum.php?mod=attachment&amp;aid=1">题目.pdf</a></span></div>'
                       '<div class="pattl"><div class="attach_nopermission">权限不足</div></div>')
        videos = '<video><source src="/data/v.mp4"/></video><iframe src="https://player.example.com/x"></iframe>'
        post = parse_thread(thread_html(post_html(8, '正文', attachments=attachments, videos=videos)), THREAD_URL)['posts'][0]
        self.assertEqual(post['attachments'], [
            {'name': '题目.pdf', 'url': 'https://www.1point3acres.com/bbs/forum.php?mod=attachment&aid=1', 'restricted': False},
            {'name': None, 'url': None, 'restricted': True}])
        self.assertEqual(post['videos'], [{'url': 'https://www.1point3acres.com/data/v.mp4', 'kind': 'video'},
                                          {'url': 'https://player.example.com/x', 'kind': 'iframe'}])
