"""Trusted bridge integration. No real model, QQ delivery or server access."""
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from qunling import qq_dispatch
from qunling.proactive import host
from qunling.proactive.engine import Decision
from test_proactive_host import bridge, event, query_for


def setup_followup(bridge):
    request = event("你怎么看这张漫画", explicit=True, message_id="ask")
    request.timestamp -= 4
    bridge.observe(request)
    bridge.engine.on_bot_message("30000", "漫画里他还握着方向盘，先看路啊。",
        message_id="answer", recipient_id="20000", reply_to="ask")
    host.completed(bridge.bot.adapter, request)
    follow = event("你知道这啥漫画不", sender="20001", message_id="follow")
    bridge.observe(follow)
    args = dict(bot_uuid="bot", adapter=bridge.bot.adapter, launcher_id="30000",
        launcher_type=NS(value="group"), sender_id="20001", message_event=follow,
        message_chain=follow.message_chain, pipeline_uuid="pipeline", routed_by_rule=False,
        variables={}, execution_context=bridge.bot.execution_context)
    host.remember_ingress(bridge.bot.adapter, follow, args)
    state = bridge.engine._groups["30000"]
    context = bridge.engine._context("30000", state, state.lanes["20001"], bridge.configuration().groups["30000"])
    return follow, args, context, Decision("tool_followup", "follow", ("follow",))


@pytest.mark.asyncio
async def test_cross_member_tool_followup_keeps_real_identity_and_frozen_evidence(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_active", AsyncMock())
    follow, args, context, decision = setup_followup(bridge)
    bridge.observe(event("未来的消息不能进入旧问题", sender="20002", message_id="future"))
    async def admit(ap, original, *, validate):
        assert original["message_event"] is follow
        assert original["sender_id"] == "20001"
        assert not host._is_explicit(original["message_chain"], "10000")
        assert await validate() is True
        return NS(query_uuid="accepted")
    accept = AsyncMock(side_effect=admit)
    monkeypatch.setattr(qq_dispatch, "admit_continuation", accept)
    assert await bridge.handoff(context, decision)
    state, history = bridge.request_context(follow)
    assert state["questioner_id"] == "20001" and state["continuation_read_only"]
    assert {"ask", "answer"} <= set(state["topic_message_ids"])
    assert "future" not in {m["message_id"] for m in history}
    query = query_for(bridge, follow)
    host.prepare_prompt(query)
    assert "只执行资料查询" in query.prompt.messages[1].content
    accept.assert_awaited_once()
    host.completed(bridge.bot.adapter, follow)
    assert not bridge.queued and not bridge._request_contexts
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_revocation_during_admission_releases_pending_request(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_active", AsyncMock())
    follow, _, context, decision = setup_followup(bridge)
    async def revoke(ap, original, *, validate):
        config = bridge.store.read()
        config["enabled"] = False
        bridge.store.save(config, known_group_ids={"30000"})
        assert await validate() is False
        return None
    monkeypatch.setattr(qq_dispatch, "admit_continuation", revoke)
    assert not await bridge.handoff(context, decision)
    assert not bridge.queued and not bridge._request_contexts
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_ingress_cannot_rebind_sender_or_mutate_target(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_active", AsyncMock())
    follow, args, context, decision = setup_followup(bridge)
    bridge._continuation_ingress.clear()
    host.remember_ingress(bridge.bot.adapter, follow, {**args, "sender_id": "20002"})
    assert not bridge._continuation_ingress
    host.remember_ingress(bridge.bot.adapter, follow, args)
    follow.sender.id = "20002"
    accept = AsyncMock()
    monkeypatch.setattr(qq_dispatch, "admit_continuation", accept)
    assert not await bridge.handoff(context, decision)
    accept.assert_not_awaited()
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_nudge_from_peer_preserves_original_question_across_meta_answer(bridge):
    _, _, _, _ = setup_followup(bridge)
    nudge = event("非得我@你才说话吗", sender="20001", message_id="nudge", explicit=True)
    bridge.observe(nudge)
    state, _ = bridge.request_context(nudge)
    assert "follow" in {m["message_id"] for m in state["unresolved_question_candidates"]}
    bridge.engine.on_bot_message("30000", "刚才那句漏回了，不用每次@我。", message_id="meta",
        recipient_id="20001", reply_to="nudge")
    host.completed(bridge.bot.adapter, nudge)
    second = event("你倒是回答他啊", sender="20002", message_id="again", explicit=True)
    bridge.observe(second)
    state, _ = bridge.request_context(second)
    assert "follow" in {m["message_id"] for m in state["unresolved_question_candidates"]}
    assert state["questioner_id"] == "20002"
    await bridge.engine.close()
