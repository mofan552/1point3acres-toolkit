"""Shared serialized states and business semantics. Consumers never invent aliases."""
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum


class RunStatus(StrEnum):
    COMPLETE = 'complete'
    NEEDS_ATTENTION = 'needs_attention'
    FAILED = 'failed'


class SessionState(StrEnum):
    NOT_CONFIGURED = 'not_configured'
    LOGGED_OUT = 'logged_out'
    WRONG_ACCOUNT = 'wrong_account'
    CHALLENGE = 'challenge'
    LOGGED_IN = 'logged_in'
    UNAVAILABLE = 'unavailable'


class AccountRequestError(RuntimeError):
    """Remote error text has no authority to set a local diagnostic state."""


class BrowserConnectionError(RuntimeError):
    """The link to the owned Chrome is gone or the session's deadline passed: nothing more can be sent.

    Named 'browser_connection_lost' or 'daily_run_timeout'. Callers that swallow ordinary page errors while
    polling must let this one through, or a dead link would only surface as a page timeout much later."""


def session_result(error=None):
    """One diagnostic contract; arbitrary server errors never enter public output."""
    reason = str(error) if isinstance(error, RuntimeError) else None
    if isinstance(error, AccountRequestError):
        reason = 'account_request_rejected'
    known = {'account_not_configured': SessionState.NOT_CONFIGURED,
             'login_required': SessionState.LOGGED_OUT,
             'unexpected_account': SessionState.WRONG_ACCOUNT,
             'api_challenge_not_resolved': SessionState.CHALLENGE,
             'page_challenge_not_resolved': SessionState.CHALLENGE,
             'login_challenge_not_resolved': SessionState.CHALLENGE,
             'login_rejected': SessionState.LOGGED_OUT,
             'login_required_credentials_not_configured': SessionState.LOGGED_OUT,
             'saved_credentials_invalid': SessionState.LOGGED_OUT,
             'wechat_login_timeout': SessionState.LOGGED_OUT,
             'wechat_login_cancelled': SessionState.LOGGED_OUT}
    state = SessionState.LOGGED_IN if error is None else known.get(reason, SessionState.UNAVAILABLE)
    if state == SessionState.UNAVAILABLE and reason not in {
            'another_task_is_using_the_browser', 'chrome_profile_busy_or_start_failed',
            'automatic_page_or_verification_timeout', 'api_returned_non_json',
            'page_expression_failed', 'account_http_error', 'account_request_rejected',
            'account_response_invalid', 'unsupported_login_method', 'login_submission_unconfirmed',
            'windows_credential_encryption_failed', 'automatic_login_failed', 'login_fields_not_accepted',
            'network_timeout', 'network_unavailable', 'browser_read_failed',
            'browser_connection_lost', 'daily_run_timeout',
            'invalid_wait_seconds', 'wechat_qr_not_shown', 'wechat_display_failed'}:
        reason = 'session_status_unavailable'
    usable = state == SessionState.LOGGED_IN
    return {'configured': state != SessionState.NOT_CONFIGURED, 'session_state': state,
            'identity_matches': True if usable else False if state == SessionState.WRONG_ACCOUNT else None,
            'session_usable': usable, 'error': reason,
            'status': RunStatus.COMPLETE if usable else RunStatus.FAILED if state == SessionState.UNAVAILABLE
                      else RunStatus.NEEDS_ATTENTION}


def login_result(error=None, *, configured, login_attempted, wechat=None):
    """`wechat` is the scan-login report (where the QR was shown, how long was waited, what was cleaned up);
    None for the password path and for a wechat call that stopped before the page was reached."""
    result = session_result(error)
    result.update(configured=configured, login_attempted=login_attempted, wechat=wechat)
    return result


def logout_result(error=None, *, cookies_removed, verified, scope):
    """A session reset is complete only when the site itself no longer recognises the account afterwards.

    Nothing here ever restores a login: the verification is a plain identity read that is expected to be
    refused. A reset that could not be verified, or one refused because another task holds the browser,
    is reported as such with what was and was not removed.
    """
    reason = format_error(error) if error is not None else None
    if reason is None and not verified:
        reason = 'logout_unverified'
    if reason is None:
        status, state = RunStatus.COMPLETE, SessionState.LOGGED_OUT
    elif reason in {'another_task_is_using_the_browser', 'chrome_profile_busy_or_start_failed'}:
        status, state = RunStatus.FAILED, SessionState.UNAVAILABLE
    else:
        status, state = RunStatus.NEEDS_ATTENTION, SessionState.UNAVAILABLE
    return {'status': status, 'session_state': state, 'session_usable': False, 'login_restored': False,
            'cookies_removed': cookies_removed, 'verified': bool(verified), 'scope': scope, 'error': reason}


def recovery_summary(read_retries, submission_checks):
    return {'read_retries': read_retries, 'submission_checks': submission_checks}


class ActionStatus(StrEnum):
    ALREADY_DONE = 'already_done'
    NOT_DONE = 'not_done'
    ANSWER_NEEDED = 'answer_needed'
    REWARD_VERIFIED = 'reward_verified'
    REWARD_UNCONFIRMED = 'reward_unconfirmed'
    SUBMISSION_UNCONFIRMED = 'submission_unconfirmed'


class ResumeDecision(StrEnum):
    NOT_DUE = 'not_due'
    ALREADY_COMPLETE = 'already_complete'
    EXECUTED = 'executed'


class HealthVerdict(StrEnum):
    OK = 'ok'
    WARN = 'warn'
    ALERT = 'alert'


class HealthReason(StrEnum):
    """Why a verdict is not ok. Schedulers act on these, never on prose."""
    INCOMPLETE_DAYS = 'incomplete_days'
    RUNS_STALE = 'runs_stale'
    NO_HISTORY = 'no_history'


DAILY_RETRY_ERRORS = frozenset({'network_timeout', 'network_unavailable',
    'automatic_page_or_verification_timeout', 'page_challenge_not_resolved',
    'api_challenge_not_resolved', 'login_challenge_not_resolved',
    'browser_connection_lost', 'daily_run_timeout'})


class ContentStatus(StrEnum):
    VISIBLE = 'visible'
    RESTRICTED = 'restricted'


class CollectionStatus(StrEnum):
    PENDING = 'pending'
    COMPLETE = 'complete'
    RESTRICTED = 'restricted'
    VISIBLE = 'visible'
    PARTIAL_PAGES = 'partial_pages'
    CACHED = 'cached'
    FAILED = 'failed'


@dataclass(frozen=True)
class Action:
    key: str
    flag: str
    route: str
    method: str
    reward_title: str


ACTIONS = (Action('checkin', 'checkin', 'daily-checkin', 'reward.checkin', '签到奖励'),
           Action('quiz', 'question', 'daily-question', 'dailyQuestion.answer', '每日答题'))
REWARD_TITLES = frozenset(action.reward_title for action in ACTIONS)


def daily_history_record(result):
    """Project trusted daily observations into an identity/receipt-free history record."""
    entries = {row['action']: row for row in result.get('actions', [])}
    actions = []
    for spec in ACTIONS:
        entry = entries.get(spec.key, {})
        status = ActionStatus(entry['status']) if entry.get('status') else None
        observations = [entry.get('completed')]
        if status in {ActionStatus.ALREADY_DONE, ActionStatus.REWARD_VERIFIED}:
            observations.append(True)
        elif status in {ActionStatus.NOT_DONE, ActionStatus.ANSWER_NEEDED}:
            observations.append(False)
        for account in [result.get('before', {}), result.get('after', {})]:
            if spec.flag in account.get('app_status', {}):
                observations.append(bool(account['app_status'][spec.flag]))
        completed = True if True in observations else False if False in observations else None
        actions.append({'action': spec.key, 'status': status, 'completed': completed,
                        'response_seen': entry.get('response_seen'),
                        # What the check-in said in the member's name, kept so later days can avoid repeats.
                        'mood': entry.get('mood'), 'phrase': entry.get('phrase'),
                        'reward_verified': status == ActionStatus.REWARD_VERIFIED
                            or result.get('reward_verified', {}).get(spec.key) is True})
    error = result.get('error')
    if error and error not in {'question_changed_or_not_confirmed', 'checkin_submission_unconfirmed',
                              'quiz_submission_unconfirmed', 'site_day_changed', 'api_http_error',
                              'daily_history_conflict', 'daily_history_unavailable', 'button_not_ready',
                              'daily_clock_changed'}:
        error = session_result(RuntimeError(error))['error']
        if error == 'session_status_unavailable':
            error = 'daily_run_failed'
    return {'run_id': result['run_id'], 'started_at': result['started_at'],
            'finished_at': result['finished_at'], 'site_day': result['site_day'],
            'status_only': result.get('status_only') if type(result.get('status_only')) is bool else None,
            'status': RunStatus(result['status']),
            'actions': actions, 'error': error,
            'recovery': recovery_summary(**result.get('recovery', {'read_retries': 0, 'submission_checks': 0}))}


def day_complete(day):
    """The single definition of a finished site day: both actions done and paid for."""
    return bool(day) and all(action['completed'] is True and action['reward_verified']
                             for action in day['actions'])


def summarize_daily_history(records):
    """Keep positive evidence across attempts; repeated receipts are never summed.

    Submission attempts and whether the site ever answered one are counted so a later run can tell a
    request that was lost in flight from one the site already processed.
    """
    days = {}
    for record in records:
        day = days.setdefault(record['site_day'], {'site_day': record['site_day'], 'run_count': 0,
            'actions': [{'action': spec.key, 'completed': None, 'reward_verified': False,
                         'completion_run_id': None, 'reward_run_id': None,
                         'submission_attempts': 0, 'response_ever_seen': False,
                         'unconfirmed_submission_run_id': None} for spec in ACTIONS]})
        day['run_count'] += 1
        for current, observed in zip(day['actions'], record['actions']):
            if observed.get('response_seen') is not None:
                current['submission_attempts'] += 1
                current['response_ever_seen'] = current['response_ever_seen'] or observed['response_seen']
            if observed['status'] == ActionStatus.SUBMISSION_UNCONFIRMED:
                current['unconfirmed_submission_run_id'] = current['unconfirmed_submission_run_id'] or record['run_id']
            if observed['completed'] is True:
                current['completed'] = True
                current['completion_run_id'] = current['completion_run_id'] or record['run_id']
            elif observed['completed'] is False and current['completed'] is None:
                current['completed'] = False
            if observed['reward_verified']:
                current['reward_verified'] = True
                current['reward_run_id'] = current['reward_run_id'] or record['run_id']
    for day in days.values():
        for action in day['actions']:
            if action['completed'] is True or action['reward_verified']:
                action['unconfirmed_submission_run_id'] = None
    return [days[key] for key in sorted(days, reverse=True)]


UNREAD_COUNTERS = (('prompt', 'newprompt'), ('pm', 'newpm'), ('chat', 'chat_unread_total'))


def notification_action_result(action, *, notification, target, outcome, error):
    """A notification-bound action (#35, #36): which notification, which post it resolved to, and the
    ordinary reply / reaction result under `result`. The outer status is that result's status; a stop
    before the action (not found, no post, invalid input) is failed with the notification when known."""
    status = RunStatus(outcome['status']) if isinstance(outcome, dict) and 'status' in outcome else RunStatus.FAILED
    return {'status': status, 'action': action, 'notification': notification, 'target': target,
            'result': outcome, 'error': error if error is not None else (outcome or {}).get('error')}


def notification_item(raw, category):
    """One notification as the site's list returns it, reduced to stable fields (#34).

    Anything the site did not clearly send is None, never guessed: a target without a thread id is
    reported missing rather than mapped elsewhere, and `content` is display text from another member,
    carried as data only."""
    if not isinstance(raw, dict) or type(raw.get('id')) is not int:
        raise ValueError('invalid_notification_item')
    actor = raw.get('actor') if isinstance(raw.get('actor'), dict) else None
    target = raw.get('target') if isinstance(raw.get('target'), dict) else {}
    tid = target.get('tid') if type(target.get('tid')) is int and target['tid'] > 0 else None
    pid = target.get('pid') if type(target.get('pid')) is int and target['pid'] > 0 else None
    dateline = raw.get('dateline')
    return {'id': raw['id'], 'category': category,
            'kind': raw['action'] if isinstance(raw.get('action'), str) else None,
            'new': bool(raw['new']) if type(raw.get('new')) is int else None,
            'at': datetime.fromtimestamp(dateline, timezone.utc).isoformat() if type(dateline) is int else None,
            'actor': None if actor is None else {
                'uid': actor['uid'] if type(actor.get('uid')) is int else None,
                'name': actor['name'] if isinstance(actor.get('name'), str) else None},
            'target': {'tid': tid, 'pid': pid,
                       'subject': target['subject'] if isinstance(target.get('subject'), str) else None},
            'target_level': 'post' if pid else 'thread' if tid else None,
            'target_missing': None if tid else 'target_unavailable',
            'content': raw['content'] if isinstance(raw.get('content'), str) else None}


def unread_summary(user, read_at):
    """Only what the site actually sent: an absent or non-integer counter is unknown, never zero."""
    counts, missing = {}, []
    for name, field in UNREAD_COUNTERS:
        value = user.get(field) if isinstance(user, dict) else None
        if type(value) is int and value >= 0:
            counts[name] = value
        else:
            counts[name] = None
            missing.append(name)
    return {'counts': counts, 'missing': missing, 'read_at': read_at, 'source': 'user.me'}


class Attribution(StrEnum):
    AUTHOR = 'author'   # said by the thread's own poster
    REPLY = 'reply'     # said by someone else; never the author's confirmed experience


class Certainty(StrEnum):
    STATED = 'stated'
    SPECULATED = 'speculated'


# \b is useless next to CJK text (those characters count as word characters), so English tokens are
# bounded by "not another Latin letter" instead: "Stripe OA真的是" must still match OA.
_L = r'(?<![A-Za-z])'
_R = r'(?![A-Za-z])'
ROUND_PATTERNS = (
    ('OA', _L + r'OA' + _R + r'|online assessment|笔试'),
    ('电面', r'电面|店面|滇缅|phone\s*screen|phone interview|tech(?:nical)? screen'),
    ('Onsite', _L + r'(?:onsite|VO)' + _R + r'|virtual onsite|现场面|终面|final round'),
    ('Coding', _L + r'coding' + _R + r'|代码题|编程题|算法题'),
    ('Debug', _L + r'debug' + _R + r'|bug squash'),
    ('Integration', _L + r'integration' + _R + r'|集成'),
    ('系统设计', r'system design|系统设计|' + _L + r'SD' + _R),
    ('BQ', _L + r'BQ' + _R + r'|behavioral|行为面'),
    ('HM', r'hiring manager|' + _L + r'HM' + _R + r'|经理面'),
)
QUESTION_CUES = r'题目|这题|问题|problem|question|给定|要求|输入|输出|设计一个|实现一个|design a|implement|write a|given a|return'
SPECULATION_CUES = r'感觉|可能|大概|应该是|不知道|猜|估计|听说|好像|不确定|我觉得|我以为|个人认为|maybe|probably|not sure|i think|i guess|i heard'
NOISE_CUES = r'本帖最后由|求加米|求米|加米|求大米|谢谢大家|楼主加油'
RESTRICTED_MARKER = '[内容受限，当前账户未获得该部分正文]'


def is_failure(payload):
    if not isinstance(payload, dict) or 'status' not in payload:
        return False
    return RunStatus(payload['status']) != RunStatus.COMPLETE


UNLABELLED = '未标注'


def normalize_post_date(value):
    """'2026-9-9', '2026-9-22 00:32' or '2026-9-22 13:21:15' become '2026-09-09'; anything else is None."""
    match = re.match(r'\s*(\d{4})-(\d{1,2})-(\d{1,2})', str(value or ''))
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None


def record_post_date(record):
    """The posting date, never the collection time: the thread page's own publish time first, then the
    date the listing showed. A record with neither has no date, and no date never satisfies a bound."""
    stats = record.get('stats') or {}
    return normalize_post_date(stats.get('published_at')) or normalize_post_date(record.get('listed_date'))


def record_search_text(record):
    """The one text the keyword search runs over, in the library column and in the reader alike."""
    return '\n'.join([record.get('title') or '', ' '.join(record.get('tags') or []), record.get('role') or '',
                      record.get('level') or ''] + [post.get('text') or '' for post in record.get('posts') or []])


def record_matches(record, *, role=None, level=None, date_from=None, date_to=None):
    """One rule for every entry point: exact role and level (未标注 is a real value), inclusive bounds on
    the posting date, and an undated record excluded as soon as any date bound is set."""
    if role and (record.get('role') or UNLABELLED) != role:
        return False
    if level and (record.get('level') or UNLABELLED) != level:
        return False
    if date_from or date_to:
        posted = record_post_date(record)
        if posted is None or (date_from and posted < date_from) or (date_to and posted > date_to):
            return False
    return True


def validate_date_bound(value, name):
    if value is None or value == '':
        return None
    if not isinstance(value, str) or normalize_post_date(value) != value:
        raise ValueError('invalid_' + name)
    return value


def health_alerting(health):
    """One place decides what a watcher must act on, so no caller compares verdict strings itself."""
    if not isinstance(health, dict) or 'verdict' not in health:
        return False
    return HealthVerdict(health['verdict']) == HealthVerdict.ALERT


def record_state(record):
    if not record['pagination_complete']:
        return CollectionStatus.PARTIAL_PAGES
    if record['content_status'] == ContentStatus.RESTRICTED:
        return CollectionStatus.RESTRICTED
    return CollectionStatus.COMPLETE if record['complete'] else CollectionStatus.VISIBLE


def collection_status(results):
    states = [CollectionStatus(row['status']) for row in results]
    if states and all(state == CollectionStatus.FAILED for state in states):
        return RunStatus.FAILED
    if not states or any(state in (CollectionStatus.FAILED, CollectionStatus.PARTIAL_PAGES) for state in states):
        return RunStatus.NEEDS_ATTENTION
    return RunStatus.COMPLETE


def format_error(error):
    return str(error)[:180] if isinstance(error, (RuntimeError, ValueError)) else type(error).__name__


def validate_record(record):
    """Validate the durable record boundary before a database write."""
    if type(record.get('tid')) is not int or record['tid'] <= 0:
        raise ValueError('invalid_thread_id')
    if any(type(record.get(field)) is not bool for field in ('complete', 'pagination_complete')):
        raise ValueError('invalid_completeness_type')
    state = ContentStatus(record['content_status'])
    posts = record.get('posts')
    if not isinstance(posts, list) or not posts:
        raise ValueError('missing_posts')
    if any(type(post.get('pid')) is not int or post['pid'] <= 0 or not isinstance(post.get('text'), str)
           or type(post.get('restricted')) is not bool for post in posts):
        raise ValueError('invalid_post_record')
    if len({post['pid'] for post in posts}) != len(posts):
        raise ValueError('duplicate_post_ids')
    if (state == ContentStatus.RESTRICTED) != any(post['restricted'] for post in posts):
        raise ValueError('inconsistent_permission_state')
    if record['pagination_complete'] and (type(record.get('expected_posts')) is not int or len(posts) < record['expected_posts']):
        raise ValueError('inconsistent_pagination_state')
    if record['complete'] != (record['pagination_complete'] and state == ContentStatus.VISIBLE):
        raise ValueError('inconsistent_complete_state')


def target_state_result(action, tid, desired, *, before=None, after=None, submitted=False, error=None, **detail):
    """One shape for every target-state operation on the site (favorite, later like and others).

    The read-back state decides: a submission the site accepted but the read-back cannot confirm is
    attention, never success; a read-back that disagrees with the goal is a failure even when the site
    said ok. Nothing submitted and the state already at the goal is a complete no-op with changed=False.
    """
    if error is None and submitted and after is None:
        error = 'result_unverified'
    if error is None and after is not None and after != desired:
        error = 'target_state_not_reached'
    if error is None:
        status = RunStatus.COMPLETE
    elif submitted and after is None:
        status = RunStatus.NEEDS_ATTENTION
    else:
        status = RunStatus.FAILED
    changed = None if before is None or after is None else before != after
    return {'status': status, 'action': action, 'tid': tid, 'desired_state': desired, 'before': before,
            'after': after, 'changed': changed, 'submitted': submitted, **detail, 'error': error}


class MediaOutcome(StrEnum):
    """What happened to one media reference of a saved thread when archiving; the record is untouched."""
    ARCHIVED = 'archived'
    REUSED = 'reused'
    SKIPPED_RESTRICTED = 'skipped_restricted'
    SKIPPED_EXTERNAL = 'skipped_external'
    SKIPPED_LIMIT = 'skipped_limit'
    FAILED = 'failed'


class TaskState(StrEnum):
    """Where a persisted collection task is in its life; business outcomes keep their own statuses."""
    QUEUED = 'queued'
    RUNNING = 'running'
    PAUSED = 'paused'
    COMPLETE = 'complete'
    FAILED = 'failed'


class TaskControl(StrEnum):
    """What the owner asked of a task; the executor honours it at the next safe point."""
    RUN = 'run'
    PAUSE = 'pause'


class PauseRequested(Exception):
    """Raised inside an executor at a safe point once the owner asked for a pause."""


TASK_ACTIVE_STATES = (TaskState.QUEUED, TaskState.RUNNING, TaskState.PAUSED)


def task_view(task, now):
    """A task as a client should see it: its frozen parameters, progress, and whether anyone is really
    working on it. A running task whose executor stopped sending heartbeats is reported as abandoned,
    never as still progressing."""
    state = TaskState(task['state'])
    heartbeat = task.get('heartbeat_at')
    age = None
    if heartbeat:
        age = round((now - datetime.fromisoformat(heartbeat)).total_seconds(), 1)
    stale = age is None or age > task['stale_after']
    if state == TaskState.RUNNING and stale:
        diagnosis = 'executor_missing'
    elif state == TaskState.RUNNING:
        diagnosis = 'executing'
    elif state == TaskState.QUEUED:
        diagnosis = 'waiting_for_executor'
    elif state == TaskState.PAUSED:
        diagnosis = 'paused_at_checkpoint'
    else:
        diagnosis = str(state)
    return {'task_id': task['task_id'], 'kind': task['kind'], 'state': state, 'control': TaskControl(task['control']),
            'diagnosis': diagnosis, 'params': task['params'], 'progress': task['progress'], 'result': task.get('result'),
            'created_at': task['created_at'], 'updated_at': task['updated_at'], 'executor': task.get('worker'),
            'heartbeat_age_seconds': age, 'executor_alive': state == TaskState.RUNNING and not stale, 'error': task.get('error')}


def publish_result(action, *, submitted, preview, tid=None, pid=None, url=None, confirmed=None,
                   pending_review=False, rejected=False, site_message=None, recovered=False, error=None):
    """One shape for everything that creates content on the site (a thread, a reply).

    A preview is complete without touching the site. Once something was submitted, only content read
    back from the site confirms it: an accepted submission the read-back cannot confirm, or one the site
    holds for review, is attention for a human, never success and never a reason to submit again. A
    submission the site rejected created nothing and is a plain failure.
    """
    if error is None and submitted and pending_review:
        error = 'pending_review'
    if error is None and submitted and not confirmed:
        error = 'result_unverified'
    if error is None:
        status = RunStatus.COMPLETE
    elif submitted and not rejected:
        status = RunStatus.NEEDS_ATTENTION
    else:
        status = RunStatus.FAILED
    return {'status': status, 'action': action, 'submitted': submitted, 'confirmed': bool(confirmed),
            'pending_review': pending_review, 'tid': tid, 'pid': pid, 'url': url, 'preview': preview,
            'site_message': site_message, 'recovered': recovered, 'error': error}
