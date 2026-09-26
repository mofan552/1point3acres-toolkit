import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class GovernanceTests(unittest.TestCase):
    def test_runtime_fingerprint_tracks_code_but_ignores_private_files(self):
        g = self.scanner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'architecture.json').write_text(json.dumps({'modules': {'settings': []}}), encoding='utf-8')
            for name in ('settings.py', 'answers.json', 'mood-phrases.json'):
                (root / name).write_text('{}', encoding='utf-8')
            before = g.source_fingerprint(root)
            (root / 'account.json').write_text('private sentinel', encoding='utf-8')
            self.assertEqual(g.source_fingerprint(root), before)
            (root / 'settings.py').write_text('VALUE = 2', encoding='utf-8')
            self.assertNotEqual(g.source_fingerprint(root), before)

    def test_runtime_info_reports_reload_without_exposing_identity(self):
        g = self.scanner()
        with patch.object(g.settings, 'config_matches_disk', return_value=False), \
                patch.object(g.settings, 'USERNAME', 'private_identity_sentinel'), \
                patch.object(g.settings, 'ACCOUNT_UID', 987654321):
            result = g.runtime_info()
        self.assertTrue(result['restart_required'])
        self.assertTrue(result['configuration_changed'])
        self.assertNotIn('private_identity_sentinel', json.dumps(result))
        self.assertNotIn('987654321', json.dumps(result))

    def scanner(self):
        self.assertIsNotNone(importlib.util.find_spec('governance'), 'Consistency checks are missing')
        import governance
        return governance

    def scan(self, files, modules=None):
        g = self.scanner()
        policy = {'modules': modules or {'settings': [], 'contracts': [], 'rules': ['settings', 'contracts'], 'browser': ['settings'], 'mcp_server': []},
                  'assets': [], 'external_owners': {'requests': ['browser'], 'urllib.request': ['browser'], 'http.client': ['browser']}, 'public_tools': [],
                  'token_budgets': {'font-family': 1, 'font-size': 2, 'color': 2, 'space': 2, 'radius': 2, 'weight': 2, 'line': 2}}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, text in files.items():
                (root / name).write_text(text, encoding='utf-8')
            return g.scan_sources(root, policy)

    def test_registered_local_dependency_passes(self):
        errors = self.scan({'settings.py': 'VALUE = 1', 'rules.py': 'from settings import VALUE'})
        self.assertEqual(errors, [])

    def test_new_module_requires_registration(self):
        self.assertTrue(any('unregistered_module' in e for e in self.scan({'new_helper.py': 'x = 1'})))

    def test_cross_layer_dependency_is_rejected(self):
        self.assertTrue(any('forbidden_dependency' in e for e in self.scan({'rules.py': 'from browser import Browser'})))

    def test_network_dependency_cannot_move_into_rules(self):
        self.assertTrue(any('external_owner' in e for e in self.scan({'rules.py': 'import requests'})))

    def test_from_import_cannot_bypass_network_owner(self):
        for source in ['from urllib import request', 'from http import client']:
            with self.subTest(source=source):
                errors = self.scan({'rules.py': source})
                self.assertTrue(any('external_owner' in e for e in errors))

    def test_undeclared_dependency_is_rejected(self):
        self.assertTrue(any('undeclared_dependency' in e for e in self.scan({'rules.py': 'import imaginary_client'})))

    def test_copied_identity_is_rejected(self):
        with patch('settings.USERNAME', 'test_member'):
            errors = self.scan({'rules.py': 'username = "test_member"\nurl = "https://www.1point3acres.com/foo"'})
        self.assertEqual(sum('owned_configuration' in e for e in errors), 2)

    def test_new_status_alias_is_rejected(self):
        errors = self.scan({'rules.py': 'def run():\n    return {"status": "successful-ish"}'})
        self.assertTrue(any('literal_status' in e for e in errors))

    def test_dict_update_and_get_cannot_create_status_aliases(self):
        for statement in ['dict(status="successful-ish")', 'payload.update(status="successful-ish")',
                          'payload.get("status")=="successful-ish"']:
            with self.subTest(statement=statement):
                self.assertTrue(any('literal_status' in e for e in self.scan({'rules.py': statement})))

    def test_contract_branch_can_read_named_payload_fields(self):
        errors = self.scan({'rules.py': 'from contracts import RunStatus\ndef run(payload):\n    return {"status": RunStatus.COMPLETE if payload["after"]["complete"] else RunStatus.NEEDS_ATTENTION}'})
        self.assertEqual(errors, [])

    def test_duplicate_default_argument_is_rejected(self):
        errors = self.scan({'rules.py': 'def collect(limit=12):\n    return limit'})
        self.assertTrue(any('literal_default' in e for e in errors))

    def test_keyword_only_defaults_use_shared_config(self):
        errors = self.scan({'rules.py': 'def collect(*, limit=999):\n    return limit'})
        self.assertTrue(any('literal_default' in e for e in errors))

    def test_dependency_cycle_is_rejected(self):
        errors = self.scan({'rules.py': 'import browser', 'browser.py': 'import rules'}, {'rules': ['browser'], 'browser': ['rules']})
        self.assertTrue(any('dependency_cycle' in e for e in errors))

    def test_raw_css_color_font_and_spacing_are_rejected(self):
        g = self.scanner()
        errors = g.scan_styles('x{color:#123456;font-size:17px;padding:13px;}', ':root{--color-text:#000;}', {'color': 2})
        for code in ['raw_color', 'raw_typography', 'raw_spacing']:
            self.assertTrue(any(code in e for e in errors), code)

    def test_new_font_exceeding_budget_is_rejected(self):
        g = self.scanner()
        errors = g.scan_styles('', ':root{--font-a:12px;--font-b:14px;--font-c:17px;}', {'font-size': 2})
        self.assertTrue(any('token_budget' in e for e in errors))

    def test_token_file_cannot_hide_component_rules(self):
        g = self.scanner()
        errors = g.scan_styles('', ':root{--font-a:12px;} .card{font-size:37px;color:red;padding:19px;}', {'font-size': 2})
        self.assertTrue(any('token_file_structure' in e for e in errors))

    def test_mixed_font_named_border_color_and_unusual_spacing_are_rejected(self):
        g = self.scanner()
        errors = g.scan_styles('x{font:37px var(--font-family);border:1px solid red;padding:3ch;}', ':root{--font-family:system-ui;}', {'font-family': 1})
        for code in ['raw_typography', 'raw_color', 'raw_spacing']:
            self.assertTrue(any(code in e for e in errors), code)

    def test_valid_border_structure_and_token_priority_are_not_colors(self):
        g = self.scanner()
        css = 'table{border-collapse:collapse;border-style:groove;color:var(--color-text) !important;border:1px groove var(--color-text);}'
        self.assertEqual(g.scan_styles(css, ':root{--color-text:#000;}', {'color': 2}), [])

    def test_missing_export_does_not_block_static_startup(self):
        import shutil
        from unittest.mock import patch
        import settings
        g = self.scanner()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for path in settings.ROOT.iterdir():
                if path.is_file():
                    shutil.copy2(path, root / path.name)
            (root / 'README.md').write_text('[Reader](exports/missing.html)', encoding='utf-8')
            with patch.object(settings, 'EXPORT_DIRECTORY', root / 'exports'):
                self.assertEqual(g.check_consistency(root, include_reader=False), [])
                self.assertTrue(any('broken_document_link' in e for e in g.check_consistency(root)))

    def test_registered_extra_stylesheet_still_obeys_tokens(self):
        import shutil
        from unittest.mock import patch
        import settings
        g = self.scanner()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for path in settings.ROOT.iterdir():
                if path.is_file():
                    shutil.copy2(path, root / path.name)
            policy = json.loads((root / 'architecture.json').read_text(encoding='utf-8'))
            policy['assets'].append('extra.css')
            (root / 'architecture.json').write_text(json.dumps(policy), encoding='utf-8')
            (root / 'extra.css').write_text('.new{font-size:37px;}', encoding='utf-8')
            errors = g.check_consistency(root, include_reader=False)
            self.assertTrue(any('raw_typography' in e for e in errors))

    def test_undefined_style_token_is_rejected(self):
        g = self.scanner()
        errors = g.scan_styles('x{color:var(--color-missing);}', ':root{--color-text:#000;}', {'color': 2})
        self.assertTrue(any('undefined_token' in e for e in errors))

    def test_generated_files_use_repository_line_endings(self):
        g = self.scanner()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'design.md'
            content = '# Synthetic design\n\nShared contracts\n'
            path.write_bytes(content.replace('\n', '\r\n').encode('utf-8'))
            with patch.object(g, 'generated_files', return_value={path: content}):
                g.sync_generated()
                self.assertEqual(path.read_bytes(), content.encode('utf-8'))
                g.sync_generated()
                self.assertEqual(path.read_bytes(), content.encode('utf-8'))

    def test_generated_artifact_drift_is_rejected(self):
        g = self.scanner()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            path.write_text('{"command":"wrong"}', encoding='utf-8')
            self.assertTrue(g.compare_generated(path, json.dumps({'command': 'correct'})))

    def test_cli_blocks_daily_before_browser_on_failed_preflight(self):
        import contextlib
        import io
        import runpy
        from unittest.mock import patch
        import settings
        output = io.StringIO()
        with patch('governance.ensure_consistent', side_effect=RuntimeError('consistency_check_failed: fixture')), \
                patch('browser.Browser.__enter__', side_effect=AssertionError('browser must not run')), \
                patch('sys.argv', ['cli.py', 'daily']), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(settings.ROOT / 'cli.py'), run_name='__main__')
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(json.loads(output.getvalue())['error'], 'consistency_check_failed: fixture')

    def test_mcp_blocks_server_start_on_failed_preflight(self):
        import runpy
        from unittest.mock import patch
        import settings
        with patch('governance.ensure_consistent', side_effect=RuntimeError('consistency_check_failed: fixture')), \
                patch('mcp.server.mcpserver.MCPServer.run', side_effect=AssertionError('server must not start')):
            with self.assertRaisesRegex(RuntimeError, 'consistency_check_failed'):
                runpy.run_path(str(settings.ROOT / 'mcp_server.py'), run_name='__main__')
