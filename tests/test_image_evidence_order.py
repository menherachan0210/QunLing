"""Arrival-order image facts; integer QQ times and no external calls."""
import asyncio

import pytest

from test_proactive_engine import Harness


def incoming(h, message_id, text, *, sender="20000", explicit=False, timestamp=None):
    return h.message(message_id, sender_id=sender, text=text,
                     explicit_request=explicit,
                     timestamp=int(h.now) if timestamp is None else timestamp)


def deliver_image(h, *, message_id="picture", reply_to="draw"):
    h.engine.on_bot_message(
        "100", "机器人已发送 1 张图片（QQ 回执已确认；未记录成图视觉核验）。",
        message_id=message_id, recipient_id="20000", reply_to=reply_to, has_media=True,
    )


def test_explicit_short_followup_keeps_earlier_same_second_image_receipt():
    async def scenario():
        h = Harness()
        second = int(h.now)
        try:
            assert h.engine.on_message(incoming(h, "draw", "生成我的头像", explicit=True))
            h.now = second + .25
            deliver_image(h)
            h.engine.explicit_done("100", "draw")
            h.now = second + .5
            assert h.engine.on_message(incoming(h, "feedback", "一点都不像", explicit=True))
            frozen = h.engine.snapshot("100", before_message_id="feedback", before_timestamp=second)
            assert [m.message_id for m in frozen] == ["draw", "picture"]
            assert frozen[-1].timestamp > second and frozen[-1].has_media
            shared = h.engine.context_metadata("100", frozen, "20000")
            assert shared["topic_message_ids"] == ("draw", "picture")
            assert shared["covered_message_ids"] == ("draw",)

            # Even an older platform timestamp cannot put a later ingress turn
            # or a later bot receipt into this request's frozen context.
            h.now = second + .75
            assert h.engine.on_message(incoming(h, "later-human", "后来发来的话", sender="20001",
                                                timestamp=second - 1))
            deliver_image(h, message_id="later-picture")
            assert frozen == h.engine.snapshot("100", before_message_id="feedback", before_timestamp=second)
            assert h.engine.context_metadata("100", frozen, "20000")["topic_message_ids"] == ("draw", "picture")
        finally:
            await h.engine.close()
    asyncio.run(scenario())


def test_free_short_peer_followup_keeps_same_second_image_topic_and_focus():
    async def scenario():
        h = Harness()
        h.action = "silence"
        second = int(h.now)
        try:
            assert h.engine.on_message(incoming(h, "draw", "生成我的头像", explicit=True))
            h.now = second + .25
            deliver_image(h)
            h.engine.explicit_done("100", "draw")
            h.now = second + .5
            assert h.engine.on_message(incoming(h, "feedback", "一点都不像", sender="20001"))
            await h.engine.drain()
            assert len(h.checked) == 1
            context = h.checked[0]
            assert [m.message_id for m in context.messages] == ["draw", "picture", "feedback"]
            assert context.topic_message_ids == ("draw", "picture", "feedback")
            assert set(context.focus_message_ids) == {"draw", "picture", "feedback"}
            assert context.participant_ids == ("20000", "20001")
            assert context.reply_candidate_ids == ("feedback",)

            h.now = second + .75
            deliver_image(h, message_id="later-picture")
            assert [m.message_id for m in context.messages] == ["draw", "picture", "feedback"]
            assert "later-picture" not in context.topic_message_ids
        finally:
            await h.engine.close()
    asyncio.run(scenario())


def test_delayed_known_anchor_still_excludes_cross_second_future_image():
    async def scenario():
        h = Harness()
        try:
            assert h.engine.on_message(incoming(h, "earlier", "已经收到的消息", timestamp=int(h.now) - 10))
            deliver_image(h)
            assert h.engine.on_message(incoming(h, "delayed", "那刚才呢", explicit=True,
                                                timestamp=int(h.now) - 5))
            assert [m.message_id for m in h.engine.snapshot(
                "100", before_message_id="delayed", before_timestamp=int(h.now) - 5,
            )] == ["earlier"]
        finally:
            await h.engine.close()
    asyncio.run(scenario())


def test_unknown_anchor_retains_strict_time_boundary_and_group_scope():
    async def scenario():
        h = Harness()
        second = int(h.now)
        try:
            assert h.engine.on_message(incoming(h, "older", "更早的消息", timestamp=second - 1))
            assert h.engine.on_message(incoming(h, "same-second", "同秒消息"))
            h.now = second + .25
            deliver_image(h)
            assert [m.message_id for m in h.engine.snapshot(
                "100", before_message_id="not-observed", before_timestamp=second,
            )] == ["older"]
            assert h.engine.snapshot("100", before_message_id="not-observed") == ()
            assert h.engine.snapshot("other-group", before_message_id="same-second", before_timestamp=second) == ()
        finally:
            await h.engine.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("anchor_fraction", [0, .1])
def test_precision_exception_applies_only_to_bot_receipt_and_integer_anchor(anchor_fraction):
    async def scenario():
        h = Harness()
        second = int(h.now)
        try:
            h.now = second + .25
            assert h.engine.on_message(incoming(h, "human", "时间戳较晚的普通消息", timestamp=h.now))
            deliver_image(h)
            h.now = second + .5
            boundary = second + anchor_fraction
            assert h.engine.on_message(incoming(h, "anchor", "看刚才的消息", explicit=True,
                                                timestamp=boundary))
            frozen = h.engine.snapshot("100", before_message_id="anchor", before_timestamp=boundary)
            assert [m.message_id for m in frozen] == (["picture"] if anchor_fraction == 0 else [])
        finally:
            await h.engine.close()
    asyncio.run(scenario())


def test_same_second_precision_exception_does_not_join_cross_second_future_image():
    async def scenario():
        h = Harness()
        h.action = "silence"
        second = int(h.now)
        try:
            assert h.engine.on_message(incoming(h, "draw", "生成我的头像", explicit=True))
            h.now = second + 1.25
            deliver_image(h)
            h.engine.explicit_done("100", "draw")
            h.now = second + 1.5
            assert h.engine.on_message(incoming(h, "delayed-feedback", "一点都不像", sender="20001",
                                                timestamp=second))
            await h.engine.drain()
            assert len(h.checked) == 1
            context = h.checked[0]
            assert context.topic_message_ids == ("delayed-feedback",)
            assert "picture" not in context.focus_message_ids
        finally:
            await h.engine.close()
    asyncio.run(scenario())
