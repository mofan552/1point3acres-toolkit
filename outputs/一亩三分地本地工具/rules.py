import json
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from random import SystemRandom
from zoneinfo import ZoneInfo
from settings import (SITE_TIMEZONE, SCHEDULE_TIMEZONE, SCHEDULE_TIME, SCHEDULE_RECOVERY_HOURS,
                      SCHEDULE_MODE, SCHEDULE_WINDOW_START, SCHEDULE_WINDOW_END, SCHEDULE_CURVE,
                      CHECKIN_MOOD_WEIGHTS, CHECKIN_MOOD_PERSISTENCE, CHECKIN_MOOD_DEFAULT, MOOD_PHRASE_MAX_LENGTH,
                      HEALTH_ALERT_DAYS, HEALTH_STALE_RUNS, QUIZ_GAP_SECONDS)
from contracts import (ACTIONS, HealthVerdict, HealthReason, day_complete, Attribution, Certainty, ContentStatus,
                       ROUND_PATTERNS, QUESTION_CUES, SPECULATION_CUES, NOISE_CUES, RESTRICTED_MARKER)

LA = ZoneInfo(SITE_TIMEZONE)


def normalize(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text))).strip()


def choose_answer(question, options, bank):
    wanted = {normalize(value) for key, value in bank.items() if normalize(key) == normalize(question)}
    if len(wanted) != 1:
        return None
    matches = [key for key, value in options.items() if normalize(value) in wanted]
    return matches[0] if len(matches) == 1 else None


def site_day(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(LA).date().isoformat()


def daily_due_at(now):
    """Deterministic deadline: random plans without a stored draw are due at window end."""
    midnight = datetime.fromisoformat(site_day(now)).replace(tzinfo=LA)
    if SCHEDULE_MODE == 'random':
        return midnight.replace(hour=SCHEDULE_WINDOW_END).astimezone(timezone.utc)
    local = midnight.astimezone(ZoneInfo(SCHEDULE_TIMEZONE))
    hour, minute = map(int, SCHEDULE_TIME.split(':'))
    return local.replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(timezone.utc)


def draw_daily_due_at(now, rng=None):
    if SCHEDULE_MODE == 'fixed':
        return daily_due_at(now)
    rng = rng or SystemRandom()
    start = datetime.fromisoformat(site_day(now)).replace(tzinfo=LA, hour=SCHEDULE_WINDOW_START)
    minutes = (SCHEDULE_WINDOW_END - SCHEDULE_WINDOW_START) * 60
    # Minute resolution matches the lightweight local scheduler, with the upper endpoint excluded.
    offset = min(minutes - 1, int(rng.betavariate(*SCHEDULE_CURVE) * minutes))
    return (start + timedelta(minutes=offset)).astimezone(timezone.utc)


def make_quiz_gap(now, rng=None):
    delay = (rng or SystemRandom()).uniform(*QUIZ_GAP_SECONDS)
    return {'observed_at': now.isoformat(), 'delay_seconds': delay,
            'ready_at': (now + timedelta(seconds=delay)).isoformat()}


def verify_reward(uid, action, logs, now=None):
    title = next(item.reward_title for item in ACTIONS if item.key == action)
    today = site_day(now)
    for row in logs:
        try:
            if (int(row.get("uid", -1)) == int(uid)
                    and float(row.get("extcredits1", 0)) > 0
                    and row.get("details", {}).get("title") == title
                    and site_day(datetime.fromtimestamp(int(row["dateline"]), timezone.utc)) == today):
                return True
        except (TypeError, ValueError, KeyError, OverflowError):
            continue
    return False


def last_due_site_day(now, due_at=None):
    """The most recent site day that should already be finished; today only counts once it is due."""
    today = site_day(now)
    if now >= (due_at or daily_due_at(now)):
        return today
    return (date.fromisoformat(today) - timedelta(days=1)).isoformat()


def daily_health(days, runs, heartbeat_at=None, now=None, *, due_at=None):
    """One deterministic verdict over stored evidence, so a scheduler never has to judge a trend itself.

    A site day with no rows counts as incomplete: a dead scheduler leaves silence, not a failure row.
    Liveness comes from the recorded heartbeat, never from the newest run: a healthy day finishes in
    one run and every later invocation exits quietly, so run age measures work done, not firing.
    """
    now = now or datetime.now(timezone.utc)
    evaluated_through = last_due_site_day(now, due_at)
    known = {day['site_day']: day for day in days}
    fired = [datetime.fromisoformat(run['started_at']) for run in runs if run.get('started_at')]
    if heartbeat_at:
        fired.append(datetime.fromisoformat(heartbeat_at))
    age = (now - max(fired)).total_seconds() / 3600 if fired else None
    health = {'verdict': HealthVerdict.OK, 'reasons': [], 'evaluated_through': evaluated_through,
              'consecutive_incomplete_days': 0, 'incomplete_days': [],
              'last_fired_age_hours': None if age is None else round(age, 2)}
    if not known:
        health.update(verdict=HealthVerdict.ALERT, reasons=[HealthReason.NO_HISTORY])
        return health
    oldest, cursor = date.fromisoformat(min(known)), date.fromisoformat(evaluated_through)
    while cursor >= oldest and not day_complete(known.get(cursor.isoformat())):
        health['incomplete_days'].append(cursor.isoformat())
        cursor -= timedelta(days=1)
    health['consecutive_incomplete_days'] = len(health['incomplete_days'])
    stale = age is None or age > SCHEDULE_RECOVERY_HOURS * HEALTH_STALE_RUNS
    levels = [HealthVerdict.ALERT if health['consecutive_incomplete_days'] >= HEALTH_ALERT_DAYS
              else HealthVerdict.WARN if health['incomplete_days'] else HealthVerdict.OK,
              HealthVerdict.ALERT if stale else HealthVerdict.OK]
    order = [HealthVerdict.OK, HealthVerdict.WARN, HealthVerdict.ALERT]
    health['verdict'] = max(levels, key=order.index)
    if health['incomplete_days']:
        health['reasons'].append(HealthReason.INCOMPLETE_DAYS)
    if stale:
        health['reasons'].append(HealthReason.RUNS_STALE)
    return health


def choose_mood(previous=None, rng=None):
    """Pick a mood the way a member would, not the way a fair die would.

    Uniform independent draws are themselves a signature: over a calendar year every mood turns up
    equally often and no mood ever runs two days, which no real member looks like. So the marginal
    distribution is skewed toward mundane days, and a run continues with CHECKIN_MOOD_PERSISTENCE.
    Streaks need yesterday's mood; pass None when it is unknown and only the weights apply.
    """
    rng = rng or SystemRandom()
    if previous in CHECKIN_MOOD_WEIGHTS and rng.random() < CHECKIN_MOOD_PERSISTENCE:
        return previous
    target = rng.random() * sum(CHECKIN_MOOD_WEIGHTS.values())
    for mood, weight in CHECKIN_MOOD_WEIGHTS.items():
        target -= weight
        if target < 0:
            return mood
    return CHECKIN_MOOD_DEFAULT


def load_mood_phrases(path):
    """The phrase pool shipped with the tool, checked before a single line can be published: one group
    per mood the site offers, every entry a short non-blank sentence, no entry repeated anywhere."""
    try:
        pool = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('invalid_mood_phrases') from None
    if not isinstance(pool, dict) or set(pool) != set(CHECKIN_MOOD_WEIGHTS):
        raise ValueError('invalid_mood_phrases')
    seen = set()
    for mood, phrases in pool.items():
        if not isinstance(phrases, list) or not phrases:
            raise ValueError('invalid_mood_phrases')
        for phrase in phrases:
            if (not isinstance(phrase, str) or phrase != phrase.strip() or not phrase
                    or len(phrase) > MOOD_PHRASE_MAX_LENGTH or '\n' in phrase or phrase in seen):
                raise ValueError('invalid_mood_phrases')
            seen.add(phrase)
    return pool


def choose_phrase(mood, pool, recent=(), rng=None):
    """One line from the chosen mood's group, avoiding what the account said recently.

    The default mood publishes nothing, so it gets no phrase. When every line of a group was used
    within the recent window the whole group is eligible again rather than falling back to silence or
    to another mood's tone.
    """
    if mood == CHECKIN_MOOD_DEFAULT:
        return None
    rng = rng or SystemRandom()
    group = pool[mood]
    fresh = [phrase for phrase in group if phrase not in set(recent)]
    return rng.choice(fresh or group)


def _lines(text):
    for raw in str(text or '').split('\n'):
        line = raw.strip()
        if line and line != RESTRICTED_MARKER and not re.search(NOISE_CUES, line):
            yield line


def build_outline(record):
    """A reviewable outline of an interview thread, built only from what the posts literally say.

    Every entry points back to its post and quotes it verbatim, is attributed to the author or to a
    reply, and is marked speculated when the line hedges. Nothing is inferred to fill a gap: a thread
    with no round words has no rounds, a title with no role has no role, restricted text stays missing.
    The same posts always yield the same outline, so a stored outline can be checked by content hash.
    """
    posts = record.get('posts') or []
    author_uid = ((posts[0].get('author') or {}).get('uid') if posts else None)
    rounds, questions, missing = [], [], []
    restricted_segments = 0
    for index, post in enumerate(posts):
        text = post.get('text') or ''
        restricted_segments += text.count(RESTRICTED_MARKER)
        poster = (post.get('author') or {}).get('uid')
        attribution = Attribution.AUTHOR if (index == 0 or (poster is not None and poster == author_uid)) else Attribution.REPLY
        current_round = None
        for line in _lines(text):
            certainty = Certainty.SPECULATED if re.search(SPECULATION_CUES, line, re.I) else Certainty.STATED
            found = [name for name, pattern in ROUND_PATTERNS if re.search(pattern, line, re.I)]
            excerpt = line[:200]
            for name in found:
                rounds.append({'round': name, 'pid': post['pid'], 'excerpt': excerpt,
                               'attribution': attribution, 'certainty': certainty, 'extraction': 'rule'})
            if found:
                current_round = found[0]
            # A question needs a round context or the author's own voice; a reply chatting about
            # "问题" with no round around it is conversation, not an interview question.
            if re.search(QUESTION_CUES, line, re.I) and len(line) >= 12 and (current_round or attribution == Attribution.AUTHOR):
                questions.append({'round': current_round, 'pid': post['pid'], 'excerpt': excerpt,
                                  'attribution': attribution, 'certainty': certainty, 'extraction': 'rule'})
    if not rounds:
        missing.append('rounds')
    if not questions:
        missing.append('questions')
    role = record.get('role') if record.get('role') not in (None, '未标注') else None
    level = record.get('level') if record.get('level') not in (None, '未标注') else None
    if role is None:
        missing.append('role')
    if level is None:
        missing.append('level')
    if record.get('content_status') == ContentStatus.RESTRICTED or restricted_segments:
        missing.append('restricted_text')
    return {'tid': record['tid'], 'content_hash': record.get('content_hash'), 'company': record.get('company'),
            'role': role, 'level': level, 'rounds': rounds, 'questions': questions,
            'restricted_segments': restricted_segments, 'content_status': record.get('content_status'),
            'pagination_complete': record.get('pagination_complete'), 'missing': missing,
            'method': 'rule_based_review_required'}
