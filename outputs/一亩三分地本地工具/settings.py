"""Single owner of deployment, site identity, limits and artifact names; no secrets."""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent.parent
STATE = WORKSPACE / 'work' / 'local-toolkit-state'
DATABASE_NAME = 'interviews.sqlite'
PROFILE = WORKSPACE / 'work' / 'account-browser' / 'chrome-profile'
WINDOWS = sys.platform == 'win32'
MACOS = sys.platform == 'darwin'
PYTHON = WORKSPACE / 'work' / 'cf-probe-venv' / ('Scripts/python.exe' if WINDOWS else 'bin/python')
CHROME = (Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe') if WINDOWS
          else Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'))
CHROME_BUNDLE_ID = 'com.google.Chrome'
CREDENTIAL_SERVICE = '1point3acres-toolkit'
ACCOUNT_FILE = STATE / 'account.json'
LEARNED_ANSWERS_NAME = 'learned-answers.json'
SCHEDULE_KEYS = {'schedule_time', 'schedule_timezone', 'schedule_mode'}
# Controls public check-in phrases; absent uses mood_random_enabled's default.
MOOD_RANDOM_KEY = 'checkin_mood_random'
OPTIONAL_KEYS = SCHEDULE_KEYS | {MOOD_RANDOM_KEY}


def load_identity(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return '', 0, {}
    except (OSError, ValueError):
        raise RuntimeError('invalid_local_account_config') from None
    if (not isinstance(value, dict) or not {'username', 'uid'} <= set(value)
            or not set(value) <= {'username', 'uid'} | OPTIONAL_KEYS
            or not isinstance(value['username'], str) or not value['username'].strip()
            or value['username'] != value['username'].strip()
            or type(value['uid']) is not int or value['uid'] <= 0
            or (MOOD_RANDOM_KEY in value and type(value[MOOD_RANDOM_KEY]) is not bool)):
        raise RuntimeError('invalid_local_account_config')
    return value['username'], value['uid'], {key: value[key] for key in OPTIONAL_KEYS if key in value}


def load_schedule(overrides, default_time, default_zone):
    """Machine-local schedule lives beside the identity, never in tracked source (see issue #65)."""
    moment = overrides.get('schedule_time', default_time)
    zone = overrides.get('schedule_timezone', default_zone)
    if not isinstance(moment, str) or not re.fullmatch(r'([01][0-9]|2[0-3]):[0-5][0-9]', moment):
        raise RuntimeError('invalid_local_schedule_config')
    try:
        ZoneInfo(zone)
    except Exception:
        raise RuntimeError('invalid_local_schedule_config') from None
    return moment, zone


USERNAME, ACCOUNT_UID, _SCHEDULE = load_identity(ACCOUNT_FILE)


def config_matches_disk():
    try:
        return load_identity(ACCOUNT_FILE) == (USERNAME, ACCOUNT_UID, _SCHEDULE)
    except RuntimeError:
        return False


SITE = 'https://www.1point3acres.com'
SITE_HOST = 'www.1point3acres.com'
AUTH_HOST = 'auth.1point3acres.com'
RPC_HOST = 'trpc.1point3acres.com'
API_HOST = 'api.1point3acres.com'
# A session reset touches only the site's own cookie domains inside the dedicated profile.
SESSION_COOKIE_DOMAINS = ('1point3acres.com', '1p3a.com')
AUTH_URL = 'https://' + AUTH_HOST
RPC_URL = 'https://' + RPC_HOST
API_URL = 'https://' + API_HOST
# The site's own emoji reactions are its only zero-cost, revocable "like"; ❤ is the default one.
LIKE_REACTION_ID = 56
# Discuz keeps 80 characters of a subject; the message bound is this tool's own guard, not the site's.
SUBJECT_MAX_LENGTH = 80
MESSAGE_MAX_LENGTH = 20000
# Images a thread may carry: the site's own uploader takes these types; the size and count bounds are
# this tool's guard (the site did not publish its limits; it rejects with a message when exceeded).
UPLOADER_URL = 'https://uploader.1p3a.com/'
UPLOAD_TIMEOUT_MS = 120000
IMAGE_UPLOAD_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp'}
IMAGE_UPLOAD_MAX_BYTES = 8 * 1024 * 1024
IMAGE_UPLOAD_MAX_COUNT = 9
IMAGE_PLACEHOLDER = r'\[image:(\d+)\]'
# One native video per thread through the site's own upload flow; the size bound is this tool's guard
# (bytes travel through the page as a Blob), the site's own limits are reported by its messages.
VIDEO_UPLOAD_TYPES = {'.mp4': 'video/mp4', '.mov': 'video/quicktime', '.webm': 'video/webm'}
VIDEO_UPLOAD_MAX_BYTES = 50 * 1024 * 1024
# Notification tabs as the site's own notifications page names them (#34); the list is read through
# the same tRPC the page uses. The page bound is this tool's guard against a cursor that never ends.
NOTIFICATION_KINDS = ('post', 'appreciation', 'others')
NOTIFICATION_LIMIT = 20
NOTIFICATION_MAX = 100
NOTIFICATION_PAGE_LIMIT = 10
LOGIN_METHOD = 'password'
LOGIN_VERIFICATION_TIMEOUT = 40
# WeChat scan login (#51): the site's own page embeds WeChat Open Platform's official QR iframe. The wait is
# bounded here because the site publishes no QR lifetime; the screenshot goes to the local state directory
# unless the caller names a file, and is removed when the call ends either way.
LOGIN_METHODS = (LOGIN_METHOD, 'wechat')
WECHAT_QR_PATH = '/wechat-qr'
WECHAT_QR_FRAME_PREFIX = 'https://open.weixin.qq.com/connect/qrconnect'
WECHAT_QR_FILE = STATE / 'wechat-qr.png'
WECHAT_LOGIN_TIMEOUT = 180
WECHAT_LOGIN_MIN_WAIT = 10
WECHAT_LOGIN_MAX_WAIT = 600
# Where the owned Chrome window is put while a person has to see it; it lives off-screen otherwise.
WINDOW_ON_SCREEN = (120, 80)
SITE_TIMEZONE = 'America/Los_Angeles'
# Defaults only: a machine overrides these in account.json instead of editing tracked source (#65).
SCHEDULE_TIME, SCHEDULE_TIMEZONE = load_schedule(_SCHEDULE, '16:10', 'Asia/Shanghai')
def load_schedule_mode(overrides):
    mode = overrides.get('schedule_mode', 'fixed' if 'schedule_time' in overrides else 'random')
    if mode not in ('fixed', 'random'):
        raise RuntimeError('invalid_local_schedule_config')
    return mode


SCHEDULE_MODE = load_schedule_mode(_SCHEDULE)
# Random plans always follow the site clock, including DST; fixed legacy overrides retain their clock.
SCHEDULE_WINDOW_START = 10
SCHEDULE_WINDOW_END = 12
SCHEDULE_CURVE = (3, 3)
SCHEDULE_RECOVERY_HOURS = 4
SCHEDULE_POLL_SECONDS = 60
HEALTH_ALERT_DAYS = 2
HEALTH_STALE_RUNS = 2
# The ten the site actually offers, read off the check-in page rather than copied from a description.
# Weights are relative, not measured frequencies: no dataset of real members' moods exists, so this is a
# deliberate judgement that mundane days dominate and strong reactions are rare. Tune in one place.
CHECKIN_MOOD_WEIGHTS = {'开心': 17, '疲惫': 15, '无聊': 14, '慵懒': 13, '奋斗': 12,
                        '没心情': 11, '郁闷': 9, '难过': 5, '衰': 3, '生气': 1}
CHECKIN_MOOD_PERSISTENCE = 0.25
CHECKIN_MOOD_DEFAULT = '没心情'


def mood_random_enabled(overrides):
    """On unless account.json says "checkin_mood_random": false (issue #15). A mood with a phrase is a
    public diary entry in the member's name, so the switch stays explicit and must be a real boolean."""
    return bool(overrides.get(MOOD_RANDOM_KEY, True))


CHECKIN_MOOD_RANDOM = mood_random_enabled(_SCHEDULE)
MOOD_PHRASES_FILE = ROOT / 'mood-phrases.json'
MOOD_PHRASE_MAX_LENGTH = 60
MOOD_PHRASE_RECENT_DAYS = 30
DAILY_RETRY_LIMIT = 1
DAILY_RECOVERY_MINUTES = (5, 60)
QUIZ_GAP_SECONDS = (30, 70)
# One whole daily run in one Chrome. Healthy runs take minutes; past this the run is cut off with
# daily_run_timeout, its history is still saved, and resume_daily starts a fresh Chrome once.
DAILY_RUN_TIMEOUT = 900
COLLECT_COMPANY = 'Stripe'
COLLECT_TAG = '/bbs/tag/stripe-2126-1.html'
COLLECT_LIMIT = 12
COLLECT_MAX = 100
LIST_PAGES = 3
LIST_PAGES_MAX = 10
THREAD_PAGES = 5
THREAD_PAGES_MAX = 20
SEARCH_LIMIT = 30
SEARCH_MAX = 5000
# Persisted collection tasks: an executor that has not reported for this long is treated as gone.
TASK_HEARTBEAT_STALE_SECONDS = 300
TASK_LIST_LIMIT = 20
HISTORY_LIMIT = 30
HISTORY_MAX = 200
SITE_SEARCH_PATH = '/bbs/search.php'
SITE_SEARCH_ENCODING = 'gbk'
SITE_SEARCH_LIMIT = 30
SITE_SEARCH_MAX = 100
SITE_SEARCH_QUERY_MAX = 200
BOARD_LIMIT = 30
BOARD_MAX = 100
PAGE_TIMEOUT = 30
REQUEST_TIMEOUT_MS = 20000
READ_RETRY_LIMIT = 1
READ_RETRY_DELAY = 1
SUBMISSION_TIMEOUT = 45
# One CDP round trip. It must exceed the longest in-page abort (UPLOAD_TIMEOUT_MS): a healthy long call is ended
# by the page's own timer, so this bound only catches a link that died (sleep, Chrome gone) mid-command.
CDP_CALL_TIMEOUT = 150
# Each step of closing the owned Chrome: the graceful close, then the waits after terminate and after kill.
BROWSER_SHUTDOWN_TIMEOUT = 5
EXPORT_DIRECTORY = ROOT.parent / 'Stripe面经资料'
EXPORT_FILES = {'json': '面经.json', 'csv': '面经.csv', 'markdown': '面经.md', 'reader': '打开阅读器.html'}
# Media archived from saved threads: only the site's own hosts, bounded per file and per thread, kept under one directory.
MEDIA_DIRECTORY = STATE / 'media'
MEDIA_HOSTS = (SITE_HOST, 'oss.1p3a.com', 'assets.1p3a.com')  # the site's own attachment/image hosts, as real pages use them
MEDIA_MAX_BYTES = 10 * 1024 * 1024
MEDIA_MAX_PER_THREAD = 30
MEDIA_EXTENSIONS = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp', 'application/pdf': 'pdf',
                    'application/zip': 'zip', 'text/plain': 'txt'}
EXPORT_MEDIA_DIRNAME = '媒体'
# Text recognised from archived images: a separate layer, never the author's words; the engine is optional at runtime.
OCR_ENGINE_NAME = 'rapidocr-onnxruntime'
OCR_MAX_IMAGES = 30
OCR_MIN_SCORE = 0.5
MCP_NAME = '1point3acres-local'


def mcp_config():
    return {'mcpServers': {MCP_NAME: {'command': str(PYTHON),
            'args': [str(ROOT / 'mcp_server.py')], 'env': {'PYTHONUTF8': '1'}}}}


def daily_schedule_rrule():
    if SCHEDULE_MODE == 'random':
        return f'FREQ=MINUTELY;INTERVAL={SCHEDULE_POLL_SECONDS // 60}'
    hour, minute = map(int, SCHEDULE_TIME.split(':'))
    hours = sorted({(hour + offset) % 24 for offset in range(0, 24, SCHEDULE_RECOVERY_HOURS)})
    return 'FREQ=DAILY;BYHOUR=' + ','.join(map(str, hours)) + f';BYMINUTE={minute};BYSECOND=0'


def daily_schedule_summary(reference=None):
    """State the schedule in both clocks, because a bare RRULE does not say which one it means.

    The hour set is written from SCHEDULE_TIMEZONE's wall clock. Whether a given scheduler reads it
    as local or as UTC is invisible while the zone's offset is a whole multiple of the recovery
    interval, which is why the shipped Asia/Shanghai configuration cannot reveal the difference.
    Half-hour zones and other offsets can, so both clocks are reported rather than assumed.
    """
    if SCHEDULE_MODE == 'random':
        return {'mode': 'random', 'timezone': SITE_TIMEZONE,
                'poll_seconds': SCHEDULE_POLL_SECONDS,
                'window': [f'{SCHEDULE_WINDOW_START:02d}:00', f'{SCHEDULE_WINDOW_END:02d}:00'],
                'distribution': 'beta', 'curve': list(SCHEDULE_CURVE), 'random_source': 'SystemRandom',
                'recovery_hours': SCHEDULE_RECOVERY_HOURS, 'rrule': daily_schedule_rrule(),
                'rrule_clock_is_ambiguous': False}
    reference = reference or datetime.now(timezone.utc)
    hour, minute = map(int, SCHEDULE_TIME.split(':'))
    local = reference.astimezone(ZoneInfo(SCHEDULE_TIMEZONE)).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    offsets = range(0, 24, SCHEDULE_RECOVERY_HOURS)
    fires = [local + timedelta(hours=offset) for offset in offsets]
    return {'mode': 'fixed', 'time': SCHEDULE_TIME, 'timezone': SCHEDULE_TIMEZONE,
            'poll_seconds': SCHEDULE_POLL_SECONDS,
            'recovery_hours': SCHEDULE_RECOVERY_HOURS,
            'fires_local': sorted(moment.strftime('%H:%M') for moment in fires),
            'fires_utc': sorted(moment.astimezone(timezone.utc).strftime('%H:%M') for moment in fires),
            'rrule': daily_schedule_rrule(),
            'rrule_clock_is_ambiguous': sorted(moment.strftime('%H:%M') for moment in fires)
                                        != sorted(moment.astimezone(timezone.utc).strftime('%H:%M')
                                                  for moment in fires)}
