"""Discover every regression; the image suite always runs the complete tree.

The public source job excludes modules importing the Host/SDK (including through
another test helper). This is dependency selection, not an allowlist: a new test
is included automatically, and the real-image job never applies this filter.
"""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path


def sdk_modules(sources: dict[str, str]) -> set[str]:
    dependencies, required = {}, set()
    for name, source in sources.items():
        imports = set()
        for node in ast.walk(ast.parse(source)):
            if (isinstance(node, ast.Attribute) and node.attr == 'sdk'
                    and isinstance(node.value, ast.Attribute) and node.value.attr == 'mark'
                    and isinstance(node.value.value, ast.Name) and node.value.value.id == 'pytest'):
                required.add(name)
            if isinstance(node, ast.Import):
                imports.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split('.')[0])
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr in {'import_module', 'importorskip'} and node.args
                  and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                imports.add(node.args[0].value.split('.')[0])
        dependencies[name] = imports.intersection(sources)
        if imports.intersection({'langbot', 'langbot_plugin', 'sqlalchemy'}):
            required.add(name)
    while True:
        extended = required | {name for name, imports in dependencies.items() if imports & required}
        if extended == required:
            return required
        required = extended


@lru_cache(maxsize=1)
def sdk_test_files():
    root = Path(__file__).parent
    sources = {path.stem: path.read_text(encoding='utf-8-sig') for path in root.glob('test_*.py')}
    return {root / (name + '.py') for name in sdk_modules(sources)}


def pytest_addoption(parser):
    parser.addoption('--public-only', action='store_true',
                     help='Run source tests without Host/SDK; full image CI runs without this filter.')


def pytest_configure(config):
    config.addinivalue_line('markers', 'sdk: requires Host/SDK through dynamic or indirect imports')


def pytest_ignore_collect(collection_path, config):
    if config.getoption('--public-only') and Path(collection_path) in sdk_test_files():
        return True
    return None


def pytest_report_header(config):
    if config.getoption('--public-only'):
        return f'Public source suite: {len(sdk_test_files())} Host/SDK modules deferred to full image suite'
