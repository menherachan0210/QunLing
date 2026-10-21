"""Real Host/cache integration; external model and QQ remain offline."""
import json
import time
from unittest.mock import AsyncMock

import pytest
from langbot_plugin.api.entities.builtin.platform.message import Image, MessageChain, Quote
from qunling import group_knowledge as knowledge, group_memory
from qunling.proactive import host
from qunling.proactive.engine import ConversationContext, Decision
from test_proactive_host import bridge, event, query_for


def observation(*, anchor="draw", group="30000", actor="20000"):
    return knowledge.publish(
        workspace_id=knowledge.workspace_id("instance", "workspace"), bot_id="bot", group_id=group,
        question="生成我的头像", source="avatar_analysis", subject="小明的头像",
        text="原头像有墨绿短发、猫耳和黑色礼帽。", message_ids=(anchor,), actor_id=actor)


def image_chain():
    return MessageChain([Image(url="https://private.invalid/result.png?token=secret")])


@pytest.mark.asyncio
async def test_pure_image_receipt_links_observation_to_free_and_explicit_followups(bridge, monkeypatch):
    request = event("生成我的头像", explicit=True, message_id="draw")
    bridge.observe(request)
    assert observation()
    host.sent(bridge.bot.adapter, "30000", image_chain(), {"message_id": "picture"}, "20000", reply_to="draw")
    host.completed(bridge.bot.adapter, request)
    target = event("一点都不像", sender="20001", message_id="feedback")
    bridge.observe(target)
    snapshot = bridge.engine.snapshot("30000", before_timestamp=time.time() + 1)
    picture = next(m for m in snapshot if m.message_id == "picture")
    assert picture.has_media and picture.reply_to == "draw" and picture.reply_recipient_id == "20000"
    assert "已发送" in picture.text and "private.invalid" not in repr(snapshot)
    metadata = bridge.engine.context_metadata("30000", snapshot, "20001", None)
    assert {"draw", "picture", "feedback"} <= set(metadata["topic_message_ids"])
    context = ConversationContext("bot", "30000", "active", "", snapshot, (), "", None, None,
                                  participant_id="20001", reply_candidate_ids=("feedback",), **metadata)
    monkeypatch.setattr(group_memory, "recall_group_memory", AsyncMock(return_value={"records": []}))
    bridge._complete = AsyncMock(return_value="原头像的观察还在，成图是否相似没有重新核验。")
    await bridge.generate(context, Decision("reply", "feedback"))
    payload = bridge._complete.await_args.args[1]
    records = payload["shared_knowledge"]
    assert {r["source"] for r in records} == {"avatar_analysis", "image_delivery"}
    assert {r["actor_id"] for r in records} == {"20000"}
    assert "private.invalid" not in json.dumps(payload) and payload["runtime"]["tools_available"] is False
    delivery = next(r for r in records if r["source"] == "image_delivery")
    assert "墨绿" not in delivery["text"] and "未记录成图视觉核验" in delivery["text"]
    explicit = event("那刚才呢", explicit=True, sender="20002", message_id="ask-again")
    explicit.message_chain.insert(0, Quote(id="picture", group_id="30000", sender_id="10000", origin=MessageChain([])))
    bridge.observe(explicit)
    query = query_for(bridge, explicit)
    host.prepare_prompt(query)
    evidence = [m.content for m in query.prompt.messages if m.content.startswith(host.KNOWLEDGE_MARKER)]
    assert len(evidence) == 1 and "avatar_analysis" in evidence[0] and "image_delivery" in evidence[0]
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_missing_receipt_never_publishes_image_success(bridge):
    host.sent(bridge.bot.adapter, "30000", image_chain(), {}, "20000")
    assert bridge.engine.snapshot("30000") == ()
    assert knowledge.recall(workspace_id=knowledge.workspace_id("instance", "workspace"), bot_id="bot",
                            group_id="30000", question="图片消息") == []
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_parallel_same_member_tasks_never_guess_image_owner(bridge):
    for key in ("draw-a", "draw-b"):
        bridge.observe(event("生成我的头像", explicit=True, message_id=key))
    host.sent(bridge.bot.adapter, "30000", image_chain(), {"message_id": "unbound"}, "20000")
    image = next(m for m in bridge.engine.snapshot("30000", before_timestamp=time.time() + 1) if m.message_id == "unbound")
    assert image.reply_to is None
    rows = knowledge.recall(workspace_id=knowledge.workspace_id("instance", "workspace"), bot_id="bot",
                            group_id="30000", question="不是你画的吗", related_message_ids=("draw-a",))
    assert rows == []
    delivered = knowledge.recall(workspace_id=knowledge.workspace_id("instance", "workspace"), bot_id="bot",
                                 group_id="30000", question="不是你画的吗", related_message_ids=("unbound",))
    assert len(delivered) == 1 and delivered[0]["source"] == "image_delivery"
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_host_receipt_cutoff_includes_earlier_same_second_evidence_only(bridge, monkeypatch):
    second = int(time.time())
    clock = [second + .25]
    monkeypatch.setattr(host.time, "time", lambda: clock[0])
    assert observation()
    clock[0] = second + .5
    incoming = event("小明的头像是什么样", explicit=True, message_id="same-second")
    incoming.timestamp = second  # QQ timestamps discard fractional seconds.
    bridge.observe(incoming)
    clock[0] = second + .75
    assert knowledge.publish(workspace_id=knowledge.workspace_id("instance", "workspace"), bot_id="bot",
                             group_id="30000", question="小明的头像", source="avatar_analysis",
                             subject="小明的头像", text="稍后换成了白猫。", actor_id="20000")
    query = query_for(bridge, incoming)
    host.prepare_prompt(query)
    evidence = "\n".join(m.content for m in query.prompt.messages if m.content.startswith(host.KNOWLEDGE_MARKER))
    assert "墨绿短发" in evidence and "稍后换成了白猫" not in evidence
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_inactive_installation_does_not_publish_image_evidence(bridge):
    bridge.bot.enable = False
    host.sent(bridge.bot.adapter, "30000", image_chain(), {"message_id": "receipt"}, "20000")
    assert knowledge.recall(workspace_id=knowledge.workspace_id("instance", "workspace"), bot_id="bot",
                            group_id="30000", question="图片消息 receipt") == []
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_late_result_and_duplicate_receipt_cannot_rebind_new_request(bridge):
    bridge.observe(event("生成我的新头像", explicit=True, message_id="new-task"))
    host.sent(bridge.bot.adapter, "30000", image_chain(), {"message_id": "late-image"}, "20000", reply_to="old-task")
    scope = dict(workspace_id=knowledge.workspace_id("instance", "workspace"), bot_id="bot", group_id="30000",
                 question="图片消息 late-image")
    original = knowledge.recall(**scope)
    assert len(original) == 1
    # Same message callback after another request must not refresh/overwrite it.
    host.sent(bridge.bot.adapter, "30000", image_chain(), {"message_id": "late-image"}, "20001", reply_to="new-task")
    assert knowledge.recall(**scope) == original
    record = bridge.engine._groups["30000"].reply_index["late-image"]
    assert record.target_id == "old-task" and record.recipient_id == "20000"
    assert knowledge.recall(**{**scope, "question": "完全不像"}, related_message_ids=("new-task",)) == []
    await bridge.engine.close()
