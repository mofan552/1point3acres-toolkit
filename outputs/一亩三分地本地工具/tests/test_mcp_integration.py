import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from settings import ROOT, SEARCH_LIMIT, THREAD_PAGES, SITE_SEARCH_LIMIT, LOGIN_METHOD, HISTORY_LIMIT


class MCPIntegrationTests(unittest.TestCase):
    def test_real_stdio_reads_only_synthetic_database(self):
        policy = json.loads((ROOT / 'architecture.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            package = workspace / 'outputs' / ROOT.name
            package.mkdir(parents=True)
            names = [name + '.py' for name in policy['modules']] + policy['assets'] + policy['publication']['package_files']
            for name in names:
                shutil.copy2(ROOT / name, package / name)
            page = {'tid': 123, 'title': 'Synthetic interview', 'url': 'https://example.org/thread',
                    'expected_posts': 1, 'posts': [{'pid': 1, 'text': '合成滑动窗口记录', 'restricted': False}], 'next_url': None}
            daily = {'run_id': 'synthetic-daily', 'site_day': '2026-09-10',
                     'started_at': '2026-09-10T12:00:00+00:00', 'finished_at': '2026-09-10T12:01:00+00:00',
                     'status': 'needs_attention', 'status_only': True,
                     'actions': [{'action': 'checkin', 'status': 'reward_verified'}]}
            seed = subprocess.run([sys.executable, '-c',
                'import json,sys; from governance import sync_generated; from library import Library,merge_pages; '
                'sync_generated(); db=Library(); data=json.load(sys.stdin); '
                'db.save(merge_pages([data["page"]], "Stripe")); db.save_daily(data["daily"], 0); db.close()'],
                cwd=package, input=json.dumps({'page': page, 'daily': daily}), text=True, capture_output=True, timeout=20)
            self.assertEqual(seed.returncode, 0, 'Synthetic MCP fixture setup failed')
            self.assertFalse((workspace / 'work/local-toolkit-state/account.json').exists())

            async def exercise():
                parameters = StdioServerParameters(command=sys.executable, args=[str(package / 'mcp_server.py')], cwd=str(package))
                async with Client(parameters) as client:
                    listed = await client.list_tools()
                    tools = listed.tools if hasattr(listed, 'tools') else listed
                    self.assertEqual({tool.name for tool in tools}, set(policy['public_tools']))
                    diagnostic = next(tool for tool in tools if tool.name == 'runtime_info')
                    self.assertTrue(diagnostic.annotations.read_only_hint)
                    version = await client.call_tool('runtime_info', {})
                    self.assertFalse(version.is_error)
                    version_payload = json.loads(''.join(item.text for item in version.content if hasattr(item, 'text')))
                    self.assertFalse(version_payload['restart_required'])
                    self.assertFalse(version_payload['account_configured'])
                    self.assertEqual(version_payload['loaded']['fingerprint'], version_payload['disk']['fingerprint'])
                    history = next(tool for tool in tools if tool.name == 'daily_history')
                    self.assertTrue(history.annotations.read_only_hint)
                    self.assertEqual(history.input_schema['properties']['limit']['default'], HISTORY_LIMIT)
                    daily_tool = next(tool for tool in tools if tool.name == 'daily_run')
                    self.assertFalse(daily_tool.annotations.read_only_hint)
                    self.assertEqual(daily_tool.input_schema['properties']['resume']['default'], False)
                    recovery = await client.call_tool('daily_run', {'resume': True})
                    self.assertTrue(recovery.is_error)
                    recovery_payload = json.loads(''.join(item.text for item in recovery.content if hasattr(item, 'text')))
                    self.assertEqual(recovery_payload['error'], 'account_not_configured')
                    self.assertEqual(recovery_payload['attempts'], [])
                    for arguments in [{'resume': 'false'}, {'resume': 1}, {'resume': True, 'date': '2026-09-10'}]:
                        rejected_daily = await client.call_tool('daily_run', arguments)
                        self.assertTrue(rejected_daily.is_error)
                    historical = await client.call_tool('daily_history', {'date': '2026-09-10', 'limit': 1})
                    self.assertFalse(historical.is_error)
                    historical_payload = json.loads(''.join(item.text for item in historical.content if hasattr(item, 'text')))
                    self.assertEqual(historical_payload['runs'][0]['run_id'], 'synthetic-daily')
                    self.assertTrue(historical_payload['days'][0]['actions'][0]['reward_verified'])
                    for arguments in [{'date': '2026-02-30'}, {'limit': True}, {'limit': 201}, {'account_uid': 123456}]:
                        rejected_history = await client.call_tool('daily_history', arguments)
                        self.assertTrue(rejected_history.is_error)
                    session = next(tool for tool in tools if tool.name == 'session_status')
                    self.assertEqual(session.input_schema['properties'], {})
                    self.assertTrue(session.annotations.read_only_hint)
                    diagnostic = await client.call_tool('session_status', {})
                    self.assertTrue(diagnostic.is_error)
                    session_payload = json.loads(''.join(item.text for item in diagnostic.content if hasattr(item, 'text')))
                    self.assertEqual(session_payload['session_state'], 'not_configured')
                    self.assertFalse(session_payload['session_usable'])
                    self.assertIsNone(session_payload['identity_matches'])
                    extra = await client.call_tool('session_status', {'recover_login': True})
                    self.assertTrue(extra.is_error)
                    self.assertIn('unsupported_session_arguments', ''.join(item.text for item in extra.content if hasattr(item, 'text')))
                    login = next(tool for tool in tools if tool.name == 'session_login')
                    self.assertFalse(login.annotations.read_only_hint)
                    self.assertEqual(login.input_schema['properties']['method']['default'], LOGIN_METHOD)
                    missing_account = await client.call_tool('session_login', {})
                    self.assertTrue(missing_account.is_error)
                    missing_payload = json.loads(''.join(item.text for item in missing_account.content if hasattr(item, 'text')))
                    self.assertEqual(missing_payload['session_state'], 'not_configured')
                    self.assertFalse(missing_payload['login_attempted'])
                    unsupported_login = await client.call_tool('session_login', {'method': 'wechat_qr'})
                    self.assertTrue(unsupported_login.is_error)
                    self.assertIn('unsupported_login_method', ''.join(item.text for item in unsupported_login.content if hasattr(item, 'text')))
                    plaintext = await client.call_tool('session_login', {'password': 'synthetic-only'})
                    self.assertTrue(plaintext.is_error)
                    self.assertIn('unsupported_login_arguments', ''.join(item.text for item in plaintext.content if hasattr(item, 'text')))
                    search = next(tool for tool in tools if tool.name == 'interviews_search')
                    self.assertEqual(search.input_schema['properties']['limit']['default'], SEARCH_LIMIT)
                    answer = await client.call_tool('interviews_search', {'query': '滑动窗口'})
                    self.assertFalse(answer.is_error)
                    payload = json.loads(''.join(item.text for item in answer.content if hasattr(item, 'text')))
                    self.assertEqual([record['tid'] for record in payload['records']], [123])
                    self.assertEqual(payload['stats']['threads'], 1)
                    self.assertLessEqual({'role', 'level', 'date_from', 'date_to'}, set(search.input_schema['properties']))
                    unlabelled = await client.call_tool('interviews_search', {'query': '', 'role': '未标注', 'level': '未标注'})
                    self.assertFalse(unlabelled.is_error)
                    unlabelled_payload = json.loads(''.join(item.text for item in unlabelled.content if hasattr(item, 'text')))
                    self.assertEqual(([r['tid'] for r in unlabelled_payload['records']], unlabelled_payload['matched']), ([123], 1))
                    # The synthetic record has no posting date, so any date bound excludes it.
                    dated = await client.call_tool('interviews_search', {'query': '', 'date_from': '2000-01-01'})
                    self.assertFalse(dated.is_error)
                    dated_payload = json.loads(''.join(item.text for item in dated.content if hasattr(item, 'text')))
                    self.assertEqual((dated_payload['records'], dated_payload['matched'], dated_payload['filters']['date_field']),
                                     ([], 0, 'posting_date'))
                    bad_date = await client.call_tool('interviews_search', {'query': '', 'date_to': '2026-9-9'})
                    self.assertTrue(bad_date.is_error)
                    detail = next(tool for tool in tools if tool.name == 'get_thread_detail')
                    self.assertEqual(detail.input_schema['properties']['max_thread_pages']['default'], THREAD_PAGES)
                    invalid = await client.call_tool('get_thread_detail', {'thread': 'https://example.org/thread'})
                    self.assertTrue(invalid.is_error)
                    rejected = json.loads(''.join(item.text for item in invalid.content if hasattr(item, 'text')))
                    self.assertEqual(rejected['status'], 'failed')
                    self.assertIsNone(rejected['record'])
                    boolean = await client.call_tool('get_thread_detail', {'thread': True})
                    self.assertTrue(boolean.is_error)
                    native = next(tool for tool in tools if tool.name == 'search_threads')
                    self.assertEqual(native.input_schema['properties']['limit']['default'], SITE_SEARCH_LIMIT)
                    self.assertTrue(native.annotations.read_only_hint)
                    unsupported = await client.call_tool('search_threads', {'query': 'Stripe', 'sort': 'views'})
                    self.assertTrue(unsupported.is_error)
                    self.assertIn('unsupported_search_arguments', ''.join(item.text for item in unsupported.content if hasattr(item, 'text')))
                    empty_query = await client.call_tool('search_threads', {'query': ''})
                    self.assertTrue(empty_query.is_error)
                    rejected_query = json.loads(''.join(item.text for item in empty_query.content if hasattr(item, 'text')))
                    self.assertEqual(rejected_query['error'], 'invalid_search_query')

            asyncio.run(asyncio.wait_for(exercise(), timeout=45))
