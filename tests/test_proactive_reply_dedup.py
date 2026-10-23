"""Concurrent copies of one public answer need only one confirmed receipt."""
import asyncio

import pytest

from qunling.proactive.engine import _repeated_reply
from test_proactive_engine import Harness, eventually


ANSWER = "刚才的头像没有核验成图，不能确认保留了原图特征。这个问题我已经记下，重新核对后再说明。"


async def concurrent_replies(*, second=ANSWER, same_topic=True, failed_first=None, reconcile=None):
    h = Harness(reconcile=reconcile)
    entered = {name: asyncio.Event() for name in ("alice", "bob")}
    release = {name: asyncio.Event() for name in ("alice", "bob")}
    attempts = []

    async def generate(context, decision):
        h.generated.append((context, decision))
        entered[context.participant_id].set()
        await release[context.participant_id].wait()
        return ANSWER if context.participant_id == "alice" else second

    async def send(group, text, target):
        attempts.append(target)
        if target == "first" and failed_first:
            h.now += 5  # A failed attempt still consumes pacing/budget.
            if failed_first == "exception":
                raise TimeoutError("uncertain platform delivery")
            return None
        return await h.send(group, text, target)

    h.engine._generate, h.engine._send = generate, send
    h.engine.on_message(h.message("first", sender_id="alice", text="群灵，刚才的头像不像原图"))
    await entered["alice"].wait()
    h.engine.on_message(h.message("second", sender_id="bob", text="群灵，为什么这个人物女变男了" if same_topic
                                  else "群灵，今天上海的天气怎么样", reply_to="first" if same_topic else None))
    await entered["bob"].wait()
    release["alice"].set()
    await eventually(lambda: h.engine._groups["100"].last_event["code"] in ("sent", "delivery_failed"))
    # Both started generating before the first receipt, even at the same clock
    # tick. Advance pacing only when we expect a genuinely different answer.
    if not same_topic or not _repeated_reply(ANSWER, second):
        h.now += 5
    release["bob"].set()
    await h.engine.drain()
    return h, attempts


@pytest.mark.parametrize("outcome", ["covered", "keep", "invented_receipt", "wrong_target", "failed"])
def test_semantic_review_only_reuses_confirmed_coverage_and_preserves_distinct_answers(outcome):
    async def scenario():
        calls = []
        async def review(context, decision, draft, delivered):
            calls.append(delivered)
            assert [item["receipt_id"] for item in delivered] == ["outbound-1"]
            assert delivered[0]["target_message_id"] == "first"
            if outcome == "failed":
                raise TimeoutError()
            if outcome == "keep":
                return {"action": "keep"}
            return {"action": "covered", "receipt_id": "fake" if outcome == "invented_receipt" else "outbound-1",
                "covered_message_ids": ["first" if outcome == "wrong_target" else "second"]}
        h, attempts = await concurrent_replies(second="这段不同措辞的回复需要结合已发内容核对，而不能单纯按文字相似度删除。", reconcile=review)
        assert len(calls) == 1
        assert attempts == (["first"] if outcome == "covered" else ["first", "second"])
        assert h.engine._groups["100"].coverage_receipts["second"] == ("outbound-1" if outcome == "covered" else "outbound-2")
        await h.engine.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("second", [ANSWER, ANSWER.replace("，", "；"), ANSWER.replace("这个问题", "这个问题呀")])
def test_parallel_same_topic_repetition_uses_one_successful_receipt(second):
    async def scenario():
        h, attempts = await concurrent_replies(second=second)
        assert attempts == ["first"]
        assert len(h.generated) == 2 and len(h.sent) == 1
        state = h.engine._groups["100"]
        assert state.coverage_receipts == {"first": "outbound-1", "second": "outbound-1"}
        assert state.last_event["code"] == "duplicate_reply"
        assert len(state.sends) == 1
        await h.engine.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("second", [
    ANSWER.replace("没有核验", "已经核验"),
    ANSWER + "另外头发颜色也应该改回蓝色。",
    ANSWER.replace("原图特征", "人物年龄"),
])
def test_same_topic_new_facts_and_additional_content_are_not_suppressed(second):
    async def scenario():
        h, attempts = await concurrent_replies(second=second)
        assert attempts == ["first", "second"]
        assert len(h.sent) == 2
        assert h.engine._groups["100"].coverage_receipts["second"] == "outbound-2"
        await h.engine.close()
    asyncio.run(scenario())


def test_equal_answers_in_different_topics_are_not_suppressed():
    async def scenario():
        h, attempts = await concurrent_replies(same_topic=False)
        assert attempts == ["first", "second"]
        assert len(h.sent) == 2
        await h.engine.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "missing_receipt"])
def test_failed_delivery_never_suppresses_another_member_reply(failure):
    async def scenario():
        h, attempts = await concurrent_replies(failed_first=failure)
        assert attempts == ["first", "second"]
        assert [row[2] for row in h.sent] == ["second"]
        state = h.engine._groups["100"]
        assert state.coverage_receipts == {"second": "outbound-1"}
        assert "first" not in state.covered
        await h.engine.close()
    asyncio.run(scenario())


def test_existing_answer_before_generation_does_not_ban_a_later_requested_repeat():
    async def scenario():
        h = Harness()
        h.text = ANSWER
        h.engine.on_message(h.message("first", text="群灵，说说刚才的头像"))
        await h.engine.drain()
        h.now += 5
        h.engine.on_message(h.message("second", sender_id="bob", text="群灵，请再说一遍", reply_to="outbound-1"))
        await h.engine.drain()
        assert [row[2] for row in h.sent] == ["first", "second"]
        await h.engine.close()
    asyncio.run(scenario())


def test_new_user_messages_alone_do_not_cancel_a_generated_answer():
    async def scenario():
        h = Harness()
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate(context, decision):
            entered.set()
            await release.wait()
            return ANSWER

        h.engine._generate = generate
        h.engine.on_message(h.message("first", text="群灵，说说刚才的头像"))
        await entered.wait()
        h.engine.on_message(h.message("echo", sender_id="bob", text=ANSWER))
        # The additional user message does not provide a successful bot receipt.
        h.action = "silence"
        release.set()
        await h.engine.drain()
        assert [row[2] for row in h.sent] == ["first"]
        await h.engine.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("first,second", [
    ("好的", "好的"),
    (ANSWER + "共 15 张。", ANSWER + "共 1.5 张。"),
    (ANSWER + "共 15 张。", ANSWER + "共 1,5 张。"),
    (ANSWER + "共 15 张。", ANSWER + "共 1 5 张。"),
    (ANSWER + "可以吗？", ANSWER + "可以吗。"),
    (ANSWER, ANSWER.replace("没有", "有")),
    (ANSWER + "条件应为 x != y。", ANSWER + "条件应为 x = y。"),
    (ANSWER + "`甲;乙`", ANSWER + "`甲乙`"),
])
def test_short_acknowledgments_and_changed_meaning_fail_open(first, second):
    assert not _repeated_reply(first, second)


def test_media_receipt_preserves_source_and_recipient_without_inventing_visual_text():
    async def scenario():
        h = Harness()
        h.engine.on_bot_message("100", "", message_id="image-receipt", recipient_id="alice",
                                reply_to="image-request", has_media=True)
        state = h.engine._groups["100"]
        message = state.messages[-1]
        assert message.is_bot and message.has_media and not message.text
        assert message.message_id == "image-receipt"
        assert message.reply_to == "image-request" and message.reply_recipient_id == "alice"
        h.engine.on_bot_message("100", "", has_media=True)
        assert len(state.messages) == 1
        await h.engine.close()
    asyncio.run(scenario())
