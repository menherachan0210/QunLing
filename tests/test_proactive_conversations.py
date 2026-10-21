"""Concurrent participants keep independent targets, history and lifecycle."""
import asyncio
from dataclasses import replace

import pytest

from qunling.proactive.engine import GroupPolicy
from test_proactive_engine import Harness, eventually


def test_two_participants_in_one_group_are_judged_independently_without_consuming_each_other():
    async def scenario():
        h = Harness()
        entered, release = asyncio.Event(), asyncio.Event()

        async def judge(context):
            h.checked.append(context)
            if context.participant_id == "alice":
                entered.set()
                await release.wait()
            return {"action": "reply", "target_message_id": context.reply_candidate_ids[-1]}

        async def send(group, text, target):
            receipt = await h.send(group, text, target)
            h.now += 5
            return receipt

        h.engine._judge, h.engine._send = judge, send
        h.engine.on_message(h.message("alice-call", sender_id="alice", text="群灵，过来聊聊"))
        await entered.wait()
        h.engine.on_message(h.message("bob-call", sender_id="bob", text="群灵，听我说说"))
        await eventually(lambda: len(h.sent) == 1)
        assert h.sent[0][2] == "bob-call"
        release.set()
        await h.engine.drain()
        assert [item[2] for item in h.sent] == ["bob-call", "alice-call"]
        assert [item.reply_candidate_ids for item in h.checked] == [("alice-call",), ("bob-call",)]
        assert len({item.conversation_id for item in h.checked}) == 2
        assert "alice-call" in h.checked[1].focus_message_ids
        assert any(m.message_id == "alice-call" for m in h.checked[1].messages)
    asyncio.run(scenario())


def test_other_person_tool_task_does_not_cancel_chat_and_own_followup_resumes_after_tool():
    async def scenario():
        h = Harness()
        entered, release = asyncio.Event(), asyncio.Event()
        cancelled = []

        async def judge(context):
            h.checked.append(context)
            if context.participant_id == "alice":
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.append(True)
                    raise
            return {"action": "reply", "target_message_id": context.reply_candidate_ids[-1]}

        async def send(group, text, target):
            result = await h.send(group, text, target)
            h.now += 5
            return result

        h.engine._judge, h.engine._send = judge, send
        h.engine.on_message(h.message("chat", sender_id="alice", text="群灵，最近怎么样？"))
        await entered.wait()
        h.engine.mark_explicit("100", "tool", "bob")
        h.engine.on_message(h.message("tool", sender_id="bob", explicit_request=True))
        h.engine.on_message(h.message("after-tool", sender_id="bob", text="群灵，等会再聊这个"))
        status = h.engine.status()["groups"]["100"]
        assert status["explicit_requests"] == 1 and status["active_conversations"] == 1
        assert status["status_code"] == "pending"
        release.set()
        await h.engine.drain()
        assert not cancelled and [item[2] for item in h.sent] == ["chat"]
        assert h.engine._groups["100"].lanes["bob"].candidates == {"after-tool"}
        h.engine.explicit_done("100", "tool")
        await h.engine.drain()
        assert [item[2] for item in h.sent] == ["chat", "after-tool"]
        assert h.engine.status()["groups"]["100"]["explicit_requests"] == 0
    asyncio.run(scenario())


def test_unknown_explicit_owner_cannot_block_other_users():
    async def scenario():
        h = Harness()
        h.engine.mark_explicit("100", "unknown-request")
        h.engine.on_message(h.message("known", text="群灵出来说话"))
        await h.engine.drain()
        assert [item[2] for item in h.sent] == ["known"]
        assert h.engine.status()["groups"]["100"]["explicit_requests"] == 0
    asyncio.run(scenario())


def test_waiting_own_tool_followup_expires_without_resurrecting_old_chat():
    async def scenario():
        h = Harness()
        h.engine.mark_explicit("100", "tool", "user")
        h.engine.on_message(h.message("followup", text="群灵，还有刚才的事"))
        h.now += 91
        h.engine.explicit_done("100", "tool")
        await h.engine.drain()
        assert not h.checked and not h.sent
        assert not h.engine._groups["100"].lanes["user"].candidates
    asyncio.run(scenario())


def test_bot_anchors_are_per_participant_and_exact_quote_overrides_latest_reply():
    async def scenario():
        h = Harness()
        h.action = "silence"
        h.engine.on_bot_message("100", "给A的回答", message_id="answer-a", recipient_id="alice")
        h.engine.on_bot_message("100", "给B的回答", message_id="answer-b", recipient_id="bob")
        h.engine.on_message(h.message("a-follow", sender_id="alice", text="那继续说说"))
        h.engine.on_message(h.message("b-quote", sender_id="bob", text="你刚才给A的这个回答是什么意思", reply_to="answer-a"))
        await h.engine.drain()
        contexts = {context.participant_id: context for context in h.checked}
        assert contexts["alice"].last_bot_message_id == "answer-a"
        assert contexts["alice"].last_bot_text == "给A的回答"
        assert contexts["alice"].followup_candidate_ids == ("a-follow",)
        assert "answer-b" in contexts["alice"].focus_message_ids
        assert contexts["bob"].last_bot_message_id == "answer-a"
        assert contexts["bob"].followup_candidate_ids == ("b-quote",)
        assert contexts["bob"].reply_candidate_ids == ("b-quote",)
        assert "answer-a" in contexts["bob"].focus_message_ids
        assert h.engine._groups["100"].lanes["alice"].last_bot_id == "answer-a"
        assert h.engine._groups["100"].lanes["bob"].last_bot_id == "answer-b"
    asyncio.run(scenario())


def test_generated_answers_wait_for_group_send_spacing_without_regenerating():
    async def scenario():
        h = Harness()
        h.engine._debounce_scale = .001
        b_generating, a_sent = asyncio.Event(), asyncio.Event()

        async def generate(context, decision):
            h.generated.append((context, decision))
            if context.participant_id == "alice":
                await b_generating.wait()
            else:
                b_generating.set()
                await a_sent.wait()
            return "针对当前这位群友的回答。"

        async def send(group, text, target):
            result = await h.send(group, text, target)
            if target == "a":
                a_sent.set()
            return result

        h.engine._generate, h.engine._send = generate, send
        h.engine.on_message(h.message("a", sender_id="alice", text="群灵，先说A"))
        h.engine.on_message(h.message("b", sender_id="bob", text="群灵，也说B"))
        await asyncio.wait_for(a_sent.wait(), 1)
        await eventually(lambda: h.engine._groups["100"].last_event["code"] == "cooldown")
        assert [item[2] for item in h.sent] == ["a"]
        assert len(h.generated) == 2
        h.now += 5
        h.engine._groups["100"].lanes["bob"].wakeup.set()
        await asyncio.wait_for(h.engine.drain(), 1)
        assert [item[2] for item in h.sent] == ["a", "b"]
        assert len(h.generated) == len(h.checked) == 2
    asyncio.run(scenario())


def test_conversation_and_running_limits_are_bounded_and_close_cancels_all():
    async def scenario():
        h = Harness()
        entered = set()

        async def judge(context):
            entered.add(context.participant_id)
            await asyncio.Event().wait()

        h.engine._judge = judge
        for index in range(16):
            h.engine.on_message(h.message(str(index), sender_id=f"sender-{index}", text=f"群灵，单独聊第{index}件事"))
        await eventually(lambda: len(entered) == 2)
        state = h.engine._groups["100"]
        assert len(state.lanes) == 16 and len(h.engine._tasks) == 16
        assert len(entered) == 2  # Waiting tasks do not occupy model slots.
        h.engine.on_message(h.message("overflow", sender_id="extra"))
        assert len(state.lanes) == 16 and "extra" not in state.lanes
        status = h.engine.status()["groups"]["100"]
        assert status["active_conversations"] == 16 and status["queued_conversations"] == 14
        await h.engine.close()
        assert not h.engine._tasks and not h.engine._pending and not h.sent
    asyncio.run(scenario())


def test_equal_message_ids_in_different_groups_keep_scoped_anchors_and_conversation_keys():
    async def scenario():
        h = Harness()
        h.action = "silence"
        h.config = replace(h.config, groups={"100": GroupPolicy(), "200": GroupPolicy()})
        h.engine.on_bot_message("100", "仅第一群", message_id="same-receipt", recipient_id="user")
        h.engine.on_message(h.message("same-input", group="100", reply_to="same-receipt"))
        h.engine.on_message(h.message("same-input", group="200", reply_to="same-receipt"))
        await h.engine.drain()
        contexts = {context.group_id: context for context in h.checked}
        assert contexts["100"].last_bot_text == "仅第一群"
        assert contexts["200"].last_bot_text == ""
        assert not contexts["200"].followup_candidate_ids
        assert contexts["100"].conversation_id != contexts["200"].conversation_id
        assert h.engine.resolve_target("100", "same-input").group_id == "100"
        assert h.engine.resolve_target("200", "same-input").group_id == "200"
        assert h.engine.resolve_target("300", "same-input") is None
        assert h.engine.resolve_target("100", "same-receipt") is None
        h.now += 91
        assert h.engine.resolve_target("100", "same-input") is None
    asyncio.run(scenario())


def test_focus_helper_uses_only_frozen_snapshot_and_keeps_recipient_reply():
    async def scenario():
        h = Harness()
        h.engine.on_bot_message("100", "已确认给A的答复", message_id="answer", recipient_id="alice")
        h.engine.on_message(h.message("before", sender_id="alice", explicit_request=True))
        snapshot = h.engine.snapshot("100", before_message_id="before", before_timestamp=h.now)
        h.engine.on_bot_message("100", "后来的答复", message_id="new-answer", recipient_id="alice")
        assert h.engine.focus_message_ids("100", "alice", snapshot) == ("answer",)
        assert h.engine.focus_message_ids("200", "alice", snapshot) == ()
        await h.engine.close()
    asyncio.run(scenario())


def test_unanswered_backoff_is_per_lane_while_normal_group_spacing_remains_shared():
    async def scenario():
        h = Harness()
        h.engine.on_message(h.message("alice-first", sender_id="alice"))
        await h.engine.drain()
        h.now += 45
        h.engine.on_message(h.message("alice-early", sender_id="alice"))
        h.engine.on_message(h.message("bob-first", sender_id="bob"))
        await h.engine.drain()
        assert [item[2] for item in h.sent] == ["alice-first", "bob-first"]
        assert h.engine._groups["100"].lanes["alice"].last_interjection_at == h.now - 45
        h.now += 44
        h.engine.on_message(h.message("alice-group-too-early", sender_id="alice"))
        await h.engine.drain()
        assert len(h.sent) == 2
        h.now += 1
        h.engine.on_message(h.message("alice-later", sender_id="alice"))
        await h.engine.drain()
        assert [item[2] for item in h.sent] == ["alice-first", "bob-first", "alice-later"]
    asyncio.run(scenario())


def test_two_generated_normal_answers_can_send_after_group_spacing():
    async def scenario():
        h = Harness()
        h.engine._debounce_scale = .001
        b_ready, a_sent = asyncio.Event(), asyncio.Event()

        async def generate(context, decision):
            h.generated.append((context, decision))
            if context.participant_id == "alice":
                await b_ready.wait()
            else:
                b_ready.set()
                await a_sent.wait()
            return "各自话题的普通接话。"

        async def send(group, text, target):
            result = await h.send(group, text, target)
            if target == "a":
                a_sent.set()
            return result

        h.engine._generate, h.engine._send = generate, send
        h.engine.on_message(h.message("a", sender_id="alice", text="预算下降影响采购计划"))
        h.engine.on_message(h.message("b", sender_id="bob", text="今天下雨要带伞"))
        await asyncio.wait_for(a_sent.wait(), 1)
        await eventually(lambda: h.engine._groups["100"].last_event["code"] == "cooldown")
        h.now += 45
        h.engine._groups["100"].lanes["bob"].wakeup.set()
        await asyncio.wait_for(h.engine.drain(), 1)
        assert [item[2] for item in h.sent] == ["a", "b"]
        assert len(h.generated) == 2
    asyncio.run(scenario())


def test_target_expiring_while_waiting_for_send_lock_is_not_sent_or_reserved():
    async def scenario():
        h = Harness()
        generated = asyncio.Event()

        async def generate(context, decision):
            generated.set()
            return "不会发出过期目标。"

        h.engine._generate = generate
        h.engine.on_bot_message("100", "之前的回答", message_id="seed", recipient_id="user")
        state = h.engine._groups["100"]
        await state.send_lock.acquire()
        h.engine.on_message(h.message("target", text="群灵，说说这个"))
        await generated.wait()
        for _ in range(3):
            await asyncio.sleep(0)
        h.now += 91
        state.send_lock.release()
        await h.engine.drain()
        assert not h.sent and not state.sends
        assert state.last_event["code"] == "batch_expired"
    asyncio.run(scenario())


def test_two_ordinary_cooldown_waiters_do_not_hold_up_a_direct_name_call():
    async def scenario():
        h = Harness()
        h.engine._debounce_scale = .001
        h.engine.refresh_configuration()
        state = h.engine._state("100")
        state.sends.append(h.now - 6)
        h.engine.on_message(h.message("a", sender_id="alice"))
        h.engine.on_message(h.message("b", sender_id="bob"))
        await asyncio.sleep(.005)
        assert len(h.engine._tasks) == 2 and not h.checked
        h.engine.on_message(h.message("c", sender_id="carol", text="群灵，出来聊聊"))
        for _ in range(100):
            if h.sent:
                break
            await asyncio.sleep(.001)
        assert [item[2] for item in h.sent] == ["c"]
        assert [item.participant_id for item in h.checked] == ["carol"]
        assert state.lanes["alice"].candidates == {"a"}
        assert state.lanes["bob"].candidates == {"b"}
        await h.engine.close()
    asyncio.run(scenario())


def test_quote_of_other_participant_answer_focuses_its_exact_original_question_only():
    async def scenario():
        h = Harness()
        h.action = "silence"
        h.engine.on_message(h.message("question-a", sender_id="alice", text="A原始问题", explicit_request=True))
        h.engine.on_message(h.message("another-a-task", sender_id="alice", text="A另一个无关任务", explicit_request=True))
        h.engine.on_bot_message("100", "A原问题的答复", message_id="answer-a", recipient_id="alice", reply_to="question-a")
        h.engine.on_message(h.message("quote-b", sender_id="bob", text="这个回答的意思是？", reply_to="answer-a"))
        await h.engine.drain()
        context = h.checked[0]
        assert context.participant_id == "bob"
        assert context.focus_message_ids == ("question-a", "answer-a", "quote-b")
        assert "another-a-task" not in context.focus_message_ids
        assert any(message.message_id == "another-a-task" for message in context.messages)
        frozen = tuple(message for message in context.messages if message.message_id != "question-a")
        assert "question-a" not in h.engine.focus_message_ids("100", "bob", frozen, "answer-a")
    asyncio.run(scenario())
