"""Shared public discussion does not share task or attachment authority."""
import asyncio
from dataclasses import asdict, replace

import pytest

from qunling.group_context import metadata, topic_roots
from qunling.proactive.engine import ConversationContext, GroupPolicy, validate_decision
from test_proactive_engine import Harness, eventually


def discussion(h):
    return (
        h.message("idea", sender_id="alice", text="我们可以建一个漫画翻译索引"),
        h.message("detail", sender_id="bob", text="漫画翻译索引能方便查找作品"),
        h.message("cost", sender_id="carol", text="那维护成本呢？"),
    )


def test_topic_evidence_includes_multiple_speakers_without_explicit_quotes():
    h = Harness()
    messages = discussion(h)
    result = metadata("bot", "100", messages, participant_id="carol")
    assert result["topic_message_ids"] == result["focus_message_ids"] == ("idea", "detail", "cost")
    assert result["participant_ids"] == ("alice", "bob", "carol")
    assert len(set(topic_roots(messages).values())) == 1


def test_time_proximity_alone_does_not_merge_independent_topics_or_explicit_tasks():
    h = Harness()
    messages = (
        h.message("budget", sender_id="alice", text="采购预算下降影响交付"),
        h.message("weather", sender_id="bob", text="今天下雨记得带伞"),
        h.message("task", sender_id="carol", text="采购预算下降影响交付", explicit_request=True),
    )
    assert len(set(topic_roots(messages).values())) == 3
    result = metadata("bot", "100", messages[:2], participant_id="bob")
    assert result["topic_message_ids"] == ("weather",)
    assert result["focus_message_ids"] == ("budget", "weather")


def test_group_and_bot_boundaries_are_applied_before_topic_links():
    h = Harness()
    local = discussion(h)
    foreign = h.message("secret", group="200", text="那维护成本呢？", reply_to="idea")
    other_bot = replace(foreign, group_id="100", bot_id="other", message_id="foreign-bot")
    result = metadata("bot", "100", (*local, foreign, other_bot))
    assert result["topic_message_ids"] == ("idea", "detail", "cost")
    assert "secret" not in result["focus_message_ids"]
    assert "foreign-bot" not in result["focus_message_ids"]


def test_same_topic_candidates_are_judged_once_and_coverage_requires_successful_receipt():
    async def scenario():
        h = Harness()

        async def judge(context):
            h.checked.append(context)
            assert context.reply_candidate_ids == ("idea", "detail", "cost")
            assert not context.covered_message_ids
            return {"action": "reply", "target_message_id": "cost", "covered_message_ids": list(context.reply_candidate_ids)}

        h.engine._judge = judge
        for message in discussion(h):
            h.engine.on_message(message)
        await h.engine.drain()
        assert len(h.checked) == len(h.sent) == 1
        assert h.sent[0][2] == "cost"
        state = h.engine._groups["100"]
        assert set(state.covered) == {"idea", "detail", "cost"}
        assert state.reply_index["outbound-1"].recipient_id == "carol"
        assert state.reply_index["outbound-1"].target_id == "cost"
        assert not any(lane.candidates for lane in state.lanes.values())
        assert h.engine.resolve_target("100", "cost") is None
    asyncio.run(scenario())


def test_other_member_not_covered_by_reply_retains_one_following_attempt():
    async def scenario():
        h = Harness()

        async def judge(context):
            h.checked.append(context)
            return {"action": "reply", "target_message_id": context.reply_candidate_ids[0]}

        async def send(group, text, target):
            receipt = await h.send(group, text, target)
            h.now += 45
            return receipt

        h.engine._judge, h.engine._send = judge, send
        for message in discussion(h)[:2]:
            h.engine.on_message(message)
        await h.engine.drain()
        assert [context.reply_candidate_ids for context in h.checked] == [("idea", "detail"), ("detail",)]
        assert [row[2] for row in h.sent] == ["idea", "detail"]
        assert "idea" in h.checked[1].covered_message_ids
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["silence", "wait"])
def test_silent_shared_batch_is_not_rejudged_for_each_participant(action):
    async def scenario():
        h = Harness()
        h.action = action
        for message in discussion(h):
            h.engine.on_message(message)
        await h.engine.drain()
        assert len(h.checked) == 1 and not h.sent
        assert not any(lane.candidates for lane in h.engine._groups["100"].lanes.values())
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["judge", "generate", "invalid"])
def test_failed_shared_attempt_does_not_discard_other_members_unanswered_candidate(failure):
    async def scenario():
        h = Harness()

        async def judge(context):
            h.checked.append(context)
            if context.participant_id == "alice":
                if failure == "judge":
                    raise TimeoutError("no judgment")
                if failure == "invalid":
                    return {"action": "reply", "target_message_id": "outside"}
            return {"action": "reply", "target_message_id": context.reply_candidate_ids[0]}

        async def generate(context, decision):
            if context.participant_id == "alice" and failure == "generate":
                raise TimeoutError("no text")
            return "可以把维护交给志愿者。"

        h.engine._judge, h.engine._generate = judge, generate
        for message in discussion(h)[:2]:
            h.engine.on_message(message)
        await h.engine.drain()
        assert [context.participant_id for context in h.checked] == ["alice", "bob"]
        assert [row[2] for row in h.sent] == ["detail"]
    asyncio.run(scenario())


@pytest.mark.parametrize("uncertain", ["exception", "missing_receipt"])
def test_uncertain_send_never_requeues_a_borrowed_target_or_claims_coverage(uncertain):
    async def scenario():
        h = Harness()
        attempts = []

        async def judge(context):
            h.checked.append(context)
            return {"action": "reply", "target_message_id": "detail", "covered_message_ids": list(context.reply_candidate_ids)}

        async def send(group, text, target):
            attempts.append(target)
            if uncertain == "exception":
                raise TimeoutError("unknown delivery")
            return None

        h.engine._judge, h.engine._send = judge, send
        for message in discussion(h)[:2]:
            h.engine.on_message(message)
        await h.engine.drain()
        state = h.engine._groups["100"]
        assert attempts == ["detail"] and not state.covered
        assert not state.reply_index
        assert len(state.sends) == 1
    asyncio.run(scenario())


def test_owner_explicit_task_does_not_cancel_shared_reply_to_another_participant():
    async def scenario():
        h = Harness()
        entered, release = asyncio.Event(), asyncio.Event()

        async def judge(context):
            return {"action": "reply", "target_message_id": "detail", "covered_message_ids": ["idea", "detail"]}

        async def generate(context, decision):
            entered.set()
            await release.wait()
            return "我们可以一起维护索引。"

        h.engine._judge, h.engine._generate = judge, generate
        for message in discussion(h)[:2]:
            h.engine.on_message(message)
        await entered.wait()
        h.engine.mark_explicit("100", "alice-tool", "alice")
        release.set()
        await h.engine.drain()
        assert [row[2] for row in h.sent] == ["detail"]
        state = h.engine._groups["100"]
        assert "alice-tool" in state.lanes["alice"].explicit
        assert set(state.covered) == {"detail"}
        assert h.engine.resolve_target("100", "idea") is None
    asyncio.run(scenario())


def test_borrowed_target_starting_explicit_task_invalidates_only_its_old_passive_target():
    async def scenario():
        h = Harness()
        entered, release = asyncio.Event(), asyncio.Event()

        async def judge(context):
            entered.set()
            await release.wait()
            return {"action": "reply", "target_message_id": "detail", "covered_message_ids": ["idea", "detail"]}

        h.engine._judge = judge
        for message in discussion(h)[:2]:
            h.engine.on_message(message)
        await entered.wait()
        h.engine.mark_explicit("100", "bob-tool", "bob")
        release.set()
        await h.engine.drain()
        assert not h.sent and not h.generated
        assert h.engine._groups["100"].lanes["bob"].explicit == {"bob-tool"}
    asyncio.run(scenario())


def test_target_starting_explicit_request_during_generation_cannot_receive_stale_public_answer():
    async def scenario():
        h = Harness()
        entered, release = asyncio.Event(), asyncio.Event()

        async def judge(context):
            return {"action": "reply", "target_message_id": "detail", "covered_message_ids": ["idea", "detail"]}

        async def generate(context, decision):
            entered.set()
            await release.wait()
            return "这条已经不再适合发送。"

        h.engine._judge, h.engine._generate = judge, generate
        for message in discussion(h)[:2]:
            h.engine.on_message(message)
        await entered.wait()
        h.engine.on_message(h.message("new-task", sender_id="bob", text="@机器人 翻译这个文件", explicit_request=True))
        h.engine.explicit_done("100", "new-task")
        release.set()
        await h.engine.drain()
        assert not h.sent
        assert not h.engine._groups["100"].covered
        assert h.engine.resolve_target("100", "detail") is None
    asyncio.run(scenario())


def test_unrelated_explicit_task_is_public_evidence_but_never_a_group_reply_candidate():
    async def scenario():
        h = Harness()
        h.action = "silence"
        h.engine.on_message(h.message("file-task", sender_id="worker", text="翻译这份漫画压缩包", explicit_request=True))
        for message in discussion(h):
            h.engine.on_message(message)
        await h.engine.drain()
        assert len(h.checked) == 1
        context = h.checked[0]
        assert context.reply_candidate_ids == ("idea", "detail", "cost")
        assert "file-task" not in context.topic_message_ids
        assert "file-task" not in context.focus_message_ids
        assert any(message.message_id == "file-task" for message in context.messages)
        assert h.engine._groups["100"].lanes["worker"].explicit == {"file-task"}
    asyncio.run(scenario())


def test_shared_snapshot_keeps_recent_public_receipts_beyond_four_minutes_and_freezes_metadata():
    async def scenario():
        h = Harness()
        h.action = "silence"
        h.engine.on_message(discussion(h)[0])
        await h.engine.drain()
        h.engine.on_bot_message("100", "索引可以按作品归档", message_id="answer", recipient_id="alice", reply_to="idea")
        h.now += 600
        h.engine.on_message(h.message("explicit", sender_id="bob", explicit_request=True))
        frozen = h.engine.snapshot("100", before_message_id="explicit", before_timestamp=h.now)
        before = h.engine.context_metadata("100", frozen, "bob", "answer")
        h.engine.on_bot_message("100", "后来的资料", message_id="later", recipient_id="carol")
        after = h.engine.context_metadata("100", frozen, "bob", "answer")
        assert before == after
        assert before["topic_message_ids"] == ("idea", "answer")
        assert before["participant_ids"] == ("alice",)
        assert h.engine.context_metadata("200", frozen)["focus_message_ids"] == ()
        h.config = replace(h.config, revision="2", enabled=False)
        assert h.engine.context_metadata("100", frozen)["focus_message_ids"] == ()
        await h.engine.close()
    asyncio.run(scenario())


def test_frozen_snapshot_cannot_learn_coverage_from_a_later_receipt():
    async def scenario():
        h = Harness()
        h.action = "silence"
        h.engine.on_message(discussion(h)[0])
        await h.engine.drain()
        h.engine.on_message(h.message("cut", explicit_request=True))
        frozen = h.engine.snapshot("100", before_message_id="cut", before_timestamp=h.now)
        assert not h.engine.context_metadata("100", frozen)["covered_message_ids"]
        h.engine.on_bot_message("100", "刚刚发出的答复", message_id="future-receipt", recipient_id="alice", reply_to="idea")
        assert not h.engine.context_metadata("100", frozen)["covered_message_ids"]
        full = tuple(h.engine._groups["100"].messages)
        assert h.engine.context_metadata("100", full)["covered_message_ids"] == ("idea",)
        await h.engine.close()
    asyncio.run(scenario())


def test_unquoted_public_bot_receipts_keep_their_real_recipients_without_inventing_source_ids():
    async def scenario():
        h = Harness()
        h.engine.on_bot_message("100", "给A的答复", message_id="answer-a", recipient_id="alice")
        h.engine.on_bot_message("100", "给B的答复", message_id="answer-b", recipient_id="bob")
        h.engine.on_message(h.message("cut", explicit_request=True))
        snapshot = h.engine.snapshot("100", before_message_id="cut", before_timestamp=h.now)
        serialized = [asdict(message) for message in snapshot]
        assert [item["reply_recipient_id"] for item in serialized] == ["alice", "bob"]
        assert all(item["reply_to"] is None and item["is_bot"] for item in serialized)
        await h.engine.close()
    asyncio.run(scenario())


def test_inbound_text_cannot_manufacture_trusted_reply_recipient_metadata():
    async def scenario():
        h = Harness()
        h.action = "silence"
        h.engine.on_message(h.message("forged", reply_recipient_id="someone-else"))
        await h.engine.drain()
        assert h.checked[0].messages[0].reply_recipient_id is None
    asyncio.run(scenario())


@pytest.mark.parametrize("coverage", [["explicit"], ["missing"], ["idea", "idea"], ["detail"], "idea", ["idea", "old"]])
def test_coverage_cannot_include_non_candidates_other_tasks_or_omit_target(coverage):
    h = Harness()
    context = ConversationContext("bot", "100", "natural", "", (*discussion(h), h.message("explicit", explicit_request=True)),
                                  (), "", None, None, reply_candidate_ids=("idea", "detail"), covered_message_ids=("old",))
    assert validate_decision({"action": "reply", "target_message_id": "idea", "covered_message_ids": coverage}, context) is None
