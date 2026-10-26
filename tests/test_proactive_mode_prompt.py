"""Synthetic participation cases and offline host-protocol checks.

CASES can also be run through HostBridge.judge with an explicitly configured
model by an evaluation runner. The offline test uses a fake completion: it
verifies mode/context transport and the decision contract, not LLM semantics.
No case contains real group history and this module has no QQ/network client.
"""
from __future__ import annotations

from dataclasses import asdict
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qunling.proactive.engine import ConversationContext, Message, validate_decision
from qunling.proactive.host import HostBridge


def _message(message_id, sender, text, *, is_bot=False, has_media=False, explicit=False):
    return Message("synthetic-bot", "synthetic-group", message_id, sender, text,
                   1000.0, sender_name=sender, is_bot=is_bot, has_media=has_media,
                   explicit_request=explicit)


def _context(mode, messages, *, addressed=(), followups=(), last_bot=None, candidates=None,
             participant="", focus=()):
    if candidates is None:
        candidates = tuple(message.message_id for message in messages
                           if not message.is_bot and message.text.strip())
    return ConversationContext(
        bot_id="synthetic-bot", group_id="synthetic-group", mode=mode,
        persona="自然、简短，像熟悉的群友。", messages=tuple(messages),
        followup_candidate_ids=tuple(followups),
        last_bot_text=last_bot.text if last_bot else "",
        last_bot_message_id=last_bot.message_id if last_bot else None,
        last_bot_at=last_bot.timestamp if last_bot else None,
        display_name="小岚", addressed_candidate_ids=tuple(addressed),
        reply_candidate_ids=tuple(candidates),
        conversation_id=f"synthetic-group:{participant}" if participant else "",
        participant_id=participant, focus_message_ids=tuple(focus),
    )


_BOT_REPLY = _message("b1", "synthetic-bot", "我比较喜欢周末看电影放松。", is_bot=True)
CASES = [
    # Social invitations need no new factual information, in any enabled mode.
    *((f"{mode}_direct_greeting", _context(mode, [
        _message("m1", "user-a", "小岚出来说话"),
    ], addressed=("m1",)), frozenset({"reply"})) for mode in ("quiet", "natural", "active")),
    ("active_open_social_invitation", _context("active", [
        _message("m1", "user-a", "终于下班了，大家今晚打算怎么放松？"),
    ]), frozenset({"reply"})),
    ("quiet_open_social_invitation", _context("quiet", [
        _message("m1", "user-a", "终于下班了，大家今晚打算怎么放松？"),
    ]), frozenset({"silence"})),
    ("active_third_person_name", _context("active", [
        _message("m1", "user-a", "小林，你昨天说小岚的声音挺好听，你在哪儿设置的？"),
    ], addressed=("m1",)), frozenset({"silence"})),
    ("active_answered_conversation", _context("active", [
        _message("m1", "user-a", "小林，快递帮我拿了吗？"),
        _message("m2", "user-b", "拿了，已经放你桌上了。"),
        _message("m3", "user-a", "好，谢谢。"),
    ]), frozenset({"silence"})),
    ("active_unseen_picture", _context("active", [
        _message("m1", "user-a", "小岚，这张图片上写的是什么？", has_media=True),
    ], addressed=("m1",)), frozenset({"research"})),
    ("active_unfinished_statement", _context("active", [
        _message("m1", "user-a", "我遇到个奇怪的事，等一下我把后半段发出来。"),
    ]), frozenset({"wait", "silence"})),
    *((f"{mode}_genuine_followup", _context(mode, [
        _BOT_REPLY,
        _message("m1", "user-a", "那你比较喜欢哪一类电影？"),
    ], followups=("m1",), last_bot=_BOT_REPLY), frozenset({"followup"}))
      for mode in ("quiet", "natural", "active")),
    *((f"{mode}_direct_greeting_then_sticker", _context(mode, [
        _message("m1", "user-a", "小岚出来说话"),
        _message("m2", "user-a", "", has_media=True),
    ], addressed=("m1",), candidates=("m1",)), frozenset({"reply"}))
      for mode in ("quiet", "active")),
    ("natural_direct_greeting_then_other_person", _context("natural", [
        _message("m1", "user-a", "小岚出来说话"),
        _message("m2", "user-b", "小林，我把链接私聊发给你了。"),
    ], addressed=("m1",), candidates=("m1", "m2")), frozenset({"reply"})),
    ("active_b_chats_while_a_translates", _context("active", [
        _message("a1", "user-a", "请翻译这张图", has_media=True, explicit=True),
        _message("b1", "user-b", "小岚，你今天有什么开心事？"),
    ], addressed=("b1",), candidates=("b1",), participant="user-b", focus=("b1",)), frozenset({"reply"})),
    ("natural_translation_text_followup_despite_unrelated_picture", _context("natural", [
        _message("a1", "user-a", "原句是『待ってください』，译成『请等一下』可以吗？"),
        _message("answer", "synthetic-bot", "可以，『请等一下』就是在请对方稍等。", is_bot=True),
        _message("other-picture", "user-b", "看看我的新头像", has_media=True),
        _message("a2", "user-a", "那如果加上『再』字，语气有什么变化？"),
    ], followups=("a2",), candidates=("a2",), participant="user-a", focus=("a1", "answer", "a2")), frozenset({"followup"})),
]

# These positive cases deliberately expect the first eligible target. In the
# trailing-message cases this catches an otherwise plausible reply to m2.
EXPECTED_REPLY_TARGETS = {
    name: context.reply_candidate_ids[0]
    for name, context, actions in CASES if actions & {"reply", "followup", "research"}
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name,context,allowed_actions", CASES, ids=[item[0] for item in CASES])
async def test_judge_protocol_preserves_participation_mode_and_conversation(name, context, allowed_actions):
    """Exercise the real host entry point while leaving model choice to live evals."""
    action = sorted(allowed_actions)[0]
    expected = {
        "action": action,
        "target_message_id": EXPECTED_REPLY_TARGETS[name] if action in {"reply", "followup", "research"} else None,
    }
    if expected["target_message_id"]:
        expected["understanding"] = {
            "purpose": "根据当前群友的发言参与交流",
            "context_message_ids": [expected["target_message_id"]], "information_needs": [],
            "participation": {"conversation": "群友邀请机器人参与当前话题",
                "role": "continuing" if action == "followup" else "addressed",
                "entry_point": "回应原话中的邀请", "context_sufficient": True},
        }
    bridge = HostBridge.__new__(HostBridge)
    bridge.bot = SimpleNamespace(adapter=SimpleNamespace(bot_account_id="10000"))
    bridge._complete = AsyncMock(return_value=json.dumps(expected))

    actual = await bridge.judge(context)

    bridge._complete.assert_awaited_once()
    system, payload = bridge._complete.await_args.args
    assert isinstance(system, str) and system
    assert payload == {**{key: value for key, value in asdict(context).items()
                         if key not in {"persona", "default_style", "examples", "default_examples"}}, "bot_account_id": "10000"}
    assert context.persona in system
    assert "persona" not in payload and "default_style" not in payload
    decision = validate_decision(actual, context)
    assert decision is not None and decision.action in allowed_actions
    assert decision.target_message_id == expected["target_message_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("meaning", [None,
    {"purpose": "回答问题", "context_message_ids": ["m1"], "information_needs": []},
    {"purpose": "不知在说谁", "context_message_ids": ["m1"], "information_needs": [],
     "participation": {"conversation": "两人在聊缺少前情的人", "role": "joining",
                       "entry_point": "询问他们在说谁", "context_sufficient": False}},
])
@pytest.mark.parametrize("action", ["reply", "research", "task_request"])
async def test_live_judge_requires_a_conversation_assessment_before_any_outgoing_action(meaning, action):
    context = _context("active", [_message("m1", "user-a", "他又那样了")])
    bridge = HostBridge.__new__(HostBridge)
    bridge.bot = SimpleNamespace(adapter=SimpleNamespace(bot_account_id="10000"))
    bridge._complete = AsyncMock(return_value=json.dumps({
        "action": action, "target_message_id": "m1", "understanding": meaning}))
    assert await bridge.judge(context) == {"action": "silence", "target_message_id": None}
    bridge._complete.assert_awaited_once()
