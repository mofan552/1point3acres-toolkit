import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlsplit
from bs4 import BeautifulSoup
from settings import SITE, SITE_HOST, SITE_SEARCH_PATH
from contracts import ContentStatus


def parse_thread_reference(value, *, first_page=False):
    """Accept a thread identifier or a site view-thread URL, never an arbitrary fetch target."""
    if type(value) is int:
        tid, page = value, 1
    elif isinstance(value, str):
        if re.search(r'[\x00-\x20\x7f\\]', value):
            raise ValueError('invalid_thread_reference')
        if re.fullmatch(r'[0-9]+', value):
            tid, page = int(value), 1
        else:
            parsed = urlsplit(value)
            if (parsed.scheme != 'https' or parsed.hostname != SITE_HOST or parsed.username is not None
                    or parsed.password is not None or parsed.port not in (None, 443)):
                raise ValueError('unsupported_thread_target')
            match = re.fullmatch(r'/bbs/thread-([0-9]+)-([0-9]+)-[1-9][0-9]*\.html', parsed.path)
            if match:
                tid, page = int(match[1]), int(match[2])
            elif parsed.path == '/bbs/forum.php':
                query = parse_qs(parsed.query, keep_blank_values=True)
                if (query.get('mod') != ['viewthread'] or len(query.get('tid', [])) != 1
                        or len(query.get('page', ['1'])) != 1
                        or not re.fullmatch(r'[0-9]+', query['tid'][0])
                        or not re.fullmatch(r'[0-9]+', query.get('page', ['1'])[0])):
                    raise ValueError('invalid_thread_reference')
                tid, page = int(query['tid'][0]), int(query.get('page', ['1'])[0])
            else:
                raise ValueError('invalid_thread_reference')
    else:
        raise ValueError('invalid_thread_reference')
    if tid <= 0 or page <= 0:
        raise ValueError('invalid_thread_reference')
    page = 1 if first_page else page
    return {'tid': tid, 'page': page, 'url': f'{SITE}/bbs/thread-{tid}-{page}-1.html'}


def clean_text(node):
    for item in list(node.select('script,style,noscript')):
        item.decompose()
    return re.sub(r"\n{3,}", "\n\n", node.get_text("\n", strip=True)).strip()


def next_link(soup, base, *, preserve_invalid=False):
    node = soup.select_one('.pg a.nxt, a[rel="next"]')
    if not node:
        return None
    target = urljoin(SITE + "/bbs/", node.get("href", ""))
    # Thread traversal must reject malformed links; listing discovery keeps its existing filter.
    return target if preserve_invalid or (urlsplit(target).hostname == SITE_HOST and target != base) else None


def parse_listing(html, url):
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one('#challenge-form'):
        raise ValueError("Cloudflare challenge, not a listing")
    if not soup.select_one('tr a[href*="thread-"]'):
        raise ValueError('No recognizable forum listing; possible login, maintenance, or empty response')
    items = {}
    for row in soup.select('tr'):
        row_text = row.get_text(' ', strip=True)
        if not any(word in row_text for word in ("海外面经", "数科面经")):
            continue
        for anchor in row.select('a[href]'):
            match = re.search(r"thread-(\d+)-", anchor['href'])
            title = anchor.get_text(' ', strip=True)
            if not match or not title or title.isdigit():
                continue
            tid = int(match[1])
            if tid not in items:
                date = re.search(r"20\d{2}-\d{1,2}-\d{1,2}", row_text)
                items[tid] = {"tid": tid, "title": title,
                              "url": f"{SITE}/bbs/thread-{tid}-1-1.html",
                              "listed_date": date[0] if date else None}
    return {"threads": list(items.values()), "next_url": next_link(soup, url)}


def parse_profile_reference(value):
    """A member is a uid or one of this site's own profile links; nothing else is a target."""
    text = str(value).strip()
    uid = None
    if re.fullmatch(r'\d{1,9}', text):
        uid = int(text)
    elif text.startswith('http'):
        parts = urlsplit(text)
        if parts.hostname == SITE_HOST:
            match = re.fullmatch(r'/bbs/space-uid-(\d{1,9})\.html', parts.path)
            query = parse_qs(parts.query)
            if match:
                uid = int(match[1])
            elif parts.path == '/bbs/home.php' and query.get('mod') == ['space'] and re.fullmatch(r'\d{1,9}', (query.get('uid') or [''])[0]):
                uid = int(query['uid'][0])
    if not uid:
        raise ValueError('invalid_profile_reference')
    return {'uid': uid, 'space_url': f'{SITE}/bbs/space-uid-{uid}.html',
            'threads_url': f'{SITE}/bbs/home.php?mod=space&uid={uid}&do=thread&view=me&from=space'}


def _profile_gate(soup):
    """The outcomes every profile page shares, told apart by structure the site itself uses."""
    if soup.select_one('#challenge-form'):
        raise ValueError('profile_challenge')
    notice = soup.select_one('#messagetext')
    if notice is not None:
        # Discuz answers a missing or private space with its notice box, not a 404.
        raise ValueError('profile_not_found' if '不存在' in notice.get_text(' ', strip=True) else 'profile_restricted')
    if soup.select_one('form#login, input[name=password]') and not soup.select_one('#space'):
        raise ValueError('profile_requires_login')


def parse_profile(html, url):
    """Public identity block of a member's space; only fields the page actually carries."""
    soup = BeautifulSoup(html, "html.parser")
    _profile_gate(soup)
    if not soup.select_one('#space') or not soup.select_one('#spacename'):
        raise ValueError('profile_not_recognized')
    owner = soup.select_one('#profile_content a[href*="space-uid-"]')
    match = re.search(r'space-uid-(\d+)', owner.get('href', '')) if owner else None
    name = soup.select_one('#profile_content h2 a')
    description = soup.select_one('#spacedescription')
    return {'uid': int(match[1]) if match else None,
            'username': name.get_text(' ', strip=True) if name else None,
            'space_title': soup.select_one('#spacename').get_text(' ', strip=True),
            'description': (description.get_text(' ', strip=True) or None) if description else None}


def parse_profile_threads(html, url):
    """One page of a member's own threads. The header row is the page's own column map: th=title."""
    soup = BeautifulSoup(html, "html.parser")
    _profile_gate(soup)
    table = next((t for t in soup.select('#ct table') if t.select_one('tr.th')), None)
    if table is None:
        raise ValueError('profile_not_recognized')
    threads = []
    for row in table.select('tr'):
        anchor = row.select_one('th a[href*="thread-"]')
        match = re.search(r'thread-(\d+)-', anchor['href']) if anchor else None
        title = anchor.get_text(' ', strip=True) if anchor else ''
        if not match or not title:
            continue
        tid = int(match[1])
        board = row.select_one('td a.xg1[href^="forum-"]')
        replies = row.select_one('td.num a.xi2')
        views = row.select_one('td.num em')
        threads.append({'tid': tid, 'title': title, 'url': f"{SITE}/bbs/thread-{tid}-1-1.html",
                        'board': board.get_text(' ', strip=True) if board else None,
                        'replies': int(replies.get_text(strip=True)) if replies and replies.get_text(strip=True).isdigit() else None,
                        'views': int(views.get_text(strip=True)) if views and views.get_text(strip=True).isdigit() else None})
    return {'threads': threads, 'next_url': next_link(soup, url)}


def parse_favorites(html, url):
    """One page of the signed-in member's own thread favorites; tid is the shared thread identity."""
    soup = BeautifulSoup(html, "html.parser")
    _profile_gate(soup)
    box = soup.select_one('#favorite_ul')
    if box is None:
        raise ValueError('favorites_not_recognized')
    favorites = []
    for item in box.select('li[id^="fav_"]'):
        anchor = item.select_one('a[href*="thread-"]')
        match = re.search(r'thread-(\d+)-', anchor['href']) if anchor else None
        favid = re.fullmatch(r'fav_(\d+)', item.get('id', ''))
        title = anchor.get_text(' ', strip=True) if anchor else ''
        if not match or not favid or not title:
            continue
        when = item.select_one('span.xg1')
        favorites.append({'tid': int(match[1]), 'title': title,
                          'url': f"{SITE}/bbs/thread-{int(match[1])}-1-1.html",
                          'favorite_id': int(favid[1]),
                          'favorited_at': when.get_text(' ', strip=True) if when else None})
    return {'favorites': favorites, 'next_url': next_link(soup, url)}


def parse_discuz_ajax(text):
    """Discuz's inajax envelope: a popup form (its hidden fields and action) or a notice carrying the site's
    own succeedhandle_/errorhandle_ call. Scripts are removed before any visible text is read."""
    match = re.search(r'<!\[CDATA\[(.*)\]\]>', text, re.S)
    inner = match[1] if match else text
    handler = re.search(r"(succeedhandle|errorhandle)_\w+\('((?:[^'\\]|\\.)*)'(?:,\s*'((?:[^'\\]|\\.)*)')?", inner)
    soup = BeautifulSoup(inner, 'html.parser')
    for script in soup.select('script'):
        script.decompose()
    form = soup.select_one('form')
    notice = soup.select_one('.alert_error, .alert_right, .alert_info, #messagetext')
    message = None
    if handler:
        # succeedhandle_x(url, message, values) versus errorhandle_x(message, values)
        message = (handler[3] if handler[1] == 'succeedhandle' and handler[3] is not None else handler[2]).strip()
    return {'form': {'action': urljoin(SITE + '/bbs/', form.get('action', '')),
                     'fields': {node['name']: node.get('value', '') for node in form.select('input[type=hidden][name]')}}
            if form is not None else None,
            'notice': notice.get_text(' ', strip=True) if notice is not None else None,
            'succeeded': bool(handler and handler[1] == 'succeedhandle'),
            'failed': bool(handler and handler[1] == 'errorhandle'), 'message': message}


def parse_reactions(html, tid):
    """Every post's emoji reactions on one page of thread `tid`, keyed by pid then reaction id: the count
    the site shows and whether the signed-in member's own reaction is among them (the site marks it with
    the same my-reaction class its own script toggles). A post with no reactions is present with an empty
    map. The page must be this thread's: a redirect elsewhere is refused, never read as it."""
    soup = BeautifulSoup(html, 'html.parser')
    if soup.select_one('#challenge-form'):
        raise ValueError('thread_challenge')
    canonical = soup.select_one('link[rel="canonical"][href]')
    if canonical is not None and parse_thread_reference(urljoin(SITE + '/bbs/', canonical['href']))['tid'] != tid:
        raise ValueError('unexpected_thread_document')
    posts = {}
    for box in soup.select('[id^="reaction-list-"]'):
        owner = re.fullmatch(r'reaction-list-(\d+)', box.get('id', ''))
        if not owner:
            continue
        entries = {}
        for badge in box.select('[id^="reaction-"]'):
            match = re.fullmatch(r'reaction-(\d+)-(\d+)', badge.get('id', ''))
            if not match or int(match[1]) != int(owner[1]):
                continue
            total = badge.select_one('.reaction-count')
            entries[int(match[2])] = {'count': _int_or_none(total.get_text()) if total else None,
                                      'mine': 'my-reaction' in (badge.get('class') or [])}
        posts[int(owner[1])] = entries
    if not posts:
        notice = soup.select_one('#messagetext')
        if notice is not None:
            raise ValueError('thread_not_found' if '不存在' in notice.get_text(' ', strip=True) else 'thread_restricted')
        raise ValueError('thread_not_recognized')
    if canonical is None:
        raise ValueError('unexpected_thread_document')
    return posts


def parse_thread_page(html, tid):
    """A thread page reached by a site redirect (findpost, lastpost): the page number comes from the
    page's own pager, and parse_thread then applies every check it applies to a directly read page."""
    soup = BeautifulSoup(html, 'html.parser')
    current = soup.select_one('.pg strong, .pg [aria-current="page"]')
    page = int(current.get_text(strip=True)) if current and current.get_text(strip=True).isdigit() else 1
    result = parse_thread(html, f'{SITE}/bbs/thread-{tid}-{page}-1.html')
    canonical = soup.select_one('link[rel="canonical"][href]')
    if canonical is None:
        raise ValueError('unexpected_thread_document')  # a redirect target must prove which thread it is
    return {**result, 'page': page}


def normalize_message(text):
    """Text as the site will show it back: whitespace collapsed, so a read-back compares content only."""
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def parse_board_reference(value):
    """Only this site's own board listings; an arbitrary URL is not a browsing target."""
    text = str(value).strip()
    match = re.fullmatch(r'(\d{1,9})', text) or re.search(r'/bbs/forum-(\d{1,9})-\d+\.html', text)
    if not match or (text.startswith('http') and urlsplit(text).hostname != SITE_HOST):
        raise ValueError('invalid_board_reference')
    board = int(match[1])
    return {'board': board, 'url': f'{SITE}/bbs/forum-{board}-1.html'}


def parse_board(html, url):
    """Any board's listing, unlike parse_listing which keeps its interview-tag filter.

    The four outcomes the caller must tell apart are distinguished by structure, not by guessing:
    a challenge, a login wall, a page that is not a board at all, and a board that is genuinely empty.
    """
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one('#challenge-form'):
        raise ValueError('board_challenge')
    if not soup.select_one('#threadlist'):
        if soup.select_one('form#login, input[name=password]'):
            raise ValueError('board_requires_login')
        raise ValueError('board_not_recognized')
    threads = []
    for row in soup.select('tbody[id^=normalthread_], tbody[id^=stickthread_]'):
        identity = re.fullmatch(r'(normal|stick)thread_(\d+)', row.get('id', '') or '')
        anchor = row.select_one('a.s.xst')
        title = anchor.get_text(' ', strip=True) if anchor else ''
        if not identity or not title:
            continue
        author = row.select_one('a[href^="space-uid-"]')
        replies = row.select_one('a.xi2')
        threads.append({'tid': int(identity[2]), 'title': title,
                        'url': f"{SITE}/bbs/thread-{int(identity[2])}-1-1.html",
                        # Discuz repeats stickies on every page; the caller dedupes by tid.
                        'sticky': identity[1] == 'stick',
                        'author': author.get_text(' ', strip=True) if author else None,
                        'replies': int(replies.get_text(strip=True)) if replies and replies.get_text(strip=True).isdigit() else None})
    return {'threads': threads, 'next_url': next_link(soup, url)}


def parse_search_reference(value):
    """Only a native result page; never accept another operation from a next link."""
    if not isinstance(value, str) or re.search(r'[\x00-\x20\x7f\\]', value):
        raise ValueError('invalid_search_page')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname != SITE_HOST or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443) or parsed.path != SITE_SEARCH_PATH):
        raise ValueError('unsupported_search_target')
    fields = parse_qs(parsed.query, keep_blank_values=True)
    if (set(fields) - {'mod', 'searchid', 'orderby', 'ascdesc', 'searchsubmit', 'page', 'kw'}
            or any(len(values) != 1 for values in fields.values()) or fields.get('mod') != ['forum']
            or not re.fullmatch(r'[1-9][0-9]*', fields.get('searchid', [''])[0])
            or not re.fullmatch(r'[1-9][0-9]*', fields.get('page', ['1'])[0])
            or fields.get('orderby', ['lastpost']) != ['lastpost']
            or fields.get('ascdesc', ['desc']) != ['desc']
            or fields.get('searchsubmit', ['yes']) != ['yes']):
        raise ValueError('invalid_search_page')
    return {'search_id': fields['searchid'][0], 'page': int(fields.get('page', ['1'])[0]),
            'url': parsed._replace(fragment='').geturl()}


def parse_search(html, url, query):
    soup = BeautifulSoup(html, 'html.parser')
    if soup.select_one('#challenge-form, #cf-challenge-running'):
        raise ValueError('search_access_challenge')
    notice = soup.select_one('#messagetext, #main_message, .alert_error')
    if notice:
        text = notice.get_text(' ', strip=True)
        if re.search(r'登录|登入|log\s*in', text, re.I):
            raise ValueError('login_required')
        if re.search(r'两次搜索|搜索间隔|频繁|稍后再|繁忙', text):
            raise ValueError('search_rate_limited')
        if re.search(r'积分不足|扣除|消耗|购买|付费', text):
            raise ValueError('search_payment_required')
        raise ValueError('search_rejected')
    heading = soup.select_one('.sttl')
    echoed = heading.select_one('.emfont') if heading else None
    count = re.search(r'相关内容\s*([0-9]+(?:,[0-9]{3})*)\s*个\s*$', heading.get_text(' ', strip=True)) if heading else None
    if not echoed or echoed.get_text().strip() != query or not count:
        raise ValueError('unrecognized_search_results')
    location = parse_search_reference(url)
    current = soup.select_one('.pg strong, .pg [aria-current="page"]')
    if (location['page'] > 1 and not current) or (current and current.get_text(strip=True) != str(location['page'])):
        raise ValueError('unexpected_search_document_page')
    total = int(count[1].replace(',', ''))
    rows = soup.select('.slst li.pbw')
    if bool(total) != bool(rows):
        raise ValueError('search_results_missing')
    threads = {}
    for row in rows:
        anchor = row.select_one('h3 a[href]')
        if not anchor or not anchor.get_text(strip=True):
            raise ValueError('invalid_search_result')
        thread = parse_thread_reference(urljoin(url, anchor['href']), first_page=True)
        if str(thread['tid']) != row.get('id'):
            raise ValueError('unexpected_search_result_id')
        paragraphs = row.find_all('p', recursive=False)
        summary = paragraphs[1].get_text(' ', strip=True) if len(paragraphs) >= 2 else None
        metadata = paragraphs[2] if len(paragraphs) >= 3 else None
        author = metadata.select_one('a[href*="space-uid-"]') if metadata else None
        date = metadata.find('span', recursive=False) if metadata else None
        date_text = date.get_text(' ', strip=True) if date and not date.find('a') else ''
        listed_date = date_text if re.fullmatch(r'\d{4}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?', date_text) else None
        threads.setdefault(thread['tid'], {'tid': thread['tid'], 'title': anchor.get_text(' ', strip=True),
            'url': thread['url'], 'summary': summary,
            'author': author.get_text(' ', strip=True) if author else None,
            'listed_date': listed_date})
    if len(threads) > total:
        raise ValueError('search_result_count_mismatch')
    return {'threads': list(threads.values()), 'total': total, 'location': location,
            'next_url': next_link(soup, url, preserve_invalid=True)}


def _int_or_none(text):
    digits = re.search(r'[-+]?\d+', text or '')
    return int(digits[0]) if digits else None


def _post_author(outer):
    """The poster is the credited link in the post's own byline; a quoted or mentioned member is not."""
    byline = outer.select_one('.authi') if outer else None
    if byline is None:
        return None
    link = byline.select_one('a[href*="space-uid-"]')
    if link is not None:
        match = re.search(r'space-uid-(\d+)', link['href'])
        return {'uid': int(match[1]) if match else None, 'name': link.get_text(' ', strip=True) or None, 'anonymous': False}
    copy = byline.__copy__()
    for node in copy.select('em, time, span, img, meta'):
        node.decompose()
    name = clean_text(copy)
    return {'uid': None, 'name': name or None, 'anonymous': True} if name else None


def _post_time(outer, pid):
    """Only the post's own stamp. The itemprop metas in a byline describe the thread, not that post:
    on real pages every reply carried the thread's publish time and its last-reply time there."""
    stamp = outer.select_one(f'#authorposton{pid} [title], .authi [title]') if outer else None
    return stamp.get('title') if stamp else None


def _post_quotes(node, url):
    quotes = []
    for block in node.select('.quote blockquote, blockquote'):
        link = block.select_one('a[href*="findpost"]')
        pid = re.search(r'pid=(\d+)', link.get('href', '')) if link else None
        credit = link.get_text(' ', strip=True) if link else ''
        author = re.sub(r'\s*发表于.*$', '', credit).strip() or None
        quotes.append({'pid': int(pid[1]) if pid else None, 'author': author, 'text': clean_text(block)[:200]})
    return quotes


def _post_ratings(outer):
    table = outer.select_one('table.ratl') if outer else None
    if table is None:
        return None  # No table means the page showed no ratings; that is not a count of zero.
    head = table.select_one('tr')
    participants = rice = None
    if head is not None:
        for cell in head.select('th'):
            text = cell.get_text(' ', strip=True)
            if '参与人数' in text:
                participants = _int_or_none(text)
            elif '大米' in text:
                rice = _int_or_none(text)
    entries = []
    for row in table.select('tbody.ratl_l tr'):
        cells = row.select('td')
        if len(cells) < 2:
            continue
        link = cells[0].select_one('a[href*="space-uid-"]')
        match = re.search(r'space-uid-(\d+)', link['href']) if link else None
        entries.append({'uid': int(match[1]) if match else None,
                        'name': (link.get_text(' ', strip=True) if link and link.get_text(strip=True) else clean_text(cells[0])) or None,
                        'rice': _int_or_none(cells[1].get_text(' ', strip=True)),
                        'reason': (clean_text(cells[2]) or None) if len(cells) > 2 else None})
    return {'participants': participants, 'rice': rice, 'entries': entries}


def _post_media(node, url):
    attachments, videos = [], []
    for item in node.select('.pattl, .pattc, .attach'):
        name = item.select_one('.attnm a, a[href*="attachment"]')
        restricted = bool(item.select_one('.attach_nopermission')) or '权限' in item.get_text(' ', strip=True)
        attachments.append({'name': name.get_text(' ', strip=True) if name else None,
                            'url': urljoin(url, name['href']) if name and name.get('href') else None,
                            'restricted': restricted})
    for item in node.select('video, iframe, embed'):
        source = item.get('src') or (item.select_one('source').get('src') if item.select_one('source') else None)
        if source:
            videos.append({'url': urljoin(url, source), 'kind': item.name})
    return attachments, videos


def _thread_stats(soup, fetched_at):
    published = soup.select_one('#postlist meta[itemprop="datePublished"]')
    last_post = soup.select_one('#postlist time[itemprop="dateModified"]')
    stats = {'views': None, 'replies': None, 'favorites': None,
             'published_at': published.get('content') if published else None,
             'last_post_at': last_post.get('datetime') if last_post else None, 'fetched_at': fetched_at}
    header = soup.select_one('div.hm')
    if header is not None:
        labels = header.select('span.xg1')
        for label in labels:
            value = label.find_next_sibling('span', class_='xi1')
            if value is None:
                continue
            key = label.get_text(strip=True)
            if key.startswith('查看'):
                stats['views'] = _int_or_none(value.get_text())
            elif key.startswith('回复'):
                stats['replies'] = _int_or_none(value.get_text())
    favorites = soup.select_one('#favoritenumber')
    if favorites is not None:
        stats['favorites'] = _int_or_none(favorites.get_text())
    return stats


def parse_thread(html, url):
    soup = BeautifulSoup(html, "html.parser")
    match = re.search(r"thread-(\d+)-(\d+)-", url)
    title = soup.select_one('#thread_subject')
    nodes = soup.select('[id^="postmessage_"]')
    if not match or not title or not nodes or soup.select_one('#challenge-form'):
        raise ValueError("No readable thread structure; possible login, permission, or challenge page")
    canonical = soup.select_one('link[rel="canonical"][href]')
    if canonical and parse_thread_reference(urljoin(url, canonical['href']))['tid'] != int(match[1]):
        raise ValueError('unexpected_thread_document')
    current_page = soup.select_one('.pg strong, .pg [aria-current="page"]')
    if current_page and current_page.get_text(strip=True) != str(int(match[2])):
        raise ValueError('unexpected_thread_document_page')
    container = soup.select_one('#postlist') or soup
    count = re.search(r"回复\s*:\s*(\d+)", container.get_text(' ', strip=True))
    posts = []
    for node in nodes:
        pid = int(node['id'].split('_')[-1])
        outer = soup.select_one(f'#post_{pid}') or node.parent
        stamp = outer.select_one(f'#authorposton{pid}') if outer else None
        date_title = stamp.select_one('[title]') if stamp else None
        authored_at = date_title.get('title') if date_title else (stamp.get('title') or stamp.get_text(' ', strip=True)) if stamp else None
        notices = [clean_text(item) for item in node.select('.locked')]
        for locked in list(node.select('.locked')):
            locked.replace_with('[内容受限，当前账户未获得该部分正文]')
        images = [{"url": urljoin(url, image.get('file') or image.get('src') or ''), "alt": image.get('alt', '')}
                  for image in node.select('img') if image.get('file') or image.get('src')]
        links = [{"url": urljoin(url, a.get('href', '')), "text": a.get_text(' ', strip=True)}
                 for a in node.select('a[href]') if a.get('href', '').startswith(('http', '/', 'thread-'))]
        text = clean_text(node)
        notice_pattern = re.search(r"需要.*?积分.*?才可|无权.*?查看|本内容被作者隐藏|本帖需要.*?权限|购买.*?主题", text)
        attachments, videos = _post_media(node, url)
        posts.append({"pid": pid, "text": text, "authored_at": authored_at,
                      "restricted": bool(notices or notice_pattern), "permission_notices": notices,
                      "images": images, "links": links,
                      # Metadata the page actually carries (issue #22); absent means absent, never zero.
                      "author": _post_author(outer), "published_at": _post_time(outer, pid),
                      "quotes": _post_quotes(node, url), "ratings": _post_ratings(outer),
                      "attachments": attachments, "videos": videos})
    fetched_at = datetime.now(timezone.utc).isoformat()
    next_url = next_link(soup, url, preserve_invalid=True)
    restricted = any(post['restricted'] for post in posts)
    expected = int(count[1]) + 1 if count else None
    complete = bool(not restricted and not next_url and expected is not None and len(posts) >= expected)
    return {"tid": int(match[1]), "page": int(match[2]), "title": title.get_text(' ', strip=True),
            "url": f"{SITE}/bbs/thread-{match[1]}-1-1.html", "posts": posts,
            "expected_posts": expected, "next_url": next_url, "complete": complete,
            "content_status": ContentStatus.RESTRICTED if restricted else ContentStatus.VISIBLE,
            "fetched_at": fetched_at, "stats": _thread_stats(soup, fetched_at),
            "content_hash": hashlib.sha256('\n'.join(p['text'] for p in posts).encode('utf-8')).hexdigest()}
