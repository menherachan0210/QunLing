"""Trusted anchors, unverified forwarded claims, isolated maps, and real pixels."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pkg import forward_tools as forwards
from pkg import tools
from pkg.errors import ImageReadError, VisionError
from test_context_tools import fixture, history

PIXELS = "data:image/jpeg;base64,c3ludGhldGlj"
FORWARDED = {
    "cards": [
        {"card_ref": "card_1", "parent_card_ref": None, "origin": "quoted", "truncated": False,
         "source_message_id": "private-source-id", "messages": [
             {"index": 1, "sender_name": "声称甲", "time": 10, "text": "看这个图案",
              "images": [{"index": 1, "url": "https://example.test/picture?secret=1"}],
              "nested_card_refs": ["card_2"]},
             {"index": 2, "sender_name": "声称乙", "time": 11, "text": "这是转发，不是本群原话",
              "images": [], "nested_card_refs": []},
         ]},
        {"card_ref": "card_2", "parent_card_ref": "card_1", "origin": "nested", "truncated": False,
         "messages": [
             {"index": 1, "sender_name": "声称甲", "time": None, "text": "嵌套的另一张图",
              "images": [{"index": 1, "url": "https://example.test/second?secret=2"}],
              "nested_card_refs": []},
         ]},
    ], "selection": "current_or_quoted", "truncated": False, "stop_reason": "complete", "unavailable_cards": 0,
}


def ready(monkeypatch):
    plugin, _, _, context = fixture()
    context.group_data = {}
    context.group_data_lock = asyncio.Lock()
    plugin.call_platform_api = AsyncMock(return_value=deepcopy(FORWARDED))
    plugin.invoke_llm = AsyncMock(return_value=SimpleNamespace(content="转发图中有蓝色条纹。", tool_calls=[]))
    check = AsyncMock()
    load = AsyncMock(return_value=PIXELS)
    monkeypatch.setattr(forwards, "validate_group_access", check)
    monkeypatch.setattr(tools, "load_image", load)
    return plugin, context, check, load


async def read(plugin, context, **params):
    return await forwards.forward_tools(plugin, context, params)


async def inspect(plugin, context, refs, **params):
    return await forwards.forward_tools(plugin, context, {
        "action": "images", "image_refs": refs, "question": "分析图案", **params
    })


@pytest.mark.asyncio
async def test_default_anchors_current_group_and_preserves_nested_claim_lineage(monkeypatch):
    plugin, context, check, load = ready(monkeypatch)
    result = await read(plugin, context)
    plugin.call_platform_api.assert_awaited_once_with(context.bot, "call_platform_api", {
        "action": "get_group_forward_context", "params": {
            "group_id": "1001", "anchor_message_id": context.anchor,
            "before_time": context.timestamp, "window_seconds": 86400,
        },
    })
    assert result["cards"][1]["parent_card_ref"] == result["cards"][0]["card_ref"]
    assert result["messages"][0]["nested_card_refs"] == [result["cards"][1]["card_ref"]]
    assert result["messages"][0]["claimed_display_name"] == "声称甲"
    assert all(row["speaker_is_verified"] is False for row in result["messages"])
    assert "claimed_time" not in result["messages"][2]
    assert result["messages"][0]["speaker"] != result["messages"][2]["speaker"]
    payload = json.dumps(result, ensure_ascii=False)
    assert "secret" not in payload and "https:" not in payload and "private-source-id" not in payload
    assert "未经核验" in payload and "尚未识别" in payload
    load.assert_not_awaited()
    plugin.invoke_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_shown_history_ref_maps_to_trusted_id_and_never_mutates_history(monkeypatch):
    plugin, context, _, _ = ready(monkeypatch)
    context.history = history(context.timestamp)
    tools.present_history(context, 20)
    before = deepcopy((context.history, context.image_urls, context.shown_images, context.shown_messages))
    result = await read(plugin, context, message_ref="m_1", limit=1)
    assert plugin.call_platform_api.call_args.args[2]["params"]["source_message_id"] == "501"
    assert result["has_more"] and result["next_offset"] == 1
    page = await read(plugin, context, message_ref="m_1", offset=1, limit=1)
    assert page["messages"][0]["message_ref"] == "f_2"
    plugin.call_platform_api.assert_awaited_once()
    assert (context.history, context.image_urls, context.shown_images, context.shown_messages) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"message_ref": "501"}, {"message_ref": "m_1"}, {"message_ref": "f_1"},
    {"res_id": "secret"}, {"group_id": "1002"}, {"url": "https://example.test"},
    {"offset": -1}, {"offset": True}, {"limit": 61}, {"limit": False}, {"action": "send"},
])
async def test_invalid_or_unshown_source_never_calls_platform(monkeypatch, params):
    plugin, context, check, _ = ready(monkeypatch)
    with pytest.raises(VisionError):
        await read(plugin, context, **params)
    plugin.call_platform_api.assert_not_awaited()
    check.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiple_sources_have_distinct_refs_and_bounded_attempts(monkeypatch):
    plugin, context, _, _ = ready(monkeypatch)
    context.history = history(context.timestamp)
    tools.present_history(context, 20)
    pages = [await read(plugin, context)]
    pages.extend([await read(plugin, context, message_ref=ref) for ref in ["m_1", "m_2"]])
    refs = [row["message_ref"] for page in pages for row in page["messages"]]
    images = [image for page in pages for row in page["messages"] for image in row["image_refs"]]
    assert len(refs) == len(set(refs)) and len(images) == len(set(images))
    assert plugin.call_platform_api.await_count == 3
    context.history["messages"].append({"message_id": "503"})
    context.shown_messages["m_3"] = {"message_ref": "m_3"}
    with pytest.raises(VisionError, match="上限"):
        await read(plugin, context, message_ref="m_3")


@pytest.mark.asyncio
async def test_images_require_actual_page_exposure_and_do_not_overwrite_historical_refs(monkeypatch):
    plugin, context, _, load = ready(monkeypatch)
    context.history = history(context.timestamp)
    tools.present_history(context, 20)
    before = deepcopy((context.history, context.image_urls, context.shown_images, context.shown_messages))
    page = await read(plugin, context, limit=1)
    with pytest.raises(VisionError):
        await inspect(plugin, context, ["forwarded_img_2"])
    with pytest.raises(VisionError):
        await inspect(plugin, context, ["img_1"])
    result = await inspect(plugin, context, page["messages"][0]["image_refs"], message_refs=["f_1"])
    assert result["source"].startswith("合并转发卡片")
    assert context.vision_used and not context.vision_inflight
    assert (context.history, context.image_urls, context.shown_images, context.shown_messages) == before
    load.assert_awaited_once_with("https://example.test/picture?secret=1")
    messages = plugin.invoke_llm.call_args.kwargs["messages"]
    assert "可能被编辑或伪造" in messages[0].content
    assert "本群历史图片" not in json.dumps(result, ensure_ascii=False)
    payload = json.dumps([message.model_dump() for message in messages], ensure_ascii=False)
    assert PIXELS in payload and "声称甲" in payload and "fwd_1" in payload
    assert "secret" not in payload


@pytest.mark.asyncio
async def test_forwarded_images_and_history_share_live_paid_guard(monkeypatch):
    plugin, context, _, _ = ready(monkeypatch)
    context.history = history(context.timestamp)
    tools.present_history(context, 20)
    await read(plugin, context)
    await inspect(plugin, context, ["forwarded_img_1"])
    with pytest.raises(ImageReadError) as failure:
        await tools.read_images(plugin, context, {"image_refs": ["img_1"], "question": "看图"})
    assert failure.value.code == "vision_already_used"
    plugin.invoke_llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_history_analysis_blocks_forwarded_paid_retry(monkeypatch):
    plugin, context, _, load = ready(monkeypatch)
    context.history = history(context.timestamp)
    tools.present_history(context, 20)
    await read(plugin, context)
    await tools.read_images(plugin, context, {"image_refs": ["img_1"], "question": "看图"})
    with pytest.raises(ImageReadError) as failure:
        await inspect(plugin, context, ["forwarded_img_1"])
    assert failure.value.code == "vision_already_used"
    assert load.await_count == 1
    plugin.invoke_llm.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["download", "vision"])
async def test_forwarded_cancellation_releases_shared_guard_and_keeps_submission_claim(monkeypatch, phase):
    plugin, context, _, load = ready(monkeypatch)
    await read(plugin, context)
    entered = asyncio.Event()

    async def pending(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    if phase == "download":
        load.side_effect = pending
    else:
        plugin.invoke_llm.side_effect = pending
    task = asyncio.create_task(inspect(plugin, context, ["forwarded_img_1"]))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not context.vision_inflight and context.vision_used is (phase == "vision")


@pytest.mark.asyncio
async def test_new_history_page_cannot_revoke_forwarded_image_refs(monkeypatch):
    plugin, context, _, load = ready(monkeypatch)
    await read(plugin, context)
    context.history = history(context.timestamp)
    tools.present_history(context, 1)
    assert (await inspect(plugin, context, ["forwarded_img_1"]))["status"] == "ok"
    load.assert_awaited_once_with("https://example.test/picture?secret=1")


@pytest.mark.asyncio
async def test_cross_query_image_refs_are_not_authorized(monkeypatch):
    plugin, context, _, load = ready(monkeypatch)
    await read(plugin, context)
    other = fixture()[3]
    other.group_data = {}
    with pytest.raises(VisionError, match="先读取"):
        await inspect(plugin, other, ["forwarded_img_1"])
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_cards_unsafe_images_and_recent_candidate_ambiguity_are_visible(monkeypatch):
    plugin, context, _, _ = ready(monkeypatch)
    raw = plugin.call_platform_api.return_value
    raw["cards"][0]["messages"][0]["images"][0]["url"] = "http://127.0.0.1/secret"
    raw["cards"][1].update(parent_card_ref=None, error="timeout")
    raw.update(selection="recent_candidates", stop_reason="partial_failure", unavailable_cards=1, truncated=True)
    result = await read(plugin, context)
    assert result["messages"][0]["image_refs"] == []
    assert result["messages"][0]["unavailable_image_count"] == 1
    assert result["truncated"] and result["unavailable_cards"] == 1
    assert result["selection"] == "recent_candidates" and "多张候选" in result["instruction"]
    assert "127.0.0.1" not in json.dumps(result)


@pytest.mark.asyncio
async def test_same_display_name_does_not_merge_distinct_forward_speakers(monkeypatch):
    plugin, context, _, _ = ready(monkeypatch)
    nodes = plugin.call_platform_api.return_value["cards"][0]["messages"]
    nodes[0].update(sender_name="同名", speaker="forward_speaker_1")
    nodes[1].update(sender_name="同名", speaker="forward_speaker_2")
    nodes.append({"sender_name": "同名", "speaker": "forward_speaker_1", "text": "第一人继续", "images": []})
    nodes.extend([{"sender_name": "同名", "text": "身份未提供", "images": []}] * 2)
    rows = (await read(plugin, context))["messages"]
    assert rows[0]["speaker"] != rows[1]["speaker"]
    assert rows[0]["speaker"] == rows[2]["speaker"]
    assert rows[3]["speaker"] != rows[4]["speaker"]
    assert all(not row["speaker_is_verified"] for row in rows)


@pytest.mark.asyncio
async def test_unavailable_images_reported_by_host_survive_without_any_url(monkeypatch):
    plugin, context, _, _ = ready(monkeypatch)
    node = plugin.call_platform_api.return_value["cards"][0]["messages"][0]
    node.update(images=[], unavailable_images=3)
    row = (await read(plugin, context))["messages"][0]
    assert row["unavailable_image_count"] == 3 and row["image_refs"] == []


@pytest.mark.asyncio
async def test_page_text_budget_does_not_drop_remaining_rows(monkeypatch):
    plugin, context, _, _ = ready(monkeypatch)
    card = plugin.call_platform_api.return_value["cards"][0]
    card["messages"] = [
        {"index": i + 1, "sender_name": "甲", "text": "字" * 4000, "images": [], "nested_card_refs": []}
        for i in range(6)
    ]
    plugin.call_platform_api.return_value["cards"] = [card]
    first = await read(plugin, context, limit=60)
    assert len(first["messages"]) == 4 and first["next_offset"] == 4
    second = await read(plugin, context, offset=first["next_offset"])
    assert len(second["messages"]) == 2 and not second["has_more"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["cycle", "missing_parent", "duplicate_card", "too_many_cards", "too_many_messages", "not_dict"])
async def test_invalid_forward_structure_never_publishes_partial_refs(monkeypatch, fault):
    plugin, context, _, _ = ready(monkeypatch)
    raw = plugin.call_platform_api.return_value
    if fault == "cycle":
        raw["cards"][0]["parent_card_ref"] = "card_2"
    elif fault == "missing_parent":
        raw["cards"][0]["parent_card_ref"] = "card_missing"
    elif fault == "duplicate_card":
        raw["cards"][1]["card_ref"] = "card_1"
    elif fault == "too_many_cards":
        raw["cards"] *= 3
    elif fault == "too_many_messages":
        raw["cards"][0]["messages"] *= 100
    else:
        plugin.call_platform_api.return_value = None
    with pytest.raises(VisionError):
        await read(plugin, context)
    state = context.group_data["forwarded"]
    assert state["snapshots"] == {} and state["image_urls"] == {} and state["shown_images"] == set()


@pytest.mark.asyncio
async def test_transport_errors_are_redacted_and_retry_is_bounded(monkeypatch):
    plugin, context, _, _ = ready(monkeypatch)
    plugin.call_platform_api.side_effect = RuntimeError("secret res_id and URL")
    for _ in range(3):
        with pytest.raises(VisionError) as error:
            await read(plugin, context)
        assert "secret" not in str(error.value)
    with pytest.raises(VisionError, match="上限"):
        await read(plugin, context)
    assert plugin.call_platform_api.await_count == 3


@pytest.mark.asyncio
async def test_access_denial_prevents_cached_reads_and_image_downloads(monkeypatch):
    plugin, context, check, load = ready(monkeypatch)
    await read(plugin, context)
    check.side_effect = VisionError("访问被撤销")
    with pytest.raises(VisionError, match="撤销"):
        await read(plugin, context)
    with pytest.raises(VisionError, match="撤销"):
        await inspect(plugin, context, ["forwarded_img_1"])
    load.assert_not_awaited()
    plugin.call_platform_api.assert_awaited_once()


@pytest.mark.asyncio
async def test_actual_host_result_flows_to_plugin_without_exposing_resource_ids(monkeypatch):
    from qunling.qq_forward import read_forward_context

    plugin, context, _, load = ready(monkeypatch)
    anchor = {
        "message_type": "group", "group_id": int(context.group),
        "message_id": int(context.anchor), "time": context.timestamp,
        "message": [{"type": "forward", "data": {"id": "private-res-id"}}],
    }
    raw_nodes = [{
        "sender": {"user_id": 301, "nickname": "声称同名"}, "time": context.timestamp - 10,
        "content": [{"type": "text", "data": {"text": "第一人"}},
                    {"type": "image", "data": {"url": "https://example.test/actual?secret=1"}}],
    }, {
        "sender": {"user_id": 302, "nickname": "声称同名"}, "time": context.timestamp - 9,
        "content": [{"type": "text", "data": {"text": "第二人"}}],
    }]
    bot = SimpleNamespace(call_action=AsyncMock(side_effect=[anchor, {"messages": raw_nodes}]))

    async def platform(_bot, _action, args):
        return await read_forward_context(bot, args["params"])

    plugin.call_platform_api.side_effect = platform
    result = await read(plugin, context)
    rows = result["messages"]
    assert rows[0]["speaker"] != rows[1]["speaker"]
    assert rows[0]["claimed_display_name"] == rows[1]["claimed_display_name"]
    assert "private-res-id" not in json.dumps(result) and "secret=1" not in json.dumps(result)
    assert (await inspect(plugin, context, rows[0]["image_refs"]))["status"] == "ok"
    load.assert_awaited_once_with("https://example.test/actual?secret=1")
