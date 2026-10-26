"""Offline contract tests for the trusted Host bridge; never send to QQ."""
from __future__ import annotations

import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import typing
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from qunling.proactive import host
from qunling.proactive.config import ConfigStore, DEFAULT_GROUP, ConfigError
from langbot.pkg.api.http.authz import PermissionDeniedError
from langbot.pkg.api.http.context import ExecutionContext
from langbot_plugin.api.entities.builtin.platform.message import At, Image, MessageChain, Plain, Quote
from langbot_plugin.api.entities.builtin.provider.message import Message as ModelMessage

ROOT = Path(__file__).resolve().parents[1]
LANGBOT_ROOT = Path(next(iter(importlib.util.find_spec("langbot").submodule_search_locations))).parents[1]


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    context = ExecutionContext(instance_uuid="instance", workspace_uuid="workspace", placement_generation=1, bot_uuid="bot")
    adapter = NS(bot_account_id="10000", bot=NS(connection=NS(closed=False)),
                 send_message=AsyncMock(return_value=NS(message_id="reply-1")),
                 get_group_list=AsyncMock(return_value=[NS(id="30000", name="测试群")]))
    entity = NS(uuid="bot", workspace_uuid="workspace", adapter="onebot-v11", enable=True)
    bot = NS(adapter=adapter, bot_entity=entity, enable=True, execution_context=context,
             assert_execution_active=AsyncMock(), get_pipeline_target_for_event_type=lambda event: "pipeline")
    adapter._runtime = bot
    provider = NS(invoke_llm=AsyncMock(return_value=ModelMessage(role="assistant", content="自然接话")))
    model = NS(provider=provider)
    pipeline = NS(pipeline_entity=NS(config={"ai": {"runner": {"id": "runner"},
                  "runner_config": {"runner": {"model": {"primary": "chat-model"}}}}}))
    bot.ap = NS(workspace_service=NS(instance_uuid="instance"),
                bot_service=NS(get_bot=AsyncMock(return_value={"enable": True, "adapter": "onebot-v11"})),
                platform_mgr=NS(get_bot_by_uuid=AsyncMock(return_value=bot)),
                pipeline_mgr=NS(get_pipeline_by_uuid=AsyncMock(return_value=pipeline)),
                model_mgr=NS(get_model_by_uuid=AsyncMock(return_value=model)))
    store = ConfigStore("bot", tmp_path / "proactive")
    state = store.read()
    state.update(enabled=True, groups={"30000": {**copy.deepcopy(DEFAULT_GROUP), "mode": "natural"}})
    store.save(state, known_group_ids={"30000"})
    value = host.HostBridge(bot, store=store)
    bot._qunling_proactive = value
    value.engine._debounce_scale = 0
    value.engine._judge = AsyncMock(return_value={"action": "silence", "target_message_id": None})
    value.engine._generate = AsyncMock(return_value="自然接话")
    value.engine.refresh_configuration()
    return value


def event(text="这个话题你怎么看", *, group="30000", sender="20000", explicit=False, message_id="m1"):
    chain = MessageChain(([At(target="10000")] if explicit else []) + ([Plain(text=text)] if text else [Image(url="https://invalid.example/test.png")]))
    return NS(type="message.received", adapter_name="onebot-v11", group=NS(id=group), sender=NS(id=sender, nickname="群友"),
              message_chain=chain, message_id=message_id, timestamp=time.time())


def admin_context(role="admin", workspace="workspace"):
    return NS(workspace_uuid=workspace, workspace=NS(permissions=frozenset({"resource.manage"}) if role == "admin" else frozenset()))


def query_for(bridge, incoming):
    return NS(adapter=bridge.bot.adapter, message_event=incoming,
              _execution_context=bridge.bot.execution_context,
              prompt=NS(messages=[ModelMessage(role="system", content="保留的原始系统提示")]),
              messages=[ModelMessage(role="user", content="原本的用户消息")])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,enabled", [("off", True), ("natural", False), ("active", True)])
async def test_group_persona_replaces_managed_style_for_direct_and_tool_prompt(bridge, mode, enabled):
    from qunling.group_persona import PERSONA_MARKER, STYLE_MARKER

    state = bridge.store.read()
    state["enabled"] = enabled
    state["groups"]["30000"].update(mode=mode, display_name="冰夜姬", persona="自信爽快，敢于反驳，少说客套话。")
    bridge.store.save(state, known_group_ids={"30000"})
    incoming = event(explicit=True)
    query = query_for(bridge, incoming)
    memory = ModelMessage(role="system", content="# Long-term Memory\n旧人设：胆小、每句话都道歉。群友喜欢科幻电影。")
    query.prompt.messages.extend([ModelMessage(role="system", content=STYLE_MARKER + "\n旧内置风格"), memory])
    original_messages = query.messages

    host.prepare_prompt(query)
    # Exercise the actual Host-to-runner serialization, including tool requests.
    serialize = patched_method(LANGBOT_ROOT, "src/langbot/pkg/pipeline/process/handlers/chat.py",
                               "ChatMessageHandler", "_build_history_prompt_config", {})
    serialized = await serialize(None, query)
    system = "\n".join(item["content"] for item in serialized if item["role"] == "system")
    assert "自信爽快，敢于反驳，少说客套话。" in system
    assert "旧内置风格" not in system and "会议全勤奖" not in system
    assert system.index("旧人设") < system.index(PERSONA_MARKER)
    assert "群友喜欢科幻电影" in system
    assert memory in query.prompt.messages
    assert query.messages is original_messages
    assert json.loads(query.prompt.messages[-1].content.rsplit("\n", 1)[-1])["free_chat_enabled"] is (enabled and mode != "off")
    host.prepare_prompt(query)
    assert sum(PERSONA_MARKER in item.content for item in query.prompt.messages) == 1
    assert query.prompt.messages[0].content == "保留的原始系统提示"


@pytest.mark.asyncio
async def test_group_persona_changes_next_prompt_and_does_not_leak_between_groups(bridge):
    from qunling.group_persona import PERSONA_MARKER

    state = bridge.store.read()
    state["groups"]["30000"].update(display_name="甲", persona="自信爽快，不拘小节")
    state["groups"]["90000"] = {**copy.deepcopy(DEFAULT_GROUP), "display_name": "乙", "persona": "温和细致，喜欢安静"}
    bridge.store.save(state, known_group_ids={"30000", "90000"})
    first, second = query_for(bridge, event(explicit=True)), query_for(bridge, event(group="90000", explicit=True))
    host.prepare_prompt(first)
    host.prepare_prompt(second)
    assert "自信爽快，不拘小节" in first.prompt.messages[-1].content
    assert "温和细致，喜欢安静" not in first.prompt.messages[-1].content
    assert "温和细致，喜欢安静" in second.prompt.messages[-1].content
    assert "自信爽快，不拘小节" not in second.prompt.messages[-1].content

    state = bridge.store.read()
    state["groups"]["30000"]["persona"] = ""
    bridge.store.save(state, known_group_ids={"30000", "90000"})
    host.prepare_prompt(first)
    combined = "\n".join(item.content for item in first.prompt.messages)
    assert "自信爽快，不拘小节" not in combined
    assert "会议全勤奖" in combined
    assert combined.count(PERSONA_MARKER) == 1


@pytest.mark.asyncio
async def test_saved_default_is_visible_in_admin_api_and_used_by_both_reply_paths(bridge):
    from qunling.group_persona import builtin_style
    from qunling.proactive.engine import ConversationContext, Decision, Message

    state = bridge.store.read()
    custom = "正式克制，回答充分展开，不讲笑话。"
    result = await host.save_config(bridge.bot.ap, admin_context(), "bot", {**state, "default_style": custom})
    assert result["builtin_style"] == builtin_style()
    loaded = await host.get_config(bridge.bot.ap, admin_context(), "bot")
    assert loaded["config"]["default_style"] == custom
    assert bridge.configuration().default_style == custom
    query = query_for(bridge, event(explicit=True))
    host.prepare_prompt(query)
    assert custom in query.prompt.messages[-1].content
    assert "会议全勤奖" not in query.prompt.messages[-1].content
    message = Message("bot", "30000", "m1", "20000", "你的看法呢", time.time())
    context = ConversationContext("bot", "30000", "active", "", (message,), (), "", None, None,
                                  default_style=custom)
    bridge._complete = AsyncMock(return_value=json.dumps({"action": "reply", "text": "正式回答"}))
    await bridge.generate(context, Decision("reply", "m1"))
    system, payload = bridge._complete.await_args.args
    assert custom in system and "会议全勤奖" not in system
    assert "default_style" not in payload["context"]


@pytest.mark.asyncio
async def test_free_reply_sends_same_persona_as_direct_reply_at_system_priority(bridge, monkeypatch):
    from qunling import group_memory
    from qunling.group_persona import group_persona_prompt
    from qunling.proactive.engine import ConversationContext, Decision, Message

    persona = "你很自信，轻松俏皮，认真提问时讲清楚。"
    state = bridge.store.read()
    state["groups"]["30000"].update(display_name="冰夜姬", persona=persona)
    bridge.store.save(state, known_group_ids={"30000"})
    memory = {"records": [{"text": "旧人设：每次先道歉。群友周五要交报告。"}]}
    monkeypatch.setattr(group_memory, "recall_group_memory", AsyncMock(return_value=memory))
    provider = NS(invoke_llm=AsyncMock(return_value=ModelMessage(role="assistant",
        content=json.dumps({"action": "reply", "text": "自信接话"}))))
    bridge._model = AsyncMock(return_value=NS(provider=provider))
    message = Message("bot", "30000", "m1", "20000", "你怎么这么自信", time.time())
    context = ConversationContext("bot", "30000", "active", persona, (message,), (), "", None, None,
                                  display_name="冰夜姬", reply_candidate_ids=("m1",))
    assert await bridge.generate(context, Decision("reply", "m1")) == "自信接话"
    call = provider.invoke_llm.await_args.kwargs
    system, data = call["messages"]
    contract = group_persona_prompt("冰夜姬", persona)
    assert system.role == "system" and contract in system.content
    assert system.content.count(persona) == 1
    assert persona not in data.content
    assert "会议全勤奖" not in system.content
    assert data.role == "user" and json.loads(data.content)["shared_memory"] == memory
    assert call["funcs"] == [] and call["extra_args"]["tools"] is None
    query = query_for(bridge, event(explicit=True))
    host.prepare_prompt(query)
    assert contract in query.prompt.messages[-1].content
    assert query.prompt.messages[-1].content.count(persona) == 1


@pytest.mark.asyncio
async def test_judge_uses_the_same_selected_style_without_a_competing_default(bridge):
    from qunling.proactive.engine import ConversationContext, Message
    from qunling.group_persona import group_persona_prompt

    context = ConversationContext("bot", "30000", "active", "保持正式语气；需要讨论时充分展开。",
        (Message("bot", "30000", "m1", "20000", "说说你的看法", time.time()),), (), "", None, None,
        display_name="甲", default_style="搞怪调侃，用短句。", reply_candidate_ids=("m1",))
    bridge._complete = AsyncMock(return_value='{"action":"reply","target_message_id":"m1"}')
    await bridge.judge(context)
    system, payload = bridge._complete.await_args.args
    assert group_persona_prompt(context.display_name, context.persona, context.default_style) in system
    assert context.default_style not in system
    assert "persona" not in payload and "default_style" not in payload


def test_prepare_prompt_refreshes_owned_setup_protocol_but_preserves_custom_instructions(bridge):
    from bootstrap.configure import PROMPT

    incoming = event(explicit=True)
    query = query_for(bridge, incoming)
    query.prompt.messages.append(ModelMessage(role="system", content="[QQBOT SETUP v1]\n旧安装器隐藏的说话方式"))
    host.prepare_prompt(query)
    combined = "\n".join(item.content for item in query.prompt.messages)
    assert "旧安装器隐藏的说话方式" not in combined
    assert PROMPT in combined and "保留的原始系统提示" in combined


@pytest.mark.asyncio
async def test_reply_evidence_deduplicates_only_present_bot_speech_and_keeps_media_ownership(bridge):
    from dataclasses import replace
    from qunling.proactive.engine import ConversationContext, Decision, Message

    prior = Message("bot", "30000", "b1", "10000", "刚才读到了蓝底白字。", time.time(),
                    is_bot=True, has_media=True, reply_recipient_id="20001")
    question = Message("bot", "30000", "m1", "20002", "这个配色你怎么看", time.time(), reply_to="b1")
    context = ConversationContext("bot", "30000", "active", "具体的当前群人设", (prior, question),
                                  ("m1",), prior.text, "b1", prior.timestamp,
                                  reply_candidate_ids=("m1",), topic_message_ids=("b1", "m1"))
    bridge._complete = AsyncMock(return_value=json.dumps({"action": "reply", "text": "蓝底白字挺清楚。"}))
    await bridge.generate(context, Decision("followup", "m1"))
    _, payload = bridge._complete.await_args.args
    evidence = payload["context"]
    assert "previous_bot_reply" not in evidence
    assert "persona" not in evidence and "last_bot_text" not in evidence
    assert evidence["messages"][0]["reply_recipient_id"] == "20001"
    assert evidence["messages"][0]["has_media"] is True
    assert evidence["messages"][1]["sender_id"] == "20002"
    assert evidence["messages"][1]["reply_to"] == "b1"
    assert evidence["topic_message_ids"] == ("b1", "m1")
    await bridge.generate(replace(context, messages=(question,)), Decision("followup", "m1"))
    _, payload = bridge._complete.await_args.args
    assert payload["context"]["previous_bot_reply"] == {
        "message_id": "b1", "text": prior.text, "timestamp": prior.timestamp,
    }


@pytest.mark.asyncio
async def test_coverage_review_keeps_question_authors_quotes_and_each_confirmed_answer_once(bridge):
    from qunling.proactive.engine import ConversationContext, Decision, Message

    question = Message("bot", "30000", "q1", "alice", "人格为什么没生效？", time.time())
    answer = Message("bot", "30000", "b1", "10000", "没有证据说配置失效，刚才只是重复认错。", time.time(),
                     is_bot=True, reply_to="q1", reply_recipient_id="alice")
    followup = Message("bot", "30000", "q2", "bob", "那到底哪里出问题了？", time.time(), reply_to="b1")
    context = ConversationContext("bot", "30000", "active", "按群友实际交流意图理解接话",
                                  (question, answer, followup), ("q2",), answer.text, "b1", answer.timestamp,
                                  reply_candidate_ids=("q2",), default_style="未选中的默认语气")
    delivered = [{"receipt_id": "b1", "target_message_id": "q1", "text": answer.text, "timestamp": answer.timestamp}]
    bridge._complete = AsyncMock(return_value='{"action":"keep"}')
    assert await bridge.reconcile(context, Decision("followup", "q2", ("q2",)), "待发草稿", delivered) == {"action": "keep"}
    system, payload = bridge._complete.await_args.args
    assert "按群友实际交流意图理解接话" in system
    assert "未选中的默认语气" not in system
    assert set(payload["context"]) == {"group_id", "messages"}
    assert [item["sender_id"] for item in payload["context"]["messages"]] == ["alice", "bob"]
    assert payload["context"]["messages"][1]["reply_to"] == "b1"
    assert payload["delivered"] == delivered
    assert json.dumps(payload, ensure_ascii=False).count(answer.text) == 1


@pytest.mark.asyncio
async def test_explicit_prompt_gets_frozen_same_group_context_and_actual_capabilities(bridge):
    first = event("我打算明天搬家", sender="20001", message_id="history")
    first.timestamp -= 1
    host.observe(bridge.bot, first, bridge.bot.adapter)
    request = event("你觉得这个时间合适吗", explicit=True, message_id="question")
    host.observe(bridge.bot, request, bridge.bot.adapter)
    host.observe(bridge.bot, event("之后发的这句绝不能混入", message_id="future"), bridge.bot.adapter)
    host.observe(bridge.bot, event("另一个群的秘密", group="90000", message_id="foreign"), bridge.bot.adapter)
    query = query_for(bridge, request)
    original_messages = query.messages
    host.prepare_prompt(query)
    assert query.messages is original_messages
    assert query.prompt.messages[0].content == "保留的原始系统提示"
    status, history = query.prompt.messages[1:]
    assert status.role == "system" and history.role == "user"
    assert '"free_chat_enabled": true' in status.content
    assert '"questioner_id": "20000"' in status.content
    assert "我打算明天搬家" in history.content and '"sender_id": "20001"' in history.content
    assert "之后发的这句绝不能混入" not in history.content
    assert "另一个群的秘密" not in history.content
    host.prepare_prompt(query)
    assert len(query.prompt.messages) == 3  # Per-run update cannot duplicate evidence.
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_prompt_snapshot_revoked_on_config_change_and_does_not_leak_to_other_sender(bridge):
    host.observe(bridge.bot, event("过去的讨论", message_id="earlier"), bridge.bot.adapter)
    request = event("当前问题", explicit=True, message_id="request")
    host.observe(bridge.bot, request, bridge.bot.adapter)
    wrong_sender = event("当前问题", sender="20003", explicit=True, message_id="request")
    query = query_for(bridge, wrong_sender)
    host.prepare_prompt(query)
    assert len(query.prompt.messages) == 2
    config = bridge.store.read()
    config["enabled"] = False
    bridge.store.save(config, known_group_ids={"30000"})
    query = query_for(bridge, request)
    host.prepare_prompt(query)
    assert len(query.prompt.messages) == 2
    assert '"free_chat_enabled": false' in query.prompt.messages[-1].content
    assert not bridge._request_contexts
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_prompt_cannot_cross_workspace_or_private_message(bridge):
    request = event(explicit=True)
    host.observe(bridge.bot, request, bridge.bot.adapter)
    query = query_for(bridge, request)
    query._execution_context = ExecutionContext(instance_uuid="instance", workspace_uuid="other", placement_generation=1, bot_uuid="bot")
    host.prepare_prompt(query)
    assert len(query.prompt.messages) == 1
    private = event(explicit=True)
    private.group = None
    query = query_for(bridge, private)
    host.prepare_prompt(query)
    assert len(query.prompt.messages) == 1
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_completed_query_discards_its_snapshot(bridge):
    request = event(explicit=True)
    host.observe(bridge.bot, request, bridge.bot.adapter)
    assert bridge._request_contexts
    host.completed(bridge.bot.adapter, request)
    assert not bridge._request_contexts
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_generate_reuses_shared_style_target_author_and_dynamic_character_limit(bridge):
    from qunling.proactive.engine import ConversationContext, Decision, Message

    request = Message("bot", "30000", "m1", "20002", "写一段400字的介绍", time.time(), sender_name="另一位群友")
    context = ConversationContext("bot", "30000", "active", "", (request,), ("m1",), "我刚才的观点", "b1", time.time())
    bridge._complete = AsyncMock(return_value=json.dumps({"action": "reply", "text": "完整介绍"}))
    assert await bridge.generate(context, Decision("followup", "m1")) == "完整介绍"
    system, payload = bridge._complete.await_args.args
    assert "不因为对方语气强、人数多或要求道歉就改口" in system
    assert payload["target_author"] == {"sender_id": "20002", "sender_name": "另一位群友"}
    assert payload["reply_character_limit"] == 2000
    assert payload["runtime"]["tools_available"] is False
    assert "最多 240" not in system


@pytest.mark.asyncio
async def test_group_topic_can_answer_another_participant_and_recall_their_group_memory(bridge, monkeypatch):
    from qunling import group_memory
    from qunling.proactive.engine import ConversationContext, Decision, Message

    messages = (
        Message("bot", "30000", "a", "alice", "周末一起去看电影吧", time.time()),
        Message("bot", "30000", "b", "bob", "那预算多少合适", time.time()),
    )
    context = ConversationContext("bot", "30000", "active", "", messages, (), "", None, None,
                                  participant_id="alice", reply_candidate_ids=("a", "b"),
                                  focus_message_ids=("a", "b"))
    memory = {"group_id": "30000", "participant_id": "bob", "records": [
        {"speaker_id": "bob", "text": "我喜欢看动画电影", "timestamp": 1},
    ]}
    recall = AsyncMock(return_value=memory)
    monkeypatch.setattr(group_memory, "recall_group_memory", recall)
    bridge._complete = AsyncMock(return_value=json.dumps({"action": "reply", "text": "你们可以先选场次，再看票价。"}))

    assert await bridge.generate(context, Decision("reply", "b")) == "你们可以先选场次，再看票价。"
    recall.assert_awaited_once_with(bridge.bot, "30000", "bob", "那预算多少合适")
    _, payload = bridge._complete.await_args.args
    assert payload["shared_memory"] == memory
    assert payload["target_author"]["sender_id"] == "bob"
    assert payload["runtime"]["tools_available"] is False


@pytest.mark.asyncio
async def test_public_topic_reply_is_plain_but_direct_reply_keeps_quote(bridge):
    bridge.observe(event("大家周末去看电影怎么样", message_id="open-discussion"))
    await bridge.send("30000", "可以先看看周末的场次。", "open-discussion")
    chain = bridge.bot.adapter.send_message.await_args.args[2]
    assert [item.type for item in chain] == ["Plain"]

    directed = event("群灵，你也喜欢电影吗", message_id="direct-call")
    bridge.observe(directed)
    await bridge.send("30000", "聊电影我能接上。", "direct-call")
    chain = bridge.bot.adapter.send_message.await_args.args[2]
    assert [item.type for item in chain] == ["Quote", "Plain"]
    assert str(chain[0].id) == "direct-call"
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_free_and_explicit_share_confirmed_public_replies_beyond_old_four_minute_window(bridge):
    now = time.time()
    bridge.engine._clock = lambda: now
    older = event("周末看电影的预算就按每人八十", sender="20001", message_id="plan")
    older.timestamp = now
    bridge.observe(older)
    now += 600
    bridge.engine.on_bot_message("30000", "按每人八十来安排。", message_id="free-answer",
                                 recipient_id="20001", reply_to="plan")
    request = event("那包括晚饭吗", explicit=True, message_id="mention")
    request.timestamp = now
    bridge.observe(request)
    query = query_for(bridge, request)
    host.prepare_prompt(query)
    history = query.prompt.messages[-1].content
    assert "每人八十" in history and "free-answer" in history and '"sender_id": "20001"' in history
    host.sent(bridge.bot.adapter, "30000", MessageChain([Plain(text="晚饭另算。")]),
              receipt={"message_id": "explicit-answer"}, recipient_id="20000", reply_to="mention")
    current = bridge.engine.snapshot("30000", before_timestamp=now + 1)
    assert any(item.message_id == "explicit-answer" and item.text == "晚饭另算。" for item in current)
    host.completed(bridge.bot.adapter, request)
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_send_supports_complete_directed_text_with_hard_limit(bridge):
    await bridge.send("30000", "字" * 1600)
    with pytest.raises(host.ProactiveUnavailable):
        await bridge.send("30000", "字" * 2001)
    assert bridge.bot.adapter.send_message.await_count == 1


@pytest.mark.asyncio
async def test_unlisted_groups_and_self_messages_have_no_model_or_buffer(bridge):
    for item in (event(group="90000"), event(sender="10000")):
        host.observe(bridge.bot, item, bridge.bot.adapter)
    await bridge.engine.drain()
    assert bridge.engine._groups == {}
    bridge.engine._judge.assert_not_called()
    bridge.bot.adapter.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_image_holds_its_participant_but_another_can_chat(bridge):
    request = event(text="", explicit=True)
    host.observe(bridge.bot, request, bridge.bot.adapter)
    host.queued(bridge.bot.adapter, request)
    host.ingress_done(bridge.bot, request)
    host.observe(bridge.bot, event(message_id="same-person"), bridge.bot.adapter)
    host.observe(bridge.bot, event(message_id="other", sender="20001"), bridge.bot.adapter)
    await bridge.engine.drain()
    assert bridge.engine.status()["groups"]["30000"]["explicit_busy"]
    bridge.engine._judge.assert_awaited_once()
    context = bridge.engine._judge.await_args.args[0]
    assert context.participant_id == "20001"
    assert context.reply_candidate_ids == ("other",)
    host.completed(bridge.bot.adapter, request)
    assert not bridge.engine.status()["groups"]["30000"]["explicit_busy"]


@pytest.mark.asyncio
async def test_failed_ingress_releases_explicit_reservation(bridge):
    request = event(explicit=True)
    host.observe(bridge.bot, request, bridge.bot.adapter)
    host.ingress_done(bridge.bot, request)
    assert not bridge.engine.status()["groups"]["30000"]["explicit_busy"]


@pytest.mark.asyncio
async def test_other_person_mention_does_not_trigger_judgment(bridge):
    item = event()
    item.message_chain = MessageChain([At(target="20001"), Plain(text="你的观点呢？")])
    host.observe(bridge.bot, item, bridge.bot.adapter)
    await bridge.engine.drain()
    bridge.engine._judge.assert_not_called()


@pytest.mark.asyncio
async def test_stale_message_is_not_a_new_participation(bridge):
    item = event()
    item.timestamp -= 1000
    host.observe(bridge.bot, item, bridge.bot.adapter)
    await bridge.engine.drain()
    bridge.engine._judge.assert_not_called()


@pytest.mark.asyncio
async def test_completion_reuses_scoped_model_without_tools_or_query(bridge):
    result = await bridge._complete("system rules", {"messages": ["user text"]})
    assert result == "自然接话"
    call = bridge.bot.ap.model_mgr.get_model_by_uuid
    call.assert_awaited_once_with(bridge.bot.execution_context, "chat-model")
    args = call.return_value.provider.invoke_llm.await_args.kwargs
    assert args["query"] is None and args["funcs"] == []
    assert args["execution_context"] is bridge.bot.execution_context
    assert args["extra_args"]["tools"] is None
    assert [item.role for item in args["messages"]] == ["system", "user"]
    bridge.bot.adapter.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_tool_response_is_never_executed_or_sent(bridge):
    model = bridge.bot.ap.model_mgr.get_model_by_uuid.return_value
    model.provider.invoke_llm.return_value = NS(content="done", tool_calls=[{"name": "send_file"}])
    with pytest.raises(host.ProactiveUnavailable):
        await bridge._complete("system", {})
    bridge.bot.adapter.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_actual_pinned_provider_query_none_and_message_content(bridge, monkeypatch):
    from langbot.pkg.provider.modelmgr.requester import RuntimeLLMModel, RuntimeProvider
    from langbot.pkg.provider.modelmgr.requesters import litellmchat

    requester = litellmchat.LiteLLMRequester(bridge.bot.ap, {"custom_llm_provider": "openai"})
    # Only the network boundary is mocked. Both pinned requester argument
    # assembly and RuntimeProvider response/usage conversion execute normally.
    network = AsyncMock(return_value=NS(choices=[NS(message=NS(model_dump=lambda: {
        "role": "assistant", "content": "自然接话",
    }))], usage=None))
    monkeypatch.setattr(litellmchat, "acompletion", network)
    context = bridge.bot.execution_context
    provider = RuntimeProvider(context, NS(uuid="provider", workspace_uuid="workspace", requester="openai-chat-completions"),
                               NS(get_token=lambda: "offline-test-placeholder"), requester)
    entity = NS(uuid="chat-model", workspace_uuid="workspace", provider_uuid="provider", name="offline-model",
                extra_args={"tools": [{"type": "function", "function": {"name": "forbidden"}}]},
                reasoning_config=None, abilities=[])
    model = RuntimeLLMModel(context, entity, provider)
    bridge.bot.ap.model_mgr.get_model_by_uuid.return_value = model
    assert await bridge._complete("rules", {"messages": []}) == "自然接话"
    args = network.await_args.kwargs
    assert args["tools"] is None and args["tool_choice"] is None
    assert args["functions"] is None and args["function_call"] is None
    assert args["messages"][0] == {"role": "system", "content": "rules"}
    bridge.bot.adapter.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_sdk_text_content_elements_are_supported(bridge):
    provider = bridge.bot.ap.model_mgr.get_model_by_uuid.return_value.provider
    provider.invoke_llm.return_value = ModelMessage(role="assistant", content=[{"type": "text", "text": "自然接话"}])
    assert await bridge._complete("rules", {}) == "自然接话"


@pytest.mark.asyncio
async def test_known_other_local_bot_cannot_trigger_reply_loop(bridge):
    bridge.bot.ap.platform_mgr.bots = [NS(execution_context=bridge.bot.execution_context, adapter=NS(bot_account_id="20000"))]
    host.observe(bridge.bot, event(), bridge.bot.adapter)
    await bridge.engine.drain()
    assert bridge.engine._groups == {}
    bridge.engine._judge.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "replaced", "foreign", "offline"])
async def test_stale_or_disabled_runtime_never_sends(bridge, change):
    if change == "disabled":
        bridge.bot.ap.bot_service.get_bot.return_value["enable"] = False
    elif change == "replaced":
        bridge.bot.ap.platform_mgr.get_bot_by_uuid.return_value = NS()
    elif change == "foreign":
        bridge.bot.bot_entity.workspace_uuid = "other"
    else:
        bridge.bot.adapter.bot.connection.closed = True
    with pytest.raises(host.ProactiveUnavailable):
        await bridge.send("30000", "hello")
    bridge.bot.adapter.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_configuration_change_during_send_validation_cancels_old_reply(bridge):
    async def mutate():
        value = bridge.store.read()
        value["groups"]["30000"]["persona"] = "修改了性格"
        bridge.store.save(value, known_group_ids={"30000"})
    bridge._active = mutate
    with pytest.raises(host.ProactiveUnavailable):
        await bridge.send("30000", "准备好的旧回复")
    bridge.bot.adapter.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_send_uses_trusted_group_and_plain_component_without_observing_itself(bridge):
    async def send(kind, group_id, chain):
        host.sent(bridge.bot.adapter, group_id, chain, {"message_id": "sent"})
        return NS(message_id="sent")
    bridge.bot.adapter.send_message.side_effect = send
    await bridge.send("30000", "[CQ:at,qq=all] literal text")
    args = bridge.bot.adapter.send_message.await_args.args
    assert args[:2] == ("group", "30000")
    assert len(args[2]) == 1 and isinstance(args[2][0], Plain)
    assert bridge.engine._groups == {}
    with pytest.raises(host.ProactiveUnavailable):
        await bridge.send("90000", "hello")
    assert bridge.bot.adapter.send_message.await_count == 1


@pytest.mark.asyncio
async def test_successful_explicit_reply_is_available_for_natural_followup(bridge):
    host.sent(bridge.bot.adapter, "30000", MessageChain([Plain(text="我的观点")]), {"message_id": "real-reply"}, "20000")
    state = bridge.engine._groups["30000"].lanes["20000"]
    assert state.last_bot_text == "我的观点"
    assert state.last_bot_id == "real-reply" and state.last_recipient == "20000"


@pytest.mark.asyncio
async def test_admin_config_requires_permission_and_correct_workspace(bridge):
    with pytest.raises(PermissionDeniedError):
        await host.get_config(bridge.bot.ap, admin_context("viewer"), "bot")
    with pytest.raises(host.ProactiveUnavailable):
        await host.get_config(bridge.bot.ap, admin_context(workspace="other"), "bot")
    state = await host.get_config(bridge.bot.ap, admin_context(), "bot")
    assert state["config"]["groups"]["30000"]["mode"] == "natural"
    assert "messages" not in json.dumps(state["status"])


@pytest.mark.asyncio
async def test_management_uses_real_group_list_and_cannot_enable_unknown_group(bridge):
    assert (await host.list_groups(bridge.bot.ap, admin_context(), "bot"))["groups"] == [{"group_id": "30000", "name": "测试群"}]
    config = bridge.store.read()
    config["groups"]["90000"] = {**copy.deepcopy(DEFAULT_GROUP), "mode": "natural"}
    with pytest.raises(ConfigError):
        await host.save_config(bridge.bot.ap, admin_context(), "bot", config)
    assert "90000" not in bridge.store.read()["groups"]


@pytest.mark.asyncio
async def test_global_pause_is_available_offline_but_enabling_is_not(bridge):
    bridge.bot.adapter.get_group_list.side_effect = TimeoutError()
    config = bridge.store.read()
    config["enabled"] = False
    result = await host.save_config(bridge.bot.ap, admin_context(), "bot", config)
    assert result["config"]["enabled"] is False
    config = bridge.store.read()
    config["enabled"] = True
    with pytest.raises(ConfigError):
        await host.save_config(bridge.bot.ap, admin_context(), "bot", config)


@pytest.fixture
def patched_host(tmp_path):
    spec = importlib.util.spec_from_file_location("proactive_test_patcher", ROOT / "scripts/Apply-RuntimePatches.py")
    patcher = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = patcher
    spec.loader.exec_module(patcher)
    source = (ROOT / "patches/host-proactive-chat.patch").read_text(encoding="utf-8")
    _, paths = patcher.runtime_payload(source, "host")
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((LANGBOT_ROOT / path).read_bytes())
    plan = patcher.build_plan("host-proactive-chat.patch", tmp_path, source, "host")
    patcher.execute_plans([plan], apply=True)
    for path in paths:
        ast.parse((tmp_path / path).read_text(encoding="utf-8"))
    assert patcher.build_plan("host-proactive-chat.patch", tmp_path, source, "host").state == "already-applied"
    context_source = (ROOT / "patches/host-proactive-context.patch").read_text(encoding="utf-8")
    _, context_paths = patcher.runtime_payload(context_source, "host")
    for path in context_paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((LANGBOT_ROOT / path).read_bytes())
    context_plan = patcher.build_plan("host-proactive-context.patch", tmp_path, context_source, "host")
    patcher.execute_plans([context_plan], apply=True)
    assert patcher.build_plan("host-proactive-context.patch", tmp_path, context_source, "host").state == "already-applied"
    for path in context_paths:
        ast.parse((tmp_path / path).read_text(encoding="utf-8"))
    return tmp_path


def test_patch_applies_only_to_private_copy_and_contains_authenticated_routes(patched_host):
    router = ast.parse((patched_host / "src/langbot/pkg/api/http/controller/groups/platform/bots.py").read_text(encoding="utf-8"))
    methods = [node for node in ast.walk(router) if isinstance(node, ast.AsyncFunctionDef) and node.name in {"proactive_get", "proactive_save", "proactive_groups"}]
    assert len(methods) == 3
    for method in methods:
        route = method.decorator_list[0]
        options = {keyword.arg: ast.unparse(keyword.value) for keyword in route.keywords}
        assert options["permission"] == "Permission.RESOURCE_MANAGE"
        assert options["auth_type"] == "group.AuthType.USER_TOKEN_OR_API_KEY"


def test_context_bridge_runs_after_plugin_preprocessing(patched_host):
    source = (patched_host / "src/langbot/pkg/pipeline/preproc/preproc.py").read_text(encoding="utf-8")
    assert source.index("query.messages = event_ctx.event.prompt") < source.index("prepare_proactive_prompt(query)")
    assert "if os.environ.get('QUNLING_DATA_DIR')" in source


def patched_method(base, path, class_name, name, namespace):
    tree = ast.parse((base / path).read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next(node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(base / path), "exec"), namespace)
    return namespace[name]


@pytest.mark.asyncio
async def test_patched_queue_preserves_message_identity_and_releases_failure(bridge, patched_host):
    from langbot_plugin.api.entities.builtin.platform import entities, events, message
    from langbot_plugin.api.entities.builtin.provider import session

    method = patched_method(patched_host, "src/langbot/pkg/platform/botmgr.py", "RuntimeBot", "_handle_legacy_message_event",
                            {"os": os, "platform_events": events, "platform_message": message, "provider_session": session})
    bridge.bot.logger = NS(info=AsyncMock())
    bridge.bot.PIPELINE_DISCARD = "discard"
    bridge.bot.ap.msg_aggregator = NS(add_message=AsyncMock())
    chain = MessageChain([message.Source(id=123, time=int(time.time())), At(target="10000"), Plain(text="明确提问")])
    incoming = events.MessageReceivedEvent(adapter_name="onebot-v11", message_id="123", timestamp=time.time(),
        message_chain=chain, sender=entities.User(id="20000", nickname="群友"),
        chat_type=entities.ChatType.GROUP, chat_id="30000", group=entities.UserGroup(id="30000", name="群"))
    host.observe(bridge.bot, incoming, bridge.bot.adapter)
    legacy = incoming.to_legacy_event()
    await method(bridge.bot, legacy, bridge.bot.adapter, pipeline_uuid_override="pipeline")
    host.ingress_done(bridge.bot, incoming)
    queued = bridge.bot.ap.msg_aggregator.add_message.await_args.kwargs
    assert queued["message_event"] is legacy and queued["message_chain"] is chain
    assert queued["sender_id"] == "20000" and str(queued["launcher_id"]) == "30000"
    assert bridge.engine.status()["groups"]["30000"]["explicit_busy"]
    host.completed(bridge.bot.adapter, legacy)
    assert not bridge.engine.status()["groups"]["30000"]["explicit_busy"]
    host.observe(bridge.bot, incoming, bridge.bot.adapter)
    bridge.bot.ap.msg_aggregator.add_message.side_effect = RuntimeError("queue unavailable")
    with pytest.raises(RuntimeError, match="queue unavailable"):
        await method(bridge.bot, legacy, bridge.bot.adapter, pipeline_uuid_override="pipeline")
    assert not bridge.queued
    assert not bridge.engine.status()["groups"]["30000"]["explicit_busy"]


@pytest.mark.asyncio
async def test_patched_onebot_send_and_reply_record_actual_success_only(bridge, patched_host):
    import aiocqhttp
    from langbot.pkg.platform.adapters.aiocqhttp.message_converter import AiocqhttpMessageConverter
    from langbot_plugin.api.entities.builtin.platform import events, message

    namespace = {"os": os, "typing": typing, "aiocqhttp": aiocqhttp, "platform_events": events,
                 "platform_message": message, "AiocqhttpMessageConverter": AiocqhttpMessageConverter}
    send = patched_method(patched_host, "src/langbot/pkg/platform/adapters/aiocqhttp/api_impl.py", "AiocqhttpAPIMixin", "send_message", namespace)
    reply = patched_method(patched_host, "src/langbot/pkg/platform/adapters/aiocqhttp/api_impl.py", "AiocqhttpAPIMixin", "reply_message", namespace)
    bridge.bot.adapter.bot.send_group_msg = AsyncMock(return_value={"message_id": 910})
    chain = MessageChain([At(target="20000"), Plain(text="我的具体观点")])
    result = await send(bridge.bot.adapter, "group", "30000", chain)
    state = bridge.engine._groups["30000"].lanes["20000"]
    assert result.message_id == 910 and state.last_bot_id == "910"
    assert state.last_recipient == "20000" and state.last_bot_text == "我的具体观点"
    bridge.bot.adapter.bot.send_group_msg.side_effect = RuntimeError("transport failed")
    with pytest.raises(RuntimeError):
        await send(bridge.bot.adapter, "group", "30000", MessageChain([Plain(text="不能观察失败消息")]))
    assert state.last_bot_id == "910" and state.last_bot_text == "我的具体观点"
    native = aiocqhttp.Event.from_payload({"post_type": "message", "message_type": "group", "group_id": 30000,
                                         "user_id": 20001, "message_id": 123, "self_id": 10000})
    bridge.bot.adapter.bot.send = AsyncMock(return_value={"message_id": 911})
    result = await reply(bridge.bot.adapter, NS(source_platform_object=native), MessageChain([Plain(text="接另一个人的话")]))
    other = bridge.engine._groups["30000"].lanes["20001"]
    assert result.message_id == 911 and other.last_bot_id == "911" and other.last_recipient == "20001"
    assert bridge.engine._groups["30000"].reply_index["911"].target_id == "123"
    assert state.last_bot_id == "910" and state.last_recipient == "20000"


@pytest.mark.asyncio
async def test_directed_send_quotes_only_a_live_target_from_the_authorized_group(bridge):
    incoming = event("群灵出来聊天", message_id="123")
    host.observe(bridge.bot, incoming, bridge.bot.adapter)
    await bridge.send("30000", "[CQ:at,qq=all] 只是普通文字", "123")
    kind, group, chain = bridge.bot.adapter.send_message.await_args.args
    assert (kind, group) == ("group", "30000")
    assert len(chain) == 2 and isinstance(chain[0], Quote) and isinstance(chain[1], Plain)
    assert str(chain[0].id) == "123" and str(chain[0].group_id) == "30000"
    assert str(chain[0].sender_id) == "20000"
    assert chain[1].text == "[CQ:at,qq=all] 只是普通文字"
    for target in ("unknown", "foreign-target"):
        with pytest.raises(host.ProactiveUnavailable, match="目标已失效"):
            await bridge.send("30000", "不能接到错误消息", target)
    explicit = event("完整工具请求", explicit=True, message_id="explicit")
    host.observe(bridge.bot, explicit, bridge.bot.adapter)
    with pytest.raises(host.ProactiveUnavailable, match="目标已失效"):
        await bridge.send("30000", "不能重复代答工具任务", "explicit")
    assert bridge.bot.adapter.send_message.await_count == 1
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_generate_preserves_shared_focus_and_refuses_another_members_explicit_task(bridge):
    from qunling.proactive.engine import ConversationContext, Decision, Message

    first = Message("bot", "30000", "translation", "20001", "翻译这页", time.time(), explicit_request=True, has_media=True)
    second = Message("bot", "30000", "chat", "20002", "小灵，今天有什么开心事", time.time())
    context = ConversationContext("bot", "30000", "active", "自然简短", (first, second), (), "", None, None,
                                  conversation_id="30000:20002", participant_id="20002", focus_message_ids=("chat",),
                                  reply_candidate_ids=("chat",))
    bridge._complete = AsyncMock(return_value=json.dumps({"action": "reply", "text": "说说你的吧"}))
    assert await bridge.generate(context, Decision("reply", "chat")) == "说说你的吧"
    prompt, value = bridge._complete.await_args.args
    assert value["context"]["participant_id"] == "20002"
    assert value["context"]["focus_message_ids"] == ("chat",)
    assert value["target_author"]["sender_id"] == "20002"
    assert "participant_id 只是批次发起者" in prompt
    from qunling.group_persona import group_persona_prompt
    assert group_persona_prompt(context.display_name, context.persona, context.default_style) in prompt
    with pytest.raises(host.ProactiveUnavailable, match="目标已失效"):
        await bridge.generate(context, Decision("reply", "translation"))
    bridge._complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_prompt_marks_own_history_and_actual_quoted_evidence(bridge):
    for incoming in (event("另一人的翻译任务", sender="20001", message_id="other-task"),
                     event("我想讨论这一句", sender="20000", message_id="own-question")):
        host.observe(bridge.bot, incoming, bridge.bot.adapter)
    host.sent(bridge.bot.adapter, "30000", MessageChain([
        Quote(id="own-question", origin=MessageChain([])), Plain(text="这句可以这样理解")
    ]), {"message_id": "own-answer"}, "20000")
    request = event("这句的主语是什么", explicit=True, message_id="followup")
    request.message_chain.append(Quote(id="own-answer", origin=MessageChain([])))
    host.observe(bridge.bot, request, bridge.bot.adapter)
    query = query_for(bridge, request)
    host.prepare_prompt(query)
    status = json.loads(query.prompt.messages[1].content.rsplit("\n", 1)[-1])
    assert status["participant_id"] == "20000"
    assert set(status["focus_message_ids"]) == {"other-task", "own-question", "own-answer"}
    assert set(status["topic_message_ids"]) == {"own-question", "own-answer"}
    assert "另一人的翻译任务" in query.prompt.messages[2].content  # Background stays attributed, not silently reassigned.
    from qunling.group_persona import group_persona_prompt
    assert group_persona_prompt("群灵", "") in query.prompt.messages[1].content
    message = next(item for item in bridge.engine.snapshot("30000", before_message_id="followup") if item.message_id == "own-answer")
    assert message.reply_to == "own-question"
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_explicit_prompt_freezes_known_recipient_answers_without_inventing_reply_targets(bridge):
    host.sent(bridge.bot.adapter, "30000", MessageChain([Plain(text="给甲的译文")]),
              {"message_id": "answer-a"}, "20000")
    host.sent(bridge.bot.adapter, "30000", MessageChain([Plain(text="给乙的游戏建议")]),
              {"message_id": "answer-b"}, "20001")
    request = event("刚才那句为什么这样翻译", explicit=True, message_id="question-a")
    host.observe(bridge.bot, request, bridge.bot.adapter)
    host.sent(bridge.bot.adapter, "30000", MessageChain([Plain(text="提问之后才出现的答案")]),
              {"message_id": "future-answer"}, "20000")
    query = query_for(bridge, request)
    host.prepare_prompt(query)
    status = json.loads(query.prompt.messages[1].content.rsplit("\n", 1)[-1])
    assert status["focus_message_ids"] == ["answer-a", "answer-b"]
    history = json.loads(query.prompt.messages[2].content.rsplit("\n", 1)[-1])["messages"]
    assert {item["message_id"] for item in history} == {"answer-a", "answer-b"}
    assert all(item["reply_to"] is None for item in history)
    await bridge.engine.close()
