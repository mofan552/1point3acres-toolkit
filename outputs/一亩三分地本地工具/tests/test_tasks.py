import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import cli
import mcp_server
import library
from contracts import task_view
from library import Library, create_collection_task, run_task, task_status, control_task, list_tasks
from settings import TASK_HEARTBEAT_STALE_SECONDS

COMPANY = 'Synthetic Co'


def thread_url(tid, page=1):
    return f'https://www.1point3acres.com/bbs/thread-{tid}-{page}-1.html'


def page_of(tid, page, last):
    return {'tid': tid, 'title': f'{COMPANY} {tid}', 'url': thread_url(tid, page), 'expected_posts': last, 'page': page,
            'posts': [{'pid': tid * 10 + page, 'text': f'body {tid} page {page}', 'restricted': False}],
            'next_url': None if page >= last else thread_url(tid, page + 1)}


class PagedBrowser:
    """Three threads of two pages each; `pause_after` reads makes the owner ask for a pause mid-run."""

    def __init__(self, pages=2, pause_after=None, pause_call=None):
        self.pages, self.pause_after, self.pause_call = pages, pause_after, pause_call
        self.reads, self.entered, self.exited = [], 0, 0
        self.sb = self

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *_):
        self.exited += 1

    def sleep(self, _):
        pass

    def account(self):
        return {'uid': 1}

    def read_html(self, url):
        if 'tag/' in url:
            return 'listing'
        self.reads.append(url)
        if self.pause_after is not None and len(self.reads) == self.pause_after:
            self.pause_call()
        tid = int(url.split('thread-')[1].split('-')[0])
        page = int(url.split('thread-')[1].split('-')[1])
        return page_of(tid, page, self.pages)


class TaskTests(unittest.TestCase):
    """A collection becomes a task that survives the client, runs one at a time, and pauses safely (#37, #38)."""

    LISTING = [{'tid': tid, 'title': f'{COMPANY} {tid}', 'url': thread_url(tid)} for tid in (101, 102, 103)]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'tasks.sqlite'

    def patched(self, browser, *, rediscovery_allowed=True):
        stack = contextlib.ExitStack()
        stack.enter_context(patch('library.Library', side_effect=lambda: Library(self.path)))
        stack.enter_context(patch('library.Browser', return_value=browser))
        stack.enter_context(patch('library.parse_thread', side_effect=lambda html, url: html))
        if rediscovery_allowed:
            stack.enter_context(patch('library.parse_listing', return_value={'threads': self.LISTING, 'next_url': None}))
        else:
            stack.enter_context(patch('library.parse_listing', side_effect=AssertionError('a resumed task must not rediscover')))
        return stack

    def create(self, **kwargs):
        with self.patched(PagedBrowser()):
            return create_collection_task(COMPANY, listing='https://www.1point3acres.com/bbs/tag/synthetic-1-1.html', **kwargs)

    def saved(self):
        db = Library(self.path)
        try:
            return {row['tid']: (len(row['posts']), row['pagination_complete']) for row in db.search('', company=COMPANY, limit=50)}
        finally:
            db.close()

    def test_a_task_freezes_its_parameters_and_can_be_read_back_by_id(self):
        created = self.create(limit=2, max_thread_pages=3)
        self.assertEqual(created['status'], 'complete')
        task_id = created['task']['task_id']
        self.assertEqual((created['task']['state'], created['task']['diagnosis']), ('queued', 'waiting_for_executor'))
        with patch('library.COLLECT_LIMIT', 99), patch('library.THREAD_PAGES', 99), self.patched(PagedBrowser()):
            again = task_status(task_id)
        self.assertEqual((again['task']['params']['limit'], again['task']['params']['max_thread_pages']), (2, 3))
        with self.patched(PagedBrowser()):
            self.assertEqual(task_status('task-nope')['error'], 'task_not_found')
            self.assertEqual(create_collection_task('', listing='x')['error'], 'invalid_company_label')
            self.assertEqual([t['task_id'] for t in list_tasks()['tasks']], [task_id])

    def test_running_freezes_the_candidates_processes_them_and_completes_with_matching_records(self):
        task_id = self.create(limit=2)['task']['task_id']  # the listing offers three; the frozen limit takes two
        browser = PagedBrowser()
        with self.patched(browser), patch('library.COLLECT_LIMIT', 12):
            result = run_task(task_id)
        task = result['task']
        self.assertEqual((result['status'], task['state'], task['diagnosis']), ('complete', 'complete', 'complete'))
        self.assertEqual([c['tid'] for c in task['progress']['selected']], [101, 102])
        self.assertEqual((task['progress']['processed'], task['progress']['failed'], task['progress']['next_index']), ([101, 102], [], 2))
        self.assertEqual(task['result']['status'], 'complete')
        self.assertEqual(self.saved(), {101: (2, True), 102: (2, True)})
        self.assertEqual(len(browser.reads), 4)
        self.assertEqual((browser.entered, browser.exited), (1, 1), 'the browser is released when the task ends')

    def test_a_pause_stops_before_the_next_page_keeps_saved_pages_and_resumes_without_repeating(self):
        task_id = self.create(limit=3)['task']['task_id']

        def ask_pause():
            with self.patched(PagedBrowser()):
                self.assertTrue(control_task(task_id, 'pause')['changed'])
        browser = PagedBrowser(pause_after=3, pause_call=ask_pause)  # pause asked while reading 102's first page
        with self.patched(browser):
            result = run_task(task_id)
        task = result['task']
        self.assertEqual((result['status'], task['state'], task['diagnosis']), ('complete', 'paused', 'paused_at_checkpoint'))
        self.assertEqual(len(browser.reads), 3, 'no new page request after the pause was asked')
        self.assertEqual((browser.entered, browser.exited), (1, 1), 'the browser is released at the pause')
        self.assertEqual((task['progress']['processed'], task['progress']['next_index']), ([101], 1))
        self.assertEqual(self.saved()[102], (1, False), 'the page already read is saved, the thread is not complete')
        with self.patched(PagedBrowser()):
            self.assertEqual(run_task(task_id)['error'], 'task_paused')
            resumed = control_task(task_id, 'resume')
            self.assertEqual((resumed['task']['state'], resumed['changed']), ('queued', True))
            self.assertFalse(control_task(task_id, 'resume')['changed'])
        second = PagedBrowser()
        with self.patched(second, rediscovery_allowed=False):
            result = run_task(task_id)
        self.assertEqual((result['task']['state'], result['error']), ('complete', None))
        self.assertEqual(second.reads, [thread_url(102, 2), thread_url(103, 1), thread_url(103, 2)], 'continues from the saved page')
        self.assertEqual(self.saved(), {101: (2, True), 102: (2, True), 103: (2, True)})
        self.assertEqual(result['task']['progress']['processed'], [101, 102, 103])
        with self.patched(PagedBrowser()):
            self.assertEqual(control_task(task_id, 'pause')['error'], 'task_finished')
            self.assertEqual(control_task(task_id, 'dance')['error'], 'invalid_task_action')
            self.assertEqual(control_task('task-nope', 'pause')['error'], 'task_not_found')

    def test_two_tasks_never_run_at_once_and_an_abandoned_executor_is_diagnosed(self):
        first = self.create(limit=1)['task']['task_id']
        second = self.create(limit=1)['task']['task_id']
        db = Library(self.path)
        try:
            db.claim_task(first, 'other-process')  # a live executor holds the first task
        finally:
            db.close()
        with self.patched(PagedBrowser()):
            refused = run_task(second)
            self.assertEqual((refused['status'], refused['error'], refused['task']['state']), ('failed', 'another_task_is_running', 'queued'))
            self.assertEqual(run_task(first)['error'], 'task_already_running')
        # The executor died: its heartbeat ages past the limit and the task is no longer "progressing".
        db = Library(self.path)
        try:
            stale = (datetime.now(timezone.utc) - timedelta(seconds=TASK_HEARTBEAT_STALE_SECONDS + 5)).isoformat()
            db.db.execute('UPDATE tasks SET heartbeat_at=? WHERE task_id=?', (stale, first))
            db.db.commit()
        finally:
            db.close()
        with self.patched(PagedBrowser()):
            view = task_status(first)['task']
            self.assertEqual((view['state'], view['diagnosis'], view['executor_alive']), ('running', 'executor_missing', False))
            self.assertGreater(view['heartbeat_age_seconds'], TASK_HEARTBEAT_STALE_SECONDS)
            # A task without a live executor may be taken over, and the second task can now run after it.
            self.assertEqual(run_task(first)['task']['state'], 'complete')
            self.assertEqual(run_task(second)['task']['state'], 'complete')

    def test_a_failed_thread_is_counted_not_hidden(self):
        class Flaky(PagedBrowser):
            def read_html(self, url):
                if 'thread-102-' in url:
                    raise RuntimeError('thread_unavailable')
                return super().read_html(url)
        task_id = self.create(limit=3)['task']['task_id']
        with self.patched(Flaky()):
            task = run_task(task_id)['task']
        self.assertEqual((task['state'], task['progress']['processed'], task['progress']['failed']), ('complete', [101, 103], [102]))
        self.assertEqual(task['result']['status'], 'needs_attention')


class TaskViewTests(unittest.TestCase):
    def test_view_reports_liveness_from_the_heartbeat_only(self):
        now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
        base = {'task_id': 't', 'kind': 'collect_company', 'params': {}, 'progress': {}, 'created_at': 'c', 'updated_at': 'u',
                'control': 'run', 'stale_after': 60}
        fresh = task_view({**base, 'state': 'running', 'heartbeat_at': (now - timedelta(seconds=10)).isoformat()}, now)
        self.assertEqual((fresh['diagnosis'], fresh['executor_alive']), ('executing', True))
        gone = task_view({**base, 'state': 'running', 'heartbeat_at': (now - timedelta(seconds=61)).isoformat()}, now)
        self.assertEqual((gone['diagnosis'], gone['executor_alive']), ('executor_missing', False))
        never = task_view({**base, 'state': 'running', 'heartbeat_at': None}, now)
        self.assertEqual(never['diagnosis'], 'executor_missing')
        self.assertEqual(task_view({**base, 'state': 'paused', 'heartbeat_at': None}, now)['diagnosis'], 'paused_at_checkpoint')


class TaskEntryPointTests(unittest.TestCase):
    def test_cli_and_mcp_route_every_task_command(self):
        payload = {'status': 'complete', 'task': {'task_id': 'task-1', 'state': 'queued'}, 'error': None}
        cases = [(['task-create', COMPANY, '--listing', 'x', '--limit', '4'], 'cli.create_collection_task', ((COMPANY,), {'query': None, 'listing': 'x', 'limit': 4, 'list_pages': 3, 'max_thread_pages': 5, 'refresh': False})),
                 (['task-run', 'task-1'], 'cli.run_task', (('task-1',), {})),
                 (['task-status', 'task-1'], 'cli.task_status', (('task-1',), {})),
                 (['task-pause', 'task-1'], 'cli.control_task', (('task-1', 'pause'), {})),
                 (['task-resume', 'task-1'], 'cli.control_task', (('task-1', 'resume'), {})),
                 (['task-list', '--limit', '5'], 'cli.list_tasks', ((5,), {}))]
        for argv, target, expected in cases:
            with patch(target, return_value=payload) as called, patch('sys.argv', ['cli', *argv]), \
                    patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(cli.main(), 0, argv)
            self.assertEqual((called.call_args.args, called.call_args.kwargs), expected, argv)
            self.assertEqual(json.loads(out.getvalue())['task']['task_id'], 'task-1')
        for tool, arguments, target in [('create_collection_task', {'company': COMPANY}, 'mcp_server.persist_collection_task'),
                                        ('run_task', {'task_id': 'task-1'}, 'mcp_server.execute_task'),
                                        ('task_status', {'task_id': 'task-1'}, 'mcp_server.read_task'),
                                        ('control_task', {'task_id': 'task-1', 'action': 'pause'}, 'mcp_server.steer_task'),
                                        ('list_tasks', {}, 'mcp_server.read_tasks')]:
            with patch(target, return_value=payload) as called:
                result = asyncio.run(mcp_server.server.call_tool(tool, arguments))
            self.assertFalse(result.is_error, tool)
            called.assert_called_once()
