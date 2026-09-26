import argparse
import json
import sys
from pathlib import Path
from daily import run_daily, resume_daily
from browser import session_status, session_login, session_logout, get_unread_counts
from interact import (set_favorite, set_reaction, create_thread, reply_thread, list_notifications, reply_to_notification,
                      react_to_notification)
from library import (Library, collect_stripe, export_library, get_thread_detail, search_threads,
                     get_daily_history, browse_board, get_user_profile, get_my_profile, save_thread,
                     collect_company, organize_thread, create_collection_task, run_task, task_status,
                     control_task, list_tasks, archive_media, recognize_media)
from settings import (COLLECT_COMPANY, COLLECT_LIMIT, LIST_PAGES, SEARCH_LIMIT, THREAD_PAGES, SITE_SEARCH_LIMIT,
                      LOGIN_METHOD, WECHAT_LOGIN_TIMEOUT, HISTORY_LIMIT, BOARD_LIMIT, LIKE_REACTION_ID, TASK_LIST_LIMIT, MEDIA_MAX_PER_THREAD,
                      OCR_MAX_IMAGES, NOTIFICATION_KINDS, NOTIFICATION_LIMIT)
from contracts import RunStatus, is_failure, health_alerting, format_error
from governance import runtime_info


def main():
    parser = argparse.ArgumentParser(description='一亩三分地本地任务与面经资料库')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    commands.add_parser('info', help='离线检查源码、已加载版本和调度配置，不读取凭据或打开浏览器')
    history = commands.add_parser('daily-history')
    history.add_argument('--date')
    history.add_argument('--limit', type=int, default=HISTORY_LIMIT)
    # For an independent watcher: a scheduler reads the exit code, not the JSON.
    history.add_argument('--fail-on-alert', action='store_true')
    commands.add_parser('session-status')
    commands.add_parser('session-logout', help='清除本工具专用浏览器里的本站登录，并验证已退出；不会自动重新登录')
    commands.add_parser('unread')
    notices = commands.add_parser('notifications', help='读一个通知分页（帖子回复 / 赞与收藏 / 系统提醒）；不发标记已读请求，读取前后各查一次未读数如实回传')
    notices.add_argument('--kind', default=NOTIFICATION_KINDS[0], choices=list(NOTIFICATION_KINDS))
    notices.add_argument('--limit', type=int, default=NOTIFICATION_LIMIT)
    notices.add_argument('--cursor', help='上一次结果里的 cursor，从那里继续')
    login = commands.add_parser('session-login', help='用钥匙串密码恢复会话；--method wechat 在工具的 Chrome 窗口里显示官方微信二维码并等待本人扫码确认')
    login.add_argument('--method', default=LOGIN_METHOD, help='password 或 wechat；其他值不启动浏览器直接拒绝')
    login.add_argument('--wait', type=int, default=WECHAT_LOGIN_TIMEOUT, help='仅 wechat：等待微信确认的秒数上限')
    login.add_argument('--qr-path', help='仅 wechat：把二维码页截图写到这个文件（默认在本机状态目录），调用结束即删除')
    daily = commands.add_parser('daily')
    daily.add_argument('--answer')
    daily.add_argument('--question')
    daily.add_argument('--resume', action='store_true')
    collect = commands.add_parser('collect-stripe')
    collect.add_argument('--limit', type=int, default=COLLECT_LIMIT)
    collect.add_argument('--list-pages', type=int, default=LIST_PAGES)
    collect.add_argument('--refresh', action='store_true')
    gather = commands.add_parser('collect')
    gather.add_argument('company')
    gather.add_argument('--query')
    gather.add_argument('--listing')
    gather.add_argument('--limit', type=int, default=COLLECT_LIMIT)
    gather.add_argument('--list-pages', type=int, default=LIST_PAGES)
    gather.add_argument('--max-thread-pages', type=int, default=THREAD_PAGES)
    gather.add_argument('--refresh', action='store_true')
    detail = commands.add_parser('thread-detail')
    detail.add_argument('thread')
    detail.add_argument('--max-thread-pages', type=int, default=THREAD_PAGES)
    outline = commands.add_parser('organize')
    outline.add_argument('thread')
    outline.add_argument('--refresh', action='store_true')
    keep = commands.add_parser('save-thread')
    keep.add_argument('thread')
    keep.add_argument('--company')
    keep.add_argument('--max-thread-pages', type=int, default=THREAD_PAGES)
    keep.add_argument('--refresh', action='store_true')
    site_search = commands.add_parser('site-search')
    site_search.add_argument('query')
    site_search.add_argument('--limit', type=int, default=SITE_SEARCH_LIMIT)
    site_search.add_argument('--list-pages', type=int, default=LIST_PAGES)
    mine = commands.add_parser('my-profile')
    mine.add_argument('--limit', type=int, default=BOARD_LIMIT)
    mine.add_argument('--list-pages', type=int, default=LIST_PAGES)
    member = commands.add_parser('user-profile')
    member.add_argument('user')
    member.add_argument('--limit', type=int, default=BOARD_LIMIT)
    member.add_argument('--list-pages', type=int, default=LIST_PAGES)
    board = commands.add_parser('browse-board')
    board.add_argument('board')
    board.add_argument('--limit', type=int, default=BOARD_LIMIT)
    board.add_argument('--list-pages', type=int, default=LIST_PAGES)
    search = commands.add_parser('search')
    search.add_argument('query', nargs='?', default='')
    search.add_argument('--limit', type=int, default=SEARCH_LIMIT)
    search.add_argument('--company', default=COLLECT_COMPANY, help='空字符串表示不按公司过滤')
    search.add_argument('--role', help='岗位精确匹配，例如 SWE')
    search.add_argument('--level', help='级别精确匹配，例如 New Grad')
    search.add_argument('--date-from', help='发帖日期下限（含），YYYY-MM-DD；按发帖日期而非采集日期')
    search.add_argument('--date-to', help='发帖日期上限（含），YYYY-MM-DD')
    search.add_argument('--include-ocr', action='store_true', help='同时搜索图片识别文本；这类命中标记为 matched_in=recognized_text')
    for name in ('favorite', 'unfavorite'):
        toggle = commands.add_parser(name)
        toggle.add_argument('thread')
        toggle.add_argument('--list-pages', type=int, default=LIST_PAGES, help='为确认当前收藏状态最多翻几页收藏夹')
    for name in ('like', 'unlike'):
        react = commands.add_parser(name)
        react.add_argument('thread')
        react.add_argument('--pid', type=int, help='楼层 pid；不填为主楼')
        react.add_argument('--reaction-id', type=int, default=LIKE_REACTION_ID, help='本站表情反应编号，默认 ❤')
    task_create = commands.add_parser('task-create', help='把一次公司采集保存为本地任务（参数当场冻结），不立即执行')
    task_create.add_argument('company')
    task_create.add_argument('--query')
    task_create.add_argument('--listing')
    task_create.add_argument('--limit', type=int, default=COLLECT_LIMIT)
    task_create.add_argument('--list-pages', type=int, default=LIST_PAGES)
    task_create.add_argument('--max-thread-pages', type=int, default=THREAD_PAGES)
    task_create.add_argument('--refresh', action='store_true')
    for name in ('task-run', 'task-status', 'task-pause', 'task-resume'):
        commands.add_parser(name).add_argument('task_id')
    task_list = commands.add_parser('task-list')
    task_list.add_argument('--limit', type=int, default=TASK_LIST_LIMIT)
    media = commands.add_parser('archive-media', help='把已入库帖子当前可见的图片/附件下载到本机归档目录，索引存库')
    media.add_argument('thread')
    media.add_argument('--limit', type=int, default=MEDIA_MAX_PER_THREAD, help='本次最多下载几个文件')
    ocr = commands.add_parser('recognize-media', help='对已归档的图片做本地文字识别，结果与原文分开存放')
    ocr.add_argument('thread')
    ocr.add_argument('--limit', type=int, default=OCR_MAX_IMAGES)
    post = commands.add_parser('post', help='发一篇文字帖子；不带 --submit 只预览，不写站点')
    post.add_argument('fid', type=int)
    post.add_argument('--subject', required=True)
    post.add_argument('--message', help='正文；或用 --message-file 从文件读')
    post.add_argument('--message-file')
    post.add_argument('--typeid', type=int, help='版面分类编号（预览会列出可选项）')
    post.add_argument('--sortid', type=int)
    post.add_argument('--image', action='append', default=[], help='本地图片路径，可多次；正文里用 [image:1] 指定位置，没指定的接在正文后')
    post.add_argument('--video', help='本地视频路径（mp4/mov/webm，单个），走站点原生视频上传')
    post.add_argument('--submit', action='store_true', help='真的发布；不加只预览')
    reply = commands.add_parser('reply', help='回复一个帖子；不带 --submit 只预览，不写站点')
    reply.add_argument('thread')
    reply.add_argument('--message')
    reply.add_argument('--message-file')
    reply.add_argument('--quote-pid', type=int, help='回复/引用该帖里的某一层')
    reply.add_argument('--submit', action='store_true')
    reply_notice = commands.add_parser('reply-notification', help='回复一条通知所指的那一层；不带 --submit 只预览')
    reply_notice.add_argument('notification_id', type=int)
    reply_notice.add_argument('--message')
    reply_notice.add_argument('--message-file')
    reply_notice.add_argument('--kind', choices=list(NOTIFICATION_KINDS), help='只在这个标签里找；不填三个标签都找')
    reply_notice.add_argument('--submit', action='store_true')
    for name in ('like-notification', 'unlike-notification'):
        react_notice = commands.add_parser(name, help='给一条通知所指的那一层加上/撤回反应（默认 ❤）')
        react_notice.add_argument('notification_id', type=int)
        react_notice.add_argument('--reaction-id', type=int, default=LIKE_REACTION_ID)
        react_notice.add_argument('--kind', choices=list(NOTIFICATION_KINDS))
    commands.add_parser('export')
    commands.add_parser('save-credentials')
    args = parser.parse_args()
    if args.command == 'save-credentials':
        from secure import save_credentials
        value = json.loads(sys.stdin.read())
        backend = save_credentials(value['username'], value['password'])
        value = None
        result = {'status': RunStatus.COMPLETE, 'credential_storage': backend}
    elif args.command == 'info':
        result = runtime_info()
    elif args.command == 'daily-history':
        result = get_daily_history(args.date, args.limit)
    elif args.command == 'session-status':
        result = session_status()
    elif args.command == 'session-logout':
        result = session_logout()
    elif args.command == 'unread':
        result = get_unread_counts()
    elif args.command == 'notifications':
        result = list_notifications(args.kind, args.limit, args.cursor)
    elif args.command == 'session-login':
        if args.method == 'wechat':
            print('二维码将显示在工具的 Chrome 窗口里（截图同时写到 --qr-path 或本机状态目录），请用微信扫码并在手机上确认；'
                  f'最多等待 {args.wait} 秒，Ctrl+C 取消。', file=sys.stderr)
        result = session_login(method=args.method, wait_seconds=args.wait, qr_path=args.qr_path)
    elif args.command in ['daily', 'status']:
        if getattr(args, 'resume', False):
            result = resume_daily(args.answer, args.question)
        else:
            result = run_daily(args.command == 'status', getattr(args, 'answer', None), getattr(args, 'question', None))
    elif args.command == 'collect-stripe':
        result = collect_stripe(args.limit, args.list_pages, refresh=args.refresh)
        result['export'] = export_library()
    elif args.command == 'collect':
        result = collect_company(args.company, query=args.query, listing=args.listing, limit=args.limit,
                                 list_pages=args.list_pages, max_thread_pages=args.max_thread_pages, refresh=args.refresh)
        result['export'] = export_library(company=args.company)
    elif args.command == 'thread-detail':
        result = get_thread_detail(args.thread, max_thread_pages=args.max_thread_pages)
    elif args.command == 'my-profile':
        result = get_my_profile(limit=args.limit, list_pages=args.list_pages)
    elif args.command == 'user-profile':
        result = get_user_profile(args.user, limit=args.limit, list_pages=args.list_pages)
    elif args.command == 'browse-board':
        result = browse_board(args.board, limit=args.limit, list_pages=args.list_pages)
    elif args.command == 'organize':
        result = organize_thread(args.thread, refresh=args.refresh)
    elif args.command == 'save-thread':
        result = save_thread(args.thread, company=args.company, max_thread_pages=args.max_thread_pages, refresh=args.refresh)
    elif args.command in ('favorite', 'unfavorite'):
        result = set_favorite(args.thread, args.command == 'favorite', list_pages=args.list_pages)
    elif args.command in ('like', 'unlike'):
        result = set_reaction(args.thread, args.command == 'like', pid=args.pid, reaction_id=args.reaction_id)
    elif args.command in ('like-notification', 'unlike-notification'):
        result = react_to_notification(args.notification_id, args.command == 'like-notification',
                                       reaction_id=args.reaction_id, kind=args.kind)
    elif args.command == 'reply-notification':
        text = Path(args.message_file).read_text(encoding='utf-8') if args.message_file else args.message
        result = reply_to_notification(args.notification_id, text, submit=args.submit, kind=args.kind)
    elif args.command == 'task-create':
        result = create_collection_task(args.company, query=args.query, listing=args.listing, limit=args.limit,
                                        list_pages=args.list_pages, max_thread_pages=args.max_thread_pages, refresh=args.refresh)
    elif args.command == 'task-run':
        result = run_task(args.task_id)
    elif args.command == 'task-status':
        result = task_status(args.task_id)
    elif args.command in ('task-pause', 'task-resume'):
        result = control_task(args.task_id, args.command.split('-')[1])
    elif args.command == 'task-list':
        result = list_tasks(args.limit)
    elif args.command == 'archive-media':
        result = archive_media(args.thread, limit=args.limit)
    elif args.command == 'recognize-media':
        result = recognize_media(args.thread, limit=args.limit)
    elif args.command in ('post', 'reply'):
        # The text comes from the user's own file or argument, never from anything read off the site.
        text = Path(args.message_file).read_text(encoding='utf-8') if args.message_file else args.message
        if args.command == 'post':
            result = create_thread(args.fid, args.subject, text, typeid=args.typeid, sortid=args.sortid, submit=args.submit,
                                   images=args.image, video=args.video)
        else:
            result = reply_thread(args.thread, text, quote_pid=args.quote_pid, submit=args.submit)
    elif args.command == 'site-search':
        result = search_threads(args.query, limit=args.limit, list_pages=args.list_pages)
    elif args.command == 'search':
        db = Library()
        try:
            result = db.find(args.query, company=args.company, limit=args.limit, role=args.role, level=args.level,
                             date_from=args.date_from, date_to=args.date_to, include_ocr=args.include_ocr)
        finally:
            db.close()
    else:
        result = export_library()
    print(json.dumps(result, ensure_ascii=True))
    if is_failure(result):
        return 2
    if getattr(args, 'fail_on_alert', False) and health_alerting(result.get('health')):
        return 3
    return 0


if __name__ == '__main__':
    try:
        from governance import ensure_consistent
        ensure_consistent()
        raise SystemExit(main())
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        print(json.dumps({'status': RunStatus.FAILED, 'error_type': type(error).__name__, 'error': format_error(error)}))
        raise SystemExit(2)
