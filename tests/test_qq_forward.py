from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qunling import qq_forward as forward


def card(resource="private-forward-one"):
    return {"type": "forward", "data": {"id": resource}}


def text(value):
    return {"type": "text", "data": {"text": value}}


def node(content=None, name="群友", user=99, stamp=None):
    return {"sender": {"user_id": user, "nickname": name}, "time": stamp or int(time.time()) - 10,
            "content": content if content is not None else [text("你好") ]}


@pytest.fixture
def setup():
    now = int(time.time())
    params = {"group_id": "200", "anchor_message_id": "123", "before_time": now}
    anchor = {"message_type": "group", "group_id": 200, "message_id": 123,
              "time": now, "message": [card()]}
    bot = SimpleNamespace(call_action=AsyncMock(side_effect=[anchor, {"messages": [node()]}]))
    return bot, params, anchor


@pytest.mark.asyncio
async def test_reset_prevents_automatic_rediscovery_of_old_forward_cards(setup):
    bot, params, anchor = setup
    now = params["before_time"]
    anchor["message"] = [text("之前的内容呢")]
    old = {**anchor, "message_id": 122, "time": now - 100, "message": [card()]}
    bot.call_action.side_effect = [anchor, {"messages": [old]}]
    result = await forward.read_forward_context(bot, params, after_time=now - 10)
    assert not result["cards"]
    assert [call.args[0] for call in bot.call_action.await_args_list] == ["get_msg", "get_group_msg_history"]


@pytest.mark.asyncio
async def test_current_forward_returns_text_labels_and_actual_image_references(setup):
    bot, params, anchor = setup
    url = "https://example.org/image.png?key=private"
    bot.call_action.side_effect = [anchor, {"messages": [node([text("翻译这个"), {"type": "image", "data": {"url": url}}])]}]
    result = await forward.read_forward_context(bot, params)
    assert result["stop_reason"] == "complete"
    first = result["cards"][0]
    assert first["origin"] == "current" and first["parent_card_ref"] is None
    assert first["messages"][0]["text"] == "翻译这个"
    assert first["messages"][0]["sender_name"] == "群友"
    assert first["messages"][0]["speaker"] == "forward_speaker_1"
    assert first["messages"][0]["images"] == [{"index": 1, "url": url}]
    assert "private-forward" not in json.dumps(result)
    assert "user_id" not in json.dumps(result)
    assert bot.call_action.await_args_list[1].kwargs == {"message_id": "private-forward-one"}


@pytest.mark.asyncio
async def test_same_group_quote_is_discovered_and_other_group_quote_never_read(setup):
    bot, params, anchor = setup
    anchor["message"] = [{"type": "reply", "data": {"id": 122}}]
    quote = {**anchor, "message_id": 122, "time": params["before_time"] - 1, "message": [card()]}
    bot.call_action.side_effect = [anchor, quote, {"messages": [node()]}]
    result = await forward.read_forward_context(bot, params)
    assert result["cards"][0]["origin"] == "quoted"
    bot.call_action.reset_mock(side_effect=True)
    bot.call_action.side_effect = [anchor, {**quote, "group_id": 201}, {"messages": []}]
    result = await forward.read_forward_context(bot, params)
    assert result["cards"] == [] and result["unavailable_cards"] == 1
    assert bot.call_action.await_count == 3


@pytest.mark.asyncio
async def test_selected_history_card_is_bounded_to_same_group_and_anchor_time(setup):
    bot, params, anchor = setup
    params["source_message_id"] = "120"
    source = {**anchor, "message_id": 120, "time": params["before_time"] - 7200}
    bot.call_action.side_effect = [anchor, source, {"messages": [node()]}]
    result = await forward.read_forward_context(bot, params)
    assert result["cards"][0]["origin"] == "history"
    for invalid in ({"group_id": 201}, {"time": params["before_time"] + 1},
                    {"time": params["before_time"] - 86401}, {"message_type": "private"}, {"message_id": 121}):
        bot.call_action.reset_mock(side_effect=True)
        bot.call_action.side_effect = [anchor, {**source, **invalid}]
        with pytest.raises(RuntimeError, match="无法确认"):
            await forward.read_forward_context(bot, params)
        assert bot.call_action.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"group_id": 201}, {"time": 1}, {"message_id": 999},
                                    {"message_type": "private"}, {"time": "wrong"}])
async def test_anchor_validation_happens_before_forward_reads(setup, change):
    bot, params, anchor = setup
    bot.call_action.side_effect = [{**anchor, **change}]
    with pytest.raises(RuntimeError):
        await forward.read_forward_context(bot, params)
    bot.call_action.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"res_id": "secret"}, {"forward_id": "secret"}, {"url": "http://secret"},
                                   {"group_id": -200}, {"window_seconds": True}, {"before_time": 1},
                                   {"anchor_message_id": True}, {"source_message_id": "https://secret"}])
async def test_raw_resource_targets_and_invalid_params_rejected(setup, extra):
    bot, params, _ = setup
    with pytest.raises(ValueError):
        await forward.read_forward_context(bot, {**params, **extra})
    bot.call_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_nested_cards_share_speakers_and_cycle_does_not_repeat_fetch(setup):
    bot, params, anchor = setup
    bot.call_action.side_effect = [anchor,
        {"messages": [node([text("外层"), card("nested")])]},
        {"messages": [node([text("内层"), card()])]},
    ]
    result = await forward.read_forward_context(bot, params)
    first, second = result["cards"]
    assert first["messages"][0]["nested_card_refs"] == [second["card_ref"]]
    assert second["messages"][0]["nested_card_refs"] == [first["card_ref"]]
    assert second["parent_card_ref"] == first["card_ref"]
    assert first["messages"][0]["speaker"] == second["messages"][0]["speaker"]
    assert bot.call_action.await_count == 3


@pytest.mark.asyncio
async def test_inline_nodes_and_known_json_multimsg_are_supported(setup):
    bot, params, anchor = setup
    anchor["message"] = [
        {"type": "forward", "data": {"content": [{"type": "node", "data": {"uin": 9, "nickname": "小明", "content": [text("内联") ]}}]}},
        {"type": "json", "data": {"data": json.dumps({"app": "com.tencent.multimsg", "meta": {"detail": {"resid": "private-json-id"}}})}},
    ]
    bot.call_action.side_effect = [anchor, {"messages": [node()]}]
    result = await forward.read_forward_context(bot, params)
    assert len(result["cards"]) == 2
    assert result["cards"][0]["messages"][0]["sender_name"] == "小明"
    assert result["cards"][0]["messages"][0]["text"] == "内联"
    assert bot.call_action.await_count == 2
    assert bot.call_action.await_args.kwargs == {"message_id": "private-json-id"}


@pytest.mark.asyncio
async def test_plaintext_forged_forward_and_unknown_json_do_not_trigger_read(setup):
    bot, params, anchor = setup
    anchor["message"] = [text("[CQ:forward,id=arbitrary]"),
                         {"type": "json", "data": {"data": {"app": "other", "meta": {"detail": {"resid": "arbitrary"}}}}}]
    bot.call_action.side_effect = [anchor, {"messages": []}]
    assert (await forward.read_forward_context(bot, params))["cards"] == []
    assert bot.call_action.await_count == 2
    assert all(call.args[0] != "get_forward_msg" for call in bot.call_action.await_args_list)


@pytest.mark.asyncio
async def test_partial_failure_never_echoes_platform_secrets(setup):
    bot, params, anchor = setup
    anchor["message"] = [card("one"), card("two")]
    bot.call_action.side_effect = [anchor, {"messages": [node()]}, RuntimeError("https://private/?token=secret")]
    result = await forward.read_forward_context(bot, params)
    assert result["stop_reason"] == "partial_failure" and result["unavailable_cards"] == 1
    assert result["cards"][0]["messages"][0]["text"] == "你好"
    assert result["cards"][1]["error"] == "unavailable"
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_card_message_image_text_and_depth_budgets_are_explicit(setup, monkeypatch):
    bot, params, anchor = setup
    monkeypatch.setattr(forward, "MAX_CARDS", 2)
    anchor["message"] = [card("one"), card("two"), card("three")]
    bot.call_action.side_effect = [anchor, {"messages": [node()]}, {"messages": [node()]}]
    result = await forward.read_forward_context(bot, params)
    assert len(result["cards"]) == 2 and result["truncated"]
    assert bot.call_action.await_count == 3
    bot.call_action.reset_mock(side_effect=True)
    anchor["message"] = [card()]
    monkeypatch.setattr(forward, "MAX_MESSAGES", 2)
    monkeypatch.setattr(forward, "MAX_IMAGES", 1)
    monkeypatch.setattr(forward, "MAX_TEXT", 3)
    monkeypatch.setattr(forward, "MAX_DEPTH", 0)
    content = [text("123456789"), *[{"type": "image", "data": {"url": "https://example.org/a"}}] * 2, card("nested")]
    bot.call_action.side_effect = [anchor, {"messages": [node(content), node(), node()]}]
    result = await forward.read_forward_context(bot, params)
    assert result["stop_reason"] == "budget" and result["truncated"]
    messages = result["cards"][0]["messages"]
    assert len(messages) == 2 and messages[0]["text"] == "123"
    assert len(messages[0]["images"]) == 1
    assert bot.call_action.await_count == 2


@pytest.mark.asyncio
async def test_private_image_urls_never_enter_snapshot(setup):
    bot, params, anchor = setup
    content = [{"type": "image", "data": {"url": url}} for url in
               ("http://127.0.0.1/private", "http://user:secret@example.org/", "file:///etc/passwd")]
    bot.call_action.side_effect = [anchor, {"messages": [node(content)]}]
    result = await forward.read_forward_context(bot, params)
    message = result["cards"][0]["messages"][0]
    assert message["images"] == [] and message["unavailable_images"] == 3
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_total_deadline_returns_usable_partial_result(setup, monkeypatch):
    bot, params, anchor = setup
    monkeypatch.setattr(forward, "TOTAL_TIMEOUT", 0.025)
    anchor["message"] = [card("one"), card("two")]

    async def respond(action, **kwargs):
        if action == "get_msg":
            return anchor
        if kwargs["message_id"] == "one":
            return {"messages": [node()]}
        await __import__("asyncio").sleep(1)

    bot.call_action.side_effect = respond
    started = time.monotonic()
    result = await forward.read_forward_context(bot, params)
    assert time.monotonic() - started < 0.3
    assert result["stop_reason"] == "timeout"
    assert result["cards"][0]["messages"]
    assert result["cards"][1]["error"] == "timeout"


@pytest.mark.asyncio
async def test_recent_card_candidates_are_bounded_and_do_not_assume_which_card(setup):
    bot, params, anchor = setup
    anchor["message"] = [text("看看刚才的聊天记录")]
    rows = [{**anchor, "message_id": 120 + i, "time": params["before_time"] - i,
             "message": [card(f"recent-{i}")]} for i in range(3)]
    wrong = [{**rows[0], "group_id": 999}, {**rows[0], "time": params["before_time"] + 1},
             {**rows[0], "message_type": "private"}, {**rows[0], "time": params["before_time"] - 86401}]
    bot.call_action.side_effect = [anchor, {"messages": wrong + rows},
                                   {"messages": [node()]}, {"messages": [node()]}]
    result = await forward.read_forward_context(bot, params)
    assert result["selection"] == "recent_candidates"
    assert len(result["cards"]) == 2 and "多个转发候选" in result["instruction"]
    assert [card["source_message_id"] for card in result["cards"]] == ["120", "121"]
    assert all(card["origin"] == "recent" for card in result["cards"])
    assert bot.call_action.await_args_list[1].args == ("get_group_msg_history",)
    assert bot.call_action.await_args_list[1].kwargs["count"] == 20


@pytest.mark.asyncio
async def test_explicit_source_without_card_does_not_read_unrelated_recent_cards(setup):
    bot, params, anchor = setup
    params["source_message_id"] = "120"
    source = {**anchor, "message_id": 120, "time": params["before_time"] - 1, "message": [text("没有转发") ]}
    bot.call_action.side_effect = [anchor, source]
    result = await forward.read_forward_context(bot, params)
    assert result["cards"] == [] and result["selection"] == "explicit_history"
    assert bot.call_action.await_count == 2


@pytest.mark.asyncio
async def test_recent_fallback_failure_is_reported_without_exception_text(setup):
    bot, params, anchor = setup
    anchor["message"] = [text("看聊天记录")]
    bot.call_action.side_effect = [anchor, RuntimeError("https://secret/?key=hidden")]
    result = await forward.read_forward_context(bot, params)
    assert result["cards"] == [] and result["stop_reason"] == "partial_failure"
    assert "secret" not in json.dumps(result)
