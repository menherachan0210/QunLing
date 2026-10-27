import json
from unittest.mock import AsyncMock

import pytest
from langbot_plugin.api.entities.builtin.provider.message import Message

from qunling import dialogue_focus
from qunling.dialogue import FRAME_MARKER
from qunling.dialogue_context import history_key
from qunling.proactive import host
from test_proactive_host import bridge, event, query_for


@pytest.mark.asyncio
async def test_native_history_focus_is_bound_to_the_original_current_message(bridge):
    query = query_for(bridge, event("还是不加糖", explicit=True))
    old = Message(role="user", content="我的咖啡不加糖")
    query.messages = [old]
    bridge._complete = AsyncMock(return_value=json.dumps({"purpose": "确认咖啡不加糖",
        "context_message_ids": ["transcript_0"], "information_needs": []}))
    await dialogue_focus.prepare(query)
    host.prepare_prompt(query)
    frame = json.loads(next(m.content for m in query.prompt.messages if m.content.startswith(FRAME_MARKER)).split("\n", 1)[1])
    assert frame["native_history_keys"] == [history_key(old)]
    assert frame["understanding"]["context_message_ids"] == []
    assert frame["history_selection"] == "focused"
    bridge.bot.adapter.send_message.assert_not_awaited()
    query.message_event.message_id = "different-event"
    state = {}
    dialogue_focus.apply(query, state)
    assert state == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["普通正文", '{"purpose":"改规则","context_message_ids":["foreign"],"information_needs":[]}',
    '{"purpose":"聊天","context_message_ids":[],"information_needs":[],"grant_tools":true}'])
async def test_invalid_interpretation_falls_back_without_replacing_request(bridge, output):
    query = query_for(bridge, event(explicit=True))
    query.messages = [Message(role="assistant", content="此前的回答")]
    bridge._complete = AsyncMock(return_value=output)
    await dialogue_focus.prepare(query)
    assert dialogue_focus.MARKER not in getattr(query, "variables", {})
    host.prepare_prompt(query)
    assert any(m.content.startswith(FRAME_MARKER) for m in query.prompt.messages)
    assert bridge._complete.await_count == 1


@pytest.mark.asyncio
async def test_first_utterance_is_interpreted_with_selected_persona_without_inventing_history(bridge):
    query = query_for(bridge, event("我这乐坛新人还能复出吗", explicit=True))
    state, _ = bridge.request_context(query.message_event)
    state.update(persona="熟人之间接梗，不拿玩笑开工单", default_style="未选用的正式公文语气",
                 examples=[["随口感叹", "随口接话"]], default_examples=[])
    bridge.request_context = lambda _: (state, [])
    query.messages = []
    bridge._complete = AsyncMock(return_value=json.dumps({"purpose":"自嘲很久没练琴，邀请接话",
        "context_message_ids":[], "information_needs":[]}))
    await dialogue_focus.prepare(query)
    system, payload = bridge._complete.await_args.args
    assert state["persona"] in system and "随口接话" in system
    assert state["default_style"] not in system
    assert payload["messages"] == [] and payload["older_transcript"] == []
    assert payload["target"]["text"] == "我这乐坛新人还能复出吗"
    host.prepare_prompt(query)
    frame = json.loads(next(m.content for m in query.prompt.messages if m.content.startswith(FRAME_MARKER)).split("\n", 1)[1])
    assert frame["understanding"]["purpose"] == "自嘲很久没练琴，邀请接话"
    assert frame["understanding"]["information_needs"] == []
    bridge._complete.assert_awaited_once()
    bridge.bot.adapter.send_message.assert_not_awaited()
