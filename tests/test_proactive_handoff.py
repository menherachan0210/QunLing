"""A grounded public continuation can enter the normal chain exactly once."""
import asyncio
from dataclasses import replace

import pytest

from qunling.proactive.engine import ConversationContext, GroupPolicy, validate_decision
from test_proactive_engine import Harness, eventually


def context_fixture():
    h = Harness()
    anchor = h.message("answer", sender_id="bot", is_bot=True, text="这页漫画的分镜很有意思。")
    target = h.message("question", sender_id="peer", text="这是什么漫画？", reply_to="answer")
    return ConversationContext("bot", "100", "natural", "", (anchor, target), (), "", None, None,
                               reply_candidate_ids=("question",), continuation_anchors=(("question", "answer"),))


def decision(**changes):
    return {"action": "tool_followup", "target_message_id": "question", **changes}


def test_tool_followup_accepts_a_real_public_anchor_across_members_and_only_its_target():
    context = context_fixture()
    result = validate_decision(decision(), context)
    assert result.action == "tool_followup" and result.covered_message_ids == ("question",)
    assert validate_decision(decision(covered_message_ids=["question"]), replace(context, mode="quiet"))


@pytest.mark.parametrize("kind", ["missing", "unknown", "human", "foreign_group", "foreign_bot", "future",
                                  "synthetic", "empty", "target_scope", "explicit", "other_mention", "covered", "not_candidate"])
def test_tool_followup_rejects_untrusted_or_ineligible_anchor_targets(kind):
    context = context_fixture()
    anchor, target = context.messages
    if kind == "missing":
        context = replace(context, continuation_anchors=())
    elif kind == "unknown":
        context = replace(context, continuation_anchors=(("question", "invented"),))
    elif kind == "human":
        anchor = replace(anchor, is_bot=False)
    elif kind == "foreign_group":
        anchor = replace(anchor, group_id="other")
    elif kind == "foreign_bot":
        anchor = replace(anchor, bot_id="other")
    elif kind == "synthetic":
        anchor = replace(anchor, message_id="bot-observed-123")
        context = replace(context, continuation_anchors=(("question", anchor.message_id),))
    elif kind == "empty":
        anchor = replace(anchor, text="")
    elif kind == "target_scope":
        target = replace(target, group_id="other")
    elif kind == "explicit":
        target = replace(target, explicit_request=True)
    elif kind == "other_mention":
        target = replace(target, mentions=("another-member",))
    elif kind == "covered":
        context = replace(context, covered_message_ids=("question",))
    elif kind == "not_candidate":
        context = replace(context, reply_candidate_ids=())
    context = replace(context, messages=(target, anchor) if kind == "future" else (anchor, target))
    assert validate_decision(decision(), context) is None


def test_handoff_cannot_claim_multiple_questions_or_model_supplied_anchor():
    context = context_fixture()
    peer = replace(context.messages[-1], message_id="peer-question")
    context = replace(context, messages=(*context.messages, peer), reply_candidate_ids=("question", "peer-question"))
    assert validate_decision(decision(covered_message_ids=["question", "peer-question"]), context) is None
    assert validate_decision(decision(anchor_message_id="answer"), context) is None


def seed(h):
    h.engine.on_bot_message("100", "这页漫画的分镜很有意思。", message_id="answer", recipient_id="original-member")


async def judge_handoff(h, context):
    h.checked.append(context)
    return {"action": "tool_followup", "target_message_id": context.reply_candidate_ids[-1]}


@pytest.mark.parametrize("quiet", [False, True])
def test_cross_member_unquoted_tool_followup_is_handed_off_without_a_second_answer(quiet):
    async def scenario():
        calls = []

        async def handoff(context, selected):
            calls.append((context, selected))
            return True

        h = Harness(handoff=handoff)
        if quiet:
            h.config = replace(h.config, groups={"100": GroupPolicy(mode="quiet")})
        h.engine._judge = lambda context: judge_handoff(h, context)
        seed(h)
        h.engine.on_message(h.message("question", sender_id="peer", text="你知道这啥漫画不"))
        await h.engine.drain()
        assert len(calls) == len(h.checked) == 1
        assert calls[0][0].continuation_anchors == (("question", "answer"),)
        assert not h.generated and not h.sent
        state = h.engine._groups["100"]
        assert len(state.handoffs) == 1 and not state.sends
        assert not state.covered and not state.coverage_receipts
        assert set(state.reply_index) == {"answer"}
        assert state.last_event["code"] == "handed_off"
        status = h.engine.status()["groups"]["100"]
        assert status["handoff_attempts_day"] == 1 and status["replies_day"] == 0
        assert status["last_sent_at"] is None
        await h.engine.close()
    asyncio.run(scenario())


def test_normal_chain_promotion_does_not_cancel_its_own_admission():
    async def scenario():
        completed = []

        async def handoff(context, selected):
            target = next(message for message in context.messages if message.message_id == selected.target_message_id)
            h.engine.mark_explicit(context.group_id, target.message_id, target.sender_id)
            await asyncio.sleep(0)
            completed.append(True)
            return True

        h = Harness(handoff=handoff)
        h.engine._judge = lambda context: judge_handoff(h, context)
        seed(h)
        h.engine.on_message(h.message("question", text="这是什么漫画？", reply_to="answer"))
        await h.engine.drain()
        state = h.engine._groups["100"]
        assert completed == [True] and state.last_event["code"] == "handed_off"
        assert state.lanes["user"].explicit == {"question"}
        assert state.lanes["user"].handoff_target is None
        assert not h.sent and not h.generated
        # Only the normal chain's real receipt can mark the question answered.
        h.engine.on_bot_message("100", "查到了，这是某部漫画。", message_id="normal-receipt",
                                recipient_id="user", reply_to="question")
        assert state.coverage_receipts == {"question": "normal-receipt"}
        h.engine.explicit_done("100", "question")
        await h.engine.close()
    asyncio.run(scenario())


def test_quiet_mode_cross_member_text_followup_uses_the_real_public_bot_anchor():
    async def scenario():
        calls = []

        async def handoff(context, selected):
            calls.append(selected)
            return True

        h = Harness(handoff=handoff)
        h.config = replace(h.config, groups={"100": GroupPolicy(mode="quiet")})
        h.action = "followup"
        seed(h)
        h.engine.on_message(h.message("question", sender_id="peer", text="作者是谁？"))
        await h.engine.drain()
        assert len(h.checked) == len(h.generated) == len(h.sent) == 1
        context = h.checked[0]
        assert context.continuation_anchors == (("question", "answer"),)
        assert context.followup_candidate_ids == ("question",)
        assert context.last_bot_message_id == "answer"
        assert context.last_bot_text == "这页漫画的分镜很有意思。"
        assert context.last_bot_at == h.now
        assert h.sent[0][2] == "question" and not calls
        assert not h.engine._groups["100"].handoffs
        await h.engine.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("exclusion", ["covered", "explicit", "not_candidate", "other_mention"])
def test_shared_text_followup_does_not_promote_ineligible_messages(exclusion):
    async def scenario():
        h = Harness()
        seed(h)
        message = h.message("question", sender_id="peer", text="作者是谁？",
                            explicit_request=exclusion == "explicit",
                            mentions=("another-member",) if exclusion == "other_mention" else ())
        h.engine.on_message(message)
        state = h.engine._groups["100"]
        lane = state.lanes["peer"]
        if exclusion == "covered":
            state.covered["question"] = h.now
        elif exclusion == "not_candidate":
            lane.candidates.clear()
        context = h.engine._context("100", state, lane, GroupPolicy())
        assert "question" not in context.followup_candidate_ids
        assert "question" not in dict(context.continuation_anchors)
        assert context.last_bot_message_id is None
        await h.engine.close()
    asyncio.run(scenario())


def test_borrowed_member_target_promotes_its_owner_without_cancelling_the_handoff():
    async def scenario():
        completed = []

        async def handoff(context, selected):
            target = next(message for message in context.messages if message.message_id == selected.target_message_id)
            assert context.participant_id == "alice" and target.sender_id == "bob"
            assert context.reply_candidate_ids == ("first", "question")
            assert h.engine.get_observed_message("100", "question") == target
            h.engine.mark_explicit(context.group_id, target.message_id, target.sender_id)
            await asyncio.sleep(0)
            completed.append(True)
            return True

        h = Harness(handoff=handoff)

        async def judge(context):
            h.checked.append(context)
            if "question" in context.reply_candidate_ids:
                return decision()
            return {"action": "silence"}

        h.engine._judge = judge
        seed(h)
        h.engine.on_message(h.message("first", sender_id="alice", text="这是什么漫画？"))
        h.engine.on_message(h.message("question", sender_id="bob", text="作者是谁？"))
        await h.engine.drain()
        state = h.engine._groups["100"]
        assert completed == [True]
        assert state.lanes["bob"].explicit == {"question"}
        assert not state.lanes["alice"].explicit
        assert len(state.handoffs) == 1 and not state.sends
        assert not h.sent and not h.generated
        h.engine.explicit_done("100", "question")
        await h.engine.close()
    asyncio.run(scenario())


def test_observed_message_lookup_is_scoped_pruned_and_revoked_but_keeps_claimed_evidence():
    async def scenario():
        h = Harness()
        original = h.message("question", explicit_request=True)
        assert h.engine.on_message(original)
        assert h.engine.get_observed_message("100", "question") == original
        assert h.engine.resolve_target("100", "question") is None
        assert h.engine.get_observed_message("other", "question") is None
        assert h.engine.get_observed_message("100", "invented") is None
        h.now += 1201
        assert h.engine.get_observed_message("100", "question") is None
        fresh = h.message("fresh", explicit_request=True)
        assert h.engine.on_message(fresh)
        assert h.engine.get_observed_message("100", "fresh") == fresh
        h.config = replace(h.config, enabled=False, revision="2")
        assert h.engine.get_observed_message("100", "fresh") is None
        assert "100" not in h.engine._groups
        await h.engine.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["false", "exception", "timeout", "no_callback"])
def test_failed_or_uncertain_handoff_is_recorded_once_and_never_retried(failure):
    async def scenario():
        calls = []

        async def handoff(context, selected):
            calls.append(selected.target_message_id)
            if failure == "exception":
                raise RuntimeError("private admission detail")
            if failure == "timeout":
                await asyncio.Event().wait()
            return False

        h = Harness(handoff=None if failure == "no_callback" else handoff, model_timeout=.02)
        h.engine._judge = lambda context: judge_handoff(h, context)
        seed(h)
        message = h.message("question", text="这是什么漫画？", reply_to="answer")
        h.engine.on_message(message)
        await h.engine.drain()
        assert not h.engine.on_message(message)
        await h.engine.drain()
        state = h.engine._groups["100"]
        assert len(calls) == (0 if failure == "no_callback" else 1)
        assert state.last_event["code"] == "handoff_failed"
        assert h.engine.resolve_target("100", "question") is None
        assert len(state.handoffs) == 1 and not state.covered
        assert not h.sent and not h.generated
        await h.engine.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["expired", "revision", "disabled", "send_budget"])
def test_handoff_revalidates_target_authorization_and_budget_after_judgment(change):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def judge(context):
            entered.set()
            await release.wait()
            return await judge_handoff(h, context)

        async def handoff(context, selected):
            calls.append(selected)
            return True

        h = Harness(handoff=handoff)
        h.engine._judge = judge
        seed(h)
        h.engine.on_message(h.message("question", text="这是什么漫画？", reply_to="answer"))
        await entered.wait()
        if change == "expired":
            h.now += 91
        elif change == "revision":
            h.config = replace(h.config, revision="2")
        elif change == "disabled":
            h.config = replace(h.config, revision="2", enabled=False)
        else:
            h.engine._groups["100"].sends.extend([h.now - 20] * 30)
        release.set()
        await h.engine.drain()
        assert not calls and not h.sent and not h.generated
        await h.engine.close()
    asyncio.run(scenario())


def test_new_explicit_request_can_cancel_an_inflight_handoff_for_another_target():
    async def scenario():
        entered = asyncio.Event()
        cancelled = []

        async def handoff(context, selected):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        h = Harness(handoff=handoff)
        h.engine._judge = lambda context: judge_handoff(h, context)
        seed(h)
        h.engine.on_message(h.message("question", text="这是什么漫画？", reply_to="answer"))
        await entered.wait()
        h.engine.mark_explicit("100", "new-explicit", "user")
        await h.engine.drain()
        assert cancelled == [True]
        assert h.engine._groups["100"].last_event["code"] == "handoff_failed"
        assert not h.sent and not h.generated
        await h.engine.close()
    asyncio.run(scenario())


def test_independent_member_handoff_does_not_hold_model_or_send_lock():
    async def scenario():
        first, second = asyncio.Event(), asyncio.Event()
        calls = []

        async def handoff(context, selected):
            calls.append(selected.target_message_id)
            if selected.target_message_id == "first":
                first.set()
                await second.wait()
            else:
                second.set()
            return True

        h = Harness(handoff=handoff)
        h.engine._judge = lambda context: judge_handoff(h, context)
        seed(h)
        h.engine.on_message(h.message("first", sender_id="alice", text="群灵，这是什么漫画？", reply_to="answer"))
        await first.wait()
        h.now += 5
        h.engine.on_message(h.message("second", sender_id="bob", text="群灵，这本的作者是谁？", reply_to="answer"))
        await asyncio.wait_for(h.engine.drain(), 1)
        assert calls == ["first", "second"] and not h.generated and not h.sent
        assert len(h.engine._groups["100"].handoffs) == 2
        await h.engine.close()
    asyncio.run(scenario())


def test_handoff_attempts_persist_and_share_send_limits_without_inventing_replies(tmp_path):
    async def scenario():
        async def accepted(context, selected):
            return True

        h = Harness(handoff=accepted, state_dir=tmp_path)
        h.engine._judge = lambda context: judge_handoff(h, context)
        seed(h)
        h.engine.on_message(h.message("question", text="这是什么漫画？", reply_to="answer"))
        await h.engine.drain()
        await h.engine.close()
        restored = Harness(handoff=accepted, state_dir=tmp_path)
        status = restored.engine.status()["groups"]["100"]
        assert status["handoff_attempts_day"] == 1 and status["replies_day"] == 0
        state = restored.engine._state("100")
        state.handoffs.extend([restored.now] * 29)
        assert not restored.engine._budget(state, GroupPolicy(), sending=True)
        assert not state.reply_index and not state.covered
        await restored.engine.close()
    asyncio.run(scenario())
