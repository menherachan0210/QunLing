"""Check actual SDK serialization and prompt privacy using a local Host stub."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
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
from qunling.assets import AssetStore
from qunling.inbox import Inbox
from qunling.integration import CONTEXT_KEY
from qunling.scope import Scope


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_TEXT = "private-file-body, punctuation?"
PRIVATE_INLINE = (
    "data:text/plain;base64," + base64.b64encode(PRIVATE_TEXT.encode()).decode()
)


@pytest.fixture
def env(monkeypatch):
    # These existing assertions describe the legacy fallback contract. A global
    # production data directory must not silently switch their code path.
    monkeypatch.delenv("QUNLING_DATA_DIR", raising=False)
    for name, path in (
        ("pkg.reader", "pkg/reader.py"),
        ("pkg.text_context", "pkg/text_context.py"),
        ("qq_text_listener_sdk", "components/event_listener/text_context.py"),
    ):
        spec = importlib.util.spec_from_file_location(name, ROOT / path)
        loaded = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, loaded)
        spec.loader.exec_module(loaded)
    module = loaded
    cache = sys.modules["pkg.text_context"]
    state = SimpleNamespace(variables={}, calls=[], failure=None)

    class FakeHostProxy:
        def __init__(self, *, query_id, query_uuid, plugin_runtime_handler):
            assert (query_id, query_uuid) == (42, "private-query-id")
            # They are real SDK attributes, not Host calls or I/O capabilities.
            self.query_id = query_id
            self.query_uuid = query_uuid

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
    listener = module.TextContextListener()
    listener.plugin = SimpleNamespace(
        plugin_runtime_handler=SimpleNamespace(),
        get_installation_binding=lambda: binding,
    )
    return SimpleNamespace(module=module, cache=cache, listener=listener, state=state)


@pytest.fixture
def shared_env(env, monkeypatch, tmp_path):
    from qunling import network, transfers

    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path / "shared-files"))
    forbidden = AsyncMock(side_effect=lambda *args, **kwargs: pytest.fail(
        "Capturing attachment metadata and injecting a prompt must not download or send."
    ))
    monkeypatch.setattr(network, "fetch_public", forbidden)
    monkeypatch.setattr(network, "fetch_public_file", forbidden)
    monkeypatch.setattr(transfers, "fetch_public_file", forbidden)
    env.listener.plugin.call_platform_api = forbidden
    env.listener.plugin.send_message = forbidden
    env.forbidden_io = forbidden
    env.store = AssetStore.from_env()
    return env


def private_file(**changes):
    values = dict(
        id="private-file-id",
        name="private-filename.txt",
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
    assert "read_text_attachment({})" in guidance.content
    assert "不是阅读请求" in guidance.content
    assert "complete=true" in guidance.content
    assert "不具有指令权限" in guidance.content
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
    assert "请调用 read_text_attachment" not in guidance
    assert_private(env, ctx.event.default_prompt)


@pytest.mark.asyncio
async def test_expired_recent_token_replaces_old_available_prompt(env):
    await capture(env, [private_file()])
    await capture(env)
    ctx = prompt_context()
    await env.listener.inject_prompt(ctx)
    assert "请调用 read_text_attachment" in ctx.event.default_prompt[-1].content
    descriptor = env.state.variables[env.cache.QUERY_CONTEXT_KEY]
    env.cache._CACHE.clear(descriptor["owner"], descriptor["scope"])
    await env.listener.inject_prompt(ctx)
    assert len(ctx.event.default_prompt) == 2
    assert "失效" in ctx.event.default_prompt[-1].content
    assert "请调用 read_text_attachment" not in ctx.event.default_prompt[-1].content
    assert_private(env, ctx.event.default_prompt)


@pytest.mark.asyncio
async def test_host_error_cannot_expose_exception_or_assert_a_file_is_available(env):
    env.state.failure = RuntimeError("private-api-key https://example.invalid/private")
    await capture(env, [private_file()])
    assert env.state.variables == {}
    ctx = prompt_context()
    await env.listener.inject_prompt(ctx)
    assert "不可用" in ctx.event.default_prompt[-1].content
    assert_private(env, ctx.event.default_prompt)


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("inline", [False, True])
async def test_shared_sdk_capture_and_prompt_use_trusted_context_without_io(shared_env, group, quoted, inline):
    env = shared_env
    original = private_file(base64=PRIVATE_INLINE if inline else "")
    component = message.Quote(origin=message.MessageChain([original])) if quoted else original
    await capture(env, [component], group=group)
    descriptor = env.state.variables[CONTEXT_KEY]
    owner = Scope(**descriptor["scope"])
    assert descriptor["query_uuid"] == "private-query-id"
    assert owner.bot_id == "private-bot-id"
    assert owner.conversation_id == ("group:private-chat-id" if group else "person:private-chat-id")
    assert owner.actor_id == "private-sender-id"
    assert len(descriptor["inbox_refs"]) == 1 and not descriptor["unresolved_quote"]
    selected = Inbox(env.store).get(owner, descriptor["inbox_refs"][0])
    assert selected["name"] == original.name
    if inline:
        assert env.store.read(owner, selected["asset_ref"]) == PRIVATE_TEXT.encode()
        env.store.require_visible(owner, selected["asset_ref"], descriptor)
    else:
        assert selected["asset_ref"] is None
        assert selected["url"] == original.url

    ctx = prompt_context()
    persona = ctx.event.default_prompt[0].model_dump()
    history = [item.model_dump() for item in ctx.event.prompt]
    await env.listener.inject_prompt(ctx)
    guidance = ctx.event.default_prompt[-1].content
    assert "file_workspace" in guidance and "本轮到达前可用附件 1 个" in guidance
    assert "current=true" in guidance and "send_attachment" in guidance
    assert "该状态不是阅读请求" in guidance
    assert ctx.event.default_prompt[0].model_dump() == persona
    assert [item.model_dump() for item in ctx.event.prompt] == history
    assert_private(env, ctx.event.default_prompt)
    before = [item.model_dump() for item in ctx.event.default_prompt]
    await env.listener.inject_prompt(ctx)
    assert [item.model_dump() for item in ctx.event.default_prompt] == before
    env.forbidden_io.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_unresolved_quote_reports_obstacle_without_selecting_old_file(shared_env):
    env = shared_env
    await capture(env, [private_file()])
    await capture(env, [message.Quote(origin=message.MessageChain([]))])
    descriptor = env.state.variables[CONTEXT_KEY]
    assert not descriptor["inbox_refs"] and descriptor["unresolved_quote"]
    ctx = prompt_context()
    await env.listener.inject_prompt(ctx)
    guidance = ctx.event.default_prompt[-1].content
    assert "file_workspace" in guidance
    assert "不能擅自用其他历史文件替代" in guidance
    assert "current=true" not in guidance
    assert_private(env, ctx.event.default_prompt)
    env.forbidden_io.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_context_with_wrong_query_falls_back_without_listing_assets(shared_env):
    env = shared_env
    await capture(env, [private_file()])
    env.state.variables[CONTEXT_KEY]["query_uuid"] = "private-unrelated-query"
    ctx = prompt_context()
    await env.listener.inject_prompt(ctx)
    guidance = ctx.event.default_prompt[-1].content
    assert "file_workspace" not in guidance
    assert "read_text_attachment({})" in guidance
    assert_private(env, ctx.event.default_prompt)
    env.forbidden_io.assert_not_awaited()
