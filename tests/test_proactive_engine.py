"""Opt-in, cancellation and cost boundaries; no network, real AI or QQ sends."""
import asyncio
import contextvars
from dataclasses import replace
import json

import pytest

from qunling.proactive.engine import (
    ConversationContext, EngineConfig, GroupPolicy, Message, ProactiveEngine,
    reply_character_limit, validate_decision,
)


class Harness:
    def __init__(self, **kwargs):
        self.now = 1_800_000_000.0
        self.config = EngineConfig("1", True, {"100": GroupPolicy()})
        self.checked = []
        self.generated = []
        self.sent = []
        self.action = "reply"
        self.text = "有道理，不过这个细节还得再看看。"
        self.engine = ProactiveEngine(bot_id="bot", config=lambda: self.config,
            judge=self.judge, generate=self.generate, send=self.send,
            clock=lambda: self.now, debounce_scale=0, **kwargs)

    def message(self, message_id="m1", group="100", **kwargs):
        values = dict(bot_id="bot", group_id=group, message_id=message_id,
                      sender_id="user", text="大家怎么看这件事？", timestamp=self.now)
        values.update(kwargs)
        return Message(**values)

    async def judge(self, context):
        self.checked.append(context)
        return {"action": self.action, "target_message_id":
                context.reply_candidate_ids[-1] if self.action in ("reply", "followup") else None}

    async def generate(self, context, decision):
        self.generated.append((context, decision))
        return self.text

    async def send(self, group_id, text, target):
        self.sent.append((group_id, text, target))
        return "outbound-" + str(len(self.sent))


async def eventually(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    assert predicate()


def test_saved_default_style_is_frozen_into_free_reply_context():
    async def scenario():
        h = Harness()
        h.config = replace(h.config, default_style="正式克制，不讲笑话")
        assert h.engine.on_message(h.message())
        await h.engine.drain()
        assert h.checked[0].default_style == "正式克制，不讲笑话"
        assert h.generated[0][0].default_style == "正式克制，不讲笑话"
        await h.engine.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("snapshot", [
    EngineConfig("1", False, {"100": GroupPolicy()}),
    EngineConfig("1", True, {}),
    EngineConfig("1", True, {"100": GroupPolicy(mode="off")}),
])
def test_unapproved_groups_have_no_buffer_or_calls(snapshot):
    async def scenario():
        h = Harness()
        h.config = snapshot
        assert not h.engine.on_message(h.message())
        await h.engine.drain()
        assert not h.engine._groups
        assert not h.checked and not h.sent
        await h.engine.close()
    asyncio.run(scenario())


def test_new_groups_do_not_inherit_permission():
    async def scenario():
        h = Harness()
        assert not h.engine.on_message(h.message(group="new-group"))
        assert h.engine.on_message(h.message())
        await h.engine.drain()
        assert [row[0] for row in h.sent] == ["100"]
        assert "new-group" not in h.engine._groups
    asyncio.run(scenario())


@pytest.mark.parametrize("overrides", [
    {"sender_id": "bot"}, {"bot_id": "other-bot"}, {"is_bot": True},
    {"timestamp": 0}, {"timestamp": float("nan")}, {"timestamp": 1_900_000_000},
    {"message_id": ""}, {"message_id": "x" * 257},
])
def test_replay_echo_and_invalid_events_are_ignored(overrides):
    async def scenario():
        h = Harness()
        assert not h.engine.on_message(h.message(**overrides))
        await h.engine.drain()
        assert not h.checked and not h.engine._groups
    asyncio.run(scenario())


def test_duplicates_and_bursts_coalesce_into_one_latest_input():
    async def scenario():
        h = Harness()
        h.engine.on_message(h.message("first"))
        assert not h.engine.on_message(h.message("first"))
        h.engine.on_message(h.message("second", text="最后这一条才是当前问题"))
        await h.engine.drain()
        assert len(h.checked) == 1
        assert [m.message_id for m in h.checked[0].messages] == ["first", "second"]
        assert h.sent[0][2] == "second"
    asyncio.run(scenario())


def test_text_window_is_bounded_and_old_text_expires():
    async def scenario():
        h = Harness()
        h.action = "silence"
        for index in range(100):
            h.engine.on_message(h.message(str(index), text="字" * 3000))
        await h.engine.drain()
        context = h.checked[0]
        assert len(context.messages) <= 120
        assert sum(len(item.text) for item in context.messages) <= 12000
        assert all(len(item.text) <= 2000 for item in context.messages)
        h.now += 1201
        h.engine.on_message(h.message("fresh"))
        await h.engine.drain()
        assert [m.message_id for m in h.checked[-1].messages] == ["fresh"]
    asyncio.run(scenario())


def test_explicit_request_replaces_own_passive_and_resumes_new_followup_when_done():
    async def scenario():
        h = Harness()
        entered = asyncio.Event()
        async def slow_judge(context):
            entered.set()
            await asyncio.Event().wait()
        h.engine._judge = slow_judge
        h.engine.on_message(h.message("ordinary"))
        await entered.wait()
        h.engine.mark_explicit("100", "request")
        h.engine.on_message(h.message("request", explicit_request=True))
        h.engine.on_message(h.message("while-busy"))
        await h.engine.drain()
        assert not h.sent
        h.engine.explicit_done("100", "request")
        h.engine._judge = h.judge
        await h.engine.drain()
        assert [context.reply_candidate_ids for context in h.checked] == [("while-busy",)]
        assert [item[2] for item in h.sent] == ["while-busy"]
        h.now += 100
        h.engine.on_message(h.message("after-done"))
        await h.engine.drain()
        assert h.sent[-1][2] == "after-done"
    asyncio.run(scenario())


def test_turning_off_removes_text_and_invalidates_inflight_generation():
    async def scenario():
        h = Harness()
        entered = asyncio.Event()
        async def slow_generate(context, decision):
            entered.set()
            await asyncio.Event().wait()
        h.engine._generate = slow_generate
        h.engine.on_message(h.message())
        await entered.wait()
        h.config = replace(h.config, revision="2", enabled=False)
        h.engine.refresh_configuration()
        await h.engine.drain()
        assert not h.sent and not h.engine._groups
    asyncio.run(scenario())


def test_new_topic_during_generation_does_not_cancel_current_answer():
    async def scenario():
        h = Harness()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_first(context, decision):
            if decision.target_message_id == "old":
                entered.set()
                await release.wait()
            return "完成已经开始的回答"
        h.engine._generate = slow_first
        h.engine.on_message(h.message("old"))
        await entered.wait()
        h.engine.on_message(h.message("new", text="换个话题"))
        release.set()
        await h.engine.drain()
        assert h.sent == [("100", "完成已经开始的回答", "old")]
        assert not h.engine._pending
    asyncio.run(scenario())


def test_config_is_rechecked_after_model_even_without_reload_notification():
    async def scenario():
        h = Harness()
        async def generating(context, decision):
            h.config = replace(h.config, revision="2", groups={})
            return "这条不能发"
        h.engine._generate = generating
        h.engine.on_message(h.message())
        await h.engine.drain()
        assert not h.sent and not h.engine._groups
    asyncio.run(scenario())


def test_shared_semaphore_limits_all_groups_and_bots_to_two():
    async def scenario():
        semaphore = asyncio.Semaphore(2)
        left, right = Harness(semaphore=semaphore), Harness(semaphore=semaphore)
        left.config = replace(left.config, groups={str(i): GroupPolicy() for i in (100, 101)})
        active = 0
        peak = 0
        calls = 0
        release = asyncio.Event()
        async def waiting(context):
            nonlocal active, peak, calls
            active += 1
            calls += 1
            peak = max(peak, active)
            try:
                await release.wait()
                return {"action": "silence", "target_message_id": None}
            finally:
                active -= 1
        left.engine._judge = right.engine._judge = waiting
        left.engine.on_message(left.message("a", group="100"))
        left.engine.on_message(left.message("b", group="101"))
        right.engine.on_message(right.message("c"))
        await eventually(lambda: calls == 2)
        assert active == 2
        release.set()
        await asyncio.gather(left.engine.drain(), right.engine.drain())
        assert calls == 3 and peak == 2
    asyncio.run(scenario())


def test_event_transaction_context_is_not_inherited():
    async def scenario():
        transaction = contextvars.ContextVar("transaction", default=None)
        transaction.set("incoming-event-db-session")
        h = Harness()
        observed = []
        async def judging(context):
            observed.append(transaction.get())
            return {"action": "silence", "target_message_id": None}
        h.engine._judge = judging
        h.engine.on_message(h.message())
        await h.engine.drain()
        assert observed == [None]
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["silence", "wait"])
def test_silence_and_wait_do_not_retry_without_new_human_message(action):
    async def scenario():
        h = Harness()
        h.action = action
        h.engine.on_message(h.message())
        await h.engine.drain()
        h.now += 600
        await asyncio.sleep(0)
        assert len(h.checked) == 1 and not h.generated and not h.sent
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [
    {"action": "reply", "target_message_id": "invented"},
    {"action": "reply", "target_message_id": "m", "reason": "extra"},
    {"action": "tool", "target_message_id": "m"},
    {"action": "silence", "target_message_id": "m"},
    {"action": "followup", "target_message_id": "m"},
    {"action": ["reply"], "target_message_id": "m"},
    "reply", None,
])
def test_decision_schema_requires_known_action_and_grounded_target(value):
    context = ConversationContext("bot", "100", "natural", "",
        (Message("bot", "100", "m", "user", "hi", 1),), (), "", None, None)
    assert validate_decision(value, context) is None


def test_decision_cannot_reply_to_an_already_consumed_message_even_if_visible():
    context = ConversationContext("bot", "100", "natural", "", (
        Message("bot", "100", "old", "user", "旧话题", 1),
        Message("bot", "100", "new", "user", "新话题", 2),
    ), ("old", "new"), "", None, None, reply_candidate_ids=("new",))
    for action in ("reply", "followup"):
        assert validate_decision({"action": action, "target_message_id": "old"}, context) is None
        assert validate_decision({"action": action, "target_message_id": "new"}, context) is not None


def test_mentioning_someone_else_does_not_interrupt_their_conversation():
    async def scenario():
        h = Harness()
        h.engine.on_message(h.message(mentions=("other-person",)))
        await h.engine.drain()
        assert not h.checked and not h.sent
    asyncio.run(scenario())


def test_quiet_mode_allows_only_grounded_natural_followups():
    async def scenario():
        h = Harness()
        h.config = replace(h.config, groups={"100": GroupPolicy(mode="quiet")})
        h.engine.on_message(h.message("chatter"))
        await h.engine.drain()
        assert not h.checked
        h.engine.on_bot_message("100", "第一款适合你", message_id="bot-answer", recipient_id="user")
        h.action = "followup"
        h.now += 1
        h.engine.on_message(h.message("follow", text="那另一款呢？"))
        await h.engine.drain()
        assert h.sent[0][2] == "follow"
        assert h.checked[0].followup_candidate_ids == ("follow",)
    asyncio.run(scenario())


def test_quote_can_continue_for_different_user_but_stale_quote_cannot():
    async def scenario():
        h = Harness()
        h.action = "followup"
        h.engine.on_bot_message("100", "刚才的回答", message_id="answer", recipient_id="first-user")
        h.now += 1
        h.engine.on_message(h.message("follow", sender_id="second-user", reply_to="answer"))
        await h.engine.drain()
        assert len(h.sent) == 1
        h.now += 601
        h.engine.on_message(h.message("stale", sender_id="second-user", reply_to="answer"))
        await h.engine.drain()
        assert len(h.sent) == 1
    asyncio.run(scenario())


def test_ignored_interjection_does_not_become_repeated_unsolicited_speech():
    async def scenario():
        h = Harness()
        h.engine.on_message(h.message())
        await h.engine.drain()
        h.now += 89
        h.engine.on_message(h.message("unrelated", sender_id="user", text="我们聊别的吧"))
        await h.engine.drain()
        assert len(h.sent) == 1
        h.now += 2
        h.engine.on_message(h.message("new-topic", sender_id="user"))
        await h.engine.drain()
        assert len(h.sent) == 2
    asyncio.run(scenario())


def test_quiet_hours_include_midnight_and_invalid_configuration_fails_closed():
    async def scenario():
        h = Harness()
        from datetime import datetime, timezone
        h.now = datetime(2026, 10, 21, 16, 30, tzinfo=timezone.utc).timestamp()
        h.config = replace(h.config, groups={"100": GroupPolicy(quiet_start="23:00", quiet_end="08:00")})
        h.engine.on_message(h.message())
        await h.engine.drain()
        assert not h.checked
        h.config = replace(h.config, revision="2", groups={"100": GroupPolicy(quiet_start="invalid", quiet_end="08:00")})
        h.engine.on_message(h.message("invalid-settings"))
        await h.engine.drain()
        assert not h.checked
    asyncio.run(scenario())


def test_budget_survives_restart_without_persisting_chat_content(tmp_path):
    async def scenario():
        first = Harness(state_dir=tmp_path)
        first.action = "silence"
        first.config = replace(first.config, groups={"100": GroupPolicy(judgments_per_hour=1)})
        first.engine.on_message(first.message(text="绝不可写入预算文件的聊天原文", sender_name="隐私昵称"))
        await first.engine.drain()
        await first.engine.close()
        serialized = (tmp_path / "participation-state.json").read_text(encoding="utf-8")
        assert "聊天原文" not in serialized and "隐私昵称" not in serialized
        assert '"m1"' not in serialized
        second = Harness(state_dir=tmp_path)
        second.config = first.config
        assert not second.engine.on_message(second.message())  # Persisted ID hash rejects replay.
        second.engine.on_message(second.message("new"))
        await second.engine.drain()
        assert not second.checked
        second.now += 3601
        second.engine.on_message(second.message("later"))
        await second.engine.drain()
        assert len(second.checked) == 1
    asyncio.run(scenario())


def test_all_groups_share_daily_budget_and_corrupt_state_is_not_overwritten(tmp_path):
    async def scenario():
        h = Harness(state_dir=tmp_path)
        h.engine._global_judgments.extend([h.now] * 4800)
        h.engine.on_message(h.message())
        await h.engine.drain()
        assert not h.checked
        path = tmp_path / "participation-state.json"
        path.write_text("corrupt state", encoding="utf-8")
        broken = Harness(state_dir=tmp_path)
        broken.engine.on_message(broken.message())
        await broken.engine.drain()
        assert not broken.checked and broken.engine.status()["storage_healthy"] is False
        assert path.read_text(encoding="utf-8") == "corrupt state"
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["", " ", "字" * 401, "包含\x00控制字符", {"text": "not plain text"}])
def test_generation_is_plain_bounded_text(text):
    async def scenario():
        h = Harness()
        h.text = text
        h.engine.on_message(h.message())
        await h.engine.drain()
        assert not h.sent
    asyncio.run(scenario())


def test_delivery_timeout_is_reserved_and_never_automatically_retried(tmp_path):
    async def scenario():
        h = Harness(state_dir=tmp_path, send_timeout=0.001)
        calls = []
        async def uncertain_delivery(group, text, target):
            calls.append(target)
            await asyncio.Event().wait()
        h.engine._send = uncertain_delivery
        h.engine.on_message(h.message())
        await h.engine.drain()
        h.engine.on_message(h.message("different", sender_id="other"))
        await h.engine.drain()
        assert calls == ["m1"]
        ledger = json.loads((tmp_path / "participation-state.json").read_text())
        assert ledger["groups"]["100"]["sends"] == [h.now]
        assert h.engine.status()["groups"]["100"]["replies_day"] == 1
    asyncio.run(scenario())


def test_model_timeout_is_silent_and_consumes_budget():
    async def scenario():
        h = Harness(model_timeout=0.001)
        calls = []
        async def timeout(context):
            calls.append(context.group_id)
            await asyncio.Event().wait()
        h.engine._judge = timeout
        h.engine.on_message(h.message())
        await h.engine.drain()
        assert calls == ["100"] and not h.sent
        assert h.engine.status()["judgments_day"] == 1
    asyncio.run(scenario())


def test_queue_and_bursts_are_bounded_without_one_task_per_incoming_message():
    async def scenario():
        h = Harness()
        h.config = replace(h.config, groups={str(i): GroupPolicy() for i in range(100, 200)})
        async def blocked(context):
            await asyncio.Event().wait()
        h.engine._judge = blocked
        for index in range(1000):
            h.engine.on_message(h.message(str(index), group=str(100 + index % 100)))
            assert len(h.engine._pending) <= 32
            assert len(h.engine._tasks) <= 32
        assert not h.engine._tasks  # Synchronous burst is coalesced before task allocation.
        await asyncio.sleep(0)
        assert len(h.engine._tasks) <= 32
        for index in range(1000, 2000):
            h.engine.on_message(h.message(str(index), group=str(100 + index % 100)))
            assert len(h.engine._pending) <= 32 and len(h.engine._tasks) <= 32
        await h.engine.close()
        assert not h.engine._pending and not h.engine._tasks
    asyncio.run(scenario())


def test_same_group_never_runs_two_models_even_if_cancellation_is_slow():
    async def scenario():
        h = Harness()
        entered, release = asyncio.Event(), asyncio.Event()
        active = 0
        peak = 0
        calls = []
        async def slow_cancel(context):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            calls.append(context.messages[-1].message_id)
            try:
                if len(calls) == 1:
                    entered.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        await release.wait()
                return {"action": "silence", "target_message_id": None}
            finally:
                active -= 1
        h.engine._judge = slow_cancel
        h.engine.on_message(h.message("first"))
        await entered.wait()
        h.engine.on_message(h.message("second"))
        for _ in range(5):
            await asyncio.sleep(0)
        assert calls == ["first"]
        release.set()
        await h.engine.drain()
        assert calls == ["first", "second"] and peak == 1
    asyncio.run(scenario())


def test_long_generation_expires_even_when_no_new_event_arrives():
    async def scenario():
        h = Harness()
        async def delayed(context, decision):
            h.now += 91
            return "时机已经过去的回复"
        h.engine._generate = delayed
        h.engine.on_message(h.message())
        await h.engine.drain()
        assert not h.sent
    asyncio.run(scenario())


def test_natural_followups_stop_after_six_and_resume_after_explicit_exchange():
    async def scenario():
        h = Harness()
        h.action = "followup"
        h.engine.on_bot_message("100", "开始的话题", message_id="explicit", recipient_id="user")
        for index in range(7):
            h.now += 11
            h.engine.on_message(h.message(str(index), text="那这个呢？"))
            await h.engine.drain()
        assert len(h.sent) == 6 and len(h.checked) == 6
        h.engine.on_bot_message("100", "新的明确回答", message_id="explicit-2", recipient_id="user")
        h.now += 11
        h.engine.on_message(h.message("resumed"))
        await h.engine.drain()
        assert len(h.sent) == 7
    asyncio.run(scenario())


def test_direct_quotes_can_continue_after_six_inferred_followups_within_budgets():
    async def scenario():
        h = Harness()
        h.action = "followup"
        h.engine.on_bot_message("100", "开始的话题", message_id="explicit", recipient_id="user")
        for index in range(6):
            h.now += 11
            h.engine.on_message(h.message(str(index), text="那这个呢？"))
            await h.engine.drain()
        assert len(h.sent) == 6
        h.now += 5
        h.engine.on_message(h.message("quoted", sender_id="other", reply_to="outbound-6"))
        await h.engine.drain()
        assert len(h.sent) == 7
        h.engine.on_message(h.message("too-fast", reply_to="outbound-7"))
        await h.engine.drain()
        assert len(h.sent) == 7  # Direct quotes retain the five-second send spacing.
    asyncio.run(scenario())


def test_prepared_revision_property_does_not_refresh_to_new_settings():
    h = Harness()
    h.engine.refresh_configuration()
    assert h.engine.configuration_revision == "1"
    h.config = replace(h.config, revision="2")
    assert h.engine.configuration_revision == "1"


def test_switching_permission_off_and_on_does_not_reset_cost_limits():
    async def scenario():
        h = Harness()
        policy = GroupPolicy(judgments_per_hour=1)
        h.config = replace(h.config, groups={"100": policy})
        h.action = "silence"
        h.engine.on_message(h.message())
        await h.engine.drain()
        h.config = replace(h.config, revision="2", enabled=False)
        h.engine.refresh_configuration()
        assert not h.engine._groups
        h.config = replace(h.config, revision="3", enabled=True)
        h.engine.on_message(h.message("new"))
        await h.engine.drain()
        assert len(h.checked) == 1
    asyncio.run(scenario())


def test_unwritable_budget_storage_fails_closed(tmp_path):
    async def scenario():
        not_directory = tmp_path / "file"
        not_directory.write_text("occupied", encoding="utf-8")
        h = Harness(state_dir=not_directory)
        h.engine.on_message(h.message())
        await h.engine.drain()
        assert not h.checked and not h.sent
        assert h.engine.status()["storage_healthy"] is False
    asyncio.run(scenario())


def test_image_only_event_preserves_current_text_reply_without_invoking_vision():
    async def scenario():
        h = Harness()
        entered, release = asyncio.Event(), asyncio.Event()
        async def waiting(context, decision):
            entered.set()
            await release.wait()
            return "继续回答原先的问题"
        h.engine._generate = waiting
        h.engine.on_message(h.message())
        await entered.wait()
        assert h.engine.on_message(h.message("picture", text=""))
        release.set()
        await h.engine.drain()
        assert len(h.checked) == len(h.sent) == 1
        assert h.sent[0][2] == "m1"
        assert any(item.message_id == "picture" and item.has_media
                   for item in h.engine._groups["100"].messages)
        assert h.engine.on_message(h.message("image-request", text="", explicit_request=True))
        assert h.engine.status()["groups"]["100"]["explicit_busy"] is True
    asyncio.run(scenario())


def test_bot_reply_expires_with_short_context_without_resetting_budgets():
    async def scenario():
        h = Harness()
        h.engine.on_message(h.message("original"))
        await h.engine.drain()
        state = h.engine._groups["100"]
        lane = state.lanes["user"]
        sent_at = h.now
        lane.after_bot_ids.add("original")
        lane.followup_streak = 2
        h.now += 240
        h.engine.status()
        assert lane.last_bot_text == h.text
        h.now += 1
        h.engine.status()
        assert lane.last_bot_text == ""
        assert lane.last_bot_id is None and lane.last_bot_at is None
        assert lane.last_recipient is None
        assert not lane.after_bot_ids and lane.followup_streak == 0
        assert state.last_proactive_at == sent_at
        assert list(state.sends) == [sent_at]
        assert list(state.judgments) == [sent_at]
        # Followup timing expires before the bounded shared evidence window.
        h.now += 960
        h.action = "silence"
        h.engine.on_message(h.message("new-topic"))
        await h.engine.drain()
        context = h.checked[-1]
        assert context.last_bot_text == ""
        assert context.last_bot_message_id is None and context.last_bot_at is None
        assert context.followup_candidate_ids == ()
        assert [item.message_id for item in context.messages] == ["new-topic"]
        assert list(state.sends) == [sent_at]
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["quiet", "natural", "active"])
def test_configured_name_or_alias_can_be_considered_without_recent_bot_reply(mode):
    async def scenario():
        h = Harness()
        h.config = replace(h.config, groups={"100": GroupPolicy(mode=mode, display_name="小群", aliases=("阿群",))})
        h.engine.on_message(h.message(text="阿群你觉得这个方案如何？"))
        await h.engine.drain()
        assert h.checked[0].addressed_candidate_ids == ("m1",)
        assert h.checked[0].followup_candidate_ids == ()
        assert len(h.sent) == 1
    asyncio.run(scenario())


def test_name_mention_is_only_a_candidate_and_still_allows_semantic_silence():
    async def scenario():
        h = Harness()
        h.action = "silence"
        h.engine.on_message(h.message(text="我和小李刚才聊过群灵的设计了"))
        await h.engine.drain()
        assert h.checked[0].addressed_candidate_ids == ("m1",)
        assert not h.generated and not h.sent
        assert h.engine.status()["groups"]["100"]["last_event"]["code"] == "silence"
    asyncio.run(scenario())


@pytest.mark.parametrize("text, called", [("灵，你怎么看？", True), ("灵", True), ("灵宝，你怎么看？", True), ("灵敏度应该降低", False), ("这个挺灵的", False)])
def test_single_character_name_requires_clear_calling_boundary(text, called):
    async def scenario():
        h = Harness()
        h.config = replace(h.config, groups={"100": GroupPolicy(mode="quiet", display_name="灵", aliases=("灵宝",))})
        h.engine.on_message(h.message(text=text))
        await h.engine.drain()
        assert bool(h.checked) is called
        assert bool(h.sent) is called
    asyncio.run(scenario())


def test_addressed_reply_bypasses_ignored_interjection_backoff_but_not_send_spacing():
    async def scenario():
        h = Harness()
        h.config = replace(h.config, groups={"100": GroupPolicy(mode="active")})
        h.engine.on_message(h.message())
        await h.engine.drain()
        h.now += 1
        h.engine.on_message(h.message("too-early", sender_id="other", text="群灵，你怎么看？"))
        await h.engine.drain()
        assert len(h.sent) == 1
        h.now += 4
        h.engine.on_message(h.message("called", sender_id="other", text="群灵，你怎么看？"))
        await h.engine.drain()
        assert [item[2] for item in h.sent] == ["m1", "called"]
        assert h.engine._groups["100"].last_interjection_at is None
        # A genuine engaged exchange is not treated as ten minutes of being ignored.
        h.now += 45
        h.engine.on_message(h.message("later-topic", sender_id="third", text="这个细节也值得讨论"))
        await h.engine.drain()
        assert h.sent[-1][2] == "later-topic"
    asyncio.run(scenario())


def test_followup_clears_ignored_backoff_only_after_judge_accepts_engagement():
    async def scenario():
        h = Harness()
        h.config = replace(h.config, groups={"100": GroupPolicy(mode="active")})
        h.engine.on_message(h.message())
        await h.engine.drain()
        h.now += 31
        h.action = "followup"
        h.engine.on_message(h.message("replying", text="你刚才这个观点有意思，再说说？"))
        await h.engine.drain()
        assert len(h.sent) == 2 and h.engine._groups["100"].last_interjection_at is None
        h.now += 45
        h.action = "reply"
        h.engine.on_message(h.message("topic", sender_id="another", text="还有这个细节"))
        await h.engine.drain()
        assert len(h.sent) == 3
    asyncio.run(scenario())


def test_followup_in_batch_does_not_let_unrelated_selected_target_bypass_cooldown():
    async def scenario():
        h = Harness()
        h.engine.on_message(h.message())
        await h.engine.drain()
        h.now += 31
        h.engine.on_message(h.message("potential-followup", text="你刚才这个意思？"))
        h.engine.on_message(h.message("unrelated", sender_id="other", text="我们继续自己的话题"))
        await h.engine.drain()
        assert len(h.checked) == 2 and len(h.sent) == 1
        assert h.checked[-1].reply_candidate_ids == ("potential-followup",)
        assert "unrelated" not in h.checked[-1].followup_candidate_ids
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["name", "followup", "quote"])
def test_requested_long_text_can_be_complete_while_unsolicited_text_stays_bounded(kind):
    async def scenario():
        h = Harness()
        h.engine.on_bot_message("100", "可以讨论", message_id="answer", recipient_id="user")
        h.now += 31
        kwargs = {}
        if kind == "name":
            kwargs["text"] = "群灵，写一篇 400 字的说明"
        elif kind == "quote":
            kwargs.update(text="写一篇 400 字的说明", reply_to="answer", sender_id="other")
        else:
            h.action = "followup"
            kwargs["text"] = "那你给我写一篇 400 字的说明"
        h.text = "字" * 500
        h.engine.on_message(h.message(**kwargs))
        await h.engine.drain()
        assert len(h.sent) == 1 and len(h.sent[0][1]) == 500
        context, decision = h.generated[0]
        assert reply_character_limit(context, decision) == 2000
        h.now += 31
        h.text = "字" * 2001
        h.engine.on_message(h.message("too-long", text="群灵，继续说"))
        await h.engine.drain()
        assert len(h.sent) == 1
        assert h.engine.status()["groups"]["100"]["last_event"]["code"] == "invalid_generation"
    asyncio.run(scenario())


def test_snapshot_preserves_ordered_bot_turns_speakers_and_request_boundary():
    async def scenario():
        h = Harness()
        h.action = "silence"
        h.engine.on_message(h.message("user-one", sender_name="甲", text="第一句话"))
        await h.engine.drain()
        h.engine.on_bot_message("100", "第一条回复" * 100, message_id="bot-one", recipient_id="user")
        h.engine.on_message(h.message("user-two", sender_id="other", sender_name="乙", text="另一人的话"))
        await h.engine.drain()
        h.engine.on_bot_message("100", "第二条回复", message_id="bot-two", recipient_id="other")
        h.engine.on_message(h.message("explicit", explicit_request=True, text="看一下刚才的上下文"))
        snapshot = h.engine.snapshot("100", before_message_id="explicit", before_timestamp=h.now)
        assert [item.message_id for item in snapshot] == ["user-one", "bot-one", "user-two", "bot-two"]
        assert [item.is_bot for item in snapshot] == [False, True, False, True]
        assert snapshot[0].sender_name == "甲" and snapshot[2].sender_id == "other"
        assert len(snapshot[1].text) > 400  # Do not keep only a fragment of the answer.
        h.engine.on_message(h.message("future", text="等待期间的新话"))
        assert snapshot == h.engine.snapshot("100", before_message_id="explicit", before_timestamp=h.now)
        assert not h.engine.snapshot("100")
        assert not h.engine.snapshot("other-group", before_message_id="explicit", before_timestamp=h.now + 1)
        assert not h.engine.snapshot("100", before_message_id="missing", before_timestamp=h.now)
        h.config = replace(h.config, revision="2", enabled=False)
        assert not h.engine.snapshot("100", before_message_id="explicit", before_timestamp=h.now + 1)
        assert not h.engine._groups
        await h.engine.drain()
    asyncio.run(scenario())


def test_quote_to_earlier_visible_bot_turn_is_direct_but_bot_turn_cannot_be_target():
    async def scenario():
        h = Harness()
        h.config = replace(h.config, groups={"100": GroupPolicy(mode="quiet")})
        h.engine.on_bot_message("100", "前一个观点", message_id="earlier", recipient_id="someone")
        h.engine.on_bot_message("100", "补充一个观点", message_id="later", recipient_id="someone")
        h.engine.on_message(h.message(reply_to="earlier", text="前一个观点我有个疑问"))
        await h.engine.drain()
        assert len(h.sent) == 1
        assert h.checked[0].addressed_candidate_ids == ("m1",)
        context = h.checked[0]
        assert validate_decision({"action": "reply", "target_message_id": "earlier"}, context) is None
    asyncio.run(scenario())


def test_delayed_explicit_request_snapshot_excludes_newer_timestamp_received_before_it():
    async def scenario():
        h = Harness()
        h.action = "silence"
        h.engine.on_message(h.message("older", timestamp=h.now - 10))
        h.engine.on_message(h.message("newer", timestamp=h.now))
        h.engine.on_message(h.message("delayed", timestamp=h.now - 5, explicit_request=True))
        snapshot = h.engine.snapshot("100", before_message_id="delayed", before_timestamp=h.now - 5)
        assert [item.message_id for item in snapshot] == ["older"]
        await h.engine.drain()
    asyncio.run(scenario())


def test_direct_address_still_obeys_daily_and_hourly_budgets():
    async def scenario():
        h = Harness()
        h.config = replace(h.config, groups={"100": GroupPolicy(mode="active", judgments_per_hour=1)})
        h.engine.on_message(h.message(text="群灵，在吗？"))
        await h.engine.drain()
        h.now += 10
        h.engine.on_message(h.message("another", text="群灵，在吗？"))
        await h.engine.drain()
        assert len(h.checked) == len(h.sent) == 1
        row = h.engine.status()["groups"]["100"]
        assert row["status_code"] == "budget_exhausted"
        assert row["last_event"]["code"] == "budget_exhausted"
    asyncio.run(scenario())


def test_diagnostics_include_idle_groups_and_surviving_budget_without_chat_text(tmp_path):
    async def scenario():
        h = Harness(state_dir=tmp_path)
        status = h.engine.status()
        assert status["groups"]["100"]["status_code"] == "ready"
        assert not h.engine._groups
        h.engine.on_message(h.message(text="绝不出现在诊断中的内容"))
        await h.engine.drain()
        row = h.engine.status()["groups"]["100"]
        assert row["status_code"] == "cooldown" and row["cooldown_until"] == h.now + 45
        assert row["last_event"] == {"code": "sent", "at": h.now}
        assert row["replies_hour_limit"] == 30 and row["judgments_day_limit"] == 1200
        assert "诊断中的内容" not in json.dumps(h.engine.status(), ensure_ascii=False)
        await h.engine.close()
        restored = Harness(state_dir=tmp_path)
        row = restored.engine.status()["groups"]["100"]
        assert row["replies_day"] == 1 and row["judgments_day"] == 1
        assert row["cooldown_until"] == restored.now + 45
        assert not restored.engine._groups
    asyncio.run(scenario())
