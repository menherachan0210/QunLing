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
_QUESTION = re.compile(r"[?？]|(?:什么|啥|谁|哪|怎么|怎样|为何|为什么|咋|多少|是否|吗|么|能不能|是不是)|不[。！! ]*$", re.I)
_DEICTIC = re.compile(r"(?:这|那)(?:个|张|部|本|篇|段|句|里|些|是|啥|什么)|刚才|前面|上面|其中|里面")
_TOPIC_PROPERTY = re.compile(r"^(?:那[么，, ]*)?(?:作者|书名|名字|名称|出处|标题|角色|主角|画师|原作|导演|这是谁|叫什么)")
_NEW_TOPIC = re.compile(r"^(?:换(?:个|一个)?话题|换个问题|说点别的|不聊这个|先不说这个|另外问|另外一个问题|话说|顺便问)")
_REMINDER = re.compile(
    r"(?:还没|还不|怎么(?:还)?(?:不|没)|为什么不|为啥不|咋不|非得|非要|一定要|必须).{0,32}(?:回|答|说话|@|艾特)|"
    r"(?:回|答).{0,12}(?:刚才|前面|上面|那个问题|我的问题|他的问题|她的问题)|"
    r"(?:刚才|之前|前面|上面|问的|问题).{0,30}(?:没(?:有)?(?:回)?答|未(?:回)?答|漏回|漏答|呢[？?]?$)|"
    r"^(?:你)?(?:倒是|先|也|快|赶紧|麻烦|请)?(?:回答|回复|回)(?:一下)?"
    r"(?:他们|她们|他|她|人家)(?:啊|呀|吧|呢|嘛|一下|的问题|[，,。!?？!！ ]|$)")
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


def _same_scope(first, second):
    return first.bot_id == second.bot_id and first.group_id == second.group_id


def _ordinary(message):
    return (not message.is_bot and not message.explicit_request
            and not any(str(value) != message.bot_id for value in getattr(message, "mentions", ())))


def _real_reply(message):
    return (message.is_bot and bool(message.message_id)
            and not str(message.message_id).startswith("bot-observed-")
            and (message.text.strip() or getattr(message, "has_media", False)))


def _continuation_anchor(message, previous, roots, root):
    """Select only an earlier real bot receipt in this public conversation."""
    if not _ordinary(message) or not message.text.strip() or _NEW_TOPIC.match(message.text.strip()):
        return None
    recent = [older for older in previous[-FOCUS_MESSAGES:]
              if _same_scope(older, message) and _recent_predecessor(older, message)
              and (older.text.strip() or getattr(older, "has_media", False))]
    replies = [older for older in recent if _real_reply(older)]
    if not replies:
        return None
    quoted = next((older for older in recent if older.message_id == message.reply_to), None)
    if message.reply_to:
        if quoted is None:
            return None
        if _real_reply(quoted):
            return quoted.message_id
        quoted_root = roots[quoted.message_id]
        return next((older.message_id for older in reversed(replies)
                     if older.message_id == quoted.message_id or roots[older.message_id] == quoted_root), None)
    # Bare human stickers add no topic words and do not interrupt an ongoing
    # public exchange. A substantive new human topic still does.
    nearest = next(older for older in reversed(recent) if older.is_bot or older.text.strip())
    nearest_root = roots[nearest.message_id]
    reply_roots = {roots[older.message_id] for older in replies}
    image_roots = {roots[older.message_id] for older in recent
                   if getattr(older, "has_media", False) and roots[older.message_id] in reply_roots}
    text = message.text.strip()
    pointed_question = (len(text) <= 160 and bool(_QUESTION.search(text))
                        and bool(_DEICTIC.search(text) or _TOPIC_PROPERTY.match(text)))
    # An explicit topical reference may distinguish the comic from an
    # unrelated delivered sticker. Merely saying "author?" cannot choose
    # between two comics. Compare observed topic words, never infer pixels.
    named_root = None
    if pointed_question and _DEICTIC.search(text):
        query_terms = _terms(_QUESTION.sub(" ", _DEICTIC.sub(" ", text)), ())
        matching = [candidate for candidate in reply_roots if query_terms & _terms(
            " ".join(older.text for older in recent if roots[older.message_id] == candidate), ())]
        if len(matching) == 1:
            named_root = matching[0]
    if named_root is not None:
        nearest_root = named_root
    reply = next((older for older in reversed(replies) if roots[older.message_id] == nearest_root), None)
    if reply is None or (nearest.explicit_request and named_root is None):
        return None  # A separate human topic/task is not an invitation to the bot.
    quoted_lineage = any(not older.is_bot and older.reply_to in roots
                         and roots[older.message_id] == nearest_root for older in recent)
    if len(image_roots) > 1 and not quoted_lineage and named_root is None:
        return None
    # Existing substantial topic overlap/correction also supplies evidence.
    # A fresh direct question such as "you know which comic this is?" need not
    # repeat two words from the previous answer or come from its recipient.
    if root == nearest_root or pointed_question:
        return reply.message_id
    return None


def _relations(messages, ignored_terms):
    roots, anchors, previous = {}, {}, []
    terms = {}
    for message in tuple(messages)[-MAX_MESSAGES:]:
        key = message.message_id
        scoped = [older for older in previous if _same_scope(older, message)]
        quoted = next((older for older in reversed(scoped) if older.message_id == message.reply_to), None)
        root = roots.get(quoted.message_id) if quoted is not None else None
        if _ordinary(message) and quoted is not None and not _recent_predecessor(quoted, message):
            root = None
        current = _terms(message.text, ignored_terms)
        new_topic = bool(_NEW_TOPIC.match(message.text.strip()))
        if new_topic and not message.is_bot:
            root = None
        if root is None and _ordinary(message) and not new_topic:
            image_correction = len(message.text) <= 80 and bool(_IMAGE_CORRECTION.match(message.text.strip()))
            correction = image_correction or (len(message.text) <= 80 and bool(_CORRECTION.match(message.text.strip())))
            # A terse correction can continue a real bot answer, including a
            # peer's correction of that answer. Never jump over a separate
            # human topic merely because a bot spoke recently in this group.
            if correction:
                recent = [older for older in scoped[-FOCUS_MESSAGES:]
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
            for older in reversed(scoped[-FOCUS_MESSAGES:]):
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
        anchor = _continuation_anchor(message, scoped, roots, root)
        if anchor is not None:
            anchors[key] = anchor
            root = roots[anchor]
        roots[key] = root or key
        terms[key] = current
        previous.append(message)
    return roots, anchors


def continuation_anchors(messages, *, ignored_terms=()):
    """Map ordinary public continuations to real prior bot-reply message IDs.

    Any group member may continue a shared topic. These associations are only
    contextual evidence; they cannot confer another person's tool/file access.
    """
    return _relations(messages, ignored_terms)[1]


def topic_roots(messages, *, ignored_terms=()):
    """Link exact replies, grounded public continuations or substantial overlap."""
    return _relations(messages, ignored_terms)[0]


def unresolved_question_candidates(messages, *, current_text, participant_id, reply_to=None, limit=3):
    """Return question evidence for a reminder, never claim it is unanswered.

    The caller supplies one authorized frozen group snapshot. Quoted/public
    topic questions may belong to another participant. No execution ownership,
    tool capability or statement that an earlier answer was insufficient is
    inferred. A reminder and its explanatory reply do not hide the question.
    """
    if (not isinstance(current_text, str) or not _REMINDER.search(current_text[:500])
            or _NEW_TOPIC.match(current_text.strip()) or type(limit) is not int or limit < 1):
        return ()
    messages = tuple(messages)[-MAX_MESSAGES:]
    if not messages or len({(item.bot_id, item.group_id) for item in messages}) != 1:
        return ()
    latest = messages[-1]
    recent = [item for item in messages if _recent_predecessor(item, latest)]
    if not recent:
        return ()
    roots, _ = _relations(recent, ())
    by_id = {item.message_id: item for item in recent}
    meta = set()
    for item in recent:
        if ((not item.is_bot and _REMINDER.search(item.text[:500]))
                or (item.is_bot and item.reply_to in meta)):
            meta.add(item.message_id)
    substantive = [item for item in recent if item.message_id not in meta and item.text.strip()]
    if not substantive:
        return ()
    quoted = by_id.get(str(reply_to)) if reply_to is not None else None
    if reply_to is not None and quoted is None:
        return ()
    focus = quoted if quoted is not None and quoted.message_id not in meta else substantive[-1]
    focus_root = roots[focus.message_id]
    if _NEW_TOPIC.match(focus.text.strip()):
        return ()
    questions = []
    for item in reversed(substantive):
        if (item.is_bot or roots[item.message_id] != focus_root
                or (not item.explicit_request
                    and any(str(value) != item.bot_id for value in getattr(item, "mentions", ())))
                or not (_QUESTION.search(item.text) or item.explicit_request)):
            continue
        related = [answer for answer in recent if _real_reply(answer)
                   and roots[answer.message_id] == focus_root]
        # Unanchored private appeals only refer to this participant's own
        # question. A real public reply or exact quote grounds peer reminders.
        if (str(item.sender_id) != str(participant_id) and not related
                and not (quoted is not None and roots[quoted.message_id] == focus_root)):
            continue
        questions.append({
            "message_id": item.message_id, "sender_id": item.sender_id, "text": item.text[:800],
            "related_replies": tuple({"message_id": answer.message_id, "text": answer.text[:500]}
                                     for answer in related[-2:]),
        })
        if len(questions) >= min(limit, 3):
            break
    return tuple(questions)


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
