import asyncio
from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langbot_plugin.api.entities.builtin.platform.message import (
    MessageChain,
    Plain,
    Source,
)

from pkg.context import ContextStore, KEY, TTL
from pkg.errors import ImageReadError, VisionError
from pkg.tools import execute, present_history, read_context, read_images


def fixture():
    plugin = SimpleNamespace(
        contexts=ContextStore(), get_config=lambda: {"model_uuid": "vision-model"}
    )
    event = SimpleNamespace(
        launcher_type="group",
        launcher_id="1001",
        sender_id="201",
        message_chain=MessageChain(
            [
                Source(id=-990, time=datetime.now()),
                Plain(text="他们刚才讨论的几个漫画哪个好？"),
            ]
        ),
    )
    ctx = SimpleNamespace(event=event, query_id=10, query_uuid="query-a")
    token = plugin.contexts.capture(plugin, ctx, "bot-a")
    variables = {
        KEY: token,
        "launcher_type": "group",
        "launcher_id": "1001",
        "sender_id": "201",
    }
    item = plugin.contexts.get(plugin, variables, 10, "query-a")
    return plugin, ctx, variables, item


def history(timestamp):
    return {
        "messages": [
            {
                "message_id": "501",
                "sender_id": "301",
                "sender_name": "成员甲",
                "time": timestamp - 20,
                "text": "我想看短篇科幻，推荐星海速写。",
                "images": [{"index": 0, "url": "https://example.test/one?private=1"}],
            },
            {
                "message_id": "502",
                "sender_id": "302",
                "sender_name": "成员乙",
                "time": timestamp - 10,
                "text": "雨巷日记是长篇日常；你要短篇就选前一个。",
                "reply_message_id": "501",
                "images": [{"index": 0, "url": "https://example.test/two?private=2"}],
            },
        ],
        "truncated": False,
        "window_seconds": 3600,
    }


@pytest.mark.parametrize(
    "field,value",
    [("launcher_type", "person"), ("launcher_id", "1002"), ("sender_id", "202")],
)
def test_scope_mismatch_denied(field, value):
    plugin, _, variables, _ = fixture()
    variables[field] = value
    with pytest.raises(VisionError):
        plugin.contexts.get(plugin, variables, 10, "query-a")


@pytest.mark.parametrize(
    "query_id,query_uuid", [(11, "query-a"), (10, "query-b"), (10, None)]
)
def test_other_query_cannot_reuse_token(query_id, query_uuid):
    plugin, _, variables, _ = fixture()
    with pytest.raises(VisionError):
        plugin.contexts.get(plugin, variables, query_id, query_uuid)


def test_expiry_revocation_and_bounded_entries():
    plugin, ctx, variables, _ = fixture()
    for i in range(257):
        ctx.query_id = i + 100
        plugin.contexts.capture(plugin, ctx, "bot-a")
    assert len(plugin.contexts.entries) == 256
    assert plugin.contexts.get(plugin, variables, 10, "query-a").active
    plugin, _, variables, item = fixture()
    plugin.contexts.clock = lambda: item.created + TTL
    with pytest.raises(VisionError):
        plugin.contexts.get(plugin, variables, 10, "query-a")
    plugin, _, variables, _ = fixture()
    plugin.contexts.clear()
    with pytest.raises(VisionError):
        plugin.contexts.get(plugin, variables, 10, "query-a")


def test_private_chat_and_missing_anchor_rejected():
    plugin, ctx, _, _ = fixture()
    ctx.event.launcher_type = "person"
    with pytest.raises(ValueError):
        plugin.contexts.capture(plugin, ctx, "bot-a")
    ctx.event.launcher_type = "group"
    ctx.event.message_chain = MessageChain([Plain(text="hello")])
    with pytest.raises(ValueError):
        plugin.contexts.capture(plugin, ctx, "bot-a")


def test_cross_installation_and_workspace_rejected():
    plugin, ctx, variables, _ = fixture()
    binding = SimpleNamespace(
        instance_uuid="instance-a",
        workspace_uuid="workspace-a",
        installation_uuid="install-a",
        runtime_revision="revision-1",
        placement_generation=1,
    )
    plugin.get_installation_binding = lambda: binding
    ctx.workspace_uuid = "workspace-b"
    with pytest.raises(ValueError):
        plugin.contexts.capture(plugin, ctx, "bot-a")
    ctx.workspace_uuid = "workspace-a"
    variables[KEY] = plugin.contexts.capture(plugin, ctx, "bot-a")
    binding.runtime_revision = "revision-2"
    with pytest.raises(VisionError):
        plugin.contexts.get(plugin, variables, 10, "query-a")


@pytest.mark.asyncio
async def test_context_reads_same_group_once_and_separates_other_members():
    plugin, _, _, item = fixture()
    plugin.call_platform_api = AsyncMock(return_value=history(item.timestamp))
    first = await read_context(plugin, item, {"limit": 1})
    assert first["has_more_in_snapshot"]
    assert first["messages"][0]["image_refs"] == ["img_2"]
    second = await read_context(plugin, item, {"limit": 100})
    plugin.call_platform_api.assert_awaited_once()
    args = plugin.call_platform_api.call_args.args
    assert args[0] == "bot-a"
    assert args[2]["params"] == {
        "group_id": "1001",
        "before_message_id": "-990",
        "before_time": item.timestamp,
    }
    assert second["messages"][0]["speaker"] != second["messages"][1]["speaker"]
    assert second["messages"][1]["reply_to"] == "m_1"
    assert "科幻" in second["messages"][0]["text"]
    assert "private=" not in json.dumps(second)
    assert "sender_id" not in json.dumps(second)
    assert not hasattr(
        plugin, "invoke_llm"
    )  # Reading text requires no additional model.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"group_id": "999"},
        {"limit": True},
        {"limit": 0},
        {"limit": 101},
        {"limit": "50"},
    ],
)
async def test_context_rejects_untrusted_destinations_and_invalid_limits(params):
    plugin, _, _, item = fixture()
    plugin.call_platform_api = AsyncMock()
    with pytest.raises(VisionError):
        await read_context(plugin, item, params)
    plugin.call_platform_api.assert_not_called()


@pytest.mark.asyncio
async def test_context_failure_does_not_leak_platform_exception():
    plugin, _, _, item = fixture()
    plugin.call_platform_api = AsyncMock(side_effect=RuntimeError("secret signed url"))
    with pytest.raises(VisionError, match="暂时无法") as caught:
        await read_context(plugin, item, {})
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_multi_image_analysis_uses_actual_pixels_once(monkeypatch):
    plugin, _, _, item = fixture()
    item.history = history(item.timestamp)
    present_history(item, 100)
    load = AsyncMock(return_value="data:image/jpeg;base64,ZmFrZQ==")
    monkeypatch.setattr("pkg.tools.load_image", load)
    plugin.invoke_llm = AsyncMock(
        return_value=SimpleNamespace(
            content="第一张是短篇科幻，第二张是日常漫画。", tool_calls=[]
        )
    )
    result = await read_images(
        plugin,
        item,
        {"image_refs": ["img_1", "img_2"], "question": "哪张介绍的是短篇？"},
    )
    assert result["status"] == "ok"
    args = plugin.invoke_llm.call_args.kwargs
    assert args["funcs"] == []
    assert args["llm_model_uuid"] == "vision-model"
    assert sum(x.type == "image_url" for x in args["messages"][1].content) == 2
    assert "secret" not in json.dumps(result)
    with pytest.raises(VisionError, match="已调用"):
        await read_images(plugin, item, {"image_refs": ["img_1"], "question": "再看"})
    plugin.invoke_llm.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refs",
    [["img_1"], ["https://example.test/"], ["img_2", "img_2"], [], ["img_1"] * 5, [1]],
)
async def test_unseen_or_invalid_image_refs_rejected(monkeypatch, refs):
    plugin, _, _, item = fixture()
    item.history = history(item.timestamp)
    present_history(item, 1)  # img_1 exists privately but was never shown to the model.
    load = AsyncMock()
    monkeypatch.setattr("pkg.tools.load_image", load)
    with pytest.raises(VisionError):
        await read_images(plugin, item, {"image_refs": refs, "question": "描述"})
    load.assert_not_called()


@pytest.mark.asyncio
async def test_failed_model_call_is_not_automatically_repeated(monkeypatch):
    plugin, _, _, item = fixture()
    item.history = history(item.timestamp)
    present_history(item, 2)
    monkeypatch.setattr(
        "pkg.tools.load_image", AsyncMock(return_value="data:image/jpeg;base64,AA==")
    )
    plugin.invoke_llm = AsyncMock(side_effect=TimeoutError())
    params = {"image_refs": ["img_1"], "question": "读图"}
    with pytest.raises(ImageReadError) as caught:
        await read_images(plugin, item, params)
    assert caught.value.code == "vision_timeout"
    assert caught.value.stage == "vision"
    assert not caught.value.retryable
    with pytest.raises(VisionError, match="已调用"):
        await read_images(plugin, item, params)
    plugin.invoke_llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_rechecks_bot_and_suppresses_arbitrary_errors(monkeypatch):
    plugin, _, variables, _ = fixture()
    api = SimpleNamespace(
        get_query_vars=AsyncMock(return_value=variables),
        get_bot_uuid=AsyncMock(return_value="bot-b"),
    )
    monkeypatch.setattr("pkg.tools.QueryBasedAPIProxy", lambda **kw: api)
    plugin.plugin_runtime_handler = None
    plugin.call_platform_api = AsyncMock()
    result = json.loads(
        await execute(SimpleNamespace(plugin=plugin), "context", {}, 10, "query-a")
    )
    assert result["status"] == "error"
    plugin.call_platform_api.assert_not_called()


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed(monkeypatch):
    plugin, _, variables, _ = fixture()
    api = SimpleNamespace(
        get_query_vars=AsyncMock(side_effect=asyncio.CancelledError()),
        get_bot_uuid=AsyncMock(return_value="bot-a"),
    )
    monkeypatch.setattr("pkg.tools.QueryBasedAPIProxy", lambda **kw: api)
    plugin.plugin_runtime_handler = None
    with pytest.raises(asyncio.CancelledError):
        await execute(SimpleNamespace(plugin=plugin), "context", {}, 10, "query-a")


@pytest.mark.asyncio
async def test_parallel_context_reads_share_one_stable_snapshot():
    plugin, _, _, item = fixture()

    async def delayed(*args):
        await asyncio.sleep(0)
        return history(item.timestamp)

    plugin.call_platform_api = AsyncMock(side_effect=delayed)
    first, second = await asyncio.gather(
        read_context(plugin, item, {"limit": 1}),
        read_context(plugin, item, {"limit": 100}),
    )
    plugin.call_platform_api.assert_awaited_once()
    assert first["messages"][0]["image_refs"] == ["img_2"]
    assert second["messages"][1]["image_refs"] == ["img_2"]
    assert item.image_urls["img_2"].endswith("two?private=2")


def test_questioner_is_identified_without_disclosing_qq_id():
    _, _, _, item = fixture()
    item.history = history(item.timestamp)
    item.sender = "302"
    messages = present_history(item, 100)["messages"]
    assert not messages[0]["is_questioner"]
    assert messages[1]["is_questioner"]
    assert "sender_id" not in messages[1]


def test_oversized_snapshot_is_rejected():
    plugin, _, _, item = fixture()
    with pytest.raises(VisionError):
        plugin.contexts.save_history(item, {"messages": ["x" * (1024 * 1024)]})
