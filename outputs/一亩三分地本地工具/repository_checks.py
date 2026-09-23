"""Repository file boundaries and explicit test lanes; no account reads."""
import re
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath


def test_layout_errors(root, groups):
    errors = []
    if set(groups) != {'unit', 'integration'}:
        errors.append('invalid_test_lanes: exactly unit and integration are required')
    errors += [f'empty_test_lane: {name}' for name, paths in groups.items() if not paths]
    registered = [path for group in groups.values() for path in group]
    for path, count in Counter(registered).items():
        parts = PurePosixPath(path).parts
        if path != PurePosixPath(path).as_posix() or len(parts) != 2 or parts[0] != 'tests' or not re.fullmatch(r'test_[a-z0-9_]+\.py', parts[-1]):
            errors.append(f'invalid_test_path: {path}')
        elif not (root / path).is_file():
            errors.append(f'missing_test: {path}')
        if count != 1:
            errors.append(f'duplicate_test: {path}')
    actual = {path.relative_to(root).as_posix() for path in (root / 'tests').rglob('*.py')}
    errors += [f'unregistered_test: {path}' for path in sorted(actual - set(registered))]
    return sorted(errors)


def inventory_errors(files, package, policy):
    metadata = policy['publication']
    package_files = list(metadata['package_files']) + list(policy['assets'])
    package_files += [name + '.py' for name in policy['modules']]
    package_files += [path for group in policy['ci']['tests'].values() for path in group]
    allowed = set(metadata['root_files']) | {package + '/' + name for name in package_files}
    # Both directions: nothing tracked outside the registry, and everything registered actually tracked.
    # A registered data file that only exists on disk passes every local test and breaks CI (mood-phrases.json).
    return ([f'unexpected_tracked_file: {path}' for path in sorted(set(files) - allowed)]
            + [f'missing_tracked_file: {path}' for path in sorted(allowed - set(files))])


def content_errors(path, content):
    patterns = {
        'credential_signature': r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)',
        'personal_absolute_path': r'(?:[A-Za-z]:[/\\]+Users[/\\]+[^/\\\s]+|[/]Users[/][^/\s]+[/]|[/]home[/][^/\s]+[/])',
    }
    return [f'{kind}: {path}:{content[:match.start()].count(chr(10)) + 1}'
            for kind, pattern in patterns.items() for match in re.finditer(pattern, content)]


def workflow_errors(content):
    errors = []
    if re.search(r'pull_request_target\s*:|secrets\s*:|secrets\.|self-hosted|continue-on-error\s*:\s*true', content):
        errors.append('unsafe_workflow_boundary')
    for action in re.findall(r'uses:\s*([^\s#]+)', content):
        if not re.fullmatch(r'actions/[a-z-]+@[0-9a-f]{40}|\./\.github/actions/[a-z-]+', action):
            errors.append('unpinned_or_unregistered_action')
    return errors


def check_repository(workspace, package_root, policy):
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=workspace, capture_output=True, timeout=15)
    if result.returncode:
        return ['git_inventory_unavailable']
    files = result.stdout.decode('utf-8').strip('\0').split('\0')
    errors = inventory_errors(files, package_root.relative_to(workspace).as_posix(), policy)
    for name in files:
        path = workspace / name
        if path.is_symlink() or not path.is_file():
            errors.append(f'invalid_tracked_file: {name}')
            continue
        raw = path.read_bytes()
        if len(raw) > 300000 or b'\0' in raw:
            errors.append(f'binary_or_oversized_file: {name}')
            continue
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            errors.append(f'non_utf8_source: {name}')
            continue
        errors += content_errors(name, text)
        if name.startswith(('.github/workflows/', '.github/actions/')):
            errors += [f'{error}: {name}' for error in workflow_errors(text)]
            for local in re.findall(r'uses:\s*(\./\.github/actions/[^\s#]+)', text):
                if local.removeprefix('./') + '/action.yml' not in files:
                    errors.append(f'missing_registered_action: {name}')
    errors += test_layout_errors(package_root, policy['ci']['tests'])
    return sorted(set(errors))
