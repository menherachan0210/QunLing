"""The deployed Host patch must redact monitoring copies without changing calls."""

import copy
import importlib.util
import json
from pathlib import Path
import re
import sys
import textwrap
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATCH_NAME = "host-tool-password-redaction.patch"
RELATIVE = "src/langbot/pkg/api/http/service/monitoring.py"
SPEC = importlib.util.spec_from_file_location("monitoring_patcher", ROOT / "scripts/Apply-RuntimePatches.py")
patcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = patcher
SPEC.loader.exec_module(patcher)


def original_context(patch):
    """Rebuild unchanged upstream hunk contexts at their original line offsets."""
    lines, active = [], False
    for line in patch.splitlines(keepends=True):
        if line.startswith("@@"):
            start = int(re.match(r"@@ -(\d+)", line).group(1))
            lines.extend("\n" for _ in range(start - len(lines) - 1))
            active = True
        elif active and line.startswith((" ", "-")):
            lines.append(line[1:])
    return "".join(lines)


@pytest.fixture
def patched_source(tmp_path):
    source = (ROOT / "patches" / PATCH_NAME).read_text(encoding="utf-8")
    target = tmp_path / RELATIVE
    target.parent.mkdir(parents=True)
    original = original_context(source)
    target.write_text(original, encoding="utf-8")
    plan = patcher.build_plan(PATCH_NAME, tmp_path, source, "host")
    assert plan.state == "ready"
    patcher.execute_plans([plan], apply=True)
    patched = target.read_text(encoding="utf-8")
    assert patcher.build_plan(PATCH_NAME, tmp_path, source, "host").state == "already-applied"
    assert patcher.git_apply(tmp_path, plan.payload, "--reverse").returncode == 0
    assert target.read_text(encoding="utf-8") == original
    return patched


@pytest.fixture
def sanitize(patched_source):
    start = patched_source.index("    def _serialize_tool_arguments(")
    end = patched_source.index("    def _serialize_tool_payload(", start)
    namespace = {"json": json}
    exec(compile(textwrap.dedent(patched_source[start:end]), "host-password-redaction", "exec"), namespace)
    service = SimpleNamespace(_serialize_tool_payload=lambda value: (
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    ))
    return lambda value: namespace["_serialize_tool_arguments"](service, value)


@pytest.mark.parametrize("arguments", [
    {"password": "private-fixture", "asset_ref": "asset-123"},
    {"operation": "extract", "arguments": {"password": "private-fixture", "member": "page.png"}},
    {"items": [{"Password": "private-fixture"}, {" PASSWORD ": "private-fixture"}]},
    '{"password":"private-fixture","member":"page.png"}',
    {"arguments": '{"password":"private-fixture","asset_ref":"asset-123"}'},
    json.dumps(json.dumps({"password": "private-fixture"})),
])
def test_plain_nested_and_json_text_passwords_are_redacted_without_mutating_execution(sanitize, arguments):
    before = copy.deepcopy(arguments)
    result = sanitize(arguments)
    assert "private-fixture" not in result
    assert "redacted" in result
    assert arguments == before
    if "asset-123" in json.dumps(before):
        assert "asset-123" in result
    if "page.png" in json.dumps(before):
        assert "page.png" in result


def test_monitoring_only_wiring_and_plain_text_are_preserved(sanitize, patched_source):
    assert "'arguments': self._serialize_tool_arguments(arguments)," in patched_source
    assert "'result': self._serialize_tool_payload(result)," in patched_source
    assert "'error_message': self._serialize_tool_payload(error_message)," in patched_source
    assert sanitize({"text": "用户的聊天内容不在此补丁中修改", "count": 3}) == json.dumps(
        {"text": "用户的聊天内容不在此补丁中修改", "count": 3}, ensure_ascii=False,
    )


def test_invalid_json_deep_cycles_and_unknown_repr_fail_closed(sanitize):
    assert "private-fixture" not in sanitize('{"password":"private-fixture"')
    cyclic = {"child": None}
    cyclic["child"] = cyclic
    assert "depth limit" in sanitize(cyclic)

    class PrivateObject:
        def __repr__(self):
            return "private-fixture"

    assert "private-fixture" not in sanitize({"arguments": PrivateObject()})
