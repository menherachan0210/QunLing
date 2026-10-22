"""Public continuation anchors and reminder evidence, without tools or QQ I/O."""
import pytest

from qunling.group_context import continuation_anchors, topic_roots, unresolved_question_candidates
from qunling.proactive.engine import Message


def message(key, text, at, *, sender="alice", **kwargs):
    return Message(kwargs.pop("bot_id", "bot"), kwargs.pop("group_id", "100"), key, sender, text, at, **kwargs)


def comic_exchange():
    return (
        message("question", "你怎么看", 100, explicit_request=True, has_media=True),
        message("answer", "这张漫画的分镜挺有张力。", 101, sender="bot", is_bot=True,
                reply_to="question", reply_recipient_id="alice"),
    )


@pytest.mark.parametrize("sender", ["alice", "bob", "carol"])
@pytest.mark.parametrize("text", ["你知道这啥漫画不", "作者是谁", "这是哪部漫画？", "这是谁画的？"])
def test_any_member_can_continue_a_real_public_comic_answer(sender, text):
    messages = comic_exchange() + (message("follow", text, 102, sender=sender),)
    assert continuation_anchors(messages) == {"follow": "answer"}
    assert topic_roots(messages)["follow"] == "question"


def test_multiple_peers_can_continue_the_same_public_exchange_without_mentions():
    messages = comic_exchange() + (
        message("follow", "你知道这啥漫画不", 102, sender="bob"),
        message("author", "作者是谁", 103, sender="carol"),
    )
    assert continuation_anchors(messages) == {"follow": "answer", "author": "answer"}
    assert set(topic_roots(messages).values()) == {"question"}


@pytest.mark.parametrize("changes", [
    {"mentions": ("someone-else",)}, {"explicit_request": True},
    {"group_id": "200"}, {"bot_id": "other-bot"},
])
def test_continuation_never_crosses_scope_or_claims_addressed_or_explicit_tasks(changes):
    messages = comic_exchange() + (message("follow", "作者是谁", 102, sender="bob", **changes),)
    assert "follow" not in continuation_anchors(messages)


@pytest.mark.parametrize("text", ["换个话题，作者是谁", "另外问一个问题，作者是谁", "你知道今天北京天气吗"])
def test_explicit_topic_change_or_independent_question_does_not_borrow_comic(text):
    messages = comic_exchange() + (message("follow", text, 102, sender="bob"),)
    assert "follow" not in continuation_anchors(messages)
    assert topic_roots(messages)["follow"] == "follow"


def test_unrelated_human_topic_blocks_unquoted_deictic_question():
    messages = comic_exchange() + (
        message("other", "我昨天买了一本新小说", 102, sender="bob"),
        message("follow", "作者是谁", 103, sender="carol"),
    )
    assert "follow" not in continuation_anchors(messages)
    quoted = messages[:-1] + (message("follow", "作者是谁", 103, sender="carol", reply_to="answer"),)
    assert continuation_anchors(quoted)["follow"] == "answer"


@pytest.mark.parametrize("at", [99, 100.5, 222])
def test_future_or_expired_reply_cannot_become_a_continuation_anchor(at):
    messages = comic_exchange() + (message("follow", "作者是谁", at, sender="bob"),)
    assert "follow" not in continuation_anchors(messages)


@pytest.mark.parametrize("at", [99, 100.5, 222])
def test_quote_does_not_bypass_future_or_expired_reply_boundary(at):
    messages = comic_exchange() + (message("follow", "作者是谁", at, sender="bob", reply_to="answer"),)
    assert "follow" not in continuation_anchors(messages)
    assert topic_roots(messages)["follow"] == "follow"


def test_actual_quote_selects_its_receipt_even_when_same_topic_has_newer_reply():
    messages = comic_exchange() + (
        message("answer-2", "再补充一点。", 102, sender="bot", is_bot=True, reply_to="question"),
        message("follow", "这句话什么意思", 103, sender="carol", reply_to="answer"),
    )
    assert continuation_anchors(messages)["follow"] == "answer"


def test_synthetic_bot_observation_is_not_a_real_reply_anchor():
    messages = (
        message("bot-observed-101-1", "这页分镜挺有张力。", 101, sender="bot", is_bot=True),
        message("follow", "作者是谁", 102, sender="bob"),
    )
    assert continuation_anchors(messages) == {}


def test_parallel_image_tasks_require_exact_quote_before_peers_continue():
    messages = comic_exchange() + (
        message("image-a", "图片已经发出", 102, sender="bot", is_bot=True, has_media=True, reply_to="question"),
        message("question-b", "画另一部漫画", 103, sender="bob", explicit_request=True),
        message("image-b", "图片已经发出", 104, sender="bot", is_bot=True, has_media=True, reply_to="question-b"),
        message("follow", "作者是谁", 105, sender="carol"),
    )
    assert "follow" not in continuation_anchors(messages)
    quoted = messages[:-1] + (
        message("follow", "作者是谁", 105, sender="carol", reply_to="image-a"),
        message("next", "这是谁画的", 106, sender="bob"),
    )
    assert continuation_anchors(quoted)["follow"] == "image-a"
    assert continuation_anchors(quoted)["next"] == "image-a"


def test_parallel_incoming_images_are_also_ambiguous_without_quote():
    messages = comic_exchange() + (
        message("question-b", "看看这个", 102, sender="bob", explicit_request=True, has_media=True),
        message("answer-b", "这张漫画画面很有表现力。", 103, sender="bot", is_bot=True, reply_to="question-b"),
        message("follow", "作者是谁", 104, sender="carol"),
    )
    assert "follow" not in continuation_anchors(messages)
    quoted = messages[:-1] + (message("follow", "作者是谁", 104, sender="carol", reply_to="answer"),)
    assert continuation_anchors(quoted)["follow"] == "answer"


def test_unrelated_bare_human_stickers_do_not_interrupt_peer_public_question():
    messages = comic_exchange() + (
        message("sticker-1", "", 102, sender="bob", has_media=True),
        message("sticker-2", "", 103, sender="carol", has_media=True),
        message("follow", "作者是谁", 104, sender="dave"),
    )
    assert continuation_anchors(messages)["follow"] == "answer"
    assert topic_roots(messages)["follow"] == "question"


def test_explicit_comic_reference_distinguishes_unrelated_delivered_sticker_by_topic_words():
    messages = comic_exchange() + (
        message("sticker-request", "发个表情", 102, sender="bob", explicit_request=True),
        message("sticker", "表情已发送", 103, sender="bot", is_bot=True, has_media=True, reply_to="sticker-request"),
        message("follow", "那张漫画作者是谁", 104, sender="carol"),
    )
    assert continuation_anchors(messages)["follow"] == "answer"
    assert topic_roots(messages)["follow"] == "question"
    ambiguous = messages[:-1] + (message("follow", "作者是谁", 104, sender="carol"),)
    assert "follow" not in continuation_anchors(ambiguous)


def test_two_comics_are_not_disambiguated_by_repeating_the_generic_comic_topic():
    messages = comic_exchange() + (
        message("second-request", "看看另一本漫画", 102, sender="bob", explicit_request=True, has_media=True),
        message("second-answer", "这张漫画色彩很好。", 103, sender="bot", is_bot=True, reply_to="second-request"),
        message("follow", "那张漫画作者是谁", 104, sender="carol"),
    )
    assert "follow" not in continuation_anchors(messages)


@pytest.mark.parametrize("sender", ["alice", "bob", "carol"])
def test_reminder_offers_public_question_even_after_an_explanation_of_missing_it(sender):
    messages = comic_exchange() + (
        message("follow", "你知道这啥漫画不", 102, sender="bob"),
        message("complaint", "非得我@你才说话吗", 103, sender="bob", explicit_request=True),
        message("explanation", "刚才没接上，不是必须艾特。", 104, sender="bot", is_bot=True,
                reply_to="complaint", reply_recipient_id="bob"),
    )
    candidates = unresolved_question_candidates(messages, current_text="刚才问的漫画名字呢", participant_id=sender)
    assert candidates[0]["message_id"] == "follow"
    assert candidates[0]["sender_id"] == "bob"
    assert candidates[0]["text"] == "你知道这啥漫画不"
    assert candidates[0]["related_replies"] == ({"message_id": "answer", "text": "这张漫画的分镜挺有张力。"},)
    assert all(set(item) == {"message_id", "sender_id", "text", "related_replies"} for item in candidates)
    assert not any(item["message_id"] == "complaint" for item in candidates)


@pytest.mark.parametrize("text", ["你倒是回答他啊", "你倒是回人家啊", "请回答她一下", "回答他们的问题"])
def test_peer_nudge_after_meta_answer_keeps_original_author_and_question(text):
    messages = comic_exchange() + (
        message("follow", "你知道这啥漫画不", 102, sender="bob"),
        message("complaint", "非得我@你才说话吗", 103, sender="bob", explicit_request=True),
        message("explanation", "刚才那句漏回了，不用每次@我。", 104, sender="bot", is_bot=True,
                reply_to="complaint", reply_recipient_id="bob"),
    )
    candidates = unresolved_question_candidates(messages, current_text=text, participant_id="carol")
    assert candidates[0]["message_id"] == "follow" and candidates[0]["sender_id"] == "bob"
    assert candidates[0]["text"] == "你知道这啥漫画不"
    assert set(candidates[0]) == {"message_id", "sender_id", "text", "related_replies"}
    unrelated = messages + (message("new-topic", "周末电影票买好了", 105, sender="dave"),)
    assert unresolved_question_candidates(unrelated, current_text=text, participant_id="carol") == ()
    quoted = unresolved_question_candidates(unrelated, current_text=text, participant_id="carol", reply_to="follow")
    assert quoted[0]["message_id"] == "follow"


@pytest.mark.parametrize("text", ["别回答他", "你刚才回答他的是什么", "换个话题，你倒是回答他啊"])
def test_peer_nudge_detector_does_not_reinterpret_negation_or_new_question(text):
    assert unresolved_question_candidates(comic_exchange(), current_text=text, participant_id="carol") == ()


def test_reminder_exact_quote_can_recover_other_members_question_across_topic_change():
    messages = comic_exchange() + (
        message("follow", "作者是谁", 102, sender="bob"),
        message("other", "周末电影票已经买好", 103, sender="carol"),
    )
    assert unresolved_question_candidates(messages, current_text="刚才问的还没回答", participant_id="carol") == ()
    candidates = unresolved_question_candidates(messages, current_text="回答刚才的问题", participant_id="carol", reply_to="follow")
    assert candidates[0]["message_id"] == "follow" and candidates[0]["sender_id"] == "bob"


@pytest.mark.parametrize("text", ["你知道这啥漫画不", "换个话题，刚才的问题还没回答", "好的"])
def test_normal_new_turn_never_gets_reminder_candidates(text):
    assert unresolved_question_candidates(comic_exchange(), current_text=text, participant_id="alice") == ()


def test_reminder_evidence_never_asserts_unanswered_status_or_grants_tool_ownership():
    messages = comic_exchange() + (
        message("follow", "作者是谁", 102, sender="bob"),
        message("answered", "作者是某某。", 103, sender="bot", is_bot=True, reply_to="follow"),
    )
    candidates = unresolved_question_candidates(messages, current_text="刚才的问题呢", participant_id="carol")
    assert candidates[0]["message_id"] == "follow"
    assert candidates[0]["related_replies"][-1] == {"message_id": "answered", "text": "作者是某某。"}
    assert "unanswered" not in candidates[0] and "authorized" not in candidates[0]


def test_unquoted_unanchored_question_from_another_member_is_not_assumed_to_be_for_bot():
    messages = (message("question", "你知道这啥漫画不", 100, sender="bob"),)
    assert unresolved_question_candidates(messages, current_text="刚才的问题呢", participant_id="carol") == ()
    candidates = unresolved_question_candidates(messages, current_text="刚才的问题呢", participant_id="carol", reply_to="question")
    assert candidates[0]["message_id"] == "question"


def test_reminder_rejects_mixed_scope_unknown_quote_and_stale_question():
    reminder = dict(current_text="刚才的问题呢", participant_id="alice")
    assert unresolved_question_candidates(comic_exchange(), reply_to="missing", **reminder) == ()
    mixed = comic_exchange() + (message("foreign", "作者是谁", 102, group_id="200"),)
    assert unresolved_question_candidates(mixed, **reminder) == ()
    stale = comic_exchange() + (message("unrelated", "大家早上好", 300, sender="carol"),)
    assert unresolved_question_candidates(stale, **reminder) == ()


def test_reminder_includes_trusted_bot_mention_question_but_excludes_other_person_mention():
    request = message("question", "你怎么看这部漫画", 100, explicit_request=True, mentions=("90000",))
    answer = message("answer", "这张漫画的分镜挺有张力。", 101, sender="bot", is_bot=True, reply_to="question")
    candidates = unresolved_question_candidates((request, answer), current_text="回答他的问题", participant_id="bob")
    assert candidates[0]["message_id"] == "question" and candidates[0]["sender_id"] == "alice"
    other_person = message("other-question", "作者是谁", 102, sender="bob", mentions=("carol",), reply_to="answer")
    candidates = unresolved_question_candidates((request, answer, other_person), current_text="回答他的问题", participant_id="dave")
    assert all(item["message_id"] != "other-question" for item in candidates)


def test_reminder_candidates_and_reply_text_are_bounded():
    messages = list(comic_exchange())
    for index in range(5):
        messages.append(message(f"question-{index}", "作者是谁？" + "补充" * 500, 102 + index,
                                sender="bob", reply_to="answer"))
    candidates = unresolved_question_candidates(messages, current_text="刚才的问题呢", participant_id="carol", limit=99)
    assert len(candidates) == 3 and all(len(item["text"]) <= 800 for item in candidates)
    assert all(len(item["related_replies"]) <= 2 for item in candidates)
    assert unresolved_question_candidates(messages, current_text="刚才的问题呢", participant_id="carol", limit=0) == ()
