import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import settings
from governance import packaging_errors


class PackagingMetadataTests(unittest.TestCase):
    def policy(self):
        return json.loads((settings.ROOT / 'architecture.json').read_text(encoding='utf-8'))

    def test_shipped_metadata_agrees_with_the_registry(self):
        self.assertEqual(packaging_errors(settings.WORKSPACE, self.policy()), [])

    def test_each_drift_is_named(self):
        policy = self.policy()
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            self.assertTrue(any('packaging_unreadable' in e for e in packaging_errors(workspace, policy)))
            for name in ('pyproject.toml', 'server.json', 'PYPI_README.md'):
                shutil.copy2(settings.WORKSPACE / name, workspace / name)
            self.assertEqual(packaging_errors(workspace, policy), [])
            drifted = {**policy, 'dependencies': {**policy['dependencies'], 'extra': '1.0'}}
            self.assertTrue(any('packaging_dependency_drift' in e for e in packaging_errors(workspace, drifted)))
            server = json.loads((workspace / 'server.json').read_text(encoding='utf-8'))
            server['version'] = '0.0.0'
            (workspace / 'server.json').write_text(json.dumps(server), encoding='utf-8')
            self.assertTrue(any('registry_version_drift' in e for e in packaging_errors(workspace, policy)))
            (workspace / 'PYPI_README.md').write_text('no marker here\n', encoding='utf-8')
            self.assertTrue(any('registry_ownership_marker' in e for e in packaging_errors(workspace, policy)))


class InstalledLayoutTests(unittest.TestCase):
    def test_default_data_home_is_a_per_user_application_folder(self):
        with patch.object(settings, 'WINDOWS', False), patch.object(settings, 'MACOS', True):
            self.assertEqual(settings.default_data_home(),
                             Path.home() / 'Library' / 'Application Support' / settings.PACKAGE_NAME)
        local = Path('~/synthetic-local').expanduser()
        with patch.object(settings, 'WINDOWS', True), patch.dict(os.environ, {'LOCALAPPDATA': str(local)}):
            self.assertEqual(settings.default_data_home(), local / settings.PACKAGE_NAME)
        with patch.object(settings, 'WINDOWS', False), patch.object(settings, 'MACOS', False), \
                patch.dict(os.environ, {'XDG_DATA_HOME': str(local)}):
            self.assertEqual(settings.default_data_home(), local / settings.PACKAGE_NAME)

    def test_named_data_home_moves_every_writable_path(self):
        keys = ['DATA_HOME', 'STATE', 'PROFILE', 'EXPORT_DIRECTORY', 'CONFIG_FILE', 'ACCOUNT_FILE', 'MEDIA_DIRECTORY']
        code = f'import json, settings; print(json.dumps({{k: str(getattr(settings, k)) for k in {keys}}}))'
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-c', code], cwd=settings.ROOT, capture_output=True, text=True,
                                    env={**os.environ, settings.DATA_HOME_ENV: directory, 'PYTHONUTF8': '1'}, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            paths = json.loads(result.stdout)
            self.assertEqual(Path(paths['DATA_HOME']), Path(directory))
            for key in keys[1:]:
                self.assertTrue(Path(paths[key]).is_relative_to(Path(directory)), key)
        # The checkout is untouched when nothing names a data directory.
        self.assertIsNone(settings.DATA_HOME)
        self.assertEqual(settings.STATE, settings.WORKSPACE / 'work' / 'local-toolkit-state')
        self.assertEqual(settings.CONFIG_FILE, settings.ROOT / 'mcp.config.json')

    def test_client_config_names_the_console_script_only_when_installed(self):
        home = Path('~/synthetic-home').expanduser()
        with patch.object(settings, 'INSTALLED', True), patch.object(settings, 'DATA_HOME', home):
            server = settings.mcp_config()['mcpServers'][settings.MCP_NAME]
            self.assertEqual((server['command'], server['args']), (settings.PACKAGE_NAME, []))
            self.assertEqual(server['env'], {'PYTHONUTF8': '1', settings.DATA_HOME_ENV: str(home)})
        with patch.object(settings, 'INSTALLED', False), patch.object(settings, 'DATA_HOME', None):
            server = settings.mcp_config()['mcpServers'][settings.MCP_NAME]
            self.assertEqual((server['command'], server['args']), (str(settings.PYTHON), [str(settings.ROOT / 'mcp_server.py')]))
            self.assertEqual(server['env'], {'PYTHONUTF8': '1'})

    def test_entry_points_restore_the_script_layout_then_hand_over(self):
        import entry
        with patch('mcp_server.main') as serve:
            entry.main()
        serve.assert_called_once_with()
        self.assertEqual(Path(sys.path[0]).resolve(), settings.ROOT)
        with patch('cli.run') as command:
            entry.cli()
        command.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
