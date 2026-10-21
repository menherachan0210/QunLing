"""Terse corrections keep their actual answer lineage without joining other tasks."""
import pytest

from qunling.group_context import metadata, topic_roots
from qunling.proactive.engine import Message


def message(key, text, at, **kwargs):
    return Message("bot", "100", key, kwargs.pop("sender", "alice"), text, at, **kwargs)


def test_short_corrections_from_multiple_people_continue_the_public_bot_answer():
    messages = (
        message("request", "给我生成一个头像", 100, explicit_request=True),
        message("answer", "我根据头像分析生成了图片。", 110, sender="bot", is_bot=True,
                reply_to="request", reply_recipient_id="alice"),
        message("wrong", "完全不像", 111),
        message("peer", "搞错了", 112, sender="bob"),
    )
    roots = topic_roots(messages)
    assert set(roots.values()) == {"request"}
    evidence = metadata("bot", "100", messages)
    assert evidence["topic_message_ids"] == ("request", "answer", "wrong", "peer")
    assert evidence["participant_ids"] == ("alice", "bob")


def test_correction_does_not_jump_over_unrelated_human_topic_to_old_bot_answer():
    messages = (
        message("answer", "刚才看到的是白猫头像。", 100, sender="bot", is_bot=True),
        message("movie", "周末电影票已经买好了", 101, sender="bob"),
        message("wrong", "不对", 102, sender="carol"),
    )
    roots = topic_roots(messages)
    assert roots["wrong"] != roots["answer"]


def test_correction_does_not_join_an_independent_explicit_task_or_old_answer():
    messages = (
        message("answer", "刚才看到的是白猫头像。", 100, sender="bot", is_bot=True),
        message("request", "帮我查询天气", 101, explicit_request=True),
        message("wrong", "搞错了", 102),
        message("late", "不像", 1000),
    )
    roots = topic_roots(messages)
    assert roots["wrong"] == "wrong"
    assert roots["late"] == "late"


def test_exact_quote_can_reconnect_correction_across_an_intervening_topic():
    messages = (
        message("request", "给我生成一个头像", 100, explicit_request=True),
        message("answer", "已经发送生成图片。", 110, sender="bot", is_bot=True, reply_to="request"),
        message("movie", "周末电影票已经买好了", 111, sender="bob"),
        message("wrong", "不像", 112, reply_to="answer"),
    )
    roots = topic_roots(messages)
    assert roots["wrong"] == "request"
    assert roots["movie"] != roots["wrong"]


def test_multiple_delivered_image_topics_require_quote_for_a_terse_correction():
    messages = (
        message("request-a", "生成自己的头像", 100, explicit_request=True),
        message("image-a", "已发送一张图片", 110, sender="bot", is_bot=True,
                has_media=True, reply_to="request-a"),
        message("request-b", "画只兔子", 111, sender="bob", explicit_request=True),
        message("image-b", "已发送一张图片", 120, sender="bot", is_bot=True,
                has_media=True, reply_to="request-b"),
        message("unclear", "完全不像", 121, sender="carol"),
    )
    assert topic_roots(messages)["unclear"] == "unclear"
    quoted = messages[:-1] + (
        message("clear", "完全不像", 121, sender="carol", reply_to="image-a"),
        message("peer", "搞错了", 122, sender="bob"),
    )
    roots = topic_roots(quoted)
    assert roots["clear"] == roots["peer"] == "request-a"


def test_rounded_qq_timestamp_keeps_previously_delivered_same_second_image_in_topic_and_focus():
    messages = (
        message("request", "生成头像", 99, explicit_request=True),
        message("image", "已发送一张图片", 100.25, sender="bot", is_bot=True,
                has_media=True, reply_to="request"),
        message("wrong", "完全不像", 100),
    )
    assert topic_roots(messages)["wrong"] == "request"
    evidence = metadata("bot", "100", messages)
    assert evidence["topic_message_ids"] == ("request", "image", "wrong")
    assert "image" in evidence["focus_message_ids"]
    # Public focus is independently bounded by receipt order, including when
    # the new human text starts a different topic rather than a correction.
    independent = messages[:-1] + (message("movie", "周末电影票已经买好了", 100),)
    evidence = metadata("bot", "100", independent)
    assert "image" not in evidence["topic_message_ids"]
    assert "image" in evidence["focus_message_ids"]


@pytest.mark.parametrize("earlier_at,later_at,is_bot", [
    (101.25, 100, True), (100.75, 100.25, True), (100.25, 100, False),
])
def test_rounding_exception_never_relates_other_future_or_human_messages(earlier_at, later_at, is_bot):
    messages = (
        message("answer", "已发送一张图片", earlier_at, sender="bot" if is_bot else "bob",
                is_bot=is_bot, has_media=True),
        message("wrong", "完全不像", later_at),
    )
    assert topic_roots(messages)["wrong"] == "wrong"
    assert "answer" not in metadata("bot", "100", messages)["focus_message_ids"]


@pytest.mark.parametrize("text", [
    "女变男了都", "女性角色画成男性", "女生画成了男生", "你把女性角色画成了男性", "男变女了",
    "短发变长发了", "发色不对", "猫耳没了", "帽子画错了",
])
def test_explicit_image_feature_correction_requires_recent_confirmed_image(text):
    image = message("image", "图片已发送", 110, sender="bot", is_bot=True, has_media=True)
    feedback = message("feedback", text, 111, sender="bob")
    assert topic_roots((image, feedback))["feedback"] == "image"
    text_only = message("answer", "关于女性角色的讨论", 110, sender="bot", is_bot=True)
    assert topic_roots((text_only, feedback))["feedback"] == "feedback"
    unrelated = message("other", "周末电影票已经买好了", 110.5, sender="carol")
    assert topic_roots((image, unrelated, feedback))["feedback"] == "feedback"
    late = message("late", text, 300, sender="bob")
    assert topic_roots((image, late))["late"] == "late"


def test_image_feature_correction_does_not_guess_between_two_delivered_images():
    messages = (
        message("draw-a", "画一个女性角色", 100, explicit_request=True),
        message("image-a", "女性角色图片已发送", 110, sender="bot", is_bot=True,
                has_media=True, reply_to="draw-a"),
        message("draw-b", "画另一个女性角色", 111, sender="bob", explicit_request=True),
        message("image-b", "女性角色图片已发送", 112, sender="bot", is_bot=True,
                has_media=True, reply_to="draw-b"),
        message("wrong", "女性角色画成男性", 113, sender="carol"),
    )
    assert topic_roots(messages)["wrong"] == "wrong"
    quoted = messages[:-1] + (message("quoted", "一点都不像", 113, reply_to="image-a"),
                              message("wrong", "女性角色画成男性", 114, sender="carol"))
    assert topic_roots(quoted)["wrong"] == "draw-a"


def test_screenshot_three_person_correction_chain_recalls_original_avatar_and_delivery(tmp_path, monkeypatch):
    from qunling import group_knowledge as knowledge

    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    scope = {"workspace_id": knowledge.workspace_id("instance", "workspace"), "bot_id": "bot", "group_id": "100"}
    assert knowledge.publish(**scope, question="生成自己的头像", source="avatar_analysis",
                             subject="机器人头像（QQ900）的头像", text="原头像有墨绿短发、猫耳和黑礼帽。",
                             message_ids=["draw"], actor_id="alice")
    assert knowledge.publish(**scope, question="生成自己的头像", source="image_delivery", subject="图片消息 image",
                             text="已发送一张图片，未记录成图视觉核验。", message_ids=["draw", "image"], actor_id="alice")
    assert knowledge.publish(**scope, question="另一个人的头像", source="avatar_analysis",
                             subject="另一个头像（QQ901）", text="另一头像是白猫。", message_ids=["unrelated"], actor_id="other")
    messages = [
        message("draw", "生成自己的头像", 100, explicit_request=True),
        message("image", "图片已发送", 110, sender="bot", is_bot=True, has_media=True, reply_to="draw"),
        message("description", "保留了墨绿短发、猫耳和黑礼帽。", 111, sender="bot", is_bot=True, reply_to="draw"),
    ]
    for index, (sender, text) in enumerate((
        ("bob", "一点都不像"), ("carol", "女变男了都"), ("alice", "这个有问题 怎么能等全部处理好了在发"),
    )):
        key = f"feedback-{index}"
        messages.append(message(key, text, 112 + index, sender=sender))
        assert topic_roots(messages)[key] == "draw"
        evidence = metadata("bot", "100", messages)
        records = knowledge.recall(**scope, question=text, related_message_ids=evidence["topic_message_ids"])
        assert {row["source"] for row in records} == {"avatar_analysis", "image_delivery"}
        assert len(records) == 2 and all(row["actor_id"] == "alice" for row in records)
    assert metadata("bot", "100", messages)["participant_ids"] == ("alice", "bob", "carol")
