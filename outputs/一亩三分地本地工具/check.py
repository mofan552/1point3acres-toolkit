"""One offline delivery gate. --sync updates derived files, never site/account state."""
import argparse
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
import unittest
from pathlib import Path
from governance import check_consistency, sync_generated
from repository_checks import check_repository
from settings import ROOT, WORKSPACE, daily_schedule_summary
from contracts import RunStatus


def aggregate_success(jobs):
    return (isinstance(jobs, dict) and set(jobs) == {'static', 'unit', 'integration'}
            and all(isinstance(value, dict) and value.get('result') == 'success' for value in jobs.values()))


def tests_succeeded(count, failures, errors, skipped):
    return count > 0 and failures == 0 and errors == 0 and skipped == 0


def result_succeeded(result):
    return (result.wasSuccessful() and not result.expectedFailures and not result.unexpectedSuccesses
            and tests_succeeded(result.testsRun, len(result.failures), len(result.errors), len(result.skipped)))


def dependency_errors(policy):
    errors = []
    for package, expected in policy['dependencies'].items():
        try:
            if importlib.metadata.version(package) != expected:
                errors.append(f'installed_dependency_drift: {package}')
        except importlib.metadata.PackageNotFoundError:
            errors.append(f'missing_dependency: {package}')
    result = subprocess.run([sys.executable, '-m', 'pip', 'check'], capture_output=True, timeout=60)
    if result.returncode:
        errors.append('installed_dependency_conflict')
    return errors


def run_tests(paths):
    suite = unittest.TestSuite()
    for path in paths:
        loader = unittest.TestLoader()
        selected = loader.discover(str(ROOT / 'tests'), pattern=Path(path).name)
        if loader.errors or not selected.countTestCases():
            return {'count': 0, 'failures': 0, 'errors': 1, 'skipped': 0, 'passed': False,
                    'failed_tests': ['test_discovery_failed:' + path]}
        suite.addTests(selected)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    counts = {'count': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped)}
    return {**counts, 'passed': result_succeeded(result),
            'expected_failures': len(result.expectedFailures), 'unexpected_successes': len(result.unexpectedSuccesses),
            'failed_tests': [test.id() for test, _ in result.failures + result.errors]}


def execute(stage, sync):
    policy = json.loads((ROOT / 'architecture.json').read_text(encoding='utf-8'))
    if stage == 'aggregate':
        jobs = json.loads(os.environ.get('CI_JOB_RESULTS', '{}'))
        return {'status': RunStatus.COMPLETE if aggregate_success(jobs) else RunStatus.FAILED,
                'jobs': {name: value.get('result') for name, value in jobs.items() if isinstance(value, dict)}}
    if sync:
        sync_generated()
    source_errors = check_consistency()
    repository_errors = check_repository(WORKSPACE, ROOT, policy)
    errors = source_errors + repository_errors
    report = {'status': RunStatus.FAILED if errors else RunStatus.COMPLETE, 'violations': errors,
              'checks': {'source_and_docs': not source_errors, 'repository_and_test_layout': not repository_errors}}
    if errors or stage == 'static':
        return report
    errors = dependency_errors(policy)
    report['checks']['installed_dependencies'] = not errors
    if errors:
        return {**report, 'status': RunStatus.FAILED, 'violations': errors}
    groups = policy['ci']['tests']
    selected = [path for name, paths in groups.items() if stage == 'all' or stage == name for path in paths]
    report['tests'] = run_tests(selected)
    report['status'] = RunStatus.COMPLETE if report['tests']['passed'] else RunStatus.FAILED
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sync', action='store_true')
    parser.add_argument('--stage', choices=['all', 'static', 'unit', 'integration', 'aggregate'], default='all')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    try:
        report = execute(args.stage, args.sync)
    except Exception as error:
        report = {'status': RunStatus.FAILED, 'error_type': type(error).__name__}
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=WORKSPACE, capture_output=True, text=True, timeout=15)
    dirty = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=WORKSPACE, capture_output=True, text=True, timeout=15)
    sha = revision.stdout.strip()
    # Whoever configures a scheduler needs this, and it differs per machine and per platform.
    report.update(schema_version=1, stage=args.stage, commit=sha if len(sha) == 40 else None, source_dirty=bool(dirty.stdout.strip()),
                  platform=platform.system(), python=platform.python_version(),
                  duration_seconds=round(time.monotonic() - started, 3), account_actions=False,
                  schedule=daily_schedule_summary())
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
            summary.write(f"### {args.stage}: {report['status']}\n\nCommit: `{report['commit']}`\n\n")
            if 'tests' in report:
                summary.write(f"Tests: {report['tests']['count']}; failures: {report['tests']['failures']}; errors: {report['tests']['errors']}; skipped: {report['tests']['skipped']}\n\n")
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report['status'] == RunStatus.COMPLETE else 2


if __name__ == '__main__':
    raise SystemExit(main())
