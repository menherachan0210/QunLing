"""Frozen participation batches survive chat traffic without duplicate replies."""
import asyncio
from dataclasses import replace

import pytest

from qunling.proactive.engine import GroupPolicy, validate_decision
from test_proactive_engine import Harness, eventually


async def wait_for_status(h, code):
    async def ready():
        while h.engine._groups["100"].last_event["code"] != code:
            await asyncio.sleep(0.001)
    await asyncio.wait_for(ready(), 1)


def test_debounce_batch_can_answer_earlier_name_call_after_text_and_media():
    async def scenario():
        h = Harness()
        h.config = replace(h.config, groups={"100": GroupPolicy(mode="quiet")})

        async def select_call(context):
            h.checked.append(context)
            return {"action": "reply", "target_message_id": "called"}

        h.engine._judge = select_call
        h.engine.on_message(h.message("called", text="群灵出来说话"))
        h.engine.on_message(h.message("other", sender_id="someone-else", text="刚才那件事我也看到了"))
        h.engine.on_message(h.message("sticker", text="", has_media=True))
        await h.engine.drain()
        assert len(h.checked) == len(h.sent) == 1
        assert h.checked[0].reply_candidate_ids == ("called",)
        assert any(message.message_id == "other" for message in h.checked[0].messages)
        assert h.checked[0].messages[-1].has_media
        assert h.sent[0][2] == "called"
        assert validate_decision({"action": "reply", "target_message_id": "sticker"}, h.checked[0]) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["judging", "generating", "sending"])
@pytest.mark.parametrize("sender", ["user", "another-person"])
def test_inflight_batch_survives_new_text_and_media_then_processes_only_successor(stage, sender):
    async def scenario():
        h = Harness()
        entered, release = asyncio.Event(), asyncio.Event()
        cancelled = []

        async def block():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(stage)
                raise

        async def judge(context):
            h.checked.append(context)
            if stage == "judging" and context.reply_candidate_ids == ("first",):
                await block()
            return {"action": "reply", "target_message_id": context.reply_candidate_ids[-1]}

        async def generate(context, decision):
            h.generated.append((context, decision))
            if stage == "generating" and decision.target_message_id == "first":
                await block()
            return "这一句已经想好，正常接着说。"

        async def send(group, text, target):
            if stage == "sending" and target == "first":
                await block()
            receipt = await h.send(group, text, target)
            # The actual delivery completes before the next queued call becomes eligible.
            h.now += 5
            return receipt

        h.engine._judge, h.engine._generate, h.engine._send = judge, generate, send
        h.engine.on_message(h.message("first", text="群灵，你怎么看？"))
        await entered.wait()
        if sender != "user" and stage == "sending":
            h.engine._debounce_scale = 0.001
        h.engine.on_message(h.message("second", sender_id=sender, text="群灵，还有这个问题？"))
        h.engine.on_message(h.message("picture", sender_id=sender, text="", has_media=True))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not cancelled
        if sender == "user":
            assert len(h.engine._tasks) == 1 and len(h.engine._pending) == 1
        else:
            assert len(h.engine._tasks) <= 2
            assert ("100", sender) not in h.engine._pending
        release.set()
        await h.engine.drain()
        assert not cancelled
        assert sorted(entry[2] for entry in h.sent) == ["first", "second"]
        assert [context.reply_candidate_ids for context in h.checked] == [("first",), ("second",)]
        if sender == "user":
            assert any(message.is_bot for message in h.checked[1].messages)
        assert not h.engine._tasks and not h.engine._pending

    asyncio.run(scenario())


def test_own_send_observation_does_not_cancel_send_erase_queue_or_create_reply_loop():
    async def scenario():
        h = Harness()
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(group, text, target):
            receipt = await h.send(group, text, target)
            h.engine.on_bot_message(group, text, message_id=receipt, recipient_id="user")
            if target == "first":
                entered.set()
                await release.wait()
            h.now += 5
            return receipt

        h.engine._send = send
        h.engine.on_message(h.message("first", text="群灵，请说一下"))
        await entered.wait()
        h.engine.on_message(h.message("next", text="群灵，也说一下这个"))
        h.engine.on_message(h.message("own-echo", sender_id="bot", text="我自己的平台回声"))
        release.set()
        await h.engine.drain()
        assert [entry[2] for entry in h.sent] == ["first", "next"]
        assert [context.reply_candidate_ids for context in h.checked] == [("first",), ("next",)]
        assert len([message for message in h.engine._groups["100"].messages if message.is_bot]) == 2
        h.engine.on_bot_message("100", "另外一次发送回调", message_id="callback-only")
        await h.engine.drain()
        assert len(h.sent) == len(h.checked) == 2

    asyncio.run(scenario())


def test_followup_received_after_send_callback_survives_final_receipt_at_same_timestamp():
    async def scenario():
        h = Harness()

        async def judge(context):
            h.checked.append(context)
            target = context.reply_candidate_ids[-1]
            return {"action": "followup" if target == "follow" else "reply", "target_message_id": target}

        async def send(group, text, target):
            receipt = await h.send(group, text, target)
            h.engine.on_bot_message(group, text, message_id=receipt, recipient_id="user")
            if target == "first":
                # OneBot gives second-resolution times, so ordering must not rely on > timestamp.
                h.engine.on_message(h.message("follow", text="那刚才那个细节再说说？"))
            h.now += 10
            return receipt

        h.engine._judge, h.engine._send = judge, send
        h.engine.on_message(h.message("first", text="群灵，出来聊聊"))
        await h.engine.drain()
        assert [entry[2] for entry in h.sent] == ["first", "follow"]
        assert h.checked[1].reply_candidate_ids == ("follow",)
        assert h.checked[1].followup_candidate_ids == ("follow",)

    asyncio.run(scenario())


def test_pending_plain_batch_waits_for_cooldown_then_completes_without_new_message():
    async def scenario():
        h = Harness()
        h.engine.on_message(h.message("first"))
        await h.engine.drain()
        h.now += 1
        h.engine._debounce_scale = 0.001
        h.engine.on_message(h.message("waiting", sender_id="other"))
        await wait_for_status(h, "cooldown")
        state = h.engine._groups["100"]
        assert state.last_event["code"] == "cooldown"
        assert state.lanes["other"].candidates == {"waiting"} and len(h.checked) == 1
        h.now += 89
        state.lanes["other"].wakeup.set()
        await asyncio.wait_for(h.engine.drain(), 1)
        assert [entry[2] for entry in h.sent] == ["first", "waiting"]

    asyncio.run(scenario())


def test_name_call_wakes_longer_pending_cooldown_without_losing_batch():
    async def scenario():
        h = Harness()
        h.engine.on_message(h.message("first"))
        await h.engine.drain()
        h.now += 1
        h.engine._debounce_scale = 0.001
        h.engine.on_message(h.message("waiting", sender_id="other"))
        await wait_for_status(h, "cooldown")
        assert h.engine._groups["100"].last_event["code"] == "cooldown"
        h.now += 4
        h.engine.on_message(h.message("called", sender_id="other", text="群灵，叫你呢"))
        await asyncio.wait_for(h.engine.drain(), 1)
        assert [entry[2] for entry in h.sent] == ["first", "called"]
        assert h.checked[1].reply_candidate_ids == ("waiting", "called")
        assert not h.engine._pending

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["silence", "wait", "error"])
def test_consumed_batch_is_not_reselected_after_silence_wait_or_failure(outcome):
    async def scenario():
        h = Harness()
        entered, release = asyncio.Event(), asyncio.Event()

        async def judge(context):
            h.checked.append(context)
            if context.reply_candidate_ids == ("first",):
                entered.set()
                await release.wait()
                if outcome == "error":
                    raise RuntimeError("simulated model failure")
                return {"action": outcome, "target_message_id": None}
            assert validate_decision({"action": "reply", "target_message_id": "first"}, context) is None
            return {"action": "reply", "target_message_id": "next"}

        h.engine._judge = judge
        h.engine.on_message(h.message("first"))
        await entered.wait()
        h.engine.on_message(h.message("next"))
        release.set()
        await h.engine.drain()
        assert [context.reply_candidate_ids for context in h.checked] == [("first",), ("next",)]
        assert [entry[2] for entry in h.sent] == ["next"]
        h.engine.on_message(h.message("media-only", text=""))
        await h.engine.drain()
        assert len(h.checked) == 2

    asyncio.run(scenario())


def test_running_group_keeps_one_merged_successor_and_does_not_block_another_group():
    async def scenario():
        h = Harness()
        h.config = replace(h.config, groups={"100": GroupPolicy(), "200": GroupPolicy()})
        entered, release = asyncio.Event(), asyncio.Event()
        active = {}
        peaks = {}

        async def judge(context):
            group = context.group_id
            active[group] = active.get(group, 0) + 1
            peaks[group] = max(peaks.get(group, 0), active[group])
            h.checked.append(context)
            try:
                if context.reply_candidate_ids == ("first",):
                    entered.set()
                    await release.wait()
                return {"action": "silence", "target_message_id": None}
            finally:
                active[group] -= 1

        h.engine._judge = judge
        h.engine.on_message(h.message("first"))
        await entered.wait()
        for index in range(1000):
            h.engine.on_message(h.message(f"burst-{index}"))
        await asyncio.sleep(0)
        assert len(h.engine._tasks) == 1
        assert tuple(h.engine._pending) == (("100", "user"),)
        h.engine.on_message(h.message("other-group", group="200"))
        await eventually(lambda: any(context.group_id == "200" for context in h.checked))
        release.set()
        await h.engine.drain()
        batches = [context for context in h.checked if context.group_id == "100"]
        assert len(batches) == 2 and batches[1].reply_candidate_ids[-1] == "burst-999"
        assert "first" not in batches[1].reply_candidate_ids
        assert peaks == {"100": 1, "200": 1}
        assert not h.engine._pending and not h.engine._tasks

    asyncio.run(scenario())


@pytest.mark.parametrize("mode, interval, limit", [("quiet", 15, 4), ("natural", 10, 6), ("active", 5, 8)])
def test_each_mode_allows_longer_followup_exchange_without_unbounded_reply_chain(mode, interval, limit):
    async def scenario():
        h = Harness()
        h.config = replace(h.config, groups={"100": GroupPolicy(mode=mode)})
        h.action = "followup"
        h.engine.on_bot_message("100", "我们继续聊", message_id="initial", recipient_id="user")
        for index in range(limit + 1):
            h.now += interval
            h.engine.on_message(h.message(str(index), text="那你再说说这一点？"))
            await h.engine.drain()
        assert len(h.sent) == len(h.checked) == limit

    asyncio.run(scenario())


@pytest.mark.parametrize("preempt", ["explicit", "disable"])
def test_ambiguous_send_preemption_does_not_resend_reserved_batch_or_successor(preempt, tmp_path):
    async def scenario():
        h = Harness(state_dir=tmp_path, send_timeout=0.01)
        entered = asyncio.Event()
        attempted = []

        async def uncertain_delivery(group, text, target):
            attempted.append(target)
            entered.set()
            await asyncio.Event().wait()

        h.engine._send = uncertain_delivery
        h.engine.on_message(h.message("first"))
        await entered.wait()
        h.engine.on_message(h.message("next"))
        if preempt == "explicit":
            h.engine.mark_explicit("100", "request", "user")
        else:
            h.config = replace(h.config, revision="2", enabled=False)
            h.engine.refresh_configuration()
        await h.engine.drain()
        if preempt == "explicit":
            h.engine.explicit_done("100", "request")
        await h.engine.drain()
        assert attempted == ["first"]
        assert not h.engine._pending
        assert h.engine.status()["groups"]["100"]["replies_day"] == 1

    asyncio.run(scenario())
