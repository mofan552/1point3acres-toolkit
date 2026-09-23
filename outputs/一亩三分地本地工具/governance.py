"""Fast offline consistency checks used both at startup and before delivery."""
import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

import settings
from presentation import render_reader


def _literal_strings(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _literal_strings(node.body) + _literal_strings(node.orelse)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [text for item in node.elts for text in _literal_strings(item)]
    return []


def _is_status_target(node):
    if isinstance(node, ast.Name):
        return node.id in ('status', 'content_status')
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value in ('status', 'content_status')
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'get'
            and bool(node.args) and isinstance(node.args[0], ast.Constant) and node.args[0].value in ('status', 'content_status'))


def _is_identity_target(node):
    names = {'username', 'uid', 'USERNAME', 'ACCOUNT_UID'}
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute):
        return node.attr in names
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value in names
    return False


def _cycle(graph):
    active, done = set(), set()
    def visit(node):
        if node in active:
            return True
        if node in done:
            return False
        active.add(node)
        if any(visit(child) for child in graph.get(node, ())):
            return True
        active.remove(node)
        done.add(node)
        return False
    return any(visit(node) for node in graph)


def scan_sources(root, policy):
    errors, graph = [], {}
    modules = policy['modules']
    external_roots = set(policy.get('external_modules', {})) | {name.split('.')[0] for name in policy.get('external_owners', {})}
    for path in sorted(root.rglob('*.py')):
        relative = path.relative_to(root)
        if relative.parts[0] in ('tests', '__pycache__'):
            continue
        name = path.stem
        if len(relative.parts) != 1 or name not in modules:
            errors.append(f'unregistered_module: {relative}')
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(relative))
        except SyntaxError as error:
            errors.append(f'python_syntax: {relative}:{error.lineno}')
            continue
        graph[name] = set()
        for node in ast.walk(tree):
            imported = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ''] + [(node.module + '.' + alias.name) if node.module else alias.name for alias in node.names]
                if node.level:
                    errors.append(f'unregistered_relative_import: {relative}:{node.lineno}')
            for dependency in imported:
                base = dependency.split('.')[0]
                if base and base not in modules and base not in sys.stdlib_module_names and base not in external_roots:
                    errors.append(f'undeclared_dependency: {name} -> {base}')
                if base in modules:
                    graph[name].add(base)
                    if base not in modules[name]:
                        errors.append(f'forbidden_dependency: {name} -> {base}')
                for external, owners in policy.get('external_owners', {}).items():
                    if (dependency == external or dependency.startswith(external + '.')) and name not in owners:
                        errors.append(f'external_owner: {name} imports {external}')
            if name != 'settings':
                identity_values = []
                if isinstance(node, ast.Assign) and any(_is_identity_target(target) for target in node.targets):
                    identity_values = [node.value]
                elif isinstance(node, ast.AnnAssign) and _is_identity_target(node.target):
                    identity_values = [node.value]
                elif isinstance(node, ast.Dict):
                    identity_values = [value for key, value in zip(node.keys, node.values)
                                       if isinstance(key, ast.Constant) and key.value in ('username', 'uid')]
                elif isinstance(node, ast.Call):
                    identity_values = [kw.value for kw in node.keywords if kw.arg in ('username', 'uid')]
                if any(isinstance(value, ast.Constant) and isinstance(value.value, (str, int)) for value in identity_values):
                    errors.append(f'owned_configuration: {relative}:{node.lineno}; use settings')
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and settings.SITE_HOST.removeprefix('www.') in node.value:
                    errors.append(f'owned_configuration: {relative}:{node.lineno}; use settings')
            if name != 'contracts':
                values = []
                if isinstance(node, ast.Dict):
                    values = [value for key, value in zip(node.keys, node.values)
                              if isinstance(key, ast.Constant) and key.value in ('status', 'content_status')]
                elif isinstance(node, ast.Assign) and any(_is_status_target(target) for target in node.targets):
                    values = [node.value]
                elif isinstance(node, ast.AnnAssign) and _is_status_target(node.target):
                    values = [node.value]
                elif isinstance(node, ast.Compare) and any(_is_status_target(item) for item in [node.left, *node.comparators]):
                    values = [node.left, *node.comparators]
                elif isinstance(node, ast.Call):
                    values = [kw.value for kw in node.keywords if kw.arg in ('status', 'content_status')]
                    if isinstance(node.func, ast.Attribute) and node.func.attr in ('get', 'setdefault') and len(node.args) > 1 and isinstance(node.args[0], ast.Constant) and node.args[0].value in ('status', 'content_status'):
                        values += [node.args[1]]
                for value in values:
                    if _literal_strings(value):
                        errors.append(f'literal_status: {relative}:{node.lineno}; use contracts')
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arguments = node.args.posonlyargs + node.args.args
                defaults = list(zip(arguments[-len(node.args.defaults):], node.args.defaults)) + list(zip(node.args.kwonlyargs, node.args.kw_defaults))
                for argument, default in defaults:
                    if argument.arg in ('limit', 'list_pages', 'max_thread_pages', 'company') and isinstance(default, ast.Constant) and default.value is not None:
                        errors.append(f'literal_default: {relative}:{node.lineno}; use settings')
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'add_argument':
                if any(isinstance(arg, ast.Constant) and arg.value in ('--limit', '--list-pages') for arg in node.args):
                    if any(kw.arg == 'default' and isinstance(kw.value, ast.Constant) for kw in node.keywords):
                        errors.append(f'literal_default: {relative}:{node.lineno}; use settings')
    if _cycle(graph):
        errors.append('dependency_cycle: local modules must form an acyclic graph')
    return sorted(set(errors))


def token_values(css):
    return re.findall(r'(--[\w-]+)\s*:\s*([^;{}]+);', css)


def token_group(name):
    if name == '--font-family':
        return 'font-family'
    if name.startswith('--font-'):
        return 'font-size'
    return name[2:].split('-')[0]


def scan_styles(css, tokens, budgets):
    errors = []
    plain_tokens = re.sub(r'/\*.*?\*/', '', tokens, flags=re.S)
    body = re.fullmatch(r'\s*:root\s*\{([^{}]*)\}\s*', plain_tokens)
    if not body or re.sub(r'--[\w-]+\s*:\s*[^;{}]+;', '', body[1]).strip():
        errors.append('token_file_structure: tokens.css may only contain custom properties in :root')
    pairs = token_values(tokens)
    names = {name for name, _ in pairs}
    if len(names) != len(pairs):
        errors.append('duplicate_token: each name has one definition')
    counts = Counter(token_group(name) for name, _ in pairs)
    for group, count in counts.items():
        if group not in budgets or count > budgets[group]:
            errors.append(f'token_budget: {group} uses {count}, allowed {budgets.get(group, 0)}')
    for name in re.findall(r'var\((--[\w-]+)', css + tokens):
        if name not in names:
            errors.append(f'undefined_token: {name}')
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    for prop, value in re.findall(r'([\w-]+)\s*:\s*([^;{}]+)[;}]', css):
        prop, value = prop.lower(), re.sub(r'\s*!important\s*$', '', value, flags=re.I).strip()
        remaining = re.sub(r'var\(--[\w-]+\)', '', value).strip()
        if prop.startswith('--'):
            errors.append(f'token_outside_owner: {prop}')
        if re.search(r'#[0-9a-fA-F]{3,8}\b|\b(?:rgb|hsl)a?\(', value):
            errors.append(f'raw_color: {prop}')
        if prop in ('color', 'background', 'background-color', 'border-color') and 'var(' not in value and value not in ('inherit', 'transparent', 'currentColor'):
            errors.append(f'raw_color: {prop}')
        if prop.endswith('color') or prop in ('background', 'box-shadow', 'outline', 'border', 'border-top', 'border-right', 'border-bottom', 'border-left', 'border-block', 'border-inline'):
            words = re.findall(r'(?<![\w-])[a-zA-Z][a-zA-Z-]*', remaining)
            if any(word not in ('solid', 'dashed', 'dotted', 'double', 'groove', 'ridge', 'outset', 'hidden', 'none', 'inset', 'inherit', 'transparent', 'currentColor') for word in words):
                errors.append(f'raw_color: {prop}')
        if prop in ('font', 'font-family', 'font-size', 'font-weight', 'line-height') and value != 'inherit' and not re.fullmatch(r'var\(--[\w-]+\)', value):
            errors.append(f'raw_typography: {prop}')
        if prop.startswith(('padding', 'margin')) or prop in ('gap', 'row-gap', 'column-gap', 'border-radius'):
            if remaining and not re.fullmatch(r'(?:0|auto|\s)+', remaining):
                errors.append(f'raw_spacing: {prop}')
    return sorted(set(errors))


def compare_generated(path, expected):
    if not path.exists() or path.read_text(encoding='utf-8') != expected:
        return [f'generated_drift: {path.name}; run check.py --sync']
    return []


def generated_files(root, policy, include_reader=True):
    files = {root / 'mcp.config.json': json.dumps(settings.mcp_config(), ensure_ascii=False, indent=2) + '\n'}
    data_file = settings.EXPORT_DIRECTORY / settings.EXPORT_FILES['json']
    if include_reader and data_file.exists():
        payload = json.loads(data_file.read_text(encoding='utf-8'))
        files[settings.EXPORT_DIRECTORY / settings.EXPORT_FILES['reader']] = render_reader(payload)
    return files


def check_consistency(root=None, include_reader=True):
    root = Path(root) if root else settings.ROOT
    policy = json.loads((root / 'architecture.json').read_text(encoding='utf-8'))
    errors = scan_sources(root, policy)
    for path in root.rglob('*'):
        if path.is_file() and path.suffix in ('.css', '.html', '.js', '.cmd', '.ps1', '.sh') and path.relative_to(root).parts[0] != 'tests':
            if path.relative_to(root).as_posix() not in policy['assets']:
                errors.append(f'unregistered_asset: {path.name}')
    styles = '\n'.join(path.read_text(encoding='utf-8') for path in root.rglob('*.css') if path.name != 'tokens.css' and path.relative_to(root).parts[0] != 'tests')
    errors += scan_styles(styles, (root / 'tokens.css').read_text(encoding='utf-8'), policy['token_budgets'])
    script = '\n'.join(path.read_text(encoding='utf-8') for path in root.rglob('*.js') if path.relative_to(root).parts[0] != 'tests')
    if re.search(r'\.style\b|setAttribute\([\"\']style[\"\']|[\u4e00-\u9fff]', script):
        errors.append('presentation_owner: reader.js must use copy catalog and CSS classes')
    for path in root.rglob('*.html'):
        if path.relative_to(root).parts[0] == 'tests':
            continue
        template = path.read_text(encoding='utf-8')
        if re.search(r'\bstyle\s*=', template, re.I) or any(value != '__STYLE__' for value in re.findall(r'<style>(.*?)</style>', template, re.S | re.I)):
            errors.append(f'inline_style: {path.name} must use generated styles')
    source = ast.parse((root / 'mcp_server.py').read_text(encoding='utf-8'))
    tools = [node.name for node in source.body if isinstance(node, ast.FunctionDef) and
             any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == 'tool' for d in node.decorator_list)]
    if set(tools) != set(policy['public_tools']):
        errors.append('public_interface_drift: update the registered tools and design together')
    requirements = dict(line.split('==', 1) for line in (root / 'requirements.txt').read_text(encoding='utf-8').splitlines() if line.strip() and not line.startswith('#'))
    if requirements != policy['dependencies']:
        errors.append('dependency_inventory_drift: update registry and rationale')
    if not set(policy.get('external_modules', {}).values()).issubset(requirements):
        errors.append('dependency_import_mapping: imports must map to registered dependencies')
    for path, expected in generated_files(root, policy, include_reader=include_reader).items():
        errors += compare_generated(path, expected)
    for document in ([root / 'README.md'] if include_reader else []):
        if not document.exists():
            errors.append(f'missing_document: {document.name}')
            continue
        for url in re.findall(r'\[[^\]]*\]\(([^\s)]+)\)', document.read_text(encoding='utf-8')):
            parsed = urlsplit(url)
            if not parsed.scheme and parsed.path and not (document.parent / unquote(parsed.path)).exists():
                errors.append(f'broken_document_link: {document.name} -> {url}')
    return sorted(set(errors))


def ensure_consistent():
    errors = check_consistency(include_reader=False)
    if errors:
        raise RuntimeError('consistency_check_failed: ' + '; '.join(errors[:3]))


def sync_generated():
    policy = json.loads((settings.ROOT / 'architecture.json').read_text(encoding='utf-8'))
    files = generated_files(settings.ROOT, policy)
    for path, content in files.items():
        path.write_text(content, encoding='utf-8', newline='\n')
    return [str(path) for path in files]
