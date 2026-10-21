"""Bounded public group evidence, independent of tool/session ownership.

Relations are conservative retrieval hints, never execution permissions. Every
returned identifier comes from the caller's frozen, already-authorized snapshot.
"""
from __future__ import annotations

import hashlib
import math
import re

WINDOW_SECONDS = 20 * 60
MAX_MESSAGES = 120
MAX_CHARACTERS = 12000
FOCUS_SECONDS = 120
FOCUS_MESSAGES = 24

_CONTINUATION = re.compile(r"^(那|所以|这样|这个|这点|这么|确实|对[，。！! ]|嗯|我也|不过|可是|而且|还有|也就是说|then\b|so\b|but\b|also\b)", re.I)
_CORRECTION = re.compile(
    r"^(?:那[，, ]*)?(?:不对|不太对|不是这样|不(?:太|怎么|完全)?像|完全不像|"
    r"一点(?:也|都)?不像|根本不像|搞错了|弄错了|看错了|错了|不一样|"
    r"你(?:这|是不是)?(?:搞错|弄错|看错)|你确定|确定吗|wrong\b|not really\b)", re.I)
_IMAGE_CORRECTION = re.compile(
    r"^(?:你把|怎么把|把)?(?:"
    r"女(?:性|生|孩)?(?:角色)?(?:变(?:成)?|画成|改成)了?男(?:性|生|孩)?|"
    r"男(?:性|生|孩)?(?:角色)?(?:变(?:成)?|画成|改成)了?女(?:性|生|孩)?|"
    r"短发(?:变(?:成)?|画成|改成)了?长发|长发(?:变(?:成)?|画成|改成)了?短发|"
    r"(?:发色|瞳色|头发颜色|眼睛颜色|猫耳|帽子|发型|衣服)(?:画错了?|不对|错了|不一样|没了|不见了))")
_GENERIC = {"大家", "怎么", "什么", "这个", "那个", "一下", "觉得", "可以", "我们", "你们", "the", "and", "that", "this", "what"}


def _terms(text, ignored):
    text = str(text).casefold()
    for value in ignored:
        if value:
            text = text.replace(value.casefold(), " ")
    terms = set(re.findall(r"[a-z0-9_]{3,}", text))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(run[index:index + 2] for index in range(len(run) - 1))
    return terms - _GENERIC


def _recent_predecessor(earlier, later):
    elapsed = later.timestamp - earlier.timestamp
    if 0 <= elapsed <= FOCUS_SECONDS:
        return True
    # The caller only supplies an already-observed predecessor. Host receipts
    # retain fractions, while incoming QQ timestamps may be rounded down. This
    # one-second case must not make a delivered image disappear from its reply.
    return (elapsed < 0 and earlier.is_bot and math.isfinite(earlier.timestamp)
            and math.isfinite(later.timestamp) and later.timestamp == math.floor(later.timestamp)
            and math.floor(earlier.timestamp) == later.timestamp)


def topic_roots(messages, *, ignored_terms=()):
    """Link exact replies, clear continuations or substantial lexical overlap.

    Time proximity alone never joins two requests. Explicit requests always own
    their root unless they explicitly quote an existing message.
    """
    roots, previous = {}, []
    terms = {}
    for message in messages:
        key = message.message_id
        root = roots.get(message.reply_to)
        current = _terms(message.text, ignored_terms)
        if root is None and not message.explicit_request and not message.is_bot:
            image_correction = len(message.text) <= 80 and bool(_IMAGE_CORRECTION.match(message.text.strip()))
            correction = image_correction or (len(message.text) <= 80 and bool(_CORRECTION.match(message.text.strip())))
            # A terse correction can continue a real bot answer, including a
            # peer's correction of that answer. Never jump over a separate
            # human topic merely because a bot spoke recently in this group.
            if correction:
                recent = [older for older in previous[-FOCUS_MESSAGES:]
                          if _recent_predecessor(older, message)
                          and older.text.strip()]
                if recent:
                    nearest = recent[-1]
                    previous_root = roots[nearest.message_id]
                    image_roots = {roots[older.message_id] for older in recent
                                   if older.is_bot and getattr(older, "has_media", False)}
                    quoted_lineage = any(not older.is_bot and older.reply_to in roots
                                         and roots[older.message_id] == previous_root for older in recent)
                    if not nearest.explicit_request and any(
                            older.is_bot and roots[older.message_id] == previous_root for older in recent
                    ) and (len(image_roots) <= 1 or quoted_lineage) and (
                            not image_correction or previous_root in image_roots):
                        root = previous_root
            for older in reversed(previous[-FOCUS_MESSAGES:]):
                # Feature-change complaints need a real delivered image in
                # this lineage; lexical overlap must not bypass ambiguity.
                if root is not None or image_correction:
                    break
                if not _recent_predecessor(older, message):
                    continue
                if older.explicit_request or not older.text.strip():
                    continue
                overlap = current & terms[older.message_id]
                similar = len(overlap) >= 2 and len(overlap) / max(1, min(len(current), len(terms[older.message_id]))) >= .35
                continuation = (not correction and len(message.text) <= 120
                                and _CONTINUATION.match(message.text.strip()))
                if similar or continuation:
                    root = roots[older.message_id]
                    break
        roots[key] = root or key
        terms[key] = current
        previous.append(message)
    return roots


def metadata(bot_id, group_id, messages, *, participant_id="", reply_to=None, ignored_terms=()):
    """Build shared evidence from a frozen snapshot, with exact source lineage."""
    scoped = tuple(message for message in messages if message.bot_id == bot_id and message.group_id == group_id)
    if not scoped:
        return {"topic_id": "", "topic_message_ids": (), "participant_ids": (), "focus_message_ids": ()}
    roots = topic_roots(scoped, ignored_terms=ignored_terms)
    anchor = next((message for message in reversed(scoped) if not message.explicit_request), scoped[-1])
    root = roots.get(reply_to, roots[anchor.message_id])
    topic = {message.message_id for message in scoped if roots[message.message_id] == root}
    by_id = {message.message_id: message for message in scoped}
    # A quoted public answer carries its original question, not its author's
    # other tasks. Bound traversal even if an invalid source contains a cycle.
    frontier = list(topic | ({reply_to} if reply_to in by_id else set()))
    for _ in range(8):
        following = {by_id[key].reply_to for key in frontier if key in by_id and by_id[key].reply_to in by_id} - topic
        if not following:
            break
        topic.update(following)
        frontier = list(following)
    latest = scoped[-1]
    public = [message for message in scoped if not message.explicit_request and _recent_predecessor(message, latest)]
    # Shared discussion remains first-class evidence regardless of speaker.
    focus = topic | {message.message_id for message in public[-FOCUS_MESSAGES:]}
    topic_id = hashlib.sha256((bot_id + "\0" + group_id + "\0" + root).encode()).hexdigest()[:24]
    return {
        "topic_id": topic_id,
        "topic_message_ids": tuple(message.message_id for message in scoped if message.message_id in topic),
        "participant_ids": tuple(dict.fromkeys(message.sender_id for message in scoped if message.message_id in topic and not message.is_bot)),
        "focus_message_ids": tuple(message.message_id for message in scoped if message.message_id in focus),
    }
