"""Public patch regression tests: no LangBot install or model invocation needed."""
import ast
import asyncio
import copy
from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from qunling.tasks.hints import task_hint

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/localagent_0_2_1"


def git(*args, cwd):
    executable = shutil.which("git") or r"F:\work\Environment\Git\cmd\git.exe"
    result = subprocess.run([executable, *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture(scope="module")
def patched(tmp_path_factory):
    directory = tmp_path_factory.mktemp("localagent-patched")
    shutil.copytree(FIXTURES / "pkg", directory / "pkg")
    patch = ROOT / "patches/localagent-task-continuation.patch"
    git("apply", "--check", str(patch), cwd=directory)
    git("apply", str(patch), cwd=directory)
    git("apply", "--reverse", "--check", str(patch), cwd=directory)
    return directory


@pytest.fixture(scope="module")
def guard_module(patched):
    spec = importlib.util.spec_from_file_location("task_continuation_under_test", patched / "pkg/task_continuation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resource(name="read_web_page", owner="local/QQWebSearch", **kwargs):
    return {"tool_name": name, "source": "plugin", "source_id": owner, "operations": ["call"], **kwargs}


def result(**kwargs):
    return {"_qunling_task": task_hint(**kwargs)}


def test_fixed_baseline_matches_version_lock():
    baseline = json.loads((FIXTURES / "baseline.json").read_text())
    lock = json.loads((ROOT / "deploy/compose/sources.lock.json").read_text())
    assert baseline["version"] == lock["upstream_plugins"]["local_agent_version"] == "0.2.1"
    assert baseline["upstream_commit"] == lock["upstream_plugins"]["commit"]
    for path, digest in baseline["sha256"].items():
        assert hashlib.sha256((FIXTURES / path).read_bytes()).hexdigest() == digest


def test_sdk_provenance_patch_preserves_host_fields(tmp_path):
    relative = ".venv/Lib/site-packages/langbot_plugin/api/entities/builtin/runner/resources.py"
    destination = tmp_path / relative
    destination.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "tests/fixtures/sdk_tool_provenance/resources.py", destination)
    patch = ROOT / "patches/sdk-tool-provenance.patch"
    git("apply", "--check", str(patch), cwd=tmp_path)
    git("apply", str(patch), cwd=tmp_path)
    git("apply", "--reverse", "--check", str(patch), cwd=tmp_path)
    tree = ast.parse(destination.read_text())
    tool = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == "ToolResource")
    fields = {item.target.id: item for item in tool.body if isinstance(item, ast.AnnAssign)}
    assert {"source", "source_id"} <= fields.keys()
    assert ast.unparse(fields["source"].annotation) == "str | None"
    assert ast.unparse(fields["source_id"].value) == "None"


@pytest.mark.parametrize("bad_resource", [
    resource(owner="other/QQWebSearch"), resource(source="mcp"), resource(source_id=None),
    resource(operations=["detail"]), {"tool_name": "read_web_page"},
])
def test_provenance_fails_closed(guard_module, bad_resource):
    assert guard_module.trusted_sources([bad_resource], {"read_web_page"}) == {}


def test_only_exact_source_and_tool_allowlist_is_trusted(guard_module):
    assert guard_module.trusted_sources([resource()], {"read_web_page"}) == {"read_web_page": "local/QQWebSearch"}
    assert guard_module.trusted_sources([resource(name="execute_shell")], {"execute_shell"}) == {}
    assert guard_module.trusted_sources([resource(), resource(source="mcp")], {"read_web_page"}) == {}
    assert guard_module.trusted_sources([resource(source="mcp"), resource()], {"read_web_page"}) == {}
    assert guard_module.trusted_sources(
        [resource(name="file_workspace", owner="local/QQTextReader")], {"file_workspace"}
    ) == {"file_workspace": "local/QQTextReader"}


def test_group_image_import_requires_exact_catalog_owner_and_excludes_paid_vision(guard_module):
    allowed = {"import_group_context_images", "read_group_context_images"}
    catalog = [
        resource(name="import_group_context_images", owner="local/QQGroupContext"),
        resource(name="read_group_context_images", owner="local/QQGroupContext"),
    ]
    assert guard_module.trusted_sources(catalog, allowed) == {
        "import_group_context_images": "local/QQGroupContext",
    }
    for owner in ("langbot-team/QQGroupContext", "local/QQGroupContext-extra", "local/QQWebSearch"):
        assert guard_module.trusted_sources(
            [resource(name="import_group_context_images", owner=owner)], allowed,
        ) == {}
    # Paid vision output is data even when it contains a matching marker.
    guard = guard_module.TaskContinuation(guard_module.trusted_sources(catalog, allowed))
    guard.observe("read_group_context_images", result())
    assert guard.take_notice() is None
    guard.observe("import_group_context_images", result(status="partial", error_code="connection_failed"))
    assert guard.take_notice()


def test_plain_chat_untrusted_or_nested_document_content_never_adds_turn(guard_module):
    guard = guard_module.TaskContinuation({"read_web_page": "local/QQWebSearch"})
    assert guard.take_notice() is None
    guard.observe("unknown_tool", result())
    assert guard.take_notice() is None
    guard.observe("read_web_page", {"text": json.dumps(result())})
    assert guard.take_notice() is None
    guard.observe("read_web_page", {"result": result()})
    assert guard.take_notice() is None


@pytest.mark.parametrize("field,value", [
    ("version", True), ("version", 2), ("status", []), ("next_action", {}),
    ("can_continue", "true"), ("retryable", 1), ("error_code", {}),
    ("task_id", "../bad"), ("task_id", "x" * 129),
])
def test_malformed_runtime_hint_is_ignored_without_runner_failure(guard_module, field, value):
    guard = guard_module.TaskContinuation({"read_web_page": "local/QQWebSearch"})
    response = result()
    response["_qunling_task"][field] = value
    guard.observe("read_web_page", response)
    assert guard.take_notice() is None


def test_structured_failure_allows_one_notice_and_never_exposes_raw_content(guard_module):
    guard = guard_module.TaskContinuation({"read_web_page": "local/QQWebSearch"})
    response = result(status="partial", error_code="fetch_timeout", retryable=True, next_action="retry_read")
    response["body"] = "secret synthetic body that must not enter notice"
    guard.observe("read_web_page", json.dumps(response))
    notice = guard.take_notice()
    assert notice and response["body"] not in notice
    assert guard.take_notice() is None
    guard.observe("read_web_page", response)
    assert guard.take_notice() is None  # Identical error is not another allowance.


@pytest.mark.parametrize("code", [
    "permission", "needs_input", "delivery_unconfirmed", "budget", "cancelled", "send_unconfirmed",
])
def test_stopping_errors_override_untrusted_flags(guard_module, code):
    guard = guard_module.TaskContinuation({"read_web_page": "local/QQWebSearch"})
    raw = result()
    raw["_qunling_task"].update(error_code=code, retryable=True, can_continue=True)
    guard.observe("read_web_page", raw)
    assert guard.take_notice() is None


def test_no_repeated_send_and_latest_completed_state_clears_pending(guard_module):
    guard = guard_module.TaskContinuation({
        "download_web_file": "local/QQWebSearch", "send_web_file": "local/QQWebSearch",
    })
    guard.observe("download_web_file", result(next_action="deliver"))
    guard.observe("send_web_file", result(status="completed", can_continue=False))
    assert guard.take_notice() is None
    guard.observe("download_web_file", result(next_action="deliver"))
    guard.observe("send_web_file", "unexpected error")
    assert guard.take_notice() is None


def test_notices_cannot_reset_tool_budget_or_exceed_two(guard_module):
    guard = guard_module.TaskContinuation({"read_web_page": "local/QQWebSearch"}, max_tool_batches=24)
    for i in range(3):
        guard.observe("read_web_page", result(task_id=f"task-{i}"))
        assert bool(guard.take_notice()) is (i < 2)
    assert guard.remaining_tool_batches == 24
    guard = guard_module.TaskContinuation({"read_web_page": "local/QQWebSearch"}, max_tool_batches=1)
    guard.observe("read_web_page", result())
    guard.on_tool_batch()
    assert guard.take_notice() is None


@dataclass
class Message:
    role: str
    content: str
    def model_copy(self, deep=False):
        return copy.deepcopy(self)


def test_real_context_hooks_observe_results_and_continue_only_no_tool_turn(patched, guard_module):
    tree = ast.parse((patched / "pkg/agent_core/langbot.py").read_text())
    hook = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LangBotContextHooks")
    source = "from __future__ import annotations\n" + ast.unparse(hook)
    namespace = {
        "AgentLoopHooks": object, "Message": Message,
        "usage_total_tokens": lambda _: None,
    }
    exec(compile(source, "real-patched-hooks", "exec"), namespace)
    async def scenario():
        guard = guard_module.TaskContinuation({"read_web_page": "local/QQWebSearch"})
        hooks = namespace["LangBotContextHooks"](None, task_continuation=guard)
        messages = [Message("user", "complete synthetic task"), Message("assistant", "stopping")]
        turn = SimpleNamespace(tool_calls=[], usage=None)
        assert len(await hooks.after_model_turn(turn, messages)) == 2
        outcome = SimpleNamespace(request=SimpleNamespace(name="read_web_page"), result=result(), error=None)
        assert await hooks.after_tool_call(outcome) is outcome
        tool_turn = SimpleNamespace(tool_calls=["next"], usage=None)
        assert len(await hooks.after_model_turn(tool_turn, messages)) == 2
        continued = await hooks.after_model_turn(turn, messages)
        assert len(continued) == 3 and continued[-1].role == "system"
        assert len(await hooks.after_model_turn(turn, messages)) == 2
    asyncio.run(scenario())


def test_unknown_error_normalization_preserves_safe_alternative_action():
    hint = task_hint(error_code="no_readable_text", retryable=True, next_action="continue")
    assert hint["error_code"] == "internal" and not hint["retryable"] and hint["can_continue"]
    assert task_hint(error_code="network_timeout", retryable=True)["retryable"]
