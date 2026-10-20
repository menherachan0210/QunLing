"""Exercise registered listeners through real SDK wire events and query proxies."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

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
from langbot_plugin.entities.io.actions.enums import PluginToRuntimeAction
from langbot_plugin.entities.io.context import InstallationBinding
from langbot_plugin.runtime.io.handler import Handler

from components.event_listener.context import GUIDANCE, MARKER, GroupContextListener
from pkg.context import ContextStore, KEY, TTL
from pkg.tools import execute


def make_query(*, group=True, query_id=42, query_uuid="query-a", image=False):
    parts = [
        message.Source(
            id=-990,
            time=datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
        ),
        message.Plain(text="Synthetic ordinary discussion without a mention"),
    ]
    if image:
        parts.append(message.Image(url="https://example.test/synthetic.png"))
    chain = message.MessageChain(parts)
    if group:
        platform_event = GroupMessage(
            sender=entities.GroupMember(
                id="201",
                member_name="Synthetic member",
                permission=entities.Permission.Member,
                group=entities.Group(
                    id="1001",
                    name="Synthetic group",
                    permission=entities.Permission.Member,
                ),
            ),
            message_chain=chain,
        )
    else:
        platform_event = FriendMessage(
            sender=entities.Friend(id="201", nickname="Synthetic member", remark=""),
            message_chain=chain,
        )
    return Query(
        query_id=query_id,
        query_uuid=query_uuid,
        bot_uuid="synthetic-bot",
        launcher_type=LauncherTypes.GROUP if group else LauncherTypes.PERSON,
        launcher_id="1001" if group else "201",
        sender_id="201",
        message_event=platform_event,
        message_chain=chain,
        instance_uuid="instance-a",
        workspace_uuid="workspace-a",
        placement_generation=1,
    )


def wire_event(event):
    payload = json.loads(
        json.dumps(EventContext.from_event(event).model_dump(serialize_as_any=False))
    )
    assert "query" not in payload["event"]
    restored = EventContext.model_validate(payload)
    assert restored.event.query is None
    return restored


def received_context(query):
    kind = (
        events.GroupMessageReceived
        if query.launcher_type == LauncherTypes.GROUP
        else events.PersonMessageReceived
    )
    return wire_event(
        kind(
            query=query,
            launcher_type=query.launcher_type.value,
            launcher_id=query.launcher_id,
            sender_id=query.sender_id,
            message_event=query.message_event,
            message_chain=query.message_chain,
        )
    )


def prompt_context(query, user_content="Compare the earlier suggestions."):
    return wire_event(
        events.PromptPreProcessing(
            query=query,
            session_name="synthetic-session",
            default_prompt=[
                Message(role="system", content="Preserve the original persona."),
                Message(role="user", content=MARKER + " is ordinary quoted text."),
            ],
            prompt=[Message(role="user", content=user_content)],
        )
    )


@pytest.fixture
def env():
    state = SimpleNamespace(variables={}, calls=[])
    runtime = Mock(spec=Handler)

    async def call_action(action, payload, **kwargs):
        state.calls.append((action, dict(payload)))
        ref = payload["query_id"], payload.get("query_uuid")
        variables = state.variables[ref]
        if action == PluginToRuntimeAction.GET_BOT_UUID:
            return {"bot_uuid": "synthetic-bot"}
        if action == PluginToRuntimeAction.SET_QUERY_VAR:
            variables[payload["key"]] = payload["value"]
            return {}
        if action == PluginToRuntimeAction.GET_QUERY_VARS:
            return {"vars": dict(variables)}
        pytest.fail(
            f"Listeners must not read history, invoke models, or reply: {action}"
        )

    runtime.call_action = AsyncMock(side_effect=call_action)
    binding = InstallationBinding(
        instance_uuid="instance-a",
        workspace_uuid="workspace-a",
        installation_uuid="installation-a",
        placement_generation=1,
        runtime_revision=1,
        artifact_digest="a" * 64,
    )
    plugin = SimpleNamespace(
        contexts=ContextStore(),
        plugin_runtime_handler=runtime,
        get_installation_binding=lambda: binding,
        call_platform_api=AsyncMock(
            side_effect=AssertionError("Unexpected history read")
        ),
        invoke_llm=AsyncMock(side_effect=AssertionError("Unexpected model call")),
    )
    listener = GroupContextListener()
    listener.plugin = plugin

    def register(query):
        variables = {
            "launcher_type": query.launcher_type.value,
            "launcher_id": str(query.launcher_id),
            "sender_id": str(query.sender_id),
        }
        state.variables[(query.query_id, query.query_uuid)] = variables
        return variables

    async def dispatch(ctx):
        for callback in listener.registered_handlers.get(type(ctx.event), []):
            await callback(ctx)

    yield SimpleNamespace(
        plugin=plugin,
        listener=listener,
        state=state,
        register=register,
        dispatch=dispatch,
    )
    plugin.call_platform_api.assert_not_awaited()
    plugin.invoke_llm.assert_not_awaited()
    assert {action for action, _ in state.calls} <= {
        PluginToRuntimeAction.GET_BOT_UUID,
        PluginToRuntimeAction.SET_QUERY_VAR,
        PluginToRuntimeAction.GET_QUERY_VARS,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("image", [False, True])
async def test_unaddressed_group_messages_only_capture_a_trusted_anchor(env, image):
    query = make_query(image=image)
    variables = env.register(query)
    ctx = received_context(query)
    before = ctx.event.message_chain.model_dump()

    await env.dispatch(ctx)

    assert [action for action, _ in env.state.calls] == [
        PluginToRuntimeAction.GET_BOT_UUID,
        PluginToRuntimeAction.SET_QUERY_VAR,
    ]
    token = variables[KEY]
    assert isinstance(token, str) and len(token) == 32
    assert len(env.plugin.contexts.entries) == 1
    item = env.plugin.contexts.entries[token]
    assert (item.query_id, item.query_uuid, item.group, item.sender, item.anchor) == (
        42,
        "query-a",
        "1001",
        "201",
        "-990",
    )
    assert item.timestamp == int(query.message_chain[0].time.timestamp())
    assert item.history is None and item.image_urls == {}
    assert item.active is False and item.vision_used is False
    assert "Synthetic ordinary discussion" not in repr(item)
    assert ctx.event.message_chain.model_dump() == before
    assert not ctx.is_prevent_default and not ctx.is_prevent_postorder


@pytest.mark.asyncio
async def test_matching_prompt_injects_guidance_once_and_preserves_other_messages(env):
    query = make_query()
    env.register(query)
    await env.dispatch(received_context(query))
    ctx = prompt_context(query)
    originals = [item.model_dump() for item in ctx.event.default_prompt]
    original_prompt = [item.model_dump() for item in ctx.event.prompt]

    await env.dispatch(ctx)
    await env.dispatch(ctx)

    assert [item.model_dump() for item in ctx.event.default_prompt[:-1]] == originals
    assert [item.model_dump() for item in ctx.event.prompt] == original_prompt
    guidance = ctx.event.default_prompt[-1]
    assert guidance.role == "system"
    assert guidance.content == MARKER + "\n" + GUIDANCE
    assert (
        sum(
            item.role == "system"
            and isinstance(item.content, str)
            and item.content.startswith(MARKER)
            for item in ctx.event.default_prompt
        )
        == 1
    )
    returned = EventContext.model_validate(
        json.loads(json.dumps(ctx.model_dump(serialize_as_any=False)))
    )
    assert returned.event.default_prompt[-1].content == guidance.content
    assert all(
        (payload["query_id"], payload.get("query_uuid")) == (42, "query-a")
        for _, payload in env.state.calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_group_token", [False, True])
async def test_private_events_and_prompts_never_gain_group_guidance(
    env, reuse_group_token
):
    token = None
    if reuse_group_token:
        group = make_query()
        variables = env.register(group)
        await env.dispatch(received_context(group))
        token = variables[KEY]
    private = make_query(group=False)
    variables = env.register(private)
    if token:
        variables[KEY] = token
    count = len(env.plugin.contexts.entries)
    calls = len(env.state.calls)

    await env.dispatch(received_context(private))
    assert len(env.state.calls) == calls
    assert len(env.plugin.contexts.entries) == count
    assert events.PersonMessageReceived not in env.listener.registered_handlers
    ctx = prompt_context(private)
    before = [item.model_dump() for item in ctx.event.default_prompt]
    await env.dispatch(ctx)
    assert [item.model_dump() for item in ctx.event.default_prompt] == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query_id,query_uuid",
    [(43, "query-a"), (42, "query-b"), (42, None)],
)
async def test_prompt_for_another_query_cannot_reuse_captured_guidance(
    env, query_id, query_uuid
):
    query = make_query()
    variables = env.register(query)
    await env.dispatch(received_context(query))
    other = make_query(query_id=query_id, query_uuid=query_uuid)
    env.register(other)[KEY] = variables[KEY]
    ctx = prompt_context(other)
    before = [item.model_dump() for item in ctx.event.default_prompt]

    await env.dispatch(ctx)

    assert [item.model_dump() for item in ctx.event.default_prompt] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "unknown", "expired", "revoked"])
async def test_unavailable_anchor_does_not_inject_or_replace_the_prompt(env, invalid):
    query = make_query()
    variables = env.register(query)
    await env.dispatch(received_context(query))
    if invalid == "missing":
        variables.pop(KEY)
    elif invalid == "unknown":
        variables[KEY] = "not-a-captured-token"
    elif invalid == "expired":
        item = env.plugin.contexts.entries[variables[KEY]]
        env.plugin.contexts.clock = lambda: item.created + TTL
    else:
        env.plugin.contexts.clear()
    ctx = prompt_context(query)
    before = [item.model_dump() for item in ctx.event.default_prompt]

    await env.dispatch(ctx)

    assert [item.model_dump() for item in ctx.event.default_prompt] == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question,own_statement,other_statement",
    [
        (
            "我这样做对吗",
            "我准备先列目录，再按章节整理读书笔记。",
            "我自己的笔记打算直接从最后一章开始整理。",
        ),
        (
            "那怎么办",
            "我借的拼图少了三块，盒子上没有编号。",
            "我那盒只是外包装破了，里面的拼图是齐的。",
        ),
        (
            "你怎么看",
            "我想把读书会从每周两次改成一次。",
            "我更喜欢每天碰面讨论。",
        ),
        (
            "这样安排行不型",
            "我想把重的积木放底层，轻的放上层。",
            "我另一个积木作品用的是悬挑结构。",
        ),
    ],
)
async def test_natural_followup_keeps_anchor_and_questioner_through_tool_read(
    env, question, own_statement, other_statement
):
    """Validate the offline plumbing, without pretending to test model selection."""
    query = make_query()
    query.message_chain[1].text = question
    variables = env.register(query)
    await env.dispatch(received_context(query))
    ctx = prompt_context(query, question)
    await env.dispatch(ctx)

    assert ctx.event.prompt[0].content == question
    assert query.message_chain[1].text == question
    assert ctx.event.default_prompt[-1].content == MARKER + "\n" + GUIDANCE
    env.plugin.call_platform_api.assert_not_awaited()
    item = env.plugin.contexts.entries[variables[KEY]]
    assert item.history is None

    snapshot = {
        "messages": [
            {
                "message_id": "501",
                "time": item.timestamp - 30,
                "sender_id": item.sender,
                "sender_name": "Synthetic questioner",
                "text": own_statement,
                "images": [],
            },
            {
                "message_id": "502",
                "time": item.timestamp - 20,
                "sender_id": "202",
                "sender_name": "Synthetic other member",
                "text": other_statement,
                "images": [],
            },
        ],
        "truncated": False,
    }
    read_api = AsyncMock(return_value=snapshot)
    # Simulate an explicit tool invocation only after the prompt was delivered.
    # The listener's original plugin still must never invoke this capability.
    tool_plugin = SimpleNamespace(**{**vars(env.plugin), "call_platform_api": read_api})
    result = json.loads(
        await execute(
            SimpleNamespace(plugin=tool_plugin),
            "context",
            {},
            ctx.query_id,
            ctx.query_uuid,
        )
    )

    read_api.assert_awaited_once_with(
        "synthetic-bot",
        "call_platform_api",
        {
            "action": "get_group_recent_context",
            "params": {
                "group_id": "1001",
                "before_message_id": "-990",
                "before_time": item.timestamp,
            },
        },
    )
    assert result["status"] == "ok"
    assert [row["text"] for row in result["messages"]] == [
        own_statement,
        other_statement,
    ]
    assert [row["is_questioner"] for row in result["messages"]] == [True, False]
    assert len({row["speaker"] for row in result["messages"]}) == 2
    assert all("sender_id" not in row for row in result["messages"])
    assert ctx.event.prompt[0].content == question
    env.plugin.invoke_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_unclear_followup_with_empty_history_does_not_invent_a_referent(env):
    query = make_query()
    env.register(query)
    await env.dispatch(received_context(query))
    ctx = prompt_context(query, "那个要换吗")
    await env.dispatch(ctx)
    read_api = AsyncMock(return_value={"messages": [], "truncated": False})
    tool_plugin = SimpleNamespace(**{**vars(env.plugin), "call_platform_api": read_api})

    result = json.loads(
        await execute(
            SimpleNamespace(plugin=tool_plugin),
            "context",
            {},
            ctx.query_id,
            ctx.query_uuid,
        )
    )

    assert result["status"] == "empty"
    assert result["messages"] == []
    assert result["available_messages"] == 0
    assert ctx.event.prompt[0].content == "那个要换吗"
    assert "指代" in result["instruction"] and "不能编造" in result["instruction"]
    env.plugin.invoke_llm.assert_not_awaited()


def test_listener_and_tool_discovery_prompts_expose_the_same_reference_policy():
    """The model sees both the system guidance and the tool discovery description."""
    root = Path(__file__).resolve().parents[1]
    document = yaml.safe_load(
        (root / "components/tools/read_recent_group_context.yaml").read_text(
            encoding="utf-8"
        )
    )
    tool_prompt = document["spec"]["llm_prompt"]
    assert "提问者自己" in document["metadata"]["description"]["zh_Hans"]
    assert "is_questioner" in GUIDANCE and "is_questioner" in tool_prompt
    assert "先调用read_recent_group_context" in GUIDANCE
    assert "BEFORE asking the user to repeat" in tool_prompt
    assert "不是关键词清单" in GUIDANCE and "not keyword triggers" in tool_prompt
    assert "多个合理指代" in GUIDANCE and "multiple plausible referents" in tool_prompt
    assert "独立问题或背景已充分时不必读取" in GUIDANCE
    assert "self-contained questions" in tool_prompt
    assert "negation or numbers" in tool_prompt
    assert document["spec"]["parameters"]["additionalProperties"] is False
