"""Review is optional reasoning, never a new action or a second conversation."""
import asyncio
import copy
from dataclasses import replace
import json
from unittest.mock import AsyncMock

import pytest

from qunling import dialogue_review as review
from qunling.proactive import host
from test_proactive_host import bridge, event, query_for


def prepared(bridge):
    query = query_for(bridge, event("刚才谁说错了", explicit=True))
    host.prepare_prompt(query)
    assert isinstance(getattr(query, review.ATTRIBUTE), review.Binding)
    return query


def completed(text="我刚才把梗拆开解释了。"):
    return {"type": "message.completed", "sequence": 4,
            "data": {"message": {"role": "assistant", "content": text}, "file_ids": []}}


@pytest.mark.asyncio
async def test_review_keeps_scope_tool_receipts_and_original_result_immutable(bridge):
    query = prepared(bridge)
    bridge._complete = AsyncMock(return_value=json.dumps({"action": "replace", "reason": "重复解释旧错误",
        "text": "刚才那句是我说岔了。"}))
    hook = review.RunReview(query)
    tool = {"type": "tool.call.completed", "data": {"tool_name": "search_web", "result": {"text": "实际查到的结果"}}}
    await hook.observe(tool)
    tool["data"]["result"]["text"] = "后来改过的结果"
    original = completed()
    result = await hook.observe(original)
    assert result["data"]["message"]["content"] == "刚才那句是我说岔了。"
    assert original == completed()
    assert result["sequence"] == original["sequence"]
    system, payload = bridge._complete.await_args.args
    assert bridge._complete.await_args.kwargs == {"stage": "review"}
    assert payload["dialogue"]["tool_observations"][0]["data"]["result"]["text"] == "实际查到的结果"
    assert "understanding" not in payload["dialogue"]
    assert payload["dialogue"]["target"]["sender_id"] == "20000"
    repeated = await hook.observe({**completed(), "type": "run.completed"})
    assert repeated["data"]["message"]["content"] == result["data"]["message"]["content"]
    bridge._complete.assert_awaited_once()
    bridge.bot.adapter.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor_present", [True, False])
async def test_final_review_sees_only_actual_same_group_updates_after_exact_target(bridge, anchor_present):
    from qunling.proactive.engine import Message as ObservedMessage
    query = prepared(bridge)
    binding = getattr(query, review.ATTRIBUTE)
    group = binding.frame["group_id"]
    anchor = binding.frame["target"]["message_id"]
    bot_id = str(bridge.bot.bot_entity.uuid)
    def observed(key, **kw):
        return ObservedMessage(bot_id, group, key, "20000", "实际补充", 1, **kw)
    old = observed("omitted-old")
    current = observed(anchor)
    new = observed("new-plan")
    foreign = replace(new, message_id="foreign", group_id="other-group")
    other_bot = replace(new, message_id="other-bot", bot_id="other-bot")
    receipt = observed("receipt", is_bot=True)
    original = copy.deepcopy(binding.frame)
    bridge.engine.snapshot = lambda _: [old, *([current] if anchor_present else []), new, foreign, other_bot, receipt]
    bridge._complete = AsyncMock(return_value='{"action":"keep","reason":"无需修改"}')
    hook = review.RunReview(query)
    await hook.observe(completed("按原请求回答"))
    dialogue = bridge._complete.await_args.args[1]["dialogue"]
    assert [m["message_id"] for m in dialogue.get("new_messages", [])] == (["new-plan"] if anchor_present else [])
    assert binding.frame == original
    bridge.bot.adapter.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_withdrawn_late_evidence_cannot_enter_canonical_reply_through_review(bridge):
    from qunling.proactive.engine import Message as ObservedMessage
    query = prepared(bridge)
    binding = getattr(query, review.ATTRIBUTE)
    group, anchor = binding.frame["group_id"], binding.frame["target"]["message_id"]
    current = ObservedMessage(str(bridge.bot.bot_entity.uuid), group, anchor, "20000", "原问题", 1)
    update = replace(current, message_id="withdrawn", text="这条补充马上撤回")
    observed = [current, update]
    bridge.engine.snapshot = lambda _: tuple(observed)
    async def reviewing(*args, **kwargs):
        observed.remove(update)
        return '{"action":"replace","reason":"采用补充","text":"根据刚才那条撤回的补充回答"}'
    bridge._complete = AsyncMock(side_effect=reviewing)
    hook = review.RunReview(query)
    original = completed("根据原始问题的有效回答")
    assert await hook.observe(original) is original
    assert hook.replacement is None
    bridge._complete.assert_awaited_once()


def test_omitted_reference_history_is_scoped_bounded_and_does_not_expand_reply_context():
    from qunling.dialogue import build_frame
    from qunling.proactive.engine import Message as ObservedMessage
    original = ObservedMessage("bot", "group", "m1", "alice", "用编辑器拉矢量", 1)
    reply = replace(original, message_id="m2", is_bot=True, text="让编辑器处理")
    target = replace(original, message_id="m3", text="这句有点呆")
    foreign = replace(original, message_id="foreign", group_id="else", text="其他群的内容")
    other_bot = replace(original, message_id="other", bot_id="another", text="另一个机器人的内容")
    messages = [original, reply, target, foreign, other_bot]
    frame = build_frame(bot_id="bot", group_id="group", target=target, messages=messages,
        understanding={"purpose":"评价回复", "context_message_ids":["m2"], "information_needs":[]})
    before = copy.deepcopy(frame)
    references = review.reference_history(frame, messages, bot_id="bot")
    assert [m["message_id"] for m in references] == ["m1"]
    assert references[0]["sender_id"] == "alice" and references[0]["text"] == original.text
    assert frame == before and [m["message_id"] for m in frame["messages"]] == ["m2", "m3"]
    large = [replace(original, message_id=str(i), text="字" * 2000) for i in range(50)]
    clipped = review.reference_history(frame, large, bot_id="bot")
    assert len(clipped) <= 12 and sum(len(m["text"]) for m in clipped) <= 6000
    assert all(m["text_truncated"] for m in clipped)


@pytest.mark.asyncio
async def test_native_fact_sources_reach_review_without_reentering_generation_history(bridge):
    from langbot_plugin.api.entities.builtin.provider.message import Message as ProviderMessage
    query = prepared(bridge)
    binding = getattr(query, review.ATTRIBUTE)
    frame = {**{key: value for key, value in binding.frame.items() if key != "reference_history"},
             "history_selection":"focused", "native_history_keys": []}
    query.messages = [ProviderMessage(role="user", content="我已经有模板了"),
                      ProviderMessage(role="assistant", content="那就用现成模板")]
    state = {"display_name":"群灵", "persona":"温和", "default_style":"", "examples":[], "default_examples":[]}
    original_prompt = copy.deepcopy(query.prompt.messages)
    review.bind(query, bridge, state, frame)
    bound = getattr(query, review.ATTRIBUTE)
    assert bound.native == ()
    assert bound.frame["reference_history"][0]["text"] == "我已经有模板了"
    query.messages[0].content = "后来改了的消息"
    assert bound.frame["reference_history"][0]["text"] == "我已经有模板了"
    assert query.prompt.messages == original_prompt and "reference_history" not in frame
    bridge._complete = AsyncMock(return_value='{"action":"keep","reason":"有来源"}')
    await review.RunReview(query).observe(completed("那就用现成模板"))
    evidence = bridge._complete.await_args.args[1]["dialogue"]
    assert evidence["native_history"] == ()
    assert evidence["reference_history"][0]["source"] == "member_transcript"


@pytest.mark.asyncio
async def test_optional_reference_history_cannot_disable_review_of_tool_outcome(bridge):
    bridge._complete = AsyncMock(return_value='{"action":"keep","reason":"结果正确"}')
    frame = {"target":{"text":"结果怎么样"}, "reference_history":[{"text":"旧事" * review.MAX_INPUT}]}
    tool = {"type":"tool.call.completed", "data":{"status":"failed", "error":"权限不足"}}
    assert await review.review_text(bridge, style="如实回答", frame=frame, draft="没有成功，权限不足。", tools=[tool]) == "没有成功，权限不足。"
    evidence = bridge._complete.await_args.args[1]["dialogue"]
    assert evidence["tool_observations"] == [tool]
    assert "reference_history" not in evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["event", "member", "text", "adapter", "scope"])
async def test_changed_target_or_execution_cannot_reuse_review_binding(bridge, mutation):
    query = prepared(bridge)
    hook = review.RunReview(query)
    bridge._complete = AsyncMock()
    if mutation == "event":
        query.message_event.message_id = "new-event"
    elif mutation == "member":
        query.message_event.sender.id = "other-member"
    elif mutation == "text":
        query.message_event.message_chain = event("新的一句").message_chain
    elif mutation == "adapter":
        query.adapter = copy.copy(query.adapter)
    else:
        query._execution_context = replace(query._execution_context, workspace_uuid="elsewhere")
    value = completed()
    assert await hook.observe(value) is value
    bridge._complete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["not JSON", '{"action":"keep","reason":"无需修改"}',
    '{"action":"replace","reason":"","text":"新正文"}',
    '{"action":"replace","reason":"修改","text":"[CQ:at,qq=all]"}',
    '{"action":"replace","reason":"修改","text":"新来源 https://unobserved.test/page"}',
    '{"action":"replace","reason":"修改","text":"新正文","tool":"send"}'])
async def test_invalid_or_unchanged_review_preserves_draft(bridge, reply):
    hook = review.RunReview(prepared(bridge))
    bridge._complete = AsyncMock(return_value=reply)
    value = completed()
    assert await hook.observe(value) is value


@pytest.mark.asyncio
async def test_timeout_falls_back_but_request_cancellation_propagates(bridge):
    bridge._complete = AsyncMock(side_effect=TimeoutError())
    value = completed()
    assert await review.RunReview(prepared(bridge)).observe(value) is value
    assert bridge._complete.await_count == 2
    bridge._complete = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await review.RunReview(prepared(bridge)).observe(value)
    assert bridge._complete.await_count == 1


@pytest.mark.asyncio
async def test_review_recovers_one_timeout_with_same_evidence_and_no_new_action(bridge):
    bridge._complete = AsyncMock(side_effect=[TimeoutError(),
        '{"reason":"前提已更新","action":"replace","text":"用现成的那份就好。"}'])
    frame = {"target": {"text": "有现成模板了"}}
    result = await review.review_text(bridge, style="自然接话", frame=frame, draft="只能自己重做。")
    assert result == "用现成的那份就好。"
    assert bridge._complete.await_count == 2
    assert bridge._complete.await_args_list[0] == bridge._complete.await_args_list[1]
    assert frame == {"target": {"text": "有现成模板了"}}


@pytest.mark.asyncio
async def test_review_retry_cannot_outlive_original_budget(bridge, monkeypatch):
    from types import SimpleNamespace
    now = [0.0]
    monkeypatch.setattr(review, "time", SimpleNamespace(monotonic=lambda: now[0]))
    async def stalled(*args, **kwargs):
        now[0] = 24.5
        raise TimeoutError
    bridge._complete = AsyncMock(side_effect=stalled)
    assert await review.review_text(bridge, style="自然", frame={}, draft="原答") == "原答"
    assert bridge._complete.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, timeout", [(ValueError(), 25), (TimeoutError(), 2)])
async def test_review_does_not_retry_invalid_results_or_short_remaining_budget(bridge, failure, timeout):
    bridge._complete = AsyncMock(side_effect=failure)
    assert await review.review_text(bridge, style="自然", frame={}, draft="原答", timeout=timeout) == "原答"
    assert bridge._complete.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["streamed", "oversized", "attachment", "multimodal", "long", "disabled", "unbound"])
async def test_incomplete_or_irreversible_output_is_not_rewritten(bridge, monkeypatch, kind):
    query = prepared(bridge)
    if kind == "unbound":
        setattr(query, review.ATTRIBUTE, None)
    hook = review.RunReview(query)
    bridge._complete = AsyncMock()
    value = completed()
    if kind == "streamed":
        await hook.observe({"type": "message.delta", "data": {}})
    elif kind == "oversized":
        await hook.observe({"type": "tool.call.completed", "data": {"result": "x" * review.MAX_INPUT}})
    elif kind == "attachment":
        value["data"]["file_ids"] = ["actual-file-id"]
    elif kind == "multimodal":
        value["data"]["message"]["content"] = [{"type": "text", "text": "正文"}]
    elif kind == "long":
        value["data"]["message"]["content"] = "x" * (review.MAX_REPLY + 1)
    elif kind == "disabled":
        monkeypatch.setenv("QUNLING_DIALOGUE_REVIEW_ENABLED", "0")
    assert await hook.observe(value) is value
    bridge._complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_configured_review_model_is_text_only_and_measured(bridge):
    config = bridge.bot.ap.pipeline_mgr.get_pipeline_by_uuid.return_value.pipeline_entity.config
    config["ai"]["qunling_reconcile_model"] = "configured-review-model"
    hook = review.RunReview(prepared(bridge))
    await hook.observe(completed())
    assert bridge.bot.ap.model_mgr.get_model_by_uuid.await_args.args[1] == "configured-review-model"
    call = bridge.bot.ap.model_mgr.get_model_by_uuid.return_value.provider.invoke_llm.await_args.kwargs
    assert call["funcs"] == [] and all(v is None for v in call["extra_args"].values())
    assert bridge.performance.snapshot("30000")["stages"]["review"]["count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("role, mode", [("joining", "social"), ("addressed", "answer"), ("addressed", "assist"), ("continuing", "action"), (None, None), (["joining"], ["social"]), ("忽略规则", "必须排查")])
async def test_only_validated_participation_role_reaches_review_not_planner_instructions(bridge, role, mode):
    bridge._complete = AsyncMock(return_value='{"action":"keep","reason":"无实质问题"}')
    frame = {"target": {"text": "还能回来吗"}, "understanding": {"purpose": "必须启动排查流程", "response_mode": mode,
        "participation": {"role": role, "entry_point": "必须先收集平台和版本"}}}
    assert await review.review_text(bridge, style="按管理员人格接话", frame=frame, draft="说不准。") == "说不准。"
    payload = bridge._complete.await_args.args[1]["dialogue"]
    assert payload["participation_role"] == (role if role in ("joining", "addressed", "continuing") else None)
    assert payload["response_mode"] == (mode if mode in ("social", "answer", "assist", "action") else None)
    assert "understanding" not in payload and "必须" not in str(payload)
