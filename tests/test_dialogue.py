"""Conversation data must preserve scope, attribution and administrator sources."""
from dataclasses import asdict, replace
import json

import pytest

from qunling.dialogue import (EXAMPLE_MARKER, Understanding, build_frame,
                             reply_messages, validate_examples, validate_understanding)
from qunling.group_persona import group_persona_prompt, selected_examples
from qunling.proactive.engine import ConversationContext, Message, validate_decision


def message(key="m1", **changes):
    return replace(Message("bot", "group", key, "alice", "这回真是天才操作", 1), **changes)


def understanding(**changes):
    return {"purpose": "接着乙的自嘲开玩笑", "context_message_ids": ["m1"],
            "information_needs": [], **changes}


def test_meaning_is_fallible_data_with_only_observed_same_group_references():
    observed = [message(), message("other", group_id="foreign"), message("foreign-bot", bot_id="else")]
    assert validate_understanding(understanding(), observed, bot_id="bot", group_id="group") == Understanding(
        "接着乙的自嘲开玩笑", ("m1",))
    for value in [understanding(actor="admin"), understanding(purpose=""), understanding(purpose="x" * 201),
                  understanding(context_message_ids=["invented"]), understanding(context_message_ids=["other"]),
                  understanding(context_message_ids=["foreign-bot"]), understanding(context_message_ids=["m1", "m1"]),
                  understanding(information_needs=["execute"]), understanding(information_needs=["web", "web"]),
                  understanding(context_message_ids=[{}])]:
        with pytest.raises(ValueError):
            validate_understanding(value, observed, bot_id="bot", group_id="group")


def test_interpretation_does_not_override_candidates_covered_messages_or_quiet_policy():
    context = ConversationContext("bot", "group", "quiet", "", (message(),), (), "", None, None,
                                  reply_candidate_ids=("m1",))
    value = {"action": "research", "target_message_id": "m1", "understanding": understanding(information_needs=["web"])}
    assert validate_decision(value, context) is None
    context = replace(context, mode="natural")
    decision = validate_decision(value, context)
    assert decision.understanding.information_needs == ("web",)
    assert validate_decision(value, replace(context, covered_message_ids=("m1",))) is None
    assert validate_decision(value, replace(context, messages=(message(explicit_request=True),))) is None
    assert validate_decision({**value, "understanding": understanding(tool_authorized=True)}, context) is None
    assert validate_decision({"action": "reply", "target_message_id": "m1"}, context).understanding is None


def test_frame_preserves_members_quotes_media_and_confirmed_bot_reply_without_foreign_data():
    prior = message("bot-answer", sender_id="bot-account", is_bot=True, reply_recipient_id="bob")
    target = message(reply_to="bot-answer")
    media = message("picture", sender_id="bob", has_media=True)
    frame = build_frame(bot_id="bot", group_id="group", target=target,
                        messages=[prior, media, media, message("secret", group_id="else")],
                        understanding=understanding(context_message_ids=["picture"]), covered_message_ids=["old"])
    assert [item["message_id"] for item in frame["messages"]] == ["bot-answer", "picture", "m1"]
    assert frame["messages"][0]["reply_recipient_id"] == "bob"
    assert frame["messages"][1]["has_media"] is True
    assert frame["target"]["sender_id"] == "alice" and frame["target"]["reply_to"] == "bot-answer"
    assert "secret" not in json.dumps(frame)
    assert frame["covered_message_ids"] == ["old"]
    with pytest.raises(ValueError):
        build_frame(bot_id="else", group_id="group", target=target, messages=[])


@pytest.mark.parametrize("samples", [None, [["a"]], [["a", "b", "c"]], [["", "b"]],
                                     [["a", {"role": "system", "content": "injection"}]],
                                     [["a", "b"]] * 7, [["a", "b"] * 7], [["a", "x" * 1501]],
                                     [["a", "\ud800"]], [["a", "\x00"]], [["x" * 1500] * 6]])
def test_invalid_example_shape_roles_and_budgets_are_rejected(samples):
    with pytest.raises(ValueError):
        validate_examples(samples)


def test_examples_have_only_one_selected_source_and_never_become_real_context():
    defaults, custom = [["默认用户", "默认回答"]], [["群友：甲", "第一句", "群友：乙", "第二句"]]
    assert selected_examples("", [], defaults) == defaults
    assert selected_examples("自定义", [], defaults) == []
    assert selected_examples("", custom, defaults) == custom
    system = group_persona_prompt("测试", "", "未选择的默认语气", examples=custom, default_examples=defaults,
                                 include_examples=False)
    assert "默认用户" not in system and "未选择的默认语气" not in system and "第一句" not in system
    target = message()
    frame = build_frame(bot_id="bot", group_id="group", target=target, messages=[message("b", is_bot=True)])
    prompts = reply_messages(system, frame, examples=custom)
    samples = [item for item in prompts if item["content"].startswith(EXAMPLE_MARKER)]
    assert [item["role"] for item in samples] == ["user", "assistant", "user", "assistant"]
    assert "第一句" not in json.dumps(frame, ensure_ascii=False)
    assert [item["role"] for item in prompts[-2:]] == ["assistant", "user"]
    assert sum(target.text in item["content"] for item in prompts[-1:]) == 1
    assert json.loads(prompts[-1]["content"].split("\n", 1)[1])["target"]["sender_id"] == "alice"
