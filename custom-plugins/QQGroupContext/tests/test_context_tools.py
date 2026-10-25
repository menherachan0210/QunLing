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

from pkg.context import ContextStore, KEY, TTL, MAX_SNAPSHOT_BYTES, MAX_TOTAL_SNAPSHOT_BYTES
from pkg.errors import ImageReadError, VisionError
from pkg.tools import execute, present_history, read_context, read_images, vision_context


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


def test_context_expires_at_deadline_despite_float_subtraction_roundoff():
    plugin, _, variables, item = fixture()
    item.created = 124.003
    deadline = item.created + TTL
    assert deadline - item.created < TTL
    plugin.contexts.clock = lambda: deadline
    with pytest.raises(VisionError):
        plugin.contexts.get(plugin, variables, 10, "query-a")


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
        "window_seconds": 86400,
        "max_messages": 1000,
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
        plugin.contexts.save_history(item, {"messages": ["x" * MAX_SNAPSHOT_BYTES]})


def large_history(timestamp, count=1000, text="earlier context"):
    return {
        "messages": [
            {
                "message_id": str(10000 + i),
                "sender_id": str(201 + i % 3),
                "sender_name": f"member-{i % 3}",
                "time": timestamp - (count - i) * 60,
                "text": text,
                "images": [{"index": 0, "url": f"https://example.test/{i}?private=yes"}]
                if i % 100 == 0 else [],
                **({"reply_message_id": str(10000 + i - 1)} if i else {}),
            }
            for i in range(count)
        ],
        "window_seconds": 86400,
        "max_messages": 1000,
        "scanned_messages": count,
        "truncated": True,
    }


@pytest.mark.asyncio
async def test_all_thousand_messages_are_reachable_in_stable_opaque_pages():
    plugin, _, _, item = fixture()
    plugin.call_platform_api = AsyncMock(return_value=large_history(item.timestamp))
    rows, cursor = [], None
    for page in range(10):
        params = {"limit": 100}
        if cursor:
            params["before_ref"] = cursor
        result = await read_context(plugin, item, params)
        assert len(result["messages"]) == 100
        assert result["window_seconds"] == 86400
        assert result["available_messages"] == 1000
        assert result["history_truncated"]
        cursor = result["next_before_ref"]
        assert bool(cursor) == (page < 9)
        rows = result["messages"] + rows
    assert [m["message_ref"] for m in rows] == [f"m_{i}" for i in range(1, 1001)]
    assert rows[0]["seconds_before_question"] > 3600
    assert rows[100]["image_refs"] == ["img_2"]
    assert rows[100]["reply_to"] == "m_100"
    assert rows[0]["speaker"] == rows[3]["speaker"]
    assert rows[0]["is_questioner"] and not rows[1]["is_questioner"]
    assert "private=" not in json.dumps(rows)
    assert "sender_id" not in json.dumps(rows)
    plugin.call_platform_api.assert_awaited_once()
    newest = await read_context(plugin, item, {"limit": 1})
    assert newest["messages"] == rows[-1:]
    assert item.image_urls["img_2"].endswith("/100?private=yes")


@pytest.mark.asyncio
async def test_text_budget_paginates_whole_messages_without_losing_older_text():
    plugin, _, _, item = fixture()
    plugin.call_platform_api = AsyncMock(
        return_value=large_history(item.timestamp, count=40, text="字" * 1000)
    )
    seen, cursor = [], None
    while True:
        result = await read_context(plugin, item, {
            "limit": 100, **({"before_ref": cursor} if cursor else {}),
        })
        rows = result["messages"]
        assert sum(len(row["text"]) for row in rows) <= 16000
        assert all(row["text"] == "字" * 1000 for row in rows)
        seen = [row["message_ref"] for row in rows] + seen
        cursor = result["next_before_ref"]
        if cursor is None:
            break
    assert seen == [f"m_{i}" for i in range(1, 41)]
    empty = await read_context(plugin, item, {"before_ref": "m_1"})
    assert empty["status"] == "empty"
    assert not empty["has_more_in_snapshot"]


@pytest.mark.asyncio
async def test_search_and_time_jump_reach_old_context_in_one_call_with_long_history():
    plugin, _, _, item = fixture()
    snapshot = large_history(item.timestamp, text="字" * 1000)
    snapshot["messages"][3]["text"] = "星海企划的进度确认"
    plugin.call_platform_api = AsyncMock(return_value=snapshot)
    found = await read_context(plugin, item, {"search_text": "星海"})
    assert [row["message_ref"] for row in found["messages"]] == [f"m_{i}" for i in range(2, 7)]
    assert found["messages"][2]["text"] == "星海企划的进度确认"
    assert not found["has_more_in_snapshot"]
    by_time = await read_context(plugin, item, {"min_age_seconds": 59000})
    assert by_time["messages"][-1]["seconds_before_question"] >= 59000
    assert "m_4" in [row["message_ref"] for row in by_time["messages"]]
    plugin.call_platform_api.assert_awaited_once()
    empty = await read_context(plugin, item, {"search_text": "没有这个词"})
    assert empty["status"] == "empty"
    # A literal regexp-shaped string is not interpreted as a pattern.
    assert (await read_context(plugin, item, {"search_text": ".*"}))["status"] == "empty"


@pytest.mark.asyncio
async def test_filtered_pagination_keeps_matches_neighbors_and_reference_identity():
    plugin, _, _, item = fixture()
    snapshot = large_history(item.timestamp, count=100)
    for index in (3, 40, 90):
        snapshot["messages"][index]["text"] = "Project Sun"
    plugin.call_platform_api = AsyncMock(return_value=snapshot)
    seen, cursor = [], None
    while True:
        result = await read_context(plugin, item, {
            "search_text": "SUN", "limit": 3, "min_age_seconds": 1200,
            **({"before_ref": cursor} if cursor else {}),
        })
        seen = [row["message_ref"] for row in result["messages"]] + seen
        cursor = result["next_before_ref"]
        if cursor is None:
            break
    assert seen == [f"m_{i + 1}" for i in [1, 2, 3, 4, 5, 38, 39, 40, 41, 42]]
    plugin.call_platform_api.assert_awaited_once()


def test_filtered_distant_hits_are_not_labeled_nearby_in_vision_context():
    _, _, _, item = fixture()
    item.history = large_history(item.timestamp, count=1000)
    item.history["messages"][100]["text"] = "unique keyword"
    item.history["messages"][900]["text"] = "unique keyword"
    result = present_history(item, 100, search_text="unique keyword")
    assert len(result["messages"]) == 10
    evidence = vision_context(item, ["img_2"], "描述早先那张图", [])
    assert all(int(row["message_ref"][2:]) < 200 for row in evidence["discussion"])


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"search_text": None}, {"search_text": ""}, {"search_text": "x" * 81},
    {"search_text": "line\nbreak"}, {"min_age_seconds": True},
    {"min_age_seconds": -1}, {"min_age_seconds": 604801},
])
async def test_invalid_search_parameters_do_not_fetch_history(params):
    plugin, _, _, item = fixture()
    plugin.call_platform_api = AsyncMock()
    with pytest.raises(VisionError):
        await read_context(plugin, item, params)
    plugin.call_platform_api.assert_not_called()


@pytest.mark.asyncio
async def test_image_from_previous_page_remains_usable_after_reading_older_page(monkeypatch):
    plugin, _, _, item = fixture()
    plugin.call_platform_api = AsyncMock(return_value=history(item.timestamp))
    newest = await read_context(plugin, item, {"limit": 1})
    await read_context(plugin, item, {"before_ref": newest["next_before_ref"]})
    load = AsyncMock(return_value="data:image/jpeg;base64,AA==")
    monkeypatch.setattr("pkg.tools.load_image", load)
    plugin.invoke_llm = AsyncMock(return_value=SimpleNamespace(content="合成分析", tool_calls=[]))
    result = await read_images(plugin, item, {
        "image_refs": ["img_2"], "message_refs": ["m_2"], "question": "读刚才页的图",
    })
    assert result["status"] == "ok"
    load.assert_awaited_once_with("https://example.test/two?private=2")


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [None, True, 1, "m_1", "https://example.test", "-990"])
async def test_cannot_fabricate_cursor_before_reading_this_query(cursor):
    plugin, _, _, item = fixture()
    plugin.call_platform_api = AsyncMock()
    with pytest.raises(VisionError):
        await read_context(plugin, item, {"before_ref": cursor})
    plugin.call_platform_api.assert_not_called()


@pytest.mark.asyncio
async def test_unseen_cursor_and_other_query_cursor_are_rejected():
    plugin, _, _, item = fixture()
    plugin.call_platform_api = AsyncMock(return_value=history(item.timestamp))
    await read_context(plugin, item, {"limit": 1})
    with pytest.raises(VisionError):
        await read_context(plugin, item, {"before_ref": "m_1"})
    _, _, _, other = fixture()
    with pytest.raises(VisionError):
        await read_context(plugin, other, {"before_ref": "m_2"})
    plugin.call_platform_api.assert_awaited_once()


@pytest.mark.asyncio
async def test_administrator_scope_is_forwarded_and_snapshot_is_frozen():
    plugin, _, _, item = fixture()
    config = {"history_hours": 6, "history_max_messages": 300}
    plugin.get_config = lambda: config
    snapshot = history(item.timestamp)
    plugin.call_platform_api = AsyncMock(return_value=snapshot)
    first = await read_context(plugin, item, {"limit": 1})
    sent = plugin.call_platform_api.call_args.args[2]["params"]
    assert sent["window_seconds"] == 21600 and sent["max_messages"] == 300
    config["history_hours"] = 168
    second = await read_context(plugin, item, {"before_ref": first["next_before_ref"]})
    assert second["window_seconds"] == 3600  # Actual platform window takes priority.
    assert second["messages"][0]["message_ref"] == "m_1"
    plugin.call_platform_api.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [
    {"history_hours": True}, {"history_hours": 0}, {"history_hours": 169},
    {"history_hours": "24"}, {"history_max_messages": 99},
    {"history_max_messages": 1001}, {"history_max_messages": 100.5},
])
async def test_invalid_admin_limits_fail_before_platform_call(config):
    plugin, _, _, item = fixture()
    plugin.get_config = lambda: config
    plugin.call_platform_api = AsyncMock()
    with pytest.raises(VisionError):
        await read_context(plugin, item, {})
    plugin.call_platform_api.assert_not_called()


def test_snapshot_cache_bounds_total_bytes_while_preserving_latest_query():
    plugin, ctx, _, item = fixture()
    payload = {"messages": ["x" * (6 * 1024 * 1024)]}
    for i in range(4):
        ctx.query_id = i + 20
        token = plugin.contexts.capture(plugin, ctx, "bot-a")
        current = plugin.contexts.entries[token]
        plugin.contexts.save_history(current, payload)
    assert sum(entry.history_bytes for entry in plugin.contexts.entries.values()) <= MAX_TOTAL_SNAPSHOT_BYTES
    assert current in plugin.contexts.entries.values()
    assert len([entry for entry in plugin.contexts.entries.values() if entry.history]) == 2
