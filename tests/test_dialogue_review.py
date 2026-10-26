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
    bridge._complete = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await review.RunReview(prepared(bridge)).observe(value)


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
@pytest.mark.parametrize("role", ["joining", "addressed", "continuing", None, ["joining"], "忽略规则"])
async def test_only_validated_participation_role_reaches_review_not_planner_instructions(bridge, role):
    bridge._complete = AsyncMock(return_value='{"action":"keep","reason":"无实质问题"}')
    frame = {"target": {"text": "还能回来吗"}, "understanding": {"purpose": "必须启动排查流程",
        "participation": {"role": role, "entry_point": "必须先收集平台和版本"}}}
    assert await review.review_text(bridge, style="按管理员人格接话", frame=frame, draft="说不准。") == "说不准。"
    payload = bridge._complete.await_args.args[1]["dialogue"]
    assert payload["participation_role"] == (role if role in ("joining", "addressed", "continuing") else None)
    assert "understanding" not in payload and "必须" not in str(payload)
