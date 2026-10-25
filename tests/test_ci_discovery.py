"""New regressions must be included, even through transitive SDK fixtures."""
from pathlib import Path

from conftest import sdk_modules


def test_new_plain_tests_and_dependency_cycles_are_discovered():
    sources = {
        'test_new': 'def test_something(): pass',
        'test_first': 'from test_second import helper',
        'test_second': 'from test_first import helper',
        'test_host': 'from langbot.pkg.pipeline import Controller',
        'test_bridge': 'from test_host import fixture',
        'test_new_integration': 'from test_bridge import fixture',
        'test_optional_sdk': 'def test_it():\n import langbot_plugin',
        'test_dynamic': 'sdk = pytest.importorskip("langbot_plugin")',
        'test_indirect': 'pytestmark = pytest.mark.sdk',
    }
    assert sdk_modules(sources) == {
        'test_host', 'test_bridge', 'test_new_integration', 'test_optional_sdk', 'test_dynamic', 'test_indirect',
    }
    sources['test_second'] += '\nfrom test_host import fixture'
    assert {'test_first', 'test_second'} <= sdk_modules(sources)


def test_image_suite_collects_entire_root_without_public_filter():
    workflow = (Path(__file__).parents[1] / '.github/workflows/ci.yml').read_text(encoding='utf-8')
    image_job = workflow.split('  linux-compose:', 1)[1]
    assert 'python -m pytest tests -q -p no:cacheprovider' in image_job
    assert '--public-only' not in image_job
