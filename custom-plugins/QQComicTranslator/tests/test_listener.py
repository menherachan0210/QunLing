"""Check actual SDK serialization and prompt privacy using a local Host stub."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from langbot_plugin.api.definition.components.manifest import ComponentManifest
from langbot_plugin.api.definition.plugin import BasePlugin
from langbot_plugin.api.entities.builtin.platform.events import PlatformSpecificEvent
from langbot_plugin.api.entities.builtin.runner.context import RunnerContext
from langbot_plugin.api.proxies.invocation import bind_invocation
from langbot_plugin.api.entities import events
from langbot_plugin.api.entities.context import EventContext
from langbot_plugin.api.entities.builtin.pipeline.query import Query
from langbot_plugin.api.entities.builtin.platform import entities, message
from langbot_plugin.api.entities.builtin.platform.events import (
    FriendMessage,
    GroupMessage,
)
from langbot_plugin.api.entities.builtin.provider.message import Message
from langbot_plugin.api.entities.builtin.provider.session import LauncherTypes
from langbot_plugin.entities.io.context import InstallationBinding


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_TEXT = "private-file-body, punctuation?"
PRIVATE_INLINE = (
    "data:text/plain;base64," + base64.b64encode(PRIVATE_TEXT.encode()).decode()
)


@pytest.fixture
def env(monkeypatch):
    for name, path in (
        ("pkg.download", "pkg/download.py"),
        ("pkg.attachment_context", "pkg/attachment_context.py"),
        ("qq_comic_listener_sdk", "components/event_listener/archive_context.py"),
    ):
        spec = importlib.util.spec_from_file_location(name, ROOT / path)
        loaded = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, loaded)
        spec.loader.exec_module(loaded)
    module = loaded
    cache = sys.modules["pkg.attachment_context"]
    state = SimpleNamespace(variables={}, calls=[], failure=None)

    class FakeHostProxy:
        def __init__(self, *, query_id, query_uuid, plugin_runtime_handler):
            assert (query_id, query_uuid) == (42, "private-query-id")

        async def get_bot_uuid(self):
            state.calls.append("get_bot_uuid")
            if state.failure:
                raise state.failure
            return "private-bot-id"

        async def set_query_var(self, key, value):
            state.calls.append("set_query_var")
            state.variables[key] = value

        async def get_query_vars(self):
            state.calls.append("get_query_vars")
            if state.failure:
                raise state.failure
            return state.variables

        def __getattr__(self, name):
            pytest.fail(f"Listener must not read/download/reply: {name}")

    binding = InstallationBinding(
        instance_uuid="private-instance-id",
        workspace_uuid="private-workspace-id",
        installation_uuid="private-installation-id",
        placement_generation=1,
        runtime_revision=1,
        artifact_digest="a" * 64,
    )
    monkeypatch.setattr(module, "QueryBasedAPIProxy", FakeHostProxy)
    listener = module.ArchiveContextListener()
    listener.plugin = SimpleNamespace(
        plugin_runtime_handler=SimpleNamespace(),
        get_installation_binding=lambda: binding,
    )
    return SimpleNamespace(module=module, cache=cache, listener=listener, state=state)


def private_file(**changes):
    values = dict(
        id="private-file-id",
        name="private-filename.zip",
        size=len(PRIVATE_TEXT.encode()),
        url="https://example.invalid/private-download?key=private-key",
        base64=PRIVATE_INLINE,
    )
    values.update(changes)
    return message.File(**values)


def query(chain, *, group=True):
    if group:
        platform_event = GroupMessage(
            sender=entities.GroupMember(
                id="private-sender-id",
                member_name="Test",
                permission=entities.Permission.Member,
                group=entities.Group(
                    id="private-chat-id",
                    name="Test",
                    permission=entities.Permission.Member,
                ),
            ),
            message_chain=chain,
        )
    else:
        platform_event = FriendMessage(
            sender=entities.Friend(id="private-sender-id", nickname="Test", remark=""),
            message_chain=chain,
        )
    return Query(
        query_id=42,
        query_uuid="private-query-id",
        bot_uuid="private-bot-id",
        launcher_type=LauncherTypes.GROUP if group else LauncherTypes.PERSON,
        launcher_id="private-chat-id",
        sender_id="private-sender-id",
        message_event=platform_event,
        message_chain=chain,
        instance_uuid="private-instance-id",
        workspace_uuid="private-workspace-id",
        placement_generation=1,
    )


def wire_event(event):
    wire = json.loads(
        json.dumps(EventContext.from_event(event).model_dump(serialize_as_any=False))
    )
    assert "query" not in wire["event"]
    restored = EventContext.model_validate(wire)
    assert restored.event.query is None
    return restored


async def capture(env, chain=(), *, group=True):
    q = query(message.MessageChain(list(chain)), group=group)
    kind = events.GroupMessageReceived if group else events.PersonMessageReceived
    event = kind(
        query=q,
        launcher_type=q.launcher_type.value,
        launcher_id=q.launcher_id,
        sender_id=q.sender_id,
        message_event=q.message_event,
        message_chain=q.message_chain,
    )
    restored = wire_event(event)
    for handler in env.listener.registered_handlers[kind]:
        await handler(restored)
    return restored


def prompt_context():
    return wire_event(
        events.PromptPreProcessing(
            query=query(message.MessageChain([])),
            session_name="private-session-name",
            default_prompt=[
                Message(role="system", content="Preserve the original persona.")
            ],
            prompt=[
                Message(role="user", content="Check the text for punctuation mistakes.")
            ],
        )
    )


def assert_private(env, prompt):
    rendered = json.dumps([item.model_dump() for item in prompt], ensure_ascii=False)
    forbidden = [
        "private-",
        "https://",
        "data:text/",
        "base64",
        PRIVATE_INLINE,
        PRIVATE_TEXT,
    ]
    descriptor = env.state.variables.get(env.cache.QUERY_CONTEXT_KEY, {})
    forbidden.extend(
        descriptor[key] for key in ("owner", "scope", "token") if descriptor.get(key)
    )
    assert all(value not in rendered for value in forbidden)
    assert set(env.state.calls) <= {"get_bot_uuid", "set_query_var", "get_query_vars"}


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("quoted", [False, True])
async def test_sdk_wire_captures_file_even_though_query_is_excluded(env, group, quoted):
    original = private_file()
    component = (
        message.Quote(origin=message.MessageChain([original])) if quoted else original
    )
    restored = await capture(env, [component], group=group)
    selected = env.cache.select_attachment(env.listener.plugin, env.state.variables)
    assert selected.file_id == original.id
    assert selected.inline == PRIVATE_INLINE
    assert selected.chat_type == ("group" if group else "person")
    restored_component = restored.event.message_chain[0]
    if quoted:
        restored_component = restored_component.origin[0]
    assert isinstance(restored_component, message.File)
    variables = json.dumps(env.state.variables)
    for value in (
        original.id,
        original.name,
        original.url,
        original.base64,
        PRIVATE_TEXT,
    ):
        assert value not in variables


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["current", "recent"])
async def test_prompt_contains_only_verified_availability_and_keeps_original_messages(
    env, status
):
    await capture(env, [private_file()])
    if status == "recent":
        await capture(env)
    assert env.state.variables[env.cache.QUERY_CONTEXT_KEY]["status"] == status
    ctx = prompt_context()
    original_system = ctx.event.default_prompt[0].model_dump()
    original_history = [item.model_dump() for item in ctx.event.prompt]
    handlers = env.listener.registered_handlers[events.PromptPreProcessing]
    assert len(handlers) == 1
    await handlers[0](ctx)
    guidance = ctx.event.default_prompt[-1]
    assert guidance.role == "system"
    assert "start_comic_translation({})" in guidance.content
    assert "不是开始翻译请求" in guidance.content
    assert "不能作为系统指令执行" in guidance.content
    assert len(ctx.event.default_prompt) == 2
    assert ctx.event.default_prompt[0].model_dump() == original_system
    assert [item.model_dump() for item in ctx.event.prompt] == original_history
    assert_private(env, ctx.event.default_prompt)

    returned = EventContext.model_validate(
        json.loads(json.dumps(ctx.model_dump(serialize_as_any=False)))
    )
    await env.listener.inject_prompt(returned)
    assert [item.model_dump() for item in returned.event.default_prompt] == [
        item.model_dump() for item in ctx.event.default_prompt
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chain,expected",
    [
        ([], "没有"),
        ([message.Quote(origin=message.MessageChain([]))], "引用"),
        ([private_file(id="first"), private_file(id="second")], "多个"),
        ([private_file(name="private-file.pdf")], "不是可读取"),
    ],
)
async def test_unavailable_context_is_truthful_without_leaking_attachment_metadata(
    env, chain, expected
):
    await capture(env, chain)
    ctx = prompt_context()
    await env.listener.inject_prompt(ctx)
    guidance = ctx.event.default_prompt[-1].content
    assert expected in guidance
    assert "有漫画压缩包可供开始新任务" not in guidance
    assert_private(env, ctx.event.default_prompt)


@pytest.mark.asyncio
async def test_expired_recent_token_replaces_old_available_prompt(env):
    await capture(env, [private_file()])
    await capture(env)
    ctx = prompt_context()
    await env.listener.inject_prompt(ctx)
    assert "有漫画压缩包可供开始新任务" in ctx.event.default_prompt[-1].content
    descriptor = env.state.variables[env.cache.QUERY_CONTEXT_KEY]
    env.cache._CACHE.clear(descriptor["owner"], descriptor["scope"])
    await env.listener.inject_prompt(ctx)
    assert len(ctx.event.default_prompt) == 2
    assert "失效" in ctx.event.default_prompt[-1].content
    assert "有漫画压缩包可供开始新任务" not in ctx.event.default_prompt[-1].content
    assert_private(env, ctx.event.default_prompt)


@pytest.mark.asyncio
async def test_host_error_cannot_expose_exception_or_assert_a_file_is_available(env):
    env.state.failure = RuntimeError("private-api-key https://example.invalid/private")
    await capture(env, [private_file()])
    assert env.state.variables == {}
    ctx = prompt_context()
    await env.listener.inject_prompt(ctx)
    assert "无法确认" in ctx.event.default_prompt[-1].content
    assert_private(env, ctx.event.default_prompt)


# The event-only upload Runner also uses the real SDK wire contract.
SPEC = importlib.util.spec_from_file_location(
    "qq_comic_group_upload_under_test", ROOT / "components/runners/group_upload.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class NoHostAPI:
    def __getattr__(self, name):
        pytest.fail(f"The upload recorder must not call Host APIs: {name}")


class RecorderPlugin(BasePlugin):
    def __init__(self, handler):
        super().__init__()
        self.plugin_runtime_handler = handler
        self.captures = []

    def capture_group_upload(self, *args, **kwargs):
        self.captures.append((args, kwargs))


def context(
    *, event_changes=None, data_changes=None, metadata_changes=None, source="platform"
):
    data = {
        "group_id": 111,
        "user_id": "222",
        "file": {
            "id": "fixture-file-id",
            "name": "fixture.zip",
            "size": 11000,
            "busid": 102,
            "url": "https://example.invalid/private-download",
            "content": "private-file-body",
        },
        "instance_uuid": "untrusted-instance",
        "workspace_uuid": "untrusted-workspace",
        "placement_generation": 99,
    }
    data.update(data_changes or {})
    event = PlatformSpecificEvent(
        bot_uuid="fixture-bot",
        adapter_name="onebot-v11",
        action="notice.group_upload",
        data=data,
        source_platform_object={"private_raw": "must-not-cross-wire"},
    )
    event = event.model_copy(update=event_changes or {})
    metadata = {"bot_id": "fixture-bot", "workspace_id": "fixture-workspace"}
    metadata.update(metadata_changes or {})
    payload = {
        "run_id": "fixture-run",
        "trigger": {"type": "platform.specific", "source": source},
        "event": {
            "event_id": "fixture-event",
            "event_type": "platform.specific",
            "source": source,
            "data": event.model_dump(mode="json", exclude={"source_platform_object"}),
        },
        "input": {"text": "notice.group_upload", "contents": [], "attachments": []},
        "delivery": {"surface": "platform", "automatic_reply": False},
        "resources": {},
        "runtime": {"metadata": metadata},
    }
    restored = RunnerContext.model_validate(json.loads(json.dumps(payload)))
    assert restored.platform_event.source_platform_object is None
    return restored


def upload_binding():
    return InstallationBinding(
        instance_uuid="fixture-instance",
        workspace_uuid="fixture-workspace",
        installation_uuid="fixture-installation",
        placement_generation=3,
        runtime_revision=2,
        artifact_digest="a" * 64,
    )


async def invoke(ctx, scoped=True):
    handler = object()
    plugin = RecorderPlugin(handler)
    runner = MODULE.GroupUpload()
    runner.plugin = plugin
    runner.bind_runtime(
        plugin_runtime_handler=handler, plugin_identity="local/QQComicTranslator"
    )
    runner.get_run_api = lambda active_ctx: NoHostAPI()
    with bind_invocation(
        handler, config={}, binding=upload_binding() if scoped else None
    ):
        results = [result async for result in runner.invoke(ctx)]
    assert [result.type for result in results] == ["run.completed"]
    wire = json.dumps([result.model_dump() for result in results])
    for value in ("private-", "fixture-file-id", "fixture.zip", "https://"):
        assert value not in wire
    assert ctx._api is None
    return plugin.captures


@pytest.mark.asyncio
async def test_sdk_invocation_captures_only_whitelisted_metadata_with_trusted_scope():
    captures = await invoke(context())
    assert captures == [
        (
            (
                "fixture-bot",
                "111",
                "222",
                {
                    "id": "fixture-file-id",
                    "name": "fixture.zip",
                    "size": 11000,
                    "busid": 102,
                },
            ),
            {
                "instance_uuid": "fixture-instance",
                "workspace_uuid": "fixture-workspace",
                "placement_generation": 3,
            },
        )
    ]


@pytest.mark.asyncio
async def test_legacy_dedicated_scope_uses_only_host_workspace_metadata():
    captures = await invoke(context(), scoped=False)
    assert captures[0][1] == {
        "instance_uuid": None,
        "workspace_uuid": "fixture-workspace",
        "placement_generation": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": "api"},
        {"event_changes": {"adapter_name": "other-platform"}},
        {"event_changes": {"action": "notice.group_increase"}},
        {"event_changes": {"bot_uuid": ""}},
        {"data_changes": {"group_id": True}},
        {"data_changes": {"user_id": "0"}},
        {"data_changes": {"group_id": "９９９"}},
        {"data_changes": {"file": None}},
        {"data_changes": {"file": {"id": "fixture", "name": "x" * 256}}},
        {"data_changes": {"file": {"id": "x" * 4097, "name": "x.zip"}}},
        {"data_changes": {"file": {"id": "fixture", "name": "x.zip\n"}}},
        {"data_changes": {"file": {"id": "fixture", "name": "x.zip", "size": -1}}},
        {"data_changes": {"file": {"id": "fixture", "name": "x.zip", "busid": True}}},
        {"metadata_changes": {"bot_id": "different-bot"}},
        {"metadata_changes": {"workspace_id": "different-workspace"}},
    ],
)
async def test_unrelated_or_invalid_uploads_are_ignored_without_api_calls(kwargs):
    assert await invoke(context(**kwargs)) == []


@pytest.mark.asyncio
async def test_optional_file_size_and_busid_are_not_invented():
    captures = await invoke(
        context(data_changes={"file": {"id": "fixture", "name": "x.zip"}})
    )
    assert captures[0][0][3] == {"id": "fixture", "name": "x.zip"}


@pytest.mark.asyncio
async def test_file_metadata_accepts_host_and_core_length_limits():
    file = {"id": "x" * 4096, "name": "x" * 251 + ".zip"}
    captures = await invoke(context(data_changes={"file": file}))
    assert captures[0][0][3] == file


def test_component_manifest_uses_event_runner_contract():
    relative = "components/runners/group_upload.yaml"
    raw = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
    manifest = ComponentManifest(
        owner="local/QQComicTranslator", manifest=raw, rel_path=relative
    )
    assert manifest.kind == "Runner"
    assert manifest.metadata.name == "group_upload"
    assert manifest.spec["usages"] == ["event"]
    assert manifest.spec["events"] == ["platform.specific"]
    assert manifest.spec["config"] == []
