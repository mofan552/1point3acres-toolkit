import csv
from html import unescape as html_unescape
import os
import hashlib
import json
import re
import shutil
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from browser import Browser
from settings import (ROOT, SITE, SITE_HOST, STATE, COLLECT_COMPANY, COLLECT_TAG, COLLECT_LIMIT, COLLECT_MAX,
                      LIST_PAGES, LIST_PAGES_MAX, THREAD_PAGES, THREAD_PAGES_MAX, SEARCH_LIMIT,
                      SEARCH_MAX, EXPORT_DIRECTORY, EXPORT_FILES, SITE_SEARCH_PATH, SITE_SEARCH_ENCODING,
                      SITE_SEARCH_LIMIT, SITE_SEARCH_MAX, SITE_SEARCH_QUERY_MAX, DATABASE_NAME,
                      BOARD_LIMIT, BOARD_MAX,
                      ACCOUNT_UID, HISTORY_LIMIT, HISTORY_MAX, TASK_HEARTBEAT_STALE_SECONDS, TASK_LIST_LIMIT,
                      MEDIA_DIRECTORY, MEDIA_HOSTS, MEDIA_MAX_BYTES, MEDIA_MAX_PER_THREAD, MEDIA_EXTENSIONS, EXPORT_MEDIA_DIRNAME,
                      OCR_ENGINE_NAME, OCR_MAX_IMAGES, OCR_MIN_SCORE)
from contracts import (RunStatus, ContentStatus, CollectionStatus, collection_status, record_state,
                       format_error, validate_record, daily_history_record, summarize_daily_history,
                       record_search_text, record_matches, validate_date_bound,
                       TaskState, TaskControl, PauseRequested, TASK_ACTIVE_STATES, task_view, MediaOutcome)
from rules import site_day, daily_health, build_outline, daily_retry_due
from extract import (parse_listing, parse_thread, parse_thread_reference, parse_search,
                     parse_search_reference, parse_board, parse_board_reference,
                     parse_profile, parse_profile_reference, parse_profile_threads, parse_favorites)
from presentation import render_reader


def merge_pages(pages, company):
    first = pages[0]
    posts = {post['pid']: post for page in pages for post in page['posts']}
    visible = '\n\n'.join(post['text'] for post in posts.values())
    restricted = any(post['restricted'] for post in posts.values())
    expected = first.get('expected_posts')
    pagination_complete = not pages[-1].get('next_url') and expected is not None and len(posts) >= expected
    text = first['title'] + '\n' + visible
    patterns = {'OA': r'\boa\b|笔试', '电面': r'电面|店面|滇缅|phone|tech(?:nical)? screen',
                'Coding': r'coding|代码|编程', 'Debug': r'debug|bug squash|mako',
                'Integration': r'integration|集成', '系统设计': r'system design|系统设计|\bsd\b',
                'BQ': r'\bbq\b|behavioral|行为面试', '滑动窗口': r'滑动窗口|sliding window|incident monitor',
                'KYC': r'\bkyc\b|data verif|data valid', 'AI Coding': r'ai coding|prompt'}
    tags = [name for name, pattern in patterns.items() if re.search(pattern, text, re.I)]
    role = next((name for name, pattern in [('MLE', r'\bmle\b|machine learning'), ('前端', r'front.?end|前端'), ('后端', r'backend|后端'), ('SWE', r'\bswe\b|\bsde\b')] if re.search(pattern, first['title'], re.I)), '未标注')
    level = next((name for name, pattern in [('Staff', r'staff|士大夫'), ('Senior', r'senior|\bl3\b'), ('实习', r'intern|实习'), ('New Grad', r'\bng\b|new grad|应届')] if re.search(pattern, first['title'], re.I)), '未标注')
    return {'tid': first['tid'], 'company': company, 'title': first['title'], 'url': first['url'],
            'role': role, 'level': level, 'tags': tags, 'posts': list(posts.values()),
            'summary': posts[next(iter(posts))]['text'][:500] if posts else '',
            'content_status': ContentStatus.RESTRICTED if restricted else ContentStatus.VISIBLE,
            'pagination_complete': pagination_complete, 'complete': pagination_complete and not restricted,
            'expected_posts': expected, 'pages_fetched': len(pages), 'next_url': pages[-1].get('next_url'),
            'images_included': False, 'fetched_at': datetime.now(timezone.utc).isoformat(),
            # Thread-level counters come from the first page read; older pages simply lack them.
            'stats': first.get('stats'),
            'content_hash': hashlib.sha256(visible.encode('utf-8')).hexdigest()}


class Library:
    def __init__(self, path=None):
        path = path or STATE / DATABASE_NAME
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS threads(tid INTEGER PRIMARY KEY, company TEXT, title TEXT, search_text TEXT, data TEXT, complete INTEGER);
            CREATE TABLE IF NOT EXISTS versions(tid INTEGER, hash TEXT, data TEXT, PRIMARY KEY(tid,hash));
            CREATE TABLE IF NOT EXISTS queue(tid INTEGER PRIMARY KEY, url TEXT, company TEXT, status TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS progress(tid INTEGER PRIMARY KEY, pages TEXT);
            CREATE TABLE IF NOT EXISTS daily_runs(account_uid INTEGER, run_id TEXT, site_day TEXT,
                started_at TEXT, data TEXT, PRIMARY KEY(account_uid,run_id));
            CREATE INDEX IF NOT EXISTS daily_runs_by_day ON daily_runs(account_uid,site_day,started_at);
            CREATE TABLE IF NOT EXISTS scheduler_heartbeat(account_uid INTEGER PRIMARY KEY, fired_at TEXT);
            CREATE TABLE IF NOT EXISTS daily_plans(account_uid INTEGER, site_day TEXT, kind TEXT, data TEXT,
                PRIMARY KEY(account_uid,site_day,kind));
            CREATE TABLE IF NOT EXISTS outlines(tid INTEGER, content_hash TEXT, data TEXT, built_at TEXT,
                PRIMARY KEY(tid, content_hash));
            CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY, account_uid INTEGER, kind TEXT, params TEXT,
                state TEXT, control TEXT, created_at TEXT, updated_at TEXT, worker TEXT, heartbeat_at TEXT,
                progress TEXT, result TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS media(tid INTEGER, url TEXT, pid INTEGER, kind TEXT, name TEXT, sha256 TEXT,
                path TEXT, bytes INTEGER, mime TEXT, fetched_at TEXT, outcome TEXT, error TEXT, PRIMARY KEY(tid, url));
            CREATE TABLE IF NOT EXISTS media_text(sha256 TEXT, engine TEXT, text TEXT, lines INTEGER, mean_score REAL,
                recognized_at TEXT, PRIMARY KEY(sha256, engine));
        ''')

    def close(self):
        self.db.close()

    def daily_plan(self, account_uid, day, kind, create=None):
        """Read or atomically create a daily choice. The factory must be local and quick, never network I/O."""
        def read():
            row = self.db.execute('SELECT data FROM daily_plans WHERE account_uid=? AND site_day=? AND kind=?',
                                  (account_uid, day, kind)).fetchone()
            return json.loads(row[0]) if row else None
        if create is None:
            return read()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            value = read()
            if value is None:
                value = create()
                self.db.execute('INSERT INTO daily_plans VALUES(?,?,?,?)',
                                (account_uid, day, kind, json.dumps(value, ensure_ascii=False)))
        return value

    def save_daily(self, result, account_uid, *, checkpoint=False):
        started = datetime.fromisoformat(result['started_at'])
        if started.tzinfo is None or result['site_day'] != site_day(started) or not result['run_id']:
            raise ValueError('invalid_daily_history_record')
        record = daily_history_record(result)
        with self.db:
            self.db.execute('INSERT INTO daily_runs VALUES(?,?,?,?,?) ON CONFLICT(account_uid,run_id) '
                'DO UPDATE SET data=excluded.data WHERE ? AND daily_runs.site_day=excluded.site_day '
                'AND daily_runs.started_at=excluded.started_at',
                (account_uid, record['run_id'], record['site_day'], started.astimezone(timezone.utc).isoformat(),
                 json.dumps(record, ensure_ascii=False), checkpoint))

    def save_outline(self, outline):
        """Outlines live beside the record, never inside it; one row per content version (issue #23)."""
        with self.db:
            self.db.execute('INSERT INTO outlines VALUES(?,?,?,?) ON CONFLICT(tid, content_hash) DO UPDATE SET '
                            'data=excluded.data, built_at=excluded.built_at',
                            (outline['tid'], outline['content_hash'], json.dumps(outline, ensure_ascii=False),
                             datetime.now(timezone.utc).isoformat()))

    def outline(self, tid, content_hash):
        row = self.db.execute('SELECT data, built_at FROM outlines WHERE tid=? AND content_hash=?',
                              (tid, content_hash)).fetchone()
        return (json.loads(row[0]), row[1]) if row else (None, None)

    def outline_versions(self, tid):
        return [row[0] for row in self.db.execute('SELECT content_hash FROM outlines WHERE tid=? ORDER BY built_at', (tid,))]

    def record_heartbeat(self, account_uid, fired_at):
        """Every scheduled invocation, including the quiet ones that do no work and write no run."""
        with self.db:
            self.db.execute('INSERT INTO scheduler_heartbeat VALUES(?,?) ON CONFLICT(account_uid) '
                            'DO UPDATE SET fired_at=excluded.fired_at', (account_uid, fired_at))

    def daily_retry_due(self, account_uid, day):
        rows = self.db.execute('SELECT data FROM daily_runs WHERE account_uid=? AND site_day=? '
                               'ORDER BY started_at DESC,run_id DESC', (account_uid, day))
        return daily_retry_due(json.loads(row[0]) for row in rows)

    def last_heartbeat(self, account_uid):
        row = self.db.execute('SELECT fired_at FROM scheduler_heartbeat WHERE account_uid=?',
                              (account_uid,)).fetchone()
        return row[0] if row else None

    # --- persisted collection tasks (issues #37, #38) ---------------------------------------------

    def _task_row(self, task_id):
        row = self.db.execute('SELECT task_id, account_uid, kind, params, state, control, created_at, updated_at, worker, '
                              'heartbeat_at, progress, result, error FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            return None
        keys = ['task_id', 'account_uid', 'kind', 'params', 'state', 'control', 'created_at', 'updated_at', 'worker',
                'heartbeat_at', 'progress', 'result', 'error']
        task = dict(zip(keys, row))
        for key in ('params', 'progress', 'result'):
            task[key] = json.loads(task[key]) if task[key] else None
        task['stale_after'] = TASK_HEARTBEAT_STALE_SECONDS
        return task

    def create_task(self, task_id, kind, params, account_uid):
        """Freeze the task's parameters now: later default changes never reach a task already created."""
        moment = datetime.now(timezone.utc).isoformat()
        with self.db:
            self.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,NULL,NULL,?,NULL,NULL)',
                            (task_id, account_uid, kind, json.dumps(params, ensure_ascii=False), TaskState.QUEUED,
                             TaskControl.RUN, moment, moment, json.dumps({'discovered': None, 'selected': [], 'processed': [],
                                                                          'failed': [], 'next_index': 0}, ensure_ascii=False)))
        return self._task_row(task_id)

    def task(self, task_id):
        return self._task_row(task_id)

    def tasks(self, limit=TASK_LIST_LIMIT):
        rows = self.db.execute('SELECT task_id FROM tasks ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall()
        return [self._task_row(row[0]) for row in rows]

    def claim_task(self, task_id, worker):
        """One executor at a time: a queued task is taken only when no other task is running with a
        live heartbeat; a running task whose executor went quiet may be taken over."""
        now = datetime.now(timezone.utc)
        with self.db:
            task = self._task_row(task_id)
            if task is None:
                raise ValueError('task_not_found')
            if task['state'] == TaskState.PAUSED or task['control'] != TaskControl.RUN:
                raise ValueError('task_paused')  # resume it explicitly; an executor never overrides the owner
            if task['state'] not in (TaskState.QUEUED, TaskState.RUNNING):
                raise ValueError('task_not_runnable')
            for other in self.tasks(limit=1000):
                if other['task_id'] == task_id or other['state'] != TaskState.RUNNING or not other['heartbeat_at']:
                    continue
                if (now - datetime.fromisoformat(other['heartbeat_at'])).total_seconds() <= TASK_HEARTBEAT_STALE_SECONDS:
                    raise ValueError('another_task_is_running')
            if task['state'] == TaskState.RUNNING and task['heartbeat_at'] and \
                    (now - datetime.fromisoformat(task['heartbeat_at'])).total_seconds() <= TASK_HEARTBEAT_STALE_SECONDS:
                raise ValueError('task_already_running')
            self.db.execute('UPDATE tasks SET state=?, worker=?, heartbeat_at=?, updated_at=?, error=NULL WHERE task_id=?',
                            (TaskState.RUNNING, worker, now.isoformat(), now.isoformat(), task_id))
        return self._task_row(task_id)

    def task_heartbeat(self, task_id, progress=None):
        moment = datetime.now(timezone.utc).isoformat()
        with self.db:
            if progress is None:
                self.db.execute('UPDATE tasks SET heartbeat_at=?, updated_at=? WHERE task_id=?', (moment, moment, task_id))
            else:
                self.db.execute('UPDATE tasks SET heartbeat_at=?, updated_at=?, progress=? WHERE task_id=?',
                                (moment, moment, json.dumps(progress, ensure_ascii=False), task_id))

    def task_control(self, task_id):
        row = self.db.execute('SELECT control FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        return TaskControl(row[0]) if row else None

    def set_task_control(self, task_id, control):
        """The owner's request; the state changes only when the executor reaches a safe point (pause)
        or when a paused task is put back in the queue (resume). Finished tasks cannot be controlled."""
        with self.db:
            task = self._task_row(task_id)
            if task is None:
                raise ValueError('task_not_found')
            if task['state'] not in TASK_ACTIVE_STATES:
                raise ValueError('task_finished')
            moment = datetime.now(timezone.utc).isoformat()
            if control == TaskControl.RUN and task['state'] == TaskState.PAUSED:
                self.db.execute('UPDATE tasks SET control=?, state=?, updated_at=? WHERE task_id=?',
                                (TaskControl.RUN, TaskState.QUEUED, moment, task_id))
            else:
                self.db.execute('UPDATE tasks SET control=?, updated_at=? WHERE task_id=?', (control, moment, task_id))
        return self._task_row(task_id)

    def finish_task(self, task_id, state, progress, result=None, error=None):
        moment = datetime.now(timezone.utc).isoformat()
        with self.db:
            self.db.execute('UPDATE tasks SET state=?, progress=?, result=?, error=?, updated_at=?, heartbeat_at=? WHERE task_id=?',
                            (state, json.dumps(progress, ensure_ascii=False), json.dumps(result, ensure_ascii=False) if result is not None else None,
                             error, moment, moment, task_id))
        return self._task_row(task_id)

    # --- archived media index (issue #39): beside the record, never inside it -----------------------

    def media_for(self, tid):
        rows = self.db.execute('SELECT url, pid, kind, name, sha256, path, bytes, mime, fetched_at, outcome, error '
                               'FROM media WHERE tid=? ORDER BY pid, url', (tid,)).fetchall()
        keys = ['url', 'pid', 'kind', 'name', 'sha256', 'path', 'bytes', 'mime', 'fetched_at', 'outcome', 'error']
        return [dict(zip(keys, row)) for row in rows]

    def media_by_hash(self, sha256):
        row = self.db.execute('SELECT path, bytes, mime FROM media WHERE sha256=? AND path IS NOT NULL LIMIT 1', (sha256,)).fetchone()
        return {'path': row[0], 'bytes': row[1], 'mime': row[2]} if row else None

    def media_text(self, sha256, engine):
        row = self.db.execute('SELECT text, lines, mean_score, recognized_at FROM media_text WHERE sha256=? AND engine=?',
                              (sha256, engine)).fetchone()
        return {'text': row[0], 'lines': row[1], 'mean_score': row[2], 'recognized_at': row[3], 'engine': engine} if row else None

    def save_media_text(self, sha256, engine, text, lines, mean_score):
        with self.db:
            self.db.execute('INSERT INTO media_text VALUES(?,?,?,?,?,?) ON CONFLICT(sha256, engine) DO UPDATE SET '
                            'text=excluded.text, lines=excluded.lines, mean_score=excluded.mean_score, recognized_at=excluded.recognized_at',
                            (sha256, engine, text, lines, mean_score, datetime.now(timezone.utc).isoformat()))

    def tids_with_recognized_text(self, query):
        """Threads whose archived images carry recognised text matching the query: a separate layer,
        joined through the media index so the source of every hit stays an image, not the author."""
        escaped = query.replace('!', '!!').replace('%', '!%').replace('_', '!_')
        rows = self.db.execute("SELECT DISTINCT m.tid FROM media m JOIN media_text t ON t.sha256=m.sha256 "
                               "WHERE t.text LIKE ? ESCAPE '!' AND t.lines > 0", ('%' + escaped + '%',)).fetchall()
        return {row[0] for row in rows}

    def save_media(self, tid, entry):
        with self.db:
            self.db.execute('INSERT INTO media VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tid, url) DO UPDATE SET '
                            'pid=excluded.pid, kind=excluded.kind, name=excluded.name, sha256=excluded.sha256, path=excluded.path, '
                            'bytes=excluded.bytes, mime=excluded.mime, fetched_at=excluded.fetched_at, outcome=excluded.outcome, error=excluded.error',
                            (tid, entry['url'], entry.get('pid'), entry.get('kind'), entry.get('name'), entry.get('sha256'),
                             entry.get('path'), entry.get('bytes'), entry.get('mime'), entry.get('fetched_at'), entry['outcome'], entry.get('error')))

    def recent_checkins(self, account_uid, days, *, on=None):
        """Previous calendar days plus today for recovery; missing days never stretch the window."""
        anchor = on or site_day()
        cutoff = (date.fromisoformat(anchor) - timedelta(days=days)).isoformat()
        rows = self.db.execute('SELECT data FROM daily_runs WHERE account_uid=? AND site_day>=? AND site_day<=? '
                               'ORDER BY site_day DESC, started_at DESC', (account_uid, cutoff, anchor)).fetchall()
        recent = []
        for row in rows:
            record = json.loads(row[0])
            for action in record.get('actions', []):
                if action.get('action') == 'checkin' and action.get('mood'):
                    recent.append({'site_day': record['site_day'], 'mood': action['mood'], 'phrase': action.get('phrase')})
        return recent

    def daily_history(self, account_uid, date, limit):
        where, values = 'account_uid=?', [account_uid]
        if date is not None:
            where += ' AND site_day=?'
            values.append(date)
        rows = self.db.execute('SELECT data FROM daily_runs WHERE ' + where
            + ' ORDER BY started_at DESC,run_id DESC LIMIT ?', [*values, limit + 1]).fetchall()
        records = [json.loads(row[0]) for row in rows[:limit]]
        dates = sorted({row['site_day'] for row in records})
        days = []
        if dates:
            all_rows = self.db.execute('SELECT data FROM daily_runs WHERE account_uid=? AND site_day IN ('
                + ','.join('?' for _ in dates) + ') ORDER BY started_at,run_id', [account_uid, *dates])
            days = summarize_daily_history(json.loads(row[0]) for row in all_rows)
        now = datetime.now(timezone.utc)
        plan = self.daily_plan(account_uid, site_day(now), 'schedule') if date is None else None
        return {'status': RunStatus.COMPLETE, 'runs': records, 'days': days,
                # A single-date query is a lookup, not a trend: only the open window earns a verdict.
                'health': None if date is not None else daily_health(
                    days, records, self.last_heartbeat(account_uid), now,
                    due_at=datetime.fromisoformat(plan['due_at']) if plan else None),
                'truncated': len(rows) > limit, 'error': None}

    def _write_record(self, record):
        """The statements for one validated record; whoever calls this owns the transaction."""
        data = json.dumps(record, ensure_ascii=False)
        previous = self.get(record['tid'])
        publish = record['pagination_complete'] or not previous or (
            not previous['pagination_complete'] and
            {p['pid'] for p in previous['posts']}.issubset({p['pid'] for p in record['posts']}))
        search_text = record_search_text(record)  # the same text the reader searches, by construction
        if publish:
            self.db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?) ON CONFLICT(tid) DO UPDATE SET company=excluded.company,title=excluded.title,search_text=excluded.search_text,data=excluded.data,complete=excluded.complete',
                (record['tid'], record['company'], record['title'], search_text, data, int(record['complete'])))
        self.db.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?)', (record['tid'], record['content_hash'], data))

    def save(self, record):
        validate_record(record)
        with self.db:
            self._write_record(record)

    def save_page(self, record, pages):
        """Content, its version and the resume position commit together or roll back together (#19).

        A record that is now complete drops its progress row in the same transaction, so an interruption
        can never leave a resume position pointing past content that was never stored, or stored content
        with no position to resume from.
        """
        validate_record(record)
        with self.db:
            self._write_record(record)
            if pages and not record['pagination_complete']:
                self.db.execute('INSERT INTO progress VALUES(?,?) ON CONFLICT(tid) DO UPDATE SET pages=excluded.pages',
                                (record['tid'], json.dumps(pages, ensure_ascii=False)))
            else:
                self.db.execute('DELETE FROM progress WHERE tid=?', (record['tid'],))

    def enqueue(self, items, company):
        with self.db:
            for item in items:
                self.db.execute("INSERT OR IGNORE INTO queue VALUES(?,?,?,?,NULL)",
                                (item['tid'], item['url'], company, CollectionStatus.PENDING))

    def set_queue_state(self, tid, status, error=None):
        with self.db:
            self.db.execute('UPDATE queue SET status=?,error=? WHERE tid=?', (status, error, tid))

    def progress(self, tid):
        row = self.db.execute('SELECT pages FROM progress WHERE tid=?', (tid,)).fetchone()
        return json.loads(row[0]) if row else []

    def save_progress(self, tid, pages):
        with self.db:
            if pages:
                self.db.execute('INSERT INTO progress VALUES(?,?) ON CONFLICT(tid) DO UPDATE SET pages=excluded.pages',
                                (tid, json.dumps(pages, ensure_ascii=False)))
            else:
                self.db.execute('DELETE FROM progress WHERE tid=?', (tid,))

    def get(self, tid):
        row = self.db.execute('SELECT data FROM threads WHERE tid=?', (tid,)).fetchone()
        return json.loads(row[0]) if row else None

    def find(self, query='', company=COLLECT_COMPANY, limit=SEARCH_LIMIT, role=None, level=None,
             date_from=None, date_to=None, include_ocr=False):
        """Keyword plus facets under one rule (contracts.record_matches), with the count before the cut.

        An empty company means no company filter, so records saved without a label stay reachable.
        Role and level match exactly (未标注 is a real value); the date bounds are inclusive and apply
        to the posting date only, never to when the record was collected. With include_ocr the text
        recognised from archived images is searched as a second layer: such hits carry
        matched_in='recognized_text' so a reader never mistakes a machine reading for the author's words.
        """
        date_from = validate_date_bound(date_from, 'date_from')
        date_to = validate_date_bound(date_to, 'date_to')
        if type(include_ocr) is not bool:
            raise ValueError('invalid_include_ocr')
        escaped = query.replace('!', '!!').replace('%', '!%').replace('_', '!_')
        scope, values = ('', []) if not company else ('company=? AND ', [company])
        rows = self.db.execute("SELECT data FROM threads WHERE " + scope + "search_text LIKE ? ESCAPE '!' ORDER BY tid DESC LIMIT ?",
                               (*values, '%' + escaped + '%', SEARCH_MAX)).fetchall()
        matched = [{**record, 'matched_in': 'original'} for record in (json.loads(row[0]) for row in rows)
                   if record_matches(record, role=role, level=level, date_from=date_from, date_to=date_to)]
        if include_ocr and query:
            seen = {record['tid'] for record in matched}
            for tid in sorted(self.tids_with_recognized_text(query) - seen, reverse=True):
                record = self.get(tid)
                if record is None or (company and record.get('company') != company):
                    continue
                if record_matches(record, role=role, level=level, date_from=date_from, date_to=date_to):
                    matched.append({**record, 'matched_in': 'recognized_text'})
        cut = min(max(limit, 1), SEARCH_MAX)
        return {'records': matched[:cut], 'matched': len(matched), 'limit': cut,
                'filters': {'query': query, 'company': company or None, 'role': role or None, 'level': level or None,
                            'date_from': date_from, 'date_to': date_to, 'date_field': 'posting_date', 'include_ocr': include_ocr}}

    def search(self, query='', company=COLLECT_COMPANY, limit=SEARCH_LIMIT, **facets):
        return self.find(query, company, limit, **facets)['records']

    def stats(self):
        row = self.db.execute('SELECT count(*),coalesce(sum(complete),0) FROM threads').fetchone()
        return {'threads': row[0], 'complete': row[1], 'incomplete': row[0] - row[1]}


def get_daily_history(date=None, limit=HISTORY_LIMIT):
    try:
        if date is not None:
            if not isinstance(date, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', date):
                raise ValueError('invalid_history_date')
            try:
                datetime.strptime(date, '%Y-%m-%d')
            except ValueError:
                raise ValueError('invalid_history_date') from None
        if type(limit) is not int or not 1 <= limit <= HISTORY_MAX:
            raise ValueError('invalid_history_limit')
    except ValueError as error:
        return {'status': RunStatus.FAILED, 'runs': [], 'days': [], 'health': None, 'truncated': False, 'error': str(error)}
    db = None
    try:
        db = Library()
        return db.daily_history(ACCOUNT_UID, date, limit)
    except Exception:
        return {'status': RunStatus.FAILED, 'runs': [], 'days': [], 'health': None, 'truncated': False,
                'error': 'daily_history_unavailable'}
    finally:
        if db is not None:
            db.close()


def _read_thread_pages(browser, tid, pages, max_thread_pages, on_page=None, control=None):
    """One traversal for snapshots and durable collection; callers own persistence.

    With a task `control`, every page request is a safe point: a pause asked for in the meantime stops
    before the next request, after the pages already read were handed to `on_page`."""
    previous_page = pages[-1].get('page', len(pages)) if pages else 0
    url = pages[-1].get('next_url') if pages else parse_thread_reference(tid)['url']
    for _ in range(min(max(int(max_thread_pages), 1), THREAD_PAGES_MAX)):
        if not url:
            break
        if control is not None:
            control.checkpoint()
        location = parse_thread_reference(url)
        if location['tid'] != tid or location['page'] != previous_page + 1:
            raise ValueError('invalid_thread_pagination')
        html = browser.read_html(location['url'])
        response_url = getattr(browser, 'last_read', {}).get('url')
        if response_url and parse_thread_reference(response_url) != location:
            raise ValueError('unexpected_thread_response')
        page = parse_thread(html, location['url'])
        if page['tid'] != tid or page.get('page', location['page']) != location['page']:
            raise ValueError('unexpected_thread_page')
        validate_record(merge_pages([*pages, page], None))
        pages.append(page)
        if on_page:
            on_page(pages)
        previous_page = location['page']
        url = page['next_url']
        if url:
            browser.sb.sleep(1.2)


def get_thread_detail(thread, max_thread_pages=THREAD_PAGES):
    """Return a visible-content snapshot; do not assign a company or write the library."""
    pages, error = [], None
    try:
        location = parse_thread_reference(thread, first_page=True)
        if type(max_thread_pages) is not int or not 1 <= max_thread_pages <= THREAD_PAGES_MAX:
            raise ValueError('invalid_thread_page_budget')
        with Browser() as browser:
            _read_thread_pages(browser, location['tid'], pages, max_thread_pages)
    except Exception as caught:
        error = format_error(caught)
    record = merge_pages(pages, None) if pages else None
    if record:
        validate_record(record)
    status = RunStatus.COMPLETE if record and record['pagination_complete'] and not error else (
        RunStatus.NEEDS_ATTENTION if record else RunStatus.FAILED)
    return {'status': status, 'record': record, 'scope': 'currently_visible_content', 'error': error}


def organize_thread(thread, refresh=False):
    """Turn a saved thread into a reviewable outline; the record itself is never modified (issue #23).

    An outline is tied to the content hash it was built from. If the stored posts have changed since,
    the old outline is reported as stale and a new one is built and kept alongside it.
    """
    tid, outline, built_at, reused, stale, error = None, None, None, False, [], None
    db = None
    try:
        tid = parse_thread_reference(thread, first_page=True)['tid']
        if type(refresh) is not bool:
            raise ValueError('invalid_refresh_flag')
        db = Library()
        record = db.get(tid)
        if record is None:
            raise ValueError('thread_not_in_library')
        current = record.get('content_hash')
        stored, built_at = db.outline(tid, current) if not refresh else (None, None)
        stale = [h for h in db.outline_versions(tid) if h != current]
        if stored is not None:
            outline, reused = stored, True
        else:
            outline = build_outline(record)
            db.save_outline(outline)
            built_at = datetime.now(timezone.utc).isoformat()
    except Exception as caught:
        error = format_error(caught)
    finally:
        if db is not None:
            db.close()
    status = RunStatus.FAILED if error else (RunStatus.NEEDS_ATTENTION if outline and outline['missing'] else RunStatus.COMPLETE)
    return {'status': status, 'tid': tid, 'reused': reused, 'built_at': built_at, 'outline': outline,
            'superseded_versions': stale, 'scope': 'stored_content_only', 'error': error}


def save_thread(thread, company=None, max_thread_pages=THREAD_PAGES, refresh=False):
    """Save one chosen thread into the library so search and export can reach it (issue #20).

    The snapshot reader stays read-only; this is the explicit, separate act of keeping a thread.
    A company label is metadata the caller vouches for, never inferred from the site: without one the
    record stays unlabelled, and a label already on the record survives an unlabelled re-save.
    Each page lands through the atomic save, so an interruption leaves a resumable position.
    """
    tid, saved, pages, error, reused = None, None, [], None, False
    label = source = None
    db = None
    try:
        location = parse_thread_reference(thread, first_page=True)
        tid = location['tid']
        if type(max_thread_pages) is not int or not 1 <= max_thread_pages <= THREAD_PAGES_MAX:
            raise ValueError('invalid_thread_page_budget')
        if company is not None and (not isinstance(company, str) or not company.strip() or company != company.strip()):
            raise ValueError('invalid_company_label')
        if type(refresh) is not bool:
            raise ValueError('invalid_refresh_flag')
        db = Library()
        cached = db.get(tid)
        if company:
            label, source = company, 'caller'
        elif cached and cached.get('company'):
            label, source = cached['company'], cached.get('company_source') or 'collection'
        saved_pages = db.progress(tid)
        if cached and not refresh and not saved_pages and cached['pagination_complete']:
            reused, saved = True, cached
            if company and cached.get('company') != company:
                # Relabelling a kept record is a metadata edit, not a re-read: same content, new label.
                saved = dict(cached, company=company, company_source='caller')
                db.save_page(saved, [])
        else:
            pages = saved_pages if not refresh and saved_pages and saved_pages[-1].get('next_url') else []
            if not pages:
                db.save_progress(tid, [])

            def keep(current):
                record = merge_pages(current, label)
                record['company_source'] = source
                db.save_page(record, current)

            with Browser() as browser:
                _read_thread_pages(browser, tid, pages, max_thread_pages, on_page=keep)
            saved = db.get(tid)
    except Exception as caught:
        error = format_error(caught)
    finally:
        resume = None
        if db is not None:
            try:
                if tid is not None:
                    remaining = db.progress(tid)
                    resume = {'pages_saved': len(remaining), 'next_url': remaining[-1].get('next_url') if remaining else None}
                    if saved is None:
                        saved = db.get(tid)
            finally:
                db.close()
    state = record_state(saved) if saved else None
    status = (RunStatus.COMPLETE if saved and saved['pagination_complete'] and not error
              else RunStatus.NEEDS_ATTENTION if saved else RunStatus.FAILED)
    return {'status': status, 'tid': tid, 'reused': reused, 'collection_status': state,
            'record': saved, 'resume': resume, 'company_source': (saved or {}).get('company_source'),
            'scope': 'currently_visible_content', 'error': error}


def search_threads(query, limit=SITE_SEARCH_LIMIT, list_pages=LIST_PAGES):
    """Bounded native search, with explicit truncation and no durable-library side effects."""
    threads, pages, total, next_url, omitted, reason, error = {}, 0, None, None, 0, None, None
    pagination_complete = False
    try:
        if (not isinstance(query, str) or not query.strip()
                or re.search(r'[\x00-\x1f\x7f-\x9f]', query)):
            raise ValueError('invalid_search_query')
        query = query.strip()
        if len(query) > SITE_SEARCH_QUERY_MAX:
            raise ValueError('invalid_search_query')
        if (type(limit) is not int or not 1 <= limit <= SITE_SEARCH_MAX
                or type(list_pages) is not int or not 1 <= list_pages <= LIST_PAGES_MAX):
            raise ValueError('invalid_search_budget')
        try:
            encoded = urlencode({'mod': 'forum', 'srchtxt': query, 'searchsubmit': 'yes'}, encoding=SITE_SEARCH_ENCODING)
        except UnicodeEncodeError:
            raise ValueError('unsupported_search_query_encoding') from None
        url = SITE + SITE_SEARCH_PATH + '?' + encoded
        expected = None
        with Browser() as browser:
            for _ in range(list_pages):
                html = browser.read_html(url)
                response_url = getattr(browser, 'last_read', {}).get('url', url)
                page = parse_search(html, response_url, query)
                location = page['location']
                if (expected and (location['search_id'], location['page']) != expected
                        or not expected and location['page'] != 1):
                    raise ValueError('unexpected_search_response')
                if total is not None and page['total'] != total:
                    raise ValueError('search_result_set_changed')
                new = [row for row in page['threads'] if row['tid'] not in threads]
                if len(threads) + len(new) > page['total']:
                    raise ValueError('search_result_count_mismatch')
                pages += 1
                total = page['total']
                selected = new[:limit - len(threads)]
                threads.update((row['tid'], row) for row in selected)
                omitted = len(new) - len(selected)
                next_url = None
                if page['next_url']:
                    candidate = parse_search_reference(page['next_url'])
                    if (candidate['search_id'] != location['search_id'] or candidate['page'] != location['page'] + 1):
                        raise ValueError('invalid_search_pagination')
                    next_url = candidate['url']
                pagination_complete = not next_url and not omitted
                if pagination_complete:
                    if len(threads) != total:
                        raise ValueError('search_result_count_mismatch')
                    break
                if len(threads) >= limit:
                    reason = 'result_limit'
                    break
                if pages == list_pages:
                    reason = 'page_limit'
                    break
                expected = (location['search_id'], location['page'] + 1)
                url = next_url
                browser.sb.sleep(1)
    except Exception as caught:
        error = format_error(caught)
    status = RunStatus.COMPLETE if pages and not error else (RunStatus.NEEDS_ATTENTION if pages else RunStatus.FAILED)
    return {'status': status, 'query': query if isinstance(query, str) else None, 'threads': list(threads.values()),
            'pages_fetched': pages, 'site_reported_total': total, 'pagination_complete': pagination_complete and not error,
            'next_url': next_url, 'omitted_on_last_page': omitted, 'truncated_reason': reason,
            'scope': 'currently_visible_content', 'error': error}


def browse_board(board, limit=BOARD_LIMIT, list_pages=LIST_PAGES):
    """Bounded board browsing: no search term, no durable library writes, no full-text reads.

    A read budget is not evidence that the board ended, so pagination_complete stays false unless
    the site itself offered no next page and nothing was dropped for the limit.
    """
    threads, pages, next_url, omitted, reason, error = {}, 0, None, 0, None, None
    pagination_complete, reference = False, None
    try:
        if (type(limit) is not int or not 1 <= limit <= BOARD_MAX
                or type(list_pages) is not int or not 1 <= list_pages <= LIST_PAGES_MAX):
            raise ValueError('invalid_board_budget')
        reference = parse_board_reference(board)
        url = reference['url']
        with Browser() as browser:
            for _ in range(list_pages):
                page = parse_board(browser.read_html(url), url)
                new = [row for row in page['threads'] if row['tid'] not in threads]
                pages += 1
                selected = new[:limit - len(threads)]
                threads.update((row['tid'], row) for row in selected)
                omitted = len(new) - len(selected)
                next_url = page['next_url']
                if next_url and not re.search(r'/bbs/forum-%d-\d+\.html' % reference['board'], next_url):
                    raise ValueError('invalid_board_pagination')
                pagination_complete = not next_url and not omitted
                if pagination_complete or len(threads) >= limit or pages == list_pages:
                    reason = None if pagination_complete else ('result_limit' if len(threads) >= limit else 'page_limit')
                    break
                url = next_url
                browser.sb.sleep(1)
    except Exception as caught:
        error = format_error(caught)
    status = RunStatus.COMPLETE if pages and not error else (RunStatus.NEEDS_ATTENTION if pages else RunStatus.FAILED)
    return {'status': status, 'board': reference['board'] if reference else None,
            'threads': list(threads.values()), 'pages_fetched': pages,
            'pagination_complete': pagination_complete and not error, 'next_url': next_url,
            'omitted_on_last_page': omitted, 'truncated_reason': reason,
            'scope': 'currently_visible_content', 'error': error}


def _paged_rows(browser, url, parse, key, uid, action, limit, list_pages):
    """Shared bounded traversal for a member's lists: dedupe by tid, never mistake a budget for the end."""
    rows, pages, next_url, omitted, reason = {}, 0, None, 0, None
    pagination_complete = False
    for _ in range(list_pages):
        page = parse(browser.read_html(url), url)
        new = [row for row in page[key] if row['tid'] not in rows]
        pages += 1
        selected = new[:limit - len(rows)]
        rows.update((row['tid'], row) for row in selected)
        omitted = len(new) - len(selected)
        next_url = page['next_url']
        if next_url:
            query = parse_qs(urlsplit(next_url).query)
            if query.get('uid') != [str(uid)] or query.get('do') != [action]:
                raise ValueError('invalid_profile_pagination')
        pagination_complete = not next_url and not omitted
        if pagination_complete or len(rows) >= limit or pages == list_pages:
            reason = None if pagination_complete else ('result_limit' if len(rows) >= limit else 'page_limit')
            break
        url = next_url
        browser.sb.sleep(1)
    return {key: list(rows.values()), 'pages_fetched': pages, 'pagination_complete': pagination_complete,
            'next_url': next_url, 'omitted_on_last_page': omitted, 'truncated_reason': reason}


def _list_budget(limit, list_pages):
    if (type(limit) is not int or not 1 <= limit <= BOARD_MAX
            or type(list_pages) is not int or not 1 <= list_pages <= LIST_PAGES_MAX):
        raise ValueError('invalid_profile_budget')


def _empty_list(key):
    return {key: [], 'pages_fetched': 0, 'pagination_complete': False, 'next_url': None,
            'omitted_on_last_page': 0, 'truncated_reason': None}


def get_user_profile(user, limit=BOARD_LIMIT, list_pages=LIST_PAGES):
    """A member's public identity and their threads, bounded like every other list read.

    The page read must belong to the uid asked for, or nothing is returned: a redirect or a
    different owner is an identity mismatch, never a quietly substituted member.
    """
    threads, profile, reference, error = _empty_list('threads'), None, None, None
    try:
        _list_budget(limit, list_pages)
        reference = parse_profile_reference(user)
        with Browser() as browser:
            page = parse_profile(browser.read_html(reference['space_url']), reference['space_url'])
            if page['uid'] != reference['uid']:
                raise ValueError('profile_identity_mismatch')  # Never hand back somebody else's page.
            profile = page
            threads = _paged_rows(browser, reference['threads_url'], parse_profile_threads, 'threads',
                                  reference['uid'], 'thread', limit, list_pages)
    except Exception as caught:
        error = format_error(caught)
    pages = threads['pages_fetched']
    status = RunStatus.COMPLETE if pages and not error else (RunStatus.NEEDS_ATTENTION if pages or profile else RunStatus.FAILED)
    return {'status': status, 'uid': reference['uid'] if reference else None, 'profile': profile,
            'sections': ['threads'], **{k: (v and not error) if k == 'pagination_complete' else v for k, v in threads.items()},
            'scope': 'currently_visible_content', 'error': error}


def get_my_profile(limit=BOARD_LIMIT, list_pages=LIST_PAGES):
    """The configured account's own space: identity is taken from the verified session, never typed in.

    Sections are the ones this site really has for its members: own threads, thread favorites, and the
    favorited forums and tags its favorites endpoint returns. Nothing here writes or unfavorites.
    """
    threads, favorites = _empty_list('threads'), _empty_list('favorites')
    profile, forums, tags, uid, error = None, None, None, None, None
    try:
        _list_budget(limit, list_pages)
        with Browser() as browser:
            user = browser.profile()  # raises unexpected_account when the browser holds someone else
            uid = user['uid']
            reference = parse_profile_reference(uid)
            page = parse_profile(browser.read_html(reference['space_url']), reference['space_url'])
            if page['uid'] != uid:
                raise ValueError('profile_identity_mismatch')
            profile = page
            threads = _paged_rows(browser, reference['threads_url'], parse_profile_threads, 'threads',
                                  uid, 'thread', limit, list_pages)
            favorites_url = f"{SITE}/bbs/home.php?mod=space&uid={uid}&do=favorite&view=me&type=thread"
            favorites = _paged_rows(browser, favorites_url, parse_favorites, 'favorites',
                                    uid, 'favorite', limit, list_pages)
            saved = browser.rpc('favorite.getFavorites') or {}
            forums = [{'id': item['id'], 'name': html_unescape(str(item.get('name', '')))}
                      for item in saved.get('forums', []) if isinstance(item, dict) and type(item.get('id')) is int]
            tags = [{'id': item['id'], 'name': html_unescape(str(item.get('name', '')))}
                    for item in saved.get('tags', []) if isinstance(item, dict) and type(item.get('id')) is int]
    except Exception as caught:
        error = format_error(caught)
    read_any = threads['pages_fetched'] or favorites['pages_fetched']
    status = RunStatus.COMPLETE if read_any and not error else (RunStatus.NEEDS_ATTENTION if read_any or profile else RunStatus.FAILED)
    sections = ['threads', 'favorites'] + (['favorite_forums', 'favorite_tags'] if forums is not None else [])
    return {'status': status, 'uid': uid, 'profile': profile, 'sections': sections,
            **{k: (v and not error) if k == 'pagination_complete' else v for k, v in threads.items()},
            'favorites': {**favorites, 'pagination_complete': favorites['pagination_complete'] and not error},
            'favorite_forums': forums, 'favorite_tags': tags,
            'scope': 'currently_visible_content', 'error': error}


def _tag_listing_reference(value):
    """A company tag page on this site; the only listing form whose rows are interview threads."""
    text = str(value).strip()
    parts = urlsplit(text)
    if parts.hostname != SITE_HOST or not re.fullmatch(r'/bbs/tag/[A-Za-z0-9._%-]+\.html', parts.path):
        raise ValueError('invalid_listing_reference')
    return f'{SITE}{parts.path}'


def _collection_params(company, query, listing, limit, list_pages, max_thread_pages, refresh):
    """The checks every collection shares, applied once so a task freezes exactly what a direct call runs."""
    if not isinstance(company, str) or not company.strip() or company != company.strip():
        raise ValueError('invalid_company_label')
    if query is not None and listing is not None:
        raise ValueError('invalid_discovery_input')
    if type(refresh) is not bool or type(max_thread_pages) is not int or not 1 <= max_thread_pages <= THREAD_PAGES_MAX:
        raise ValueError('invalid_collection_budget')
    if listing is not None:
        _tag_listing_reference(listing)
    return {'company': company, 'query': query, 'listing': listing, 'limit': min(max(int(limit), 1), COLLECT_MAX),
            'list_pages': min(max(int(list_pages), 1), LIST_PAGES_MAX), 'max_thread_pages': max_thread_pages, 'refresh': refresh}


def collect_company(company, query=None, listing=None, limit=COLLECT_LIMIT, list_pages=LIST_PAGES,
                    max_thread_pages=THREAD_PAGES, refresh=False, candidates=None, start_index=0, control=None):
    """One bounded interview collection for a caller-named company (issue #21).

    Discovery is either this site's own search or a company tag page. What each candidate got labelled
    with follows the evidence, never the request: a row on the company's tag page is attributed to it;
    a search hit is attributed only when the company name is in the title, otherwise it is kept
    unlabelled and reported as unconfirmed for a person to decide.

    A persisted task (issues #37, #38) drives the same function: it passes the candidates it froze on
    its first run, the index to continue from, and a `control` whose checkpoints heartbeat and honour a
    pause before any new page request; a pause is reported, not treated as failure.
    """
    limit = min(max(int(limit), 1), COLLECT_MAX)
    list_pages = min(max(int(list_pages), 1), LIST_PAGES_MAX)
    results, found_candidates, discovery, db, account, error, paused, selected = [], {}, None, None, None, None, False, []
    try:
        _collection_params(company, query, listing, limit, list_pages, max_thread_pages, refresh)
        if candidates is not None:
            discovery = {'source': 'task', 'candidates': len(candidates), 'selected': len(candidates)}
        elif listing is not None:
            url = _tag_listing_reference(listing)
            discovery = {'source': 'listing', 'url': url}
        else:
            query = company if query is None else query
            found = search_threads(query, limit=limit, list_pages=list_pages)
            if found['error']:
                raise ValueError(found['error'])
            discovery = {'source': 'site_search', 'query': query, 'truncated_reason': found['truncated_reason']}
            for item in found['threads']:
                matched = company.casefold() in item['title'].casefold()
                found_candidates.setdefault(item['tid'], {**item, 'company_match': 'title' if matched else 'unconfirmed'})
        db = Library()
        with Browser() as browser:
            if candidates is None and listing is not None:
                seen_lists = set()
                for _ in range(list_pages):
                    if not url or url in seen_lists:
                        break
                    seen_lists.add(url)
                    page = parse_listing(browser.read_html(url), url)
                    for item in page['threads']:
                        found_candidates.setdefault(item['tid'], {**item, 'company_match': 'tag'})
                    if len(found_candidates) >= limit:
                        break
                    url = page['next_url']
                    browser.sb.sleep(1)
            if candidates is None:
                selected = list(found_candidates.values())[:limit]
                discovery.update(candidates=len(found_candidates), selected=len(selected))
                db.enqueue(selected, company)
                if control is not None:
                    control.freeze(selected, discovery)  # the task's candidates never drift after this point
            else:
                selected = list(candidates)
            for item in selected[start_index:]:
                if control is not None:
                    control.checkpoint()
                attributed = item['company_match'] in ('tag', 'title')
                label = company if attributed else None
                summary = {'tid': item['tid'], 'company_match': item['company_match'],
                           'discovery': {k: v for k, v in discovery.items() if k in ('source', 'url', 'query')}}
                cached = db.get(item['tid'])
                saved_pages = db.progress(item['tid'])
                if cached and not refresh and not saved_pages and cached['pagination_complete']:
                    results.append({**summary, 'status': CollectionStatus.CACHED, 'complete': cached['complete']})
                    if control is not None:
                        control.record(item['tid'], failed=False)  # reused is processed too; the index moves on
                    continue
                pages = saved_pages if not refresh and saved_pages and saved_pages[-1].get('next_url') else []
                if not pages:
                    db.save_progress(item['tid'], [])

                def keep(current_pages, item=item, label=label, attributed=attributed, evidence=summary['discovery']):
                    current = merge_pages(current_pages, label)
                    current['listed_date'] = item.get('listed_date')
                    current['company_source'] = item['company_match'] if attributed else None
                    current['discovery'] = evidence
                    db.save_page(current, current_pages)

                try:
                    _read_thread_pages(browser, item['tid'], pages, max_thread_pages, on_page=keep, control=control)
                    record = merge_pages(pages, label)
                    state = record_state(record)
                    if record['pagination_complete']:
                        db.save_progress(item['tid'], [])
                    results.append({**summary, 'title': record['title'], 'status': state,
                                    'posts': len(record['posts']), 'complete': record['complete']})
                    db.set_queue_state(item['tid'], state)
                    if control is not None:
                        control.record(item['tid'], failed=False)
                except PauseRequested:
                    raise  # the pages read so far are saved; the thread continues from them when resumed
                except Exception as caught:
                    reason = format_error(caught)
                    db.set_queue_state(item['tid'], CollectionStatus.FAILED, reason)
                    results.append({**summary, 'status': CollectionStatus.FAILED, 'error': reason})
                    if control is not None:
                        control.record(item['tid'], failed=True)
            account = browser.account()
    except PauseRequested:
        paused = True
    except Exception as caught:
        error = format_error(caught)
    finally:
        stats = db.stats() if db is not None else None
        if db is not None:
            db.close()
    if paused:
        status = RunStatus.NEEDS_ATTENTION
    else:
        status = collection_status(results) if not error else (RunStatus.NEEDS_ATTENTION if results else RunStatus.FAILED)
    return {'status': status, 'company': company if isinstance(company, str) else None, 'discovery': discovery,
            'results': results, 'stats': stats, 'account': account, 'scope': 'currently_visible_content', 'paused': paused,
            'error': error or (None if paused else 'no_matching_interview_threads' if not results else None)}


def collect_stripe(limit=COLLECT_LIMIT, list_pages=LIST_PAGES, max_thread_pages=THREAD_PAGES, refresh=False):
    """Compatibility entry: the original fixed collection is the generic one pointed at the Stripe tag."""
    return collect_company(COLLECT_COMPANY, listing=SITE + COLLECT_TAG, limit=limit, list_pages=list_pages,
                           max_thread_pages=max_thread_pages, refresh=refresh)


def export_library(destination=None, company=COLLECT_COMPANY):
    destination = Path(destination) if destination else EXPORT_DIRECTORY
    destination.mkdir(parents=True, exist_ok=True)
    db = Library()
    try:
        records = db.search(company=company, limit=SEARCH_MAX)
        archived_files = _attach_media(records, db, destination)
    finally:
        db.close()
    payload = {'company': company, 'exported_at': datetime.now(timezone.utc).isoformat(), 'records': records,
               'archived_files': archived_files,
               'notes': '仅保存当前账号可见内容；隐藏部分有标记；已归档的图片与附件随导出放在媒体目录，其余只保留来源链接。摘要和标签按原文规则抽取。'}
    (destination / EXPORT_FILES['json']).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    with (destination / EXPORT_FILES['csv']).open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.writer(file)
        writer.writerow(['公司', '标题', '岗位', '级别', '标签', '正文状态', '分页完整', '帖子和回复数', '来源', '摘要'])
        for record in records:
            cells = [company, record['title'], record['role'], record['level'], ', '.join(record['tags']), record['content_status'], record['pagination_complete'], len(record['posts']), record['url'], record['summary']]
            writer.writerow(["'" + str(value) if str(value).startswith(('=', '+', '-', '@')) else value for value in cells])
    lines = [f'# {company} 面经资料', '', payload['notes'], '']
    for record in records:
        lines += [f"## {record['title']}", '', f"来源：{record['url']}", f"正文：{record['content_status']}；分页完整：{record['pagination_complete']}；岗位：{record['role']}；标签：{', '.join(record['tags'])}", '']
        for post in record['posts']:
            fence = '`' * max(3, 1 + max((len(run) for run in re.findall(r'`+', post['text'])), default=0))
            author = post.get('author') or {}
            byline = f"作者：{author.get('name') or '未知'}" + (f"（uid {author['uid']}）" if author.get('uid') else '') if author else None
            lines += [f"### 内容 {post['pid']}", ''] + ([byline, ''] if byline else []) + [fence + 'text', post['text'], fence, '']
            if post.get('images'):
                lines += ['图片：'] + [f"![{image.get('alt') or ''}]({image['local']})（来源 {image['url']}）" if image.get('local')
                                    else f"{image['url']}（未归档）" for image in post['images']] + ['']
                for image in post['images']:
                    if image.get('recognized'):
                        lines += ['图片识别文本（机器识别，非原文，可能有误）：', '', '> ' + image['recognized']['text'].replace('\n', '\n> '), '']
            if post.get('attachments'):
                lines += ['附件：'] + [f"[{item.get('name') or item['url']}]({item['local']})" if item.get('local')
                                    else f"{item.get('name') or item.get('url')}（{'受限' if item.get('restricted') else '未归档'}）"
                                    for item in post['attachments']] + ['']
    (destination / EXPORT_FILES['markdown']).write_text('\n'.join(lines), encoding='utf-8')
    (destination / EXPORT_FILES['reader']).write_text(render_reader(payload), encoding='utf-8')
    return {'directory': str(destination), 'threads': len(records), 'complete': sum(row['complete'] for row in records)}


def load_learned_answers(path):
    """Answers this account proved on the site, deliberately outside the tracked bank (issue #66).

    Reading is best effort: a damaged cache costs a re-lookup, which is the behaviour before any of
    this existed, so it must never stop the day's check-in. The shipped bank keeps working.
    """
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        verified, rejected = value['verified'], value['rejected']
        if not isinstance(verified, dict) or not isinstance(rejected, dict):
            raise ValueError
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError):
        return {'verified': {}, 'rejected': {}}
    return {'verified': {str(k): str(v) for k, v in verified.items()},
            'rejected': {str(k): [str(item) for item in v] for k, v in rejected.items()
                         if isinstance(v, list)}}


def record_answer_outcome(question, answer, path, *, rewarded, completed, response_seen):
    """Only reward-backed evidence teaches; an unanswered request teaches nothing either way.

    A wrong entry that lands in `verified` would cost rice every time the question returns, so it
    takes a confirmed reward. `rejected` only narrows the next lookup, so a stale one degrades to
    the behaviour we already had.
    """
    path = Path(path)
    if not question or not answer or not response_seen:
        return None
    learned = load_learned_answers(path)
    if rewarded:
        learned['verified'][question] = answer
        learned['rejected'].pop(question, None)
    elif completed:
        wrong = learned['rejected'].setdefault(question, [])
        if answer in wrong or learned['verified'].get(question) == answer:
            return learned
        wrong.append(answer)
    else:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(learned, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)  # Atomic on both Windows and POSIX.
    return learned


# --- persisted collection tasks: creation, the local executor, status and control (issues #37, #38) ---

TASK_KIND_COLLECT = 'collect_company'


class _TaskExecution:
    """The executor's side of one task: every safe point heartbeats with the current progress and
    honours a pause the owner asked for in the meantime. Progress is what the site work confirmed."""

    def __init__(self, task_id, progress):
        self.task_id = task_id
        self.progress = progress

    def _sync(self):
        db = Library()
        try:
            db.task_heartbeat(self.task_id, self.progress)
            return db.task_control(self.task_id)
        finally:
            db.close()

    def freeze(self, selected, discovery):
        self.progress.update(discovered=discovery.get('candidates'), selected=list(selected), discovery=discovery, next_index=0)
        self._sync()

    def checkpoint(self):
        if self._sync() == TaskControl.PAUSE:
            raise PauseRequested()

    def record(self, tid, failed):
        self.progress['failed' if failed else 'processed'].append(tid)
        self.progress['next_index'] = self.progress.get('next_index', 0) + 1
        self._sync()


def _task_result(task, error=None):
    view = task_view(task, datetime.now(timezone.utc)) if task else None
    status = RunStatus.FAILED if error else RunStatus.COMPLETE
    return {'status': status, 'task': view, 'error': error}


def create_collection_task(company, query=None, listing=None, limit=COLLECT_LIMIT, list_pages=LIST_PAGES,
                           max_thread_pages=THREAD_PAGES, refresh=False):
    """Persist one bounded collection as a task with its parameters frozen; nothing runs yet.

    The checks are the same ones a direct collection applies, so a task cannot hold parameters the
    collection would refuse, and the frozen values are the ones every later run uses.
    """
    try:
        params = _collection_params(company, query, listing, limit, list_pages, max_thread_pages, refresh)
    except ValueError as caught:
        return _task_result(None, str(caught))
    db = Library()
    try:
        task = db.create_task('task-' + uuid4().hex[:12], TASK_KIND_COLLECT, params, ACCOUNT_UID)
    finally:
        db.close()
    return _task_result(task)


def run_task(task_id):
    """Execute one task in this process until it completes, fails, or reaches a pause the owner asked for.

    The task is claimed first (one live executor at a time, across tasks), then driven through the
    ordinary collection with the candidates it froze and the index it reached; the browser lock keeps
    two collections from running at once even if two executors were started by mistake.
    """
    worker = f'{os.getpid()}:{uuid4().hex[:8]}'
    db = Library()
    try:
        task = db.claim_task(task_id, worker)
    except ValueError as caught:
        current = db.task(task_id)
        db.close()
        return _task_result(current, str(caught))
    finally:
        try:
            db.close()
        except Exception:
            pass
    progress = task['progress']
    execution = _TaskExecution(task_id, progress)
    error = None
    state = TaskState.FAILED
    outcome = None
    try:
        if task['kind'] != TASK_KIND_COLLECT:
            raise ValueError('unsupported_task_kind')
        params = task['params']
        candidates = progress['selected'] or None
        outcome = collect_company(params['company'], query=params['query'], listing=params['listing'], limit=params['limit'],
                                  list_pages=params['list_pages'], max_thread_pages=params['max_thread_pages'],
                                  refresh=params['refresh'], candidates=candidates, start_index=progress.get('next_index', 0),
                                  control=execution)
        if outcome['paused']:
            state = TaskState.PAUSED
        elif outcome['status'] == RunStatus.FAILED:
            state, error = TaskState.FAILED, outcome['error']
        else:
            state = TaskState.COMPLETE
    except Exception as caught:
        error = format_error(caught)
    summary = None
    if outcome is not None:
        summary = {key: outcome.get(key) for key in ('status', 'discovery', 'results', 'stats', 'error')}
    db = Library()
    try:
        finished = db.finish_task(task_id, state, execution.progress, result=summary, error=error)
    finally:
        db.close()
    return _task_result(finished, error)


def task_status(task_id):
    db = Library()
    try:
        task = db.task(task_id)
    finally:
        db.close()
    return _task_result(task, None if task else 'task_not_found')


def list_tasks(limit=TASK_LIST_LIMIT):
    limit = min(max(int(limit), 1), 200)
    db = Library()
    try:
        now = datetime.now(timezone.utc)
        return {'status': RunStatus.COMPLETE, 'tasks': [task_view(task, now) for task in db.tasks(limit)], 'error': None}
    finally:
        db.close()


def control_task(task_id, action):
    """Ask a task to pause at its next safe point, or put a paused task back in the queue.

    Neither starts an executor: a resumed task waits for the next `run_task`. Asking twice is harmless
    and says so; finished or unknown tasks are refused by name.
    """
    control = {'pause': TaskControl.PAUSE, 'resume': TaskControl.RUN}.get(action)
    if control is None:
        return _task_result(None, 'invalid_task_action')
    db = Library()
    try:
        before = db.task(task_id)
        if before is None:
            return _task_result(None, 'task_not_found')
        try:
            task = db.set_task_control(task_id, control)
        except ValueError as caught:
            return _task_result(before, str(caught))
    finally:
        db.close()
    result = _task_result(task)
    result['changed'] = before['control'] != task['control'] or before['state'] != task['state']
    return result


# --- media archive (issue #39): download what a saved thread shows, keep an index beside the record ---

def _media_references(record):
    """Every image and attachment a saved record points at, in post order, with the reason a restricted
    attachment is left alone. Videos are references to players, not files, and are not downloaded."""
    references = []
    for post in record.get('posts') or []:
        for image in post.get('images') or []:
            if image.get('url'):
                references.append({'url': image['url'], 'pid': post['pid'], 'kind': 'image', 'name': image.get('alt') or None, 'restricted': False})
        for attachment in post.get('attachments') or []:
            if attachment.get('url'):
                references.append({'url': attachment['url'], 'pid': post['pid'], 'kind': 'attachment',
                                   'name': attachment.get('name'), 'restricted': bool(attachment.get('restricted'))})
    seen, unique = set(), []
    for reference in references:
        if reference['url'] not in seen:
            seen.add(reference['url'])
            unique.append(reference)
    return unique


def _media_filename(sha256, mime, url):
    """A name derived from the content hash and a vetted extension: nothing from the page can steer the path."""
    extension = MEDIA_EXTENSIONS.get(mime)
    if extension is None:
        guess = Path(urlsplit(url).path).suffix.lower().lstrip('.')
        extension = guess if guess in set(MEDIA_EXTENSIONS.values()) | {'jpeg', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'} else 'bin'
    return f'{sha256[:24]}.{extension}'


def archive_media(thread, limit=MEDIA_MAX_PER_THREAD):
    """Download the media a saved thread shows into the archive directory and index it beside the record.

    Only references the saved record already carries are considered; restricted attachments stay missing
    by reason, hosts other than the site's own are skipped by reason, and a file over MEDIA_MAX_BYTES is
    refused before it is read in full. Identical content is stored once (by hash) and reused. Nothing here
    touches the record's text or the site's state; a failed download is a row with its reason, not a
    damaged record. Repeating the call re-fetches only what previously failed.
    """
    outcomes, tid, error = [], None, None
    try:
        if type(limit) is not int or not 1 <= limit <= MEDIA_MAX_PER_THREAD:
            raise ValueError('invalid_media_budget')
        tid = parse_thread_reference(thread, first_page=True)['tid']
        db = Library()
        try:
            record = db.get(tid)
            if record is None:
                raise ValueError('thread_not_in_library')
            references = _media_references(record)
            existing = {row['url']: row for row in db.media_for(tid)}
            todo = [ref for ref in references if existing.get(ref['url'], {}).get('outcome') not in (MediaOutcome.ARCHIVED, MediaOutcome.REUSED)]
            for row in existing.values():
                if row['outcome'] in (MediaOutcome.ARCHIVED, MediaOutcome.REUSED):
                    outcomes.append({**row, 'outcome': MediaOutcome(row['outcome'])})
            directory = MEDIA_DIRECTORY / str(tid)
            budget = limit
            browser = None
            try:
                for reference in todo:
                    entry = {'url': reference['url'], 'pid': reference['pid'], 'kind': reference['kind'], 'name': reference['name'],
                             'sha256': None, 'path': None, 'bytes': None, 'mime': None, 'error': None,
                             'fetched_at': datetime.now(timezone.utc).isoformat()}
                    if reference['restricted']:
                        entry.update(outcome=MediaOutcome.SKIPPED_RESTRICTED, error='attachment_restricted')
                    elif urlsplit(reference['url']).hostname not in MEDIA_HOSTS:
                        entry.update(outcome=MediaOutcome.SKIPPED_EXTERNAL, error='external_host_not_downloaded')
                    elif budget <= 0:
                        entry.update(outcome=MediaOutcome.SKIPPED_LIMIT, error='media_budget_exhausted')
                    else:
                        budget -= 1
                        try:
                            if browser is None:
                                browser = Browser()
                                browser.__enter__()
                            fetched = browser.read_bytes(reference['url'], MEDIA_MAX_BYTES)
                            digest = hashlib.sha256(fetched['data']).hexdigest()
                            known = db.media_by_hash(digest)
                            if known and Path(known['path']).is_file():
                                entry.update(outcome=MediaOutcome.REUSED, sha256=digest, path=known['path'], bytes=known['bytes'], mime=known['mime'])
                            else:
                                directory.mkdir(parents=True, exist_ok=True)
                                target = directory / _media_filename(digest, fetched['mime'], reference['url'])
                                if not target.resolve().is_relative_to(MEDIA_DIRECTORY.resolve()):
                                    raise RuntimeError('media_path_outside_archive')
                                target.write_bytes(fetched['data'])
                                entry.update(outcome=MediaOutcome.ARCHIVED, sha256=digest, path=str(target), bytes=len(fetched['data']), mime=fetched['mime'])
                        except Exception as caught:
                            entry.update(outcome=MediaOutcome.FAILED, error=format_error(caught))
                    db.save_media(tid, entry)
                    outcomes.append(entry)
            finally:
                if browser is not None:
                    browser.__exit__(None, None, None)
        finally:
            db.close()
    except Exception as caught:
        error = format_error(caught)
    counts = {}
    for entry in outcomes:
        counts[str(entry['outcome'])] = counts.get(str(entry['outcome']), 0) + 1
    failed = any(entry['outcome'] == MediaOutcome.FAILED for entry in outcomes)
    status = RunStatus.FAILED if error else (RunStatus.NEEDS_ATTENTION if failed else RunStatus.COMPLETE)
    return {'status': status, 'tid': tid, 'directory': str(MEDIA_DIRECTORY / str(tid)) if tid else None, 'counts': counts,
            'media': [{**entry, 'outcome': str(entry['outcome'])} for entry in outcomes],
            'scope': 'currently_visible_content', 'error': error}


def _attach_media(records, db, destination):
    """For an export: copy archived files next to the export and point each image/attachment at its copy.

    Only the exported payload changes; the records in the library keep their source URLs. A reference
    without an archived file keeps `local=None`, so the reader can say so instead of showing a broken image.
    """
    media_dir = destination / EXPORT_MEDIA_DIRNAME
    copied = 0
    for record in records:
        index = {row['url']: row for row in db.media_for(record['tid']) if row['path'] and row['outcome'] in (MediaOutcome.ARCHIVED, MediaOutcome.REUSED)}
        for post in record.get('posts') or []:
            for item in list(post.get('images') or []) + list(post.get('attachments') or []):
                row = index.get(item.get('url'))
                item['local'] = None
                item['recognized'] = None
                if row and Path(row['path']).is_file():
                    media_dir.mkdir(parents=True, exist_ok=True)
                    target = media_dir / Path(row['path']).name
                    if not target.exists():
                        shutil.copyfile(row['path'], target)
                        copied += 1
                    item['local'] = f'{EXPORT_MEDIA_DIRNAME}/{target.name}'
                    recognized = db.media_text(row['sha256'], OCR_ENGINE_NAME) if row.get('sha256') else None
                    if recognized and recognized['lines']:
                        # Machine-read text travels beside the image, labelled as such; it never joins the post text.
                        item['recognized'] = {'text': recognized['text'], 'engine': recognized['engine'], 'lines': recognized['lines']}
    return copied


# --- recognised text for archived images (issue #40): a layer beside the media, never the author's words ---

def _ocr_engine():
    """The local OCR engine, loaded on first use; None when the optional package is not installed."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return None
    engine = RapidOCR()

    def recognise(path):
        result, _ = engine(str(path))
        return [(str(item[1]), float(item[2])) for item in (result or []) if len(item) >= 3]
    return recognise


def recognize_media(thread, limit=OCR_MAX_IMAGES):
    """Run local OCR over the images already archived for one saved thread and keep the text beside them.

    Recognition is keyed by the image's content hash, so the same image is never read twice and identical
    images share one reading. Lines under OCR_MIN_SCORE are dropped; an image with nothing readable is
    recorded as such and yields no text at all (no entry is invented from a blank or blurry picture).
    The thread's own text is never touched; the reading is reported and searched as `recognized_text`.
    """
    outcomes, tid, error = [], None, None
    try:
        if type(limit) is not int or not 1 <= limit <= OCR_MAX_IMAGES:
            raise ValueError('invalid_ocr_budget')
        tid = parse_thread_reference(thread, first_page=True)['tid']
        db = Library()
        try:
            if db.get(tid) is None:
                raise ValueError('thread_not_in_library')
            images = [row for row in db.media_for(tid) if row['kind'] == 'image' and row['path'] and row['sha256']
                      and row['outcome'] in (MediaOutcome.ARCHIVED, MediaOutcome.REUSED)]
            if not images:
                raise ValueError('no_archived_images')
            engine = None
            budget = limit
            done_hashes = set()
            for row in images:
                entry = {'url': row['url'], 'pid': row['pid'], 'sha256': row['sha256'], 'path': row['path'], 'lines': 0, 'chars': 0,
                         'mean_score': None, 'engine': OCR_ENGINE_NAME, 'error': None}
                known = db.media_text(row['sha256'], OCR_ENGINE_NAME)
                if known is not None or row['sha256'] in done_hashes:
                    known = known or db.media_text(row['sha256'], OCR_ENGINE_NAME)
                    entry.update(outcome='reused', lines=known['lines'], chars=len(known['text'] or ''), mean_score=known['mean_score'])
                elif budget <= 0:
                    entry.update(outcome='skipped_limit', error='ocr_budget_exhausted')
                elif not Path(row['path']).is_file():
                    entry.update(outcome='failed', error='archived_file_missing')
                else:
                    budget -= 1
                    if engine is None:
                        engine = _ocr_engine()
                        if engine is None:
                            raise RuntimeError('ocr_unavailable')
                    try:
                        lines = [(text.strip(), score) for text, score in engine(row['path']) if text.strip() and score >= OCR_MIN_SCORE]
                        text = '\n'.join(text for text, _ in lines)
                        mean = round(sum(score for _, score in lines) / len(lines), 3) if lines else None
                        db.save_media_text(row['sha256'], OCR_ENGINE_NAME, text, len(lines), mean)
                        done_hashes.add(row['sha256'])
                        entry.update(outcome='recognized' if lines else 'no_text', lines=len(lines), chars=len(text), mean_score=mean)
                    except Exception as caught:
                        entry.update(outcome='failed', error=format_error(caught))
                outcomes.append(entry)
        finally:
            db.close()
    except Exception as caught:
        error = format_error(caught)
    counts = {}
    for entry in outcomes:
        counts[entry['outcome']] = counts.get(entry['outcome'], 0) + 1
    failed = any(entry['outcome'] == 'failed' for entry in outcomes)
    status = RunStatus.FAILED if error else (RunStatus.NEEDS_ATTENTION if failed else RunStatus.COMPLETE)
    return {'status': status, 'tid': tid, 'engine': OCR_ENGINE_NAME, 'counts': counts, 'images': outcomes,
            'note': 'recognized_text_is_machine_read_not_the_original', 'error': error}
