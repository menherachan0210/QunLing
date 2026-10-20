"""Pin the real SDK wire boundary; all Host actions are local fakes."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langbot_plugin.api.entities import events
from langbot_plugin.api.entities.context import EventContext
from langbot_plugin.api.entities.builtin.pipeline.query import Query
from langbot_plugin.api.entities.builtin.platform import entities, message
from langbot_plugin.api.entities.builtin.platform.events import (
    FriendMessage,
    GroupMessage,
)
from langbot_plugin.api.entities.builtin.provider.session import LauncherTypes
from langbot_plugin.api.entities.builtin.provider.message import Message


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("quoted", [False, True])
async def test_image_survives_sdk_serialization_without_host_query(
    monkeypatch, group, quoted
):
    source = message.Image(base64="data:image/png;base64,offline-fixture")
    chain = message.MessageChain(
        [
            message.Quote(origin=message.MessageChain([source])) if quoted else source,
        ]
    )
    if group:
        platform_event = GroupMessage(
            sender=entities.GroupMember(
                id="sender",
                member_name="Test",
                permission=entities.Permission.Member,
                group=entities.Group(
                    id="chat", name="Test", permission=entities.Permission.Member
                ),
            ),
            message_chain=chain,
        )
    else:
        platform_event = FriendMessage(
            sender=entities.Friend(id="sender", nickname="Test", remark=""),
            message_chain=chain,
        )
    query = Query(
        query_id=7,
        query_uuid="test-query",
        bot_uuid="bot-test",
        launcher_type=LauncherTypes.GROUP if group else LauncherTypes.PERSON,
        launcher_id="chat",
        sender_id="sender",
        message_event=platform_event,
        message_chain=chain,
        instance_uuid="instance-test",
        workspace_uuid="workspace-test",
        placement_generation=1,
    )
    event_class = events.GroupMessageReceived if group else events.PersonMessageReceived
    event = event_class(
        query=query,
        launcher_type=query.launcher_type.value,
        launcher_id=query.launcher_id,
        sender_id=query.sender_id,
        message_event=platform_event,
        message_chain=chain,
    )
    payload = EventContext.from_event(event).model_dump(serialize_as_any=False)
    wire = json.loads(json.dumps(payload))
    restored = EventContext.model_validate(wire)
    assert "query" not in wire["event"]
    assert restored.event.query is None
    restored_source = restored.event.message_chain[0]
    if quoted:
        assert isinstance(restored_source, message.Quote)
        restored_source = restored_source.origin[0]
    assert isinstance(restored_source, message.Image)

    # A separate module instance prevents mock-module fixtures from masking SDK types.
    spec = importlib.util.spec_from_file_location(
        "image_context_real_sdk", ROOT / "pkg/image_context.py"
    )
    cache_module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, cache_module)
    spec.loader.exec_module(cache_module)
    monkeypatch.setitem(sys.modules, "pkg.image_context", cache_module)
    listener_spec = importlib.util.spec_from_file_location(
        "image_listener_real_sdk",
        ROOT / "components/event_listener/image_context.py",
    )
    listener_module = importlib.util.module_from_spec(listener_spec)
    listener_spec.loader.exec_module(listener_module)
    written = {}

    class FakeHostProxy:
        def __init__(self, *, query_id, query_uuid, plugin_runtime_handler):
            assert (query_id, query_uuid) == (7, "test-query")

        async def get_bot_uuid(self):
            return "bot-test"

        async def set_query_var(self, key, value):
            written[key] = value

    monkeypatch.setattr(listener_module, "QueryBasedAPIProxy", FakeHostProxy)
    listener = listener_module.ImageContextListener()
    listener.plugin = SimpleNamespace(plugin_runtime_handler=SimpleNamespace())
    for handler in listener.registered_handlers[event_class]:
        await handler(restored)
    image = cache_module.select_image(listener.plugin, written)
    assert image.base64 == source.base64
    assert "base64" not in json.dumps(written)


@pytest.fixture
def prompt_env(monkeypatch):
    """Use SDK messages and wire serialization; fake only Host I/O."""
    cache_spec = importlib.util.spec_from_file_location(
        "image_prompt_context_real_sdk", ROOT / "pkg/image_context.py"
    )
    cache = importlib.util.module_from_spec(cache_spec)
    monkeypatch.setitem(sys.modules, cache_spec.name, cache)
    cache_spec.loader.exec_module(cache)
    monkeypatch.setitem(sys.modules, "pkg.image_context", cache)
    listener_spec = importlib.util.spec_from_file_location(
        "image_prompt_listener_real_sdk",
        ROOT / "components/event_listener/image_context.py",
    )
    module = importlib.util.module_from_spec(listener_spec)
    listener_spec.loader.exec_module(module)
    state = SimpleNamespace(variables={}, calls=[], failure=None)

    class FakeHostProxy:
        def __init__(self, *, query_id, query_uuid, plugin_runtime_handler):
            assert (query_id, query_uuid) == (17, "private-query-reference")

        async def get_query_vars(self):
            state.calls.append("get_query_vars")
            if state.failure:
                raise state.failure
            return state.variables

        async def reply(self, *args, **kwargs):
            state.calls.append("reply")
            pytest.fail("Prompt injection must not send messages")

    async def forbidden_generation(*args, **kwargs):
        state.calls.append("generate_or_edit")
        pytest.fail("Prompt injection must not call an image API")

    monkeypatch.setattr(module, "QueryBasedAPIProxy", FakeHostProxy)
    listener = module.ImageContextListener()
    listener.plugin = SimpleNamespace(
        plugin_runtime_handler=SimpleNamespace(),
        generate_image=forbidden_generation,
        edit_image=forbidden_generation,
    )

    def make_query(chain):
        return Query(
            query_id=17,
            query_uuid="private-query-reference",
            bot_uuid="private-bot-reference",
            launcher_type=LauncherTypes.PERSON,
            launcher_id="private-chat-reference",
            sender_id="private-sender-reference",
            message_event=FriendMessage(
                sender=entities.Friend(
                    id="private-sender-reference", nickname="Test", remark=""
                ),
                message_chain=chain,
            ),
            message_chain=chain,
            instance_uuid="private-instance-reference",
            workspace_uuid="private-workspace-reference",
            placement_generation=1,
        )

    def capture(chain):
        descriptor = cache.capture_context(listener.plugin, make_query(chain))
        state.variables = {cache.QUERY_CONTEXT_KEY: descriptor}
        return descriptor

    def prompt_context():
        event = events.PromptPreProcessing(
            query=make_query(message.MessageChain([])),
            session_name="private-session-reference",
            default_prompt=[
                Message(role="system", content="Keep the original persona.")
            ],
            prompt=[Message(role="user", content="Change the background to blue.")],
        )
        wire = json.loads(
            json.dumps(
                EventContext.from_event(event).model_dump(serialize_as_any=False)
            )
        )
        assert "query" not in wire["event"]
        restored = EventContext.model_validate(wire)
        assert restored.event.query is None
        return restored

    return SimpleNamespace(
        cache=cache,
        listener=listener,
        module=module,
        state=state,
        capture=capture,
        prompt_context=prompt_context,
    )


def assert_prompt_is_private(env, context):
    prompt_wire = json.dumps(
        [item.model_dump() for item in context.event.default_prompt], ensure_ascii=False
    )
    sensitive = [
        "private-",
        "data:image/",
        "base64",
        "https://",
        "offline-private-image",
    ]
    descriptor = env.state.variables.get(env.cache.QUERY_CONTEXT_KEY, {})
    sensitive.extend(
        descriptor[key] for key in ("owner", "scope", "token") if descriptor.get(key)
    )
    for value in sensitive:
        assert value not in prompt_wire
    assert set(env.state.calls) <= {"get_query_vars"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["current", "recent"])
@pytest.mark.parametrize("storage", ["base64", "url"])
async def test_prompt_describes_verified_image_without_exposing_source(
    prompt_env, status, storage
):
    env = prompt_env
    source = message.Image(
        **{
            storage: (
                "data:image/png;base64,offline-private-image"
                if storage == "base64"
                else "https://example.invalid/private-image?credential=private-secret"
            )
        }
    )
    descriptor = env.capture(message.MessageChain([source]))
    if status == "recent":
        descriptor = env.capture(message.MessageChain([]))
    assert descriptor["status"] == status
    context = env.prompt_context()
    original_system = context.event.default_prompt[0].model_dump()
    original_history = [item.model_dump() for item in context.event.prompt]

    await env.listener.inject_prompt(context)
    guidance = context.event.default_prompt[-1]
    assert guidance.role == "system"
    assert "edit_image" in guidance.content
    assert "无需重新上传" in guidance.content
    assert "不是操作请求" in guidance.content
    assert len(context.event.default_prompt) == 2
    assert context.event.default_prompt[0].model_dump() == original_system
    assert [item.model_dump() for item in context.event.prompt] == original_history
    assert_prompt_is_private(env, context)

    # Return through the same SDK boundary before injecting a second time.
    restored = EventContext.model_validate(
        json.loads(json.dumps(context.model_dump(serialize_as_any=False)))
    )
    await env.listener.inject_prompt(restored)
    assert [item.model_dump() for item in restored.event.default_prompt] == [
        item.model_dump() for item in context.event.default_prompt
    ]


@pytest.mark.asyncio
async def test_prompt_rejects_recent_descriptor_after_cache_eviction(prompt_env):
    env = prompt_env
    env.capture(
        message.MessageChain([message.Image(url="https://example.invalid/image")])
    )
    descriptor = env.capture(message.MessageChain([]))
    assert descriptor["status"] == "recent"
    env.cache._CACHE.clear_scope(descriptor["owner"], descriptor["scope"])
    context = env.prompt_context()
    await env.listener.inject_prompt(context)
    guidance = context.event.default_prompt[-1].content
    assert "失效" in guidance or "过期" in guidance
    assert "无需重新上传" not in guidance
    assert "请调用 edit_image" not in guidance
    assert_prompt_is_private(env, context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "chain", "expected"),
    [
        ("missing", message.MessageChain([]), "没有"),
        (
            "ambiguous",
            message.MessageChain(
                [
                    message.Image(url="https://example.invalid/one"),
                    message.Image(url="https://example.invalid/two"),
                ]
            ),
            "多张",
        ),
        (
            "unresolved_quote",
            message.MessageChain([message.Quote(origin=message.MessageChain([]))]),
            "引用",
        ),
        ("unavailable", message.MessageChain([message.Image()]), "不可用"),
    ],
)
async def test_prompt_handler_reports_missing_or_ambiguous_source(
    prompt_env, status, chain, expected
):
    env = prompt_env
    descriptor = env.capture(chain)
    assert descriptor["status"] == status
    context = env.prompt_context()
    handlers = env.listener.registered_handlers[events.PromptPreProcessing]
    assert len(handlers) == 1
    await handlers[0](context)
    guidance = context.event.default_prompt[-1].content
    assert expected in guidance
    assert "不是操作请求" in guidance
    assert "无需重新上传" not in guidance
    assert "请调用 edit_image" not in guidance
    assert_prompt_is_private(env, context)


@pytest.mark.asyncio
async def test_prompt_host_failure_is_unavailable_without_exception_leak(prompt_env):
    env = prompt_env
    env.state.failure = RuntimeError("private-api-key https://example.invalid/private")
    context = env.prompt_context()
    await env.listener.inject_prompt(context)
    assert "不可用" in context.event.default_prompt[-1].content
    assert_prompt_is_private(env, context)
