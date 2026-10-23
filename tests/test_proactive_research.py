"""Research admission preserves original ingress and read-only authority."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from qunling import qq_dispatch
from qunling.proactive import host
from qunling.proactive.engine import ConversationContext, Decision, validate_decision
from test_proactive_engine import Harness
from test_proactive_host import bridge, event, query_for


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
