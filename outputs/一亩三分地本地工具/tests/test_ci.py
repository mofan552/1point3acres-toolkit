import importlib
import tempfile
import unittest
from pathlib import Path


class CIPolicyTests(unittest.TestCase):
    def policy_module(self):
        return importlib.import_module('repository_checks')

    def test_unregistered_and_missing_tests_fail(self):
        module = self.policy_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tests').mkdir()
            (root / 'tests/test_new.py').write_text('')
            errors = module.test_layout_errors(root, {'unit': ['tests/test_missing.py'], 'integration': []})
            self.assertTrue(any('unregistered_test' in error for error in errors))
            self.assertTrue(any('missing_test' in error for error in errors))

    def test_duplicate_lane_and_escape_are_rejected(self):
        module = self.policy_module()
        with tempfile.TemporaryDirectory() as directory:
            errors = module.test_layout_errors(Path(directory), {'unit': ['../secret.py'], 'integration': ['../secret.py']})
            self.assertTrue(any('invalid_test_path' in error for error in errors))
            self.assertTrue(any('duplicate_test' in error for error in errors))

    def test_valid_test_layout(self):
        module = self.policy_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tests').mkdir()
            (root / 'tests/test_example.py').write_text('')
            (root / 'tests/test_second.py').write_text('')
            self.assertEqual(module.test_layout_errors(root, {'unit': ['tests/test_example.py'], 'integration': ['tests/test_second.py']}), [])
            aliases = {'unit': ['tests/test_example.py', 'tests/./test_example.py'], 'integration': ['tests/test_second.py']}
            self.assertTrue(any('invalid_test_path' in error for error in module.test_layout_errors(root, aliases)))

    def test_extra_missing_or_empty_lanes_cannot_hide_tests(self):
        module = self.policy_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tests').mkdir()
            for name in ['test_unit.py', 'test_integration.py', 'test_hidden.py']:
                (root / 'tests' / name).write_text('')
            groups = {'unit': ['tests/test_unit.py'], 'integration': ['tests/test_integration.py'], 'extra': ['tests/test_hidden.py']}
            self.assertTrue(any('invalid_test_lanes' in error for error in module.test_layout_errors(root, groups)))
            self.assertTrue(any('invalid_test_lanes' in error for error in module.test_layout_errors(root, {'unit': []})))
            for empty in ('unit', 'integration'):
                configured = {'unit': ['tests/test_unit.py'], 'integration': ['tests/test_integration.py']}
                configured[empty] = []
                self.assertTrue(any('empty_test_lane' in error for error in module.test_layout_errors(root, configured)))

    def test_forced_private_file_is_rejected(self):
        module = self.policy_module()
        policy = {'modules': {'settings': []}, 'assets': [], 'ci': {'tests': {'unit': [], 'integration': []}},
                  'publication': {'root_files': ['README.md'], 'package_files': ['architecture.json']}}
        complete = ['README.md', 'outputs/tool/settings.py', 'outputs/tool/architecture.json']
        self.assertEqual(module.inventory_errors(complete, 'outputs/tool', policy), [])
        self.assertTrue(module.inventory_errors(complete + ['work/credentials.dpapi'], 'outputs/tool', policy))
        # A registered file that is not tracked is an error too: it would exist locally and be absent in CI.
        self.assertEqual(module.inventory_errors(['README.md', 'outputs/tool/settings.py'], 'outputs/tool', policy),
                         ['missing_tracked_file: outputs/tool/architecture.json'])

    def test_secret_signatures_and_personal_paths(self):
        scan = self.policy_module().content_errors
        self.assertTrue(scan('example.txt', 'ghp_' + 'A' * 36))
        self.assertTrue(scan('example.txt', '-----BEGIN ' + 'PRIVATE KEY-----'))
        self.assertTrue(scan('example.txt', 'C:' + '/' + 'Users/example/secret'))
        self.assertEqual(scan('example.txt', 'https://example.com and your_forum_username'), [])

    def test_scanner_patterns_are_valid_source(self):
        module = self.policy_module()
        self.assertEqual(module.content_errors('scanner.py', Path(module.__file__).read_text(encoding='utf-8')), [])

    def test_expected_failure_cannot_mask_a_failing_test(self):
        import check
        class HiddenFailure(unittest.TestCase):
            @unittest.expectedFailure
            def runTest(self):
                self.fail('synthetic failure')
        result = HiddenFailure().run()
        self.assertFalse(check.result_succeeded(result))

    def test_aggregate_requires_every_job_to_succeed(self):
        import check
        for states in [{}, {'static': {'result': 'success'}},
                       {'static': {'result': 'success'}, 'unit': {'result': 'skipped'}, 'integration': {'result': 'success'}},
                       {'static': {'result': 'success'}, 'unit': {'result': 'success'}, 'integration': {'result': 'failure'}}]:
            with self.subTest(states=states):
                self.assertFalse(check.aggregate_success(states))
        self.assertTrue(check.aggregate_success({key: {'result': 'success'} for key in ('static', 'unit', 'integration')}))

    def test_empty_or_skipped_suites_cannot_pass(self):
        import check
        self.assertFalse(check.tests_succeeded(0, 0, 0, 0))
        self.assertFalse(check.tests_succeeded(2, 0, 0, 1))
        self.assertFalse(check.tests_succeeded(2, 1, 0, 0))
        self.assertTrue(check.tests_succeeded(2, 0, 0, 0))

    def test_workflow_rejects_credentials_and_unpinned_actions(self):
        scan = self.policy_module().workflow_errors
        for source in ['on:\n  pull_request_target:', 'secrets: inherit', 'continue-on-error: true',
                       'runs-on: self-hosted', 'uses: actions/checkout@main']:
            with self.subTest(source=source):
                self.assertTrue(scan(source))
        self.assertEqual(scan('uses: actions/checkout@' + 'a' * 40 + '\nuses: ./.github/actions/setup-toolkit'), [])

    def test_installed_dependency_drift_is_rejected(self):
        import check
        from unittest.mock import patch
        with patch('check.importlib.metadata.version', return_value='2.0'), patch('check.subprocess.run') as process:
            process.return_value.returncode = 0
            self.assertEqual(check.dependency_errors({'dependencies': {'example': '1.0'}}), ['installed_dependency_drift: example'])

    def test_dependency_conflicts_are_not_hidden_by_cache(self):
        import check
        from unittest.mock import patch
        with patch('check.subprocess.run') as process:
            process.return_value.returncode = 1
            self.assertEqual(check.dependency_errors({'dependencies': {}}), ['installed_dependency_conflict'])
