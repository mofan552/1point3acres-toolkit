"""Target-state operations on the forum: the one place that changes site state on a user's explicit request.

daily.py owns the account's own daily reward flow and library.py owns the local archive; a favorite, a
like or a reply is neither, so those modules stay free of side effects a user asks for one at a time.
Every operation here takes a desired state, reads the real state first, submits only when they differ,
and reads the state back before claiming anything. Browser remains the only module that talks to the site.
"""
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from browser import Browser
from contracts import (format_error, publish_result, target_state_result, session_result, notification_item, RunStatus,
                       notification_action_result)
from extract import (parse_discuz_ajax, parse_favorites, parse_profile_reference, parse_profile_threads,
                     parse_reactions, parse_thread_page, parse_thread_reference, normalize_message)
from settings import (SITE, LIST_PAGES, LIST_PAGES_MAX, LIKE_REACTION_ID, SUBJECT_MAX_LENGTH, MESSAGE_MAX_LENGTH,
                      NOTIFICATION_KINDS, NOTIFICATION_LIMIT, NOTIFICATION_MAX, NOTIFICATION_PAGE_LIMIT,
                      IMAGE_UPLOAD_TYPES, IMAGE_UPLOAD_MAX_BYTES, IMAGE_UPLOAD_MAX_COUNT, IMAGE_PLACEHOLDER,
                      VIDEO_UPLOAD_TYPES, VIDEO_UPLOAD_MAX_BYTES)

FAVORITE_ACTION = 'favorite'
REACTION_ACTION = 'reaction'


def _favorite_state(browser, uid, tid, list_pages):
    """Whether the signed-in member has this thread in their favorites, from the list the site shows them.

    The list is newest first, so a fresh favorite is on the first page; an older one may need more pages.
    Running out of page budget before the end leaves the state unknown, and unknown is never a guess.
    """
    url = f'{SITE}/bbs/home.php?mod=space&uid={uid}&do=favorite&view=me&type=thread'
    scanned = 0
    for _ in range(list_pages):
        page = parse_favorites(browser.read_html(url), url)
        scanned += 1
        for row in page['favorites']:
            if row['tid'] == tid:
                return {'favorited': True, 'favorite_id': row['favorite_id'], 'pages_scanned': scanned}
        url = page['next_url']
        if not url:
            return {'favorited': False, 'favorite_id': None, 'pages_scanned': scanned}
        query = parse_qs(urlsplit(url).query)
        if query.get('uid') != [str(uid)] or query.get('do') != ['favorite']:
            raise ValueError('invalid_profile_pagination')
        browser.sb.sleep(1)
    return {'favorited': None, 'favorite_id': None, 'pages_scanned': scanned}


def _popup_form(browser, url, expected_field):
    """Read the site's confirmation popup: its hidden fields carry the formhash the submission needs."""
    popup = parse_discuz_ajax(browser.read_html(url))
    if popup['form'] is None or expected_field not in popup['form']['fields']:
        message = popup['message'] or popup['notice'] or ''
        raise RuntimeError('thread_not_favoritable' if '无法收藏' in message else 'favorite_form_unavailable')
    return popup['form']


def set_favorite(thread, favorited, list_pages=LIST_PAGES):
    """Favorite or unfavorite one thread by target state; the favorites list is the truth before and after.

    Nothing is submitted when the state is already the goal or cannot be determined, so a repeated
    request never flips the state back and a lost read never turns into a blind click.
    """
    tid = None
    before = after = None
    favorite_id = None
    submitted = False
    message = None
    scanned = 0
    error = None
    try:
        if type(favorited) is not bool:
            raise ValueError('invalid_target_state')
        if type(list_pages) is not int or not 1 <= list_pages <= LIST_PAGES_MAX:
            raise ValueError('invalid_list_budget')
        tid = parse_thread_reference(thread, first_page=True)['tid']
        with Browser() as browser:
            uid = browser.profile()['uid']
            state = _favorite_state(browser, uid, tid, list_pages)
            before, favorite_id, scanned = state['favorited'], state['favorite_id'], state['pages_scanned']
            if before is None:
                raise RuntimeError('favorite_state_unverified')
            if before == favorited:
                after = before
            else:
                if favorited:
                    form = _popup_form(browser, f'{SITE}/bbs/home.php?mod=spacecp&ac=favorite&type=thread&id={tid}'
                                       '&handlekey=k_favorite&infloat=yes&inajax=1', 'favoritesubmit')
                    fields = {**form['fields'], 'description': ''}
                else:
                    form = _popup_form(browser, f'{SITE}/bbs/home.php?mod=spacecp&ac=favorite&op=delete&favid={favorite_id}'
                                       '&type=all&handlekey=delfavorite&infloat=yes&inajax=1', 'deletesubmit')
                    fields = form['fields']
                submitted = True
                outcome = parse_discuz_ajax(browser.submit_form(form['action'], fields))
                message = outcome['message'] or outcome['notice']
                state = _favorite_state(browser, uid, tid, list_pages)
                after, favorite_id, scanned = state['favorited'], state['favorite_id'], state['pages_scanned']
    except Exception as caught:
        error = format_error(caught)
    return target_state_result(FAVORITE_ACTION, tid, favorited, before=before, after=after, submitted=submitted,
                               error=error, favorite_id=favorite_id, site_message=message,
                               favorites_pages_scanned=scanned, scope='own_account')


def _reaction_page(browser, tid, pid):
    """The thread page holding the target post, checked to be this thread, with its reactions parsed.

    Without a pid the first post on page one is the main post; with a pid the site's own findpost redirect
    lands on the page that contains it. Voting entries the site also offers (顶/踩 on a thread, 支持/反对
    on a reply, credit-costing 评分) are one-shot or paid and are deliberately not what this reads.
    """
    if pid is None:
        url = f'{SITE}/bbs/thread-{tid}-1-1.html'
    else:
        url = f'{SITE}/bbs/forum.php?mod=redirect&goto=findpost&ptid={tid}&pid={pid}'
    reactions = parse_reactions(browser.read_html(url), tid)
    if pid is None:
        pid = next(iter(reactions))
    if pid not in reactions:
        raise ValueError('post_not_found')
    return pid, reactions[pid]


def set_reaction(thread, reacted, pid=None, reaction_id=LIKE_REACTION_ID):
    """Add or remove one emoji reaction on one post by target state; the thread page is read back after.

    This is the site's only zero-cost, revocable like: the page marks the member's own reactions, so the
    state is read before (nothing is sent when it already matches) and after (the site's ok is not the
    result). pid None means the thread's main post; a reply is addressed by its own pid, never mixed up.
    """
    tid = None
    before = after = None
    count_before = count_after = None
    submitted = False
    message = None
    error = None
    try:
        if type(reacted) is not bool:
            raise ValueError('invalid_target_state')
        if pid is not None and (type(pid) is not int or pid <= 0):
            raise ValueError('invalid_post_reference')
        if type(reaction_id) is not int or reaction_id <= 0:
            raise ValueError('invalid_reaction_id')
        tid = parse_thread_reference(thread, first_page=True)['tid']
        with Browser() as browser:
            browser.profile()
            pid, entries = _reaction_page(browser, tid, pid)
            current = entries.get(reaction_id, {'count': 0, 'mine': False})
            before, count_before = current['mine'], current['count']
            if before == reacted:
                after, count_after = before, count_before
            else:
                submitted = True
                answer = browser.api_request('PUT' if reacted else 'DELETE', f'/api/posts/{pid}/reactions',
                                             {'reaction_id': reaction_id})
                body = answer['body'] if isinstance(answer['body'], dict) else {}
                message = body.get('msg')
                if answer['status'] != 200 or (isinstance(body.get('errno'), int) and body['errno'] < 0):
                    error = 'reaction_rejected'
                _, entries = _reaction_page(browser, tid, pid)
                current = entries.get(reaction_id, {'count': 0, 'mine': False})
                after, count_after = current['mine'], current['count']
                if after == reacted:
                    error = None  # the read-back, not the site's verdict, is the result
    except Exception as caught:
        error = format_error(caught)
    return target_state_result(REACTION_ACTION, tid, reacted, before=before, after=after, submitted=submitted,
                               error=error, pid=pid, reaction_id=reaction_id, count_before=count_before,
                               count_after=count_after, site_message=message, scope='own_account')


THREAD_ACTION = 'thread'
REPLY_ACTION = 'reply'


def _text_argument(value, name, limit):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('missing_' + name)
    if len(value) > limit:
        raise ValueError(name + '_too_long')
    return value.strip()


def _forum(browser, fid):
    """The board as the site's own editor reads it: name, classification lists and who may post."""
    answer = browser.rpc('forum.get', {'fid': fid})
    forum = answer.get('forum') if isinstance(answer, dict) else None
    if not isinstance(forum, dict) or forum.get('fid') != fid:
        raise RuntimeError('board_not_found')
    return forum


def _thread_detail(browser, tid):
    answer = browser.api_get(f'/api/v3/threads/{tid}')
    body = answer['body'] if isinstance(answer['body'], dict) else {}
    thread = body.get('thread')
    if answer['status'] != 200 or body.get('errno', 0) != 0 or not isinstance(thread, dict):
        raise RuntimeError('thread_not_found' if answer['status'] in (200, 404) else 'thread_http_' + str(answer['status']))
    return thread


def _find_own_thread(browser, uid, subject):
    """After a lost response: the member's own thread list, newest first, may already show the thread."""
    reference = parse_profile_reference(uid)
    page = parse_profile_threads(browser.read_html(reference['threads_url']), reference['threads_url'])
    wanted = normalize_message(subject)
    return next((row['tid'] for row in page['threads'] if normalize_message(row['title']) == wanted), None)


def _find_own_reply(browser, uid, tid, message):
    """After a lost response: the thread's last page may already carry the reply, by author and text."""
    page = parse_thread_page(browser.read_html(f'{SITE}/bbs/forum.php?mod=redirect&tid={tid}&goto=lastpost'), tid)
    wanted = normalize_message(message)
    for post in reversed(page['posts']):
        author = post.get('author') or {}
        if author.get('uid') == uid and normalize_message(post['text']) == wanted:
            return post
    return None


# Image markup the site may show back: the [img] we sent, or the [attachimg]/[attach] tags Discuz adds
# for the thread's attachments (a real publish appended "[attach]<aid>[/attach]" after the text).
IMAGE_TOKEN = re.compile(r'\[img(?:=[^\]]*)?\][^\[]*\[/img\]|\[attach(?:img)?\]\d+\[/attach(?:img)?\]', re.I)


def _plan_images(message, images):
    """Check the chosen files and fix where each one goes before anything is uploaded.

    `[image:N]` in the message places the N-th image there; images not placed follow the text in
    order. Only the named files are read, only the site's image types are accepted, and the bounds stop
    the whole thread before the first upload, so a partly uploaded thread cannot happen by design.
    """
    if images is None:
        images = []
    if not isinstance(images, (list, tuple)) or not all(isinstance(item, str) and item.strip() for item in images):
        raise ValueError('invalid_image_list')
    if len(images) > IMAGE_UPLOAD_MAX_COUNT:
        raise ValueError('too_many_images')
    plan = []
    for index, item in enumerate(images, start=1):
        path = Path(item)
        suffix = path.suffix.lower()
        if suffix not in IMAGE_UPLOAD_TYPES:
            raise ValueError('unsupported_image_type')
        if not path.is_file():
            raise ValueError('image_not_found')
        size = path.stat().st_size
        if size <= 0 or size > IMAGE_UPLOAD_MAX_BYTES:
            raise ValueError('image_too_large' if size else 'image_empty')
        plan.append({'index': index, 'path': str(path), 'name': path.name, 'mime': IMAGE_UPLOAD_TYPES[suffix],
                     'size': size, 'placement': 'appended'})
    placed = [int(number) for number in re.findall(IMAGE_PLACEHOLDER, message)]
    if any(number < 1 or number > len(plan) for number in placed):
        raise ValueError('image_placeholder_out_of_range')
    if len(placed) != len(set(placed)):
        raise ValueError('image_placeholder_repeated')
    for number in placed:
        plan[number - 1]['placement'] = 'inline'
    order = placed + [entry['index'] for entry in plan if entry['placement'] == 'appended']
    return plan, order


def _upload_images(browser, plan):
    """Upload the planned files in order through the site's three-step flow; stop at the first failure.

    Files already uploaded when a later one fails are deleted again (they are this call's own, unused
    attachments; nothing else of the member's is touched), so a failed thread leaves nothing behind.
    """
    uploaded = []
    try:
        for entry in plan:
            data = Path(entry['path']).read_bytes()
            started = browser.api_request('POST', '/api/v2/attachment/upload-init',
                                          {'type': entry['mime'], 'name': entry['name'], 'size': entry['size']})
            init = started['body'] if isinstance(started['body'], dict) else {}
            if started['status'] != 200 or type(init.get('aid')) is not int or not init.get('upload_token'):
                raise RuntimeError('image_upload_rejected')
            receipt = browser.upload_file(init['upload_token'], entry['name'], entry['mime'], data)
            token = receipt.get('receipt_token') if isinstance(receipt, dict) else None
            if not token:
                raise RuntimeError('image_upload_rejected')
            done = browser.api_request('POST', '/api/v2/attachment/upload-complete',
                                       {'aid': init['aid'], 'size': entry['size'], 'receipt_token': token})
            finished = done['body'] if isinstance(done['body'], dict) else {}
            if done['status'] != 200 or finished.get('errno', -1) != 0:
                raise RuntimeError('image_upload_rejected')
            uploaded.append({**entry, 'aid': init['aid'], 'url': init.get('attach_url')})
    except Exception:
        for entry in uploaded:
            try:
                browser.api_request('DELETE', f"/api/user/unused-attachments/{entry['aid']}", {})
            except Exception:
                pass  # the thread is not published either way; leftovers are reported, not hidden
        raise
    return uploaded


def _compose_message(message, uploaded):
    """The message as the site's editor would send it: each placeholder becomes the image's own
    BBCode, unplaced images follow the text in order, and the attachments travel alongside."""
    by_index = {entry['index']: entry for entry in uploaded}

    def inline(match):
        return f"[img]{by_index[int(match[1])]['url']}[/img]"
    text = re.sub(IMAGE_PLACEHOLDER, inline, message)
    tail = ''.join(f"\n[img]{entry['url']}[/img]" for entry in uploaded if entry['placement'] == 'appended')
    return text + tail


def _plan_video(video):
    """Check the one chosen video file before anything is uploaded: only the named file is read."""
    if video is None:
        return None
    if not isinstance(video, str) or not video.strip():
        raise ValueError('invalid_video_reference')
    path = Path(video)
    suffix = path.suffix.lower()
    if suffix not in VIDEO_UPLOAD_TYPES:
        raise ValueError('unsupported_video_type')
    if not path.is_file():
        raise ValueError('video_not_found')
    size = path.stat().st_size
    if size <= 0 or size > VIDEO_UPLOAD_MAX_BYTES:
        raise ValueError('video_too_large' if size else 'video_empty')
    return {'path': str(path), 'name': path.name, 'mime': VIDEO_UPLOAD_TYPES[suffix], 'size': size}


def _upload_video(browser, plan):
    """The site's native video flow, as its own editor runs it: ask for a one-off upload URL, send the
    file there (no cookies), then register the upload to get the video_id the thread will carry."""
    started = browser.api_request('POST', '/api/videos/upload-url', {})
    grant = started['body'] if isinstance(started['body'], dict) else {}
    if started['status'] != 200 or not isinstance(grant.get('uploadURL'), str) or not grant.get('uid'):
        raise RuntimeError('video_upload_rejected')
    browser.upload_file(None, plan['name'], plan['mime'], Path(plan['path']).read_bytes(), url=grant['uploadURL'])
    registered = browser.api_request('POST', '/api/videos/upload', {'vid': grant['uid']})
    body = registered['body'] if isinstance(registered['body'], dict) else {}
    if registered['status'] != 200 or not body.get('video_id'):
        raise RuntimeError('video_upload_rejected')
    return {**plan, 'vid': grant['uid'], 'video_id': body['video_id']}


def create_thread(fid, subject, message, typeid=None, sortid=None, submit=False, images=None, video=None):
    """Publish one text thread, with chosen local images if any, to one board, or only preview it.

    The preview reads the board (name, classification lists, posting groups), checks the files, and
    nothing else, so it can be shown and corrected without any write. With submit=True the images are
    uploaded first (all or none), then the same validated content is posted once through the site's
    editor API; the created thread is then read back and compared, images included. A response that
    never arrived is looked up in the member's own thread list before anything is concluded, and a
    thread the site holds for review is reported as pending, never as published or as a reason to retry.
    """
    submitted = False
    preview = None
    tid = url = pid = None
    confirmed = None
    pending = rejected = recovered = False
    message_from_site = None
    error = None
    try:
        if type(fid) is not int or fid <= 0:
            raise ValueError('invalid_board_reference')
        if type(submit) is not bool:
            raise ValueError('invalid_submit_flag')
        for value, name in ((typeid, 'typeid'), (sortid, 'sortid')):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('invalid_' + name)
        subject = _text_argument(subject, 'subject', SUBJECT_MAX_LENGTH)
        message = _text_argument(message, 'message', MESSAGE_MAX_LENGTH)
        plan, order = _plan_images(message, images)
        video_plan = _plan_video(video)
        with Browser() as browser:
            user = browser.profile()
            forum = _forum(browser, fid)
            field = forum.get('forum_field') or {}
            types = {int(k): v for k, v in (field.get('thread_types') or {}).items()}
            sorts = {int(k): v for k, v in (field.get('thread_sorts') or {}).items() if int(k)}
            checks = {'board_open': forum.get('status') == 1,
                      'may_post': user.get('groupid') in (field.get('post_groups') or []),
                      'type_valid': typeid is None or typeid in types,
                      'sort_valid': sortid is None or sortid in sorts,
                      'type_chosen': not types or typeid is not None}
            preview = {'fid': fid, 'board': forum.get('name'), 'typeid': typeid, 'type_label': types.get(typeid),
                       'sortid': sortid, 'sort_label': sorts.get(sortid), 'subject': subject, 'message': message,
                       'anonymous': False, 'available_types': types, 'available_sorts': sorts, 'checks': checks,
                       'images': plan, 'image_order': order, 'video': video_plan}
            for key, reason in (('board_open', 'board_closed'), ('may_post', 'post_permission_denied'),
                                ('type_valid', 'unknown_thread_type'), ('sort_valid', 'unknown_thread_sort'),
                                ('type_chosen', 'thread_type_required')):
                if not checks[key]:
                    raise RuntimeError(reason)
            if not submit:
                return publish_result(THREAD_ACTION, submitted=False, preview=preview)
            uploaded = _upload_images(browser, plan) if plan else []
            uploaded_video = None
            if video_plan is not None:
                try:
                    uploaded_video = _upload_video(browser, video_plan)
                except Exception:
                    for entry in uploaded:  # the thread will not be posted; this call's own images go too
                        try:
                            browser.api_request('DELETE', f"/api/user/unused-attachments/{entry['aid']}", {})
                        except Exception:
                            pass
                    raise
            body_text = _compose_message(message, uploaded) if uploaded else message
            payload = {'fid': fid, 'typeid': typeid or 0, 'sortid': sortid or 0, 'subject': subject, 'message': body_text,
                       'attachments': [{'aid': entry['aid'], 'description': '', 'readperm': 0, 'price': 0} for entry in uploaded],
                       'htmlon': False, 'anonymous': 0, 'usesig': 0, 'poll': None, 'magic_ids': []}
            if uploaded_video is not None:
                payload['video_id'] = uploaded_video['video_id']
            submitted = True
            try:
                answer = browser.api_request('POST', '/api/threads', payload)
            except RuntimeError as lost:
                if str(lost) != 'api_submission_unconfirmed':
                    raise
                tid = _find_own_thread(browser, user['uid'], subject)
                if tid is None:
                    raise
                recovered = True
                answer = None
            if answer is not None:
                body = answer['body'] if isinstance(answer['body'], dict) else {}
                message_from_site = body.get('msg')
                errno = body.get('errno') if isinstance(body.get('errno'), int) else None
                created = body.get('thread') if isinstance(body.get('thread'), dict) else {}
                if errno == -2:
                    pending = True
                elif answer['status'] != 200 or errno not in (0, None) or type(created.get('tid')) is not int:
                    rejected = True
                    raise RuntimeError('thread_rejected')
                else:
                    tid = created['tid']
            if tid is not None:
                thread = _thread_detail(browser, tid)
                pid, url = thread.get('pid'), f'{SITE}/bbs/thread-{tid}-1-1.html'
                shown = thread.get('message_bbcode') or ''
                # Text is compared with the image markup on both sides removed: the site's [img] tokens and
                # this tool's [image:N] placeholders; the images themselves are checked by attachment below.
                text_ok = (normalize_message(thread.get('subject')) == normalize_message(subject)
                           and normalize_message(IMAGE_TOKEN.sub('', shown)) == normalize_message(re.sub(IMAGE_PLACEHOLDER, '', message)))
                # An image counts as published only when the site lists its attachment on the thread.
                listed = {item.get('aid') for item in (thread.get('attachment_list') or []) if isinstance(item, dict)}
                images_ok = all(entry['aid'] in listed for entry in uploaded)
                confirmed = text_ok and images_ok
                if not confirmed:
                    error = 'published_content_differs' if not text_ok else 'images_not_visible'
                if uploaded_video is not None:
                    # The video counts as published only when the thread lists it by the id the site issued.
                    shown_videos = thread.get('videos') or []
                    video_ok = any(str(uploaded_video['video_id']) in {str(v) for v in (item.values() if isinstance(item, dict) else [item])}
                                   for item in shown_videos)
                    preview['video_verified'] = video_ok
                    if confirmed and not video_ok:
                        confirmed, error = False, 'video_not_visible'
                if uploaded:
                    tokens = [token for token in IMAGE_TOKEN.findall(shown) if token.lower().startswith('[img')]
                    expected = [f"[img]{entry['url']}[/img]" for entry in sorted(uploaded, key=lambda e: order.index(e['index']))]
                    preview['image_order_verified'] = tokens == expected if tokens else None
    except Exception as caught:
        error = format_error(caught)
    return publish_result(THREAD_ACTION, submitted=submitted, preview=preview, tid=tid, pid=pid, url=url,
                          confirmed=confirmed, pending_review=pending, rejected=rejected,
                          site_message=message_from_site, recovered=recovered, error=error)


def reply_thread(thread, message, quote_pid=None, submit=False):
    """Reply to one thread, plainly or to one specific post, or only preview it; the thread decides.

    Before anything: the thread must exist and be open, the site's reply-permission check must pass,
    and a quoted post must be on this thread (a pid from elsewhere is refused, never silently turned into
    a plain reply). With submit=True the reply is posted once; the new post is then read back on the
    page the site's own findpost redirect lands on, and its text and quote relation are compared. A
    response that never arrived is looked up on the thread's last page before anything is concluded.
    """
    submitted = False
    preview = None
    tid = pid = url = None
    confirmed = None
    rejected = recovered = False
    message_from_site = None
    error = None
    try:
        if type(submit) is not bool:
            raise ValueError('invalid_submit_flag')
        if quote_pid is not None and (type(quote_pid) is not int or quote_pid <= 0):
            raise ValueError('invalid_post_reference')
        tid = parse_thread_reference(thread, first_page=True)['tid']
        message = _text_argument(message, 'message', MESSAGE_MAX_LENGTH)
        with Browser() as browser:
            user = browser.profile()
            detail = _thread_detail(browser, tid)
            if detail.get('close_status'):
                raise RuntimeError('thread_closed')
            permission = browser.api_get(f'/api/threads/{tid}/reply-perm')
            verdict = permission['body'] if isinstance(permission['body'], dict) else {}
            if permission['status'] != 200 or verdict.get('errno', -1) != 0:
                message_from_site = verdict.get('msg')
                raise RuntimeError('reply_permission_denied')
            quoted = None
            if quote_pid is not None:
                page = parse_thread_page(browser.read_html(
                    f'{SITE}/bbs/forum.php?mod=redirect&goto=findpost&ptid={tid}&pid={quote_pid}'), tid)
                target = next((post for post in page['posts'] if post['pid'] == quote_pid), None)
                if target is None:
                    raise RuntimeError('quoted_post_not_in_thread')
                quoted = {'pid': quote_pid, 'author': (target.get('author') or {}).get('name'),
                          'excerpt': target['text'][:200], 'page': page['page']}
            preview = {'tid': tid, 'subject': detail.get('subject'), 'board': (detail.get('forum_name') or {}).get('name'),
                       'quote': quoted, 'message': message, 'anonymous': False}
            if not submit:
                return publish_result(REPLY_ACTION, submitted=False, preview=preview, tid=tid)
            payload = {'message': message, 'attachments': [], 'anonymous': 0, 'usesig': 0, 'magic_ids': [], 'app': 'new_home'}
            if quote_pid is not None:
                payload['quote_pid'] = quote_pid
            submitted = True
            try:
                answer = browser.api_request('POST', f'/api/threads/{tid}/posts', payload)
            except RuntimeError as lost:
                if str(lost) != 'api_submission_unconfirmed':
                    raise
                found = _find_own_reply(browser, user['uid'], tid, message)
                if found is None:
                    raise
                recovered, pid = True, found['pid']
                answer = None
            if answer is not None:
                body = answer['body'] if isinstance(answer['body'], dict) else {}
                message_from_site = body.get('msg')
                errno = body.get('errno') if isinstance(body.get('errno'), int) else None
                data = body.get('data') if isinstance(body.get('data'), dict) else {}
                if answer['status'] != 200 or errno not in (0, None) or type(data.get('pid')) is not int:
                    rejected = True
                    raise RuntimeError('reply_rejected')
                pid = data['pid']
            page = parse_thread_page(browser.read_html(
                f'{SITE}/bbs/forum.php?mod=redirect&goto=findpost&ptid={tid}&pid={pid}'), tid)
            posted = next((post for post in page['posts'] if post['pid'] == pid), None)
            url = f'{SITE}/bbs/forum.php?mod=redirect&goto=findpost&ptid={tid}&pid={pid}'
            if posted is None:
                error = 'reply_not_found_on_page'
            else:
                text_ok = normalize_message(posted['text']).endswith(normalize_message(message))
                quote_ok = quote_pid is None or any(q.get('pid') == quote_pid for q in posted.get('quotes') or [])
                confirmed = text_ok and quote_ok
                if not confirmed:
                    error = 'published_content_differs' if not text_ok else 'quote_relation_missing'
    except Exception as caught:
        error = format_error(caught)
    return publish_result(REPLY_ACTION, submitted=submitted, preview=preview, tid=tid, pid=pid, url=url,
                          confirmed=confirmed, rejected=rejected, site_message=message_from_site,
                          recovered=recovered, error=error)


def _prompts(browser):
    """The site's per-tab unread counters; a missing or non-integer value is unknown, not zero."""
    data = browser.rpc('notificationV2.getNewPrompts', {'scope': 'all'})
    prompt = data.get('prompt') if isinstance(data, dict) and isinstance(data.get('prompt'), dict) else {}
    return {name: prompt[name] if type(prompt.get(name)) is int else None for name in NOTIFICATION_KINDS + ('total',)}


def _read_notifications(browser, kind, limit, cursor):
    """Walk one tab from `cursor`: pages follow the site's cursor, ids are deduplicated, the page count
    and `limit` are bounds. Returns (items, next_cursor, pages_read, truncated); when truncated,
    next_cursor still names the cut page so nothing is skipped."""
    items, pages, truncated = [], 0, False
    next_cursor = cursor
    seen = set()
    while True:
        page_cursor = next_cursor
        request = {'type': kind, 'scope': 'all', 'direction': 'forward'}
        if page_cursor is not None:
            request['cursor'] = page_cursor
        data = browser.rpc('notificationV2.getNotifications', request)
        rows = data.get('data') if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError('notification_response_invalid')
        pages += 1
        for row in rows:
            item = notification_item(row, kind)
            if item['id'] in seen:
                continue
            if len(items) >= limit:
                truncated = True
                break
            seen.add(item['id'])
            items.append(item)
        if truncated:
            break
        next_cursor = data.get('cursor') if type(data.get('cursor')) in (str, int) else None
        if next_cursor is None or not rows or pages >= NOTIFICATION_PAGE_LIMIT:
            break
    return items, next_cursor, pages, truncated


def _find_notification(browser, notification_id, kind):
    """The one notification with this id, looked up on the site right now (never from a cached list), on
    the named tab or across all tabs. Missing means missing: no other item stands in for it."""
    for tab in ([kind] if kind else NOTIFICATION_KINDS):
        items, _, _, _ = _read_notifications(browser, tab, NOTIFICATION_MAX, None)
        for item in items:
            if item['id'] == notification_id:
                return item
    raise RuntimeError('notification_not_found')


def _notification_post(notification_id, kind):
    """Resolve a notification to the exact post it is about, or stop.

    A notification about a whole thread (a vote on it, pid null) has no post to reply to or react on; it
    is refused rather than redirected to the thread's main post. The lookup is its own browser session so
    the reply and reaction services below keep their single write path unchanged."""
    if type(notification_id) is not int or notification_id <= 0:
        raise ValueError('invalid_notification_reference')
    if kind is not None and kind not in NOTIFICATION_KINDS:
        raise ValueError('invalid_notification_kind')
    with Browser() as browser:
        browser.profile()
        item = _find_notification(browser, notification_id, kind)
    if item['target']['tid'] is None:
        raise RuntimeError('notification_target_unavailable', item)
    if item['target']['pid'] is None:
        raise RuntimeError('notification_has_no_post', item)
    return item


REPLY_FROM_NOTIFICATION = 'reply_from_notification'
REACT_FROM_NOTIFICATION = 'react_from_notification'


def _from_notification(action, notification_id, kind, perform):
    notification = target = outcome = None
    error = None
    try:
        notification = _notification_post(notification_id, kind)
        target = {'tid': notification['target']['tid'], 'pid': notification['target']['pid']}
        outcome = perform(target)
    except RuntimeError as stopped:
        error = str(stopped.args[0])[:180] if stopped.args else 'runtime_error'
        if len(stopped.args) > 1 and isinstance(stopped.args[1], dict):
            notification = stopped.args[1]
    except Exception as caught:
        error = format_error(caught)
    return notification_action_result(action, notification=notification, target=target, outcome=outcome, error=error)


def reply_to_notification(notification_id, message, submit=False, kind=None):
    """Reply to the post a notification is about (#35): the notification only names the target; the text
    is the caller's own, and the reply itself is the ordinary targeted reply with all its checks
    (thread open, permission, quoted post really on the thread, one submission, read-back)."""
    return _from_notification(REPLY_FROM_NOTIFICATION, notification_id, kind,
                              lambda target: reply_thread(target['tid'], message, quote_pid=target['pid'], submit=submit))


def react_to_notification(notification_id, reacted, reaction_id=LIKE_REACTION_ID, kind=None):
    """Put one reaction on, or take it off, the post a notification is about (#36); the reaction itself is
    the ordinary target-state operation, addressed by that post's own pid, never the main post."""
    return _from_notification(REACT_FROM_NOTIFICATION, notification_id, kind,
                              lambda target: set_reaction(target['tid'], reacted, pid=target['pid'], reaction_id=reaction_id))


def list_notifications(kind='post', limit=NOTIFICATION_LIMIT, cursor=None):
    """Read one tab of the site's notifications through the tRPC its own page uses (#34).

    No mark-as-read request is ever sent; whether the site clears its unread counter merely because the
    list was read is not assumed either way but measured: the tab's counter is read before and after, and
    `unread_cleared_by_read` says what happened when the page held unread items (None when it held none,
    so nothing could be observed). Pages follow the site's cursor, items are deduplicated by id, and a
    result cut by `limit` says so and hands back the cursor of the cut page rather than skipping past it.
    """
    read_at = datetime.now(timezone.utc).isoformat()
    items, pages, truncated = [], 0, False
    next_cursor = cursor
    before = after = None
    error = None
    state = None
    try:
        if kind not in NOTIFICATION_KINDS:
            raise ValueError('invalid_notification_kind')
        if type(limit) is not int or not 1 <= limit <= NOTIFICATION_MAX:
            raise ValueError('invalid_limit')
        if cursor is not None and (type(cursor) not in (str, int) or cursor == ''):
            raise ValueError('invalid_cursor')
        with Browser() as browser:
            browser.profile()
            before = _prompts(browser)
            items, next_cursor, pages, truncated = _read_notifications(browser, kind, limit, cursor)
            after = _prompts(browser)
            state = session_result()['session_state']
    except Exception as caught:
        error = format_error(caught)
        state = session_result(caught)['session_state']
    observed = any(item['new'] for item in items)
    cleared = None
    if error is None and observed and type(before.get(kind)) is int and type(after.get(kind)) is int:
        cleared = after[kind] < before[kind]
    return {'status': RunStatus.COMPLETE if error is None else RunStatus.FAILED, 'session_state': state, 'error': error,
            'kind': kind, 'items': items, 'count': len(items), 'cursor': next_cursor if error is None else None,
            'pagination_complete': error is None and not truncated and next_cursor is None, 'pages_read': pages,
            'mark_read_requested': False, 'prompts_before': before, 'prompts_after': after,
            'unread_cleared_by_read': cleared, 'read_at': read_at, 'source': 'notificationV2'}
