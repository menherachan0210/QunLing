"""Research admission preserves original ingress and read-only authority."""
import asyncio
from dataclasses import replace
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from qunling import qq_dispatch
from qunling.proactive import host
from qunling.proactive.engine import ConversationContext, Decision, Message, validate_decision
from qunling.dialogue import Understanding, Participation
from test_proactive_engine import Harness
from test_proactive_host import bridge, event, query_for, dialogue_prompt


@pytest.mark.asyncio
async def test_missing_conversation_context_stops_before_generation_retrieval_or_tools(bridge):
    bridge.engine._judge = bridge.judge
    bridge.engine._handoff = AsyncMock()
    bridge._complete = AsyncMock(return_value=json.dumps({
        "action": "research", "target_message_id": "103",
        "understanding": {"purpose": "想接入两位群友的交流", "context_message_ids": ["101", "102", "103"],
            "information_needs": ["history"], "participation": {
                "conversation": "两位成员在说未出现前情的人，无法确定具体事件",
                "role": "joining", "entry_point": "先问他们说的是谁", "context_sufficient": False}}}))
    bridge.observe(event("他又来了", sender="20001", message_id="101"))
    bridge.observe(event("还是上次那个？", sender="20001", message_id="102"))
    bridge.observe(event("又整上了", sender="20001", message_id="103"))
    await bridge.engine.drain()
    bridge._complete.assert_awaited_once()
    bridge.engine._generate.assert_not_awaited()
    bridge.engine._handoff.assert_not_awaited()
    bridge.bot.adapter.send_message.assert_not_awaited()
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_draft_promotes_spontaneous_comment_to_read_images_from_another_member(bridge, monkeypatch):
    from qunling.group_batch import recall
    from qunling.group_knowledge import workspace_id

    monkeypatch.setattr(bridge, "_active", AsyncMock())
    # The picture belongs to another member. The current text has no @ or
    # explicit "look at the image" command; participation has already been chosen.
    bridge.observe(event("", sender="20001", message_id="101"))
    question = event("一句话错俩，直接看懵了", sender="20002", message_id="102")
    bridge.observe(question)
    args = dict(bot_uuid="bot", adapter=bridge.bot.adapter, launcher_id="30000",
        launcher_type=NS(value="group"), sender_id="20002", message_event=question,
        message_chain=question.message_chain, pipeline_uuid="pipeline", routed_by_rule=False,
        variables={}, execution_context=bridge.bot.execution_context)
    host.remember_ingress(bridge.bot.adapter, question, args)
    interpretation = {"purpose": "接住大家对图片错字的吐槽，先确认原图文字",
                      "context_message_ids": ["101"], "information_needs": ["image"],
                      "participation": {"conversation": "群友正在吐槽另一成员发的图中文字",
                          "role": "joining", "entry_point": "结合原图接续错字话题", "context_sufficient": True}}
    bridge.engine._judge = AsyncMock(return_value={"action": "reply", "target_message_id": "102",
                                                   "understanding": interpretation})
    bridge.engine._generate = bridge.generate
    bridge._complete = AsyncMock(return_value='{"action":"research"}')
    async def admit(ap, original, *, validate, allow_actions=False):
        assert await validate()
        assert not allow_actions and original["message_event"] is question
        return NS(query_uuid="accepted")
    accept = AsyncMock(side_effect=admit)
    monkeypatch.setattr(qq_dispatch, "admit_continuation", accept)
    await bridge.engine.drain()
    accept.assert_awaited_once()
    bridge.bot.adapter.send_message.assert_not_called()
    rows = recall(workspace_id("instance", "workspace"), "bot", "30000", "20002", "102")
    assert [(row["message_id"], row["sender_id"]) for row in rows] == [("101", "20001")]
    state, history = bridge.request_context(question)
    assert state["continuation_read_only"] and state["research_request"]
    assert not state["natural_task_request"]
    assert state["understanding"]["purpose"] == interpretation["purpose"]
    assert history[0]["has_media"] and history[0]["sender_id"] == "20001"
    query = query_for(bridge, question)
    host.prepare_prompt(query)
    frame = dialogue_prompt(query)
    assert frame["understanding"]["purpose"] == interpretation["purpose"]
    assert frame["understanding"]["context_message_ids"] == ["101"]
    assert frame["understanding"]["participation"] == interpretation["participation"]
    assert frame["target"]["sender_id"] == "20002"
    assert "主动接话" in query.prompt.messages[1].content
    assert "read_group_context_images" in query.prompt.messages[1].content
    assert "read_group_context_images" in query.prompt.messages[1].content
    assert "回复仍使用当前群交流设置" in query.prompt.messages[1].content
    assert not bridge.engine._groups["30000"].covered
    host.completed(bridge.bot.adapter, question)
    await bridge.engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("output,expected", [
    ('{"action":"research"}', "research"),
    ('{"action":"reply","text":"你这错字密度可真够高的。"}', "reply"),
    ('{"action":"silence"}', "silence"),
    ('{"action":"task_request"}', "invalid"),
    ('{"action":"research","target_message_id":"someone-else"}', "invalid"),
    ('{"action":"research","text":"我没看图，先瞎聊一句"}', "invalid"),
    ('{"action":"reply","text":null}', "invalid"),
    ("我没看到图片，无法判断。", "invalid"),
])
async def test_draft_protocol_cannot_leak_control_json_or_expand_tool_authority(bridge, output, expected):
    picture = Message("bot", "30000", "pic", "20001", "", time.time(), has_media=True)
    target = Message("bot", "30000", "q", "20002", "这句错了两个字", time.time())
    context = ConversationContext("bot", "30000", "active", "", (picture, target), (), "", None, None,
                                  reply_candidate_ids=("q",))
    bridge._complete = AsyncMock(return_value=output)
    if expected == "invalid":
        with pytest.raises(host.ProactiveUnavailable, match="草稿格式"):
            await bridge.generate(context, Decision("reply", "q"))
    else:
        result = await bridge.generate(context, Decision("reply", "q"))
        if expected == "research":
            assert result == Decision("research", "q", ("q",))
        elif expected == "reply":
            assert result == json.loads(output)["text"]
        else:
            assert result == ""
    bridge.bot.adapter.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_plain_draft_is_preserved_only_after_text_only_context_was_understood(bridge):
    target = Message("bot", "30000", "q", "20002", "刚才那句是什么意思？", time.time())
    context = ConversationContext("bot", "30000", "active", "", (target,), (), "", None, None,
                                  reply_candidate_ids=("q",))
    participation = Participation("正在解释刚才的措辞", "addressed", "说明已有表述", True)
    meaning = Understanding("解释措辞", ("q",), (), participation)
    bridge._complete = AsyncMock(return_value="我的意思就是先看看实际效果。")
    assert await bridge.generate(context, Decision("reply", "q", ("q",), meaning)) == "我的意思就是先看看实际效果。"
    bridge._complete.assert_awaited_once()
    for output in ('{"action":"task_request"}', '{"action":"reply",', '<tool_call>download</tool_call>',
                   '```json\n{"action":"reply"}\n```', '"quoted JSON"', 'research', 'null'):
        bridge._complete.return_value = output
        with pytest.raises(host.ProactiveUnavailable, match="草稿格式"):
            await bridge.generate(context, Decision("reply", "q", ("q",), meaning))
    bridge._complete.return_value = "需要再看看之前那张图。"
    for unknown in (replace(meaning, information_needs=("image",)),
                    replace(meaning, participation=replace(participation, context_sufficient=False))):
        with pytest.raises(host.ProactiveUnavailable, match="草稿格式"):
            await bridge.generate(context, Decision("reply", "q", ("q",), unknown))
    bridge.bot.adapter.send_message.assert_not_called()


@pytest.mark.parametrize("promotion", [
    Decision("task_request", "m1", ("m1",)),
    Decision("research", "other", ("other",)),
    Decision("research", "m1", ("m1", "other")),
])
def test_engine_refuses_draft_target_switch_or_write_escalation(promotion):
    async def scenario():
        h = Harness(handoff=AsyncMock(return_value=True))
        h.engine._generate = AsyncMock(return_value=promotion)
        h.engine.on_message(h.message(text="群灵，聊聊这张图"))
        await h.engine.drain()
        h.engine._handoff.assert_not_called()
        assert not h.sent and not h.engine._groups["100"].covered
        assert h.engine._groups["100"].last_event["code"] == "invalid_generation"
        await h.engine.close()
    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_media_after_text_is_frozen_with_batch_including_own_sent_images(bridge, monkeypatch):
    from langbot_plugin.api.entities.builtin.platform.message import Image
    from qunling.group_batch import recall
    from qunling.group_knowledge import workspace_id
    monkeypatch.setattr(bridge, "_active", AsyncMock())
    bridge.engine.on_bot_message("30000", "已发图片", message_id="99", has_media=True)
    question = event("群灵，看看这一组图有什么不同", message_id="101")
    bridge.observe(question)
    args = dict(bot_uuid="bot", adapter=bridge.bot.adapter, launcher_id="30000",
        launcher_type=NS(value="group"), sender_id="20000", message_event=question,
        message_chain=question.message_chain, pipeline_uuid="pipeline", routed_by_rule=False,
        variables={}, execution_context=bridge.bot.execution_context)
    host.remember_ingress(bridge.bot.adapter, question, args)
    def image(key):
        value = event("", message_id=key)
        value.message_chain.append(Image(url="https://example.test/picture.png"))
        bridge.observe(value)
    image("102")
    state = bridge.engine._groups["30000"]
    context = bridge.engine._context("30000", state, state.lanes["20000"], bridge.configuration().groups["30000"])
    image("103")  # Arrives after the judge's frozen batch.
    monkeypatch.setattr(qq_dispatch, "admit_continuation", AsyncMock(return_value=NS(query_uuid="accepted")))
    assert await bridge.handoff(context, Decision("research", "101", ("101",)))
    rows = recall(workspace_id("instance", "workspace"), "bot", "30000", "20000", "101")
    assert {row["message_id"] for row in rows} == {"99", "102"}
    assert next(row for row in rows if row["message_id"] == "99")["sender_id"] == "10000"
    _, history = bridge.request_context(question)
    assert "102" in {m["message_id"] for m in history} and "103" not in {m["message_id"] for m in history}
    host.completed(bridge.bot.adapter, question)
    await bridge.engine.close()


def fresh_context(**changes):
    h = Harness()
    question = h.message(text="最近网络上大肥鱼入侵，群灵你怎么看？认真研究一下。", has_media=True)
    context = ConversationContext("bot", "100", "active", "", (question,), (), "", None, None,
        reply_candidate_ids=("m1",), addressed_candidate_ids=("m1",))
    return replace(context, **changes)


@pytest.mark.parametrize("action", ["research", "task_request"])
def test_new_research_does_not_require_a_previous_bot_answer(action):
    result = validate_decision({"action": action, "target_message_id": "m1"}, fresh_context())
    assert result == Decision(action, "m1", ("m1",))
    assert validate_decision({"action": "tool_followup", "target_message_id": "m1"}, fresh_context()) is None


@pytest.mark.parametrize("change", ["foreign_group", "foreign_bot", "other_member", "explicit", "no_candidates", "quiet", "covered"])
@pytest.mark.parametrize("action", ["research", "task_request"])
def test_research_cannot_bypass_scope_or_participation_policy(change, action):
    context = fresh_context()
    message = context.messages[0]
    if change == "foreign_group":
        message = replace(message, group_id="other")
    elif change == "foreign_bot":
        message = replace(message, bot_id="other")
    elif change == "other_member":
        message = replace(message, mentions=("other",))
    elif change == "explicit":
        message = replace(message, explicit_request=True)
    elif change == "no_candidates":
        context = replace(context, reply_candidate_ids=())
    elif change == "quiet":
        context = replace(context, mode="quiet", addressed_candidate_ids=())
    elif change == "covered":
        context = replace(context, covered_message_ids=("m1",))
    context = replace(context, messages=(message,))
    assert validate_decision({"action": action, "target_message_id": "m1"}, context) is None


@pytest.mark.parametrize("action", ["research", "task_request"])
def test_research_handoff_replaces_fast_text_generation_without_claiming_delivery(action):
    async def scenario():
        h = Harness(handoff=AsyncMock(return_value=True))
        h.engine._judge = AsyncMock(return_value={"action": action, "target_message_id": "m1"})
        h.engine.on_message(h.message(text="群灵，研究一下这个最近流行的现象"))
        await h.engine.drain()
        h.engine._handoff.assert_awaited_once()
        assert not h.generated and not h.sent
        state = h.engine._groups["100"]
        assert len(state.handoffs) == 1 and not state.covered
        await h.engine.close()
    asyncio.run(scenario())


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["research", "task_request"])
async def test_first_invitation_retains_read_only_prompt_with_no_history(bridge, monkeypatch, action):
    monkeypatch.setattr(bridge, "_active", AsyncMock())
    question = event("群灵，这个新文件帮我分析一下", message_id="fresh")
    bridge.observe(question)
    args = dict(bot_uuid="bot", adapter=bridge.bot.adapter, launcher_id="30000",
        launcher_type=NS(value="group"), sender_id="20000", message_event=question,
        message_chain=question.message_chain, pipeline_uuid="pipeline", routed_by_rule=False,
        variables={}, execution_context=bridge.bot.execution_context)
    host.remember_ingress(bridge.bot.adapter, question, args)
    state = bridge.engine._groups["30000"]
    context = bridge.engine._context("30000", state, state.lanes["20000"], bridge.configuration().groups["30000"])
    async def admit(ap, original, *, validate, allow_actions=False):
        assert await validate()
        assert allow_actions is (action == "task_request")
        assert original["message_event"] is question
        assert not host._is_explicit(original["message_chain"], "10000")
        return NS(query_uuid="accepted")
    accept = AsyncMock(side_effect=admit)
    monkeypatch.setattr(qq_dispatch, "admit_continuation", accept)
    assert await bridge.handoff(context, Decision(action, "fresh", ("fresh",)))
    state, history = bridge.request_context(question)
    assert history == [] and state["continuation_read_only"] is (action == "research")
    assert state["natural_task_request"] is (action == "task_request")
    query = query_for(bridge, question)
    host.prepare_prompt(query)
    assert ("file_workspace" if action == "research" else "无需再次@") in query.prompt.messages[1].content
    assert "display_name" in query.prompt.messages[1].content
    accept.assert_awaited_once()
    host.completed(bridge.bot.adapter, question)
    await bridge.engine.close()
