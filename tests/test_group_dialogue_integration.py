"""Cross-module acceptance checks for group chat, memory and task notices."""
from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from langbot_plugin.api.entities.builtin.platform.message import Image, MessageChain, Plain, Quote
from langbot_plugin.api.entities.builtin.provider.message import Message as ModelMessage
from qunling.proactive import host
from qunling.proactive.engine import ConversationContext, Decision, Message
from qunling.qq_dispatch import Dispatch, is_control_notice
from test_group_memory import entry, source
from test_proactive_host import bridge, event, query_for


def shared_context(messages, **kwargs):
    return ConversationContext("bot", "30000", "active", "自然讨论，不强行接梗", tuple(messages), (), "", None, None,
                               **kwargs)


@pytest.mark.asyncio
async def test_selected_topic_member_uses_real_shared_memory_lookup_but_judgment_does_not(bridge, source):
    # Keep the real HostBridge and recall implementation; only the external KB
    # boundary and model are fakes. A begins the topic, but B is the reply target.
    bridge.bot.ap.runner_registry = source.bot.ap.runner_registry
    bridge.bot.ap.rag_mgr = source.bot.ap.rag_mgr
    bridge.bot.ap.pipeline_mgr = source.bot.ap.pipeline_mgr
    source.kb.retrieve.return_value = [
        entry("B 喜欢动画电影", speaker="20002", memory_kind="speaker_profile"),
        entry("群里约好周末一起活动", sender_id="", memory_kind="group_profile"),
        entry("别的群秘密", group="90000"),
    ]
    now = time.time()
    messages = [Message("bot", "30000", "a-plan", "20001", "周末一起看电影", now),
                Message("bot", "30000", "b-budget", "20002", "那预算多少", now + 1)]
    context = shared_context(messages, participant_id="20001", participant_ids=("20001", "20002"),
                             reply_candidate_ids=("a-plan", "b-budget"), focus_message_ids=("a-plan", "b-budget"),
                             topic_message_ids=("a-plan", "b-budget"))
    bridge._complete = AsyncMock(return_value='{"action":"silence","target_message_id":null}')
    assert (await bridge.judge(context))["action"] == "silence"
    source.kb.retrieve.assert_not_awaited()
    bridge._complete.return_value = "先看场次，再一起商量预算。"
    assert await bridge.generate(context, Decision("reply", "b-budget")) == "先看场次，再一起商量预算。"
    _, payload = bridge._complete.await_args.args
    assert source.kb.retrieve.await_args.kwargs["settings"]["sender_id"] == "20002"
    assert source.kb.retrieve.await_args.kwargs["settings"]["session_name"] == "group_30000"
    assert "周末一起看电影" in source.kb.retrieve.await_args.args[1]
    assert payload["target_author"]["sender_id"] == payload["shared_memory"]["participant_id"] == "20002"
    assert [r["kind"] for r in payload["shared_memory"]["records"]] == ["speaker_profile", "group_profile"]
    assert "别的群秘密" not in json.dumps(payload, ensure_ascii=False)
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_passive_reply_and_explicit_followup_share_public_text_without_replacing_plugin_memory(bridge):
    first = event("漫画标题我理解成反击", sender="20001", message_id="public-text")
    bridge.observe(first)
    bridge.engine.on_bot_message("30000", "这里用反击更顺。", message_id="passive-answer", recipient_id="20001", reply_to="public-text")
    followup = event("那后面这个词呢", sender="20002", explicit=True, message_id="explicit-followup")
    followup.message_chain.insert(0, Quote(id="passive-answer", group_id="30000", sender_id="10000", origin=MessageChain([])))
    bridge.observe(followup)
    query = query_for(bridge, followup)
    original_memory = ModelMessage(role="system", content="# Relevant Memories\n已有插件为当前成员注入的画像")
    query.prompt.messages.append(original_memory)
    host.prepare_prompt(query)
    assert query.prompt.messages[1] is original_memory
    history = query.prompt.messages[-1].content
    assert "这里用反击更顺" in history and "public-text" in history
    assert '"sender_id": "20001"' in history
    host.sent(bridge.bot.adapter, "30000", MessageChain([Plain(text="后面的词在这里是拟声词。")]),
              receipt={"message_id": "explicit-answer"}, recipient_id="20002", reply_to="explicit-followup")
    host.completed(bridge.bot.adapter, followup)
    assert any(m.message_id == "explicit-answer" and m.reply_to == "explicit-followup"
               for m in bridge.engine.snapshot("30000", before_timestamp=time.time() + 1))
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_explicit_translation_busy_does_not_block_other_member_or_reassign_attachment(bridge):
    translating = event("翻译我发的图", sender="20001", explicit=True, message_id="translation")
    translating.message_chain.append(Image(url="https://private.invalid/alice-source.png"))
    bridge.observe(translating)
    host.queued(bridge.bot.adapter, translating)
    bridge.observe(event("周末电影计划怎么安排", sender="20002", message_id="bob-chat"))
    await bridge.engine.drain()
    context = bridge.engine._judge.await_args.args[0]
    assert context.participant_id == "20002"
    assert "bob-chat" in context.reply_candidate_ids
    assert "translation" not in context.reply_candidate_ids
    assert "alice-source.png" not in repr(context)
    bridge._complete = AsyncMock(return_value="先看看大家的时间吧。")
    with pytest.raises(host.ProactiveUnavailable):
        await bridge.generate(context, Decision("reply", "translation"))
    assert "explicit_busy" in bridge.engine.status()["groups"]["30000"]
    host.completed(bridge.bot.adapter, translating)
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_unknown_group_and_foreign_image_never_enter_explicit_public_evidence(bridge):
    other = event("另一个群的内部讨论", group="90000", sender="20003", message_id="foreign")
    other.message_chain.append(Image(url="https://private.invalid/foreign-image.png"))
    bridge.observe(other)
    image = event("这一张还在处理中", sender="20001", message_id="image-owner")
    image.message_chain.append(Image(url="https://private.invalid/owned-source.png"))
    bridge.observe(image)
    followup = event("继续说这个文字话题", sender="20002", explicit=True, message_id="text-followup")
    bridge.observe(followup)
    query = query_for(bridge, followup)
    original = query.messages
    host.prepare_prompt(query)
    injected = "\n".join(str(message.content) for message in query.prompt.messages)
    assert query.messages is original
    assert "另一个群" not in injected and "foreign-image" not in injected
    assert "owned-source.png" not in injected and "图片内容未读取" in injected
    host.completed(bridge.bot.adapter, followup)
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_cancel_and_queue_control_notices_do_not_become_bot_dialogue(bridge):
    incoming = event("帮我整理表格", sender="20001", explicit=True, message_id="task")
    bridge.observe(incoming)
    host.queued(bridge.bot.adapter, incoming)
    dispatcher = Dispatch(bridge.bot.ap)
    query = query_for(bridge, incoming)
    query.bot_uuid = "bot"
    bridge.bot.ap.workspace_service.get_execution_binding = AsyncMock(return_value=NS(instance_uuid="instance"))
    bridge.bot.ap.logger = logging.getLogger(__name__)
    delivered = []
    async def fake_reply(*, message_source, message, quote_origin):
        assert is_control_notice()
        delivered.append("".join(item.text for item in message if item.type == "Plain"))
        host.sent(bridge.bot.adapter, "30000", message, receipt={"message_id": "notice"},
                  recipient_id="20001", reply_to="task")
    bridge.bot.adapter.reply_message = AsyncMock(side_effect=fake_reply)
    await dispatcher.notice(query, "任务已取消，稍后可以重新发起。")
    await dispatcher.notice(query, "收到，正在排队处理。")
    assert len(delivered) == 2 and not is_control_notice()
    observed = bridge.engine.snapshot("30000", before_timestamp=time.time() + 1)
    assert any(m.message_id == "task" for m in observed)
    assert not any(m.message_id == "notice" for m in observed)
    host.completed(bridge.bot.adapter, incoming)
    followup = event("我们继续聊电影吧", sender="20002", explicit=True, message_id="after-cancel")
    bridge.observe(followup)
    prompt = query_for(bridge, followup)
    host.prepare_prompt(prompt)
    assert "任务已取消" not in str(prompt.prompt.messages) and "正在排队" not in str(prompt.prompt.messages)
    host.completed(bridge.bot.adapter, followup)
    await bridge.engine.close()
