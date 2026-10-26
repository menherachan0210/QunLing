"""Bounded merged-forward reads rooted in an actual current-group message.

Only the host/plugin supplies message identities. Forward IDs are discovered
from those messages; neither tool parameters nor card text can choose one.
"""
from __future__ import annotations

import asyncio
import json
import time
import unicodedata

from qunling.network import validate_public_url

MAX_CARDS = 4
MAX_DEPTH = 2
MAX_MESSAGES = 150
MAX_IMAGES = 32
MAX_TEXT = 80000
MAX_SEGMENTS = 64
TOTAL_TIMEOUT = 15
CALL_TIMEOUT = 5


def _id(value):
    if type(value) not in {str, int}:
        raise ValueError("Invalid message identity")
    text = str(value)
    digits = text[1:] if text.startswith("-") else text
    if not digits.isascii() or not digits.isdecimal() or len(digits) > 20 or int(text) == 0:
        raise ValueError("Invalid message identity")
    return str(int(text))


def _text(value, limit):
    if not isinstance(value, str):
        return ""
    return "".join(c if unicodedata.category(c) not in {"Cc", "Cf", "Cs", "Zl", "Zp"}
                   or c in "\n\t" else " " for c in value[:limit])


def _resource(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 1024
            or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "://" in value):
        return None
    return value


def _segments(value):
    return value if isinstance(value, list) else [{"type": "text", "data": {"text": value}}] if isinstance(value, str) else []


def _card_data(segment):
    if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
        return None
    data = segment["data"]
    if segment.get("type") == "forward":
        resource = _resource(data.get("id"))
        content = data.get("content")
        if resource or isinstance(content, list):
            return resource, content if isinstance(content, list) else None
    if segment.get("type") == "json":
        raw = data.get("data")
        if isinstance(raw, str):
            if len(raw) > 16384:
                return None
            try:
                raw = json.loads(raw)
            except (ValueError, RecursionError):
                return None
        if not isinstance(raw, dict) or raw.get("app") != "com.tencent.multimsg":
            return None
        meta = raw.get("meta")
        detail = meta.get("detail") if isinstance(meta, dict) else None
        resource = _resource(detail.get("resid")) if isinstance(detail, dict) else None
        if resource:
            return resource, None
    return None


async def _call(bot, action, params, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    async with asyncio.timeout(min(CALL_TIMEOUT, remaining)):
        result = await bot.call_action(action, **params)
    if isinstance(result, dict) and "status" in result and "data" in result:
        if result["status"] != "ok" or result.get("retcode", 0) != 0:
            raise ValueError("Platform read failed")
        result = result["data"]
    return result


def is_forward_card(segment):
    """Classify only; do not fetch card contents while converting ingress."""
    return _card_data(segment) is not None


def _checked_message(raw, group, message_id, before, window, *, anchor=False):
    if not isinstance(raw, dict) or raw.get("message_type") != "group":
        raise ValueError("Message is not a group message")
    if _id(raw.get("group_id")) != group or _id(raw.get("message_id")) != message_id:
        raise ValueError("Message belongs to another source")
    stamp = raw.get("time")
    if type(stamp) is not int or not before - window <= stamp <= before:
        raise ValueError("Message is outside the request window")
    if anchor and before - stamp > 60:
        raise ValueError("Anchor time mismatch")
    return _segments(raw.get("message", raw.get("content")))


class _Snapshot:
    def __init__(self):
        self.cards, self.queue, self.seen, self.people = [], [], {}, {}
        self.messages = self.images = self.text = 0
        self.truncated = False
        self.unavailable = 0
        self.timeout = False

    def card(self, data, origin, parent=None, depth=0, source_message_id=None):
        resource, inline = data
        key = resource if resource else f"inline:{id(inline)}"
        if key in self.seen:
            return self.seen[key]
        if depth > MAX_DEPTH or len(self.cards) >= MAX_CARDS:
            self.truncated = True
            return None
        ref = f"card_{len(self.cards) + 1}"
        item = {"card_ref": ref, "parent_card_ref": parent, "origin": origin,
                "messages": [], "truncated": False, "source_message_id": source_message_id}
        self.seen[key] = ref
        self.cards.append(item)
        self.queue.append((item, resource, inline, depth))
        return ref

    def parse(self, item, nodes, depth):
        if len(nodes) > MAX_MESSAGES - self.messages:
            item["truncated"] = self.truncated = True
        for raw in nodes[:max(0, MAX_MESSAGES - self.messages)]:
            if not isinstance(raw, dict):
                item["truncated"] = self.truncated = True
                continue
            if raw.get("type") == "node":
                raw = raw.get("data")
                if not isinstance(raw, dict):
                    item["truncated"] = self.truncated = True
                    continue
            sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
            name = _text(sender.get("card") or sender.get("nickname") or raw.get("nickname") or raw.get("name"), 80)
            person_value = sender.get("user_id") or raw.get("uin") or raw.get("user_id") or name
            person = _text(person_value, 100) if isinstance(person_value, str) else str(person_value) if type(person_value) is int else name
            if not person:
                person = f"unknown:{item['card_ref']}:{len(item['messages'])}"
            speaker = self.people.setdefault(person, f"forward_speaker_{len(self.people) + 1}")
            stamp = raw.get("time")
            stamp = stamp if type(stamp) is int and 0 <= stamp <= 2**40 else None
            message = {"index": len(item["messages"]) + 1, "speaker": speaker,
                       "sender_name": name or "未提供名字", "time": stamp,
                       "text": "", "images": [], "nested_card_refs": []}
            segments = _segments(raw.get("message", raw.get("content")))
            if len(segments) > MAX_SEGMENTS:
                message["truncated"] = item["truncated"] = self.truncated = True
            for index, segment in enumerate(segments[:MAX_SEGMENTS]):
                if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
                    continue
                data, kind = segment["data"], segment.get("type")
                card = _card_data(segment)
                if card:
                    nested = self.card(card, "nested", item["card_ref"], depth + 1,
                                       item["source_message_id"])
                    if nested:
                        message["nested_card_refs"].append(nested)
                    else:
                        message["truncated"] = item["truncated"] = True
                elif kind == "text":
                    value = data.get("text")
                    budget = min(4000 - len(message["text"]), MAX_TEXT - self.text)
                    text = _text(value, max(0, budget))
                    if isinstance(value, str) and len(value) > budget:
                        message["truncated"] = item["truncated"] = self.truncated = True
                    message["text"] += text
                    self.text += len(text)
                elif kind == "image":
                    if self.images >= MAX_IMAGES:
                        message["truncated"] = item["truncated"] = self.truncated = True
                        continue
                    try:
                        url = validate_public_url(data.get("url") or data.get("file"))
                    except Exception:
                        message["unavailable_images"] = message.get("unavailable_images", 0) + 1
                        continue
                    message["images"].append({"index": index, "url": url})
                    self.images += 1
                elif kind in {"file", "record", "video", "json", "xml", "face", "at"}:
                    marker = {"file": "[附件]", "record": "[语音]", "video": "[视频]", "json": "[卡片]",
                              "xml": "[卡片]", "face": "[表情]", "at": "[提及用户]"}[kind]
                    if len(message["text"]) + len(marker) <= 4000 and self.text + len(marker) <= MAX_TEXT:
                        message["text"] += marker
                        self.text += len(marker)
                    else:
                        message["truncated"] = item["truncated"] = self.truncated = True
            item["messages"].append(message)
            self.messages += 1

    def result(self):
        return {"cards": self.cards, "truncated": self.truncated, "unavailable_cards": self.unavailable,
                "stop_reason": "timeout" if self.timeout else "partial_failure" if self.unavailable
                               else "budget" if self.truncated else "complete"}


async def read_forward_context(bot, params, *, after_time=0):
    """Discover current/quoted (or selected history) cards and parse bounded nodes.

    URLs stay internal to the plugin. Sender names inside a merged forward are
    labels, not proof of identity or membership in the receiving group.
    """
    required = {"group_id", "anchor_message_id", "before_time"}
    if (not isinstance(params, dict) or not required <= set(params)
            or set(params) - required - {"source_message_id", "window_seconds"}):
        raise ValueError("Invalid forward context parameters")
    group, anchor = _id(params["group_id"]), _id(params["anchor_message_id"])
    if group.startswith("-"):
        raise ValueError("Invalid group identity")
    before, window = params["before_time"], params.get("window_seconds", 86400)
    if type(before) is not int or not int(time.time()) - 900 <= before <= int(time.time()) + 60:
        raise ValueError("Invalid forward context time")
    if type(window) is not int or not 3600 <= window <= 604800:
        raise ValueError("Invalid forward context window")
    if after_time:
        window = min(window, before - after_time)
        if window <= 0:
            return {"cards": [], "truncated": False, "unavailable_cards": 0, "stop_reason": "memory_reset"}
    source = _id(params["source_message_id"]) if "source_message_id" in params else None
    deadline = time.monotonic() + TOTAL_TIMEOUT
    snapshot = _Snapshot()
    selection = "explicit_history" if source else "current_or_quoted"
    try:
        raw = await _call(bot, "get_msg", {"message_id": int(anchor)}, deadline)
        segments = _checked_message(raw, group, anchor, before, window, anchor=True)
        anchor_time = raw["time"]
        if after_time:
            window = min(window, anchor_time - after_time)
        if source and source != anchor:
            raw_source = await _call(bot, "get_msg", {"message_id": int(source)}, deadline)
            segments = _checked_message(raw_source, group, source, anchor_time, window)
        sources = [(segments, "history" if source and source != anchor else "current", source or anchor)]
        # Only a reply segment returned by the actual source may choose a quote.
        reply = next((segment.get("data", {}).get("id") for segment in segments[:MAX_SEGMENTS]
                      if isinstance(segment, dict) and segment.get("type") == "reply"
                      and isinstance(segment.get("data"), dict)), None)
        if reply is not None:
            try:
                reply = _id(reply)
                quoted = await _call(bot, "get_msg", {"message_id": int(reply)}, deadline)
                quoted_segments = _checked_message(quoted, group, reply, anchor_time, window)
                sources.append((quoted_segments, "quoted", reply))
            except Exception as exc:
                snapshot.unavailable += 1
                snapshot.timeout |= isinstance(exc, TimeoutError)
        for segments, origin, source_id in sources:
            if len(segments) > MAX_SEGMENTS:
                snapshot.truncated = True
            for segment in segments[:MAX_SEGMENTS]:
                data = _card_data(segment)
                if data:
                    snapshot.card(data, origin, source_message_id=source_id)
    except Exception:
        raise RuntimeError("无法确认当前群的转发来源消息，可能已过期或暂时不可读取。") from None
    if not snapshot.cards and source is None:
        selection = "recent_candidates"
        try:
            history = await _call(bot, "get_group_msg_history", {
                "group_id": group, "message_seq": anchor, "count": 20,
                "reverse_order": True, "disable_get_url": False, "parse_mult_msg": False,
            }, deadline)
            rows = history.get("messages") if isinstance(history, dict) else None
            if not isinstance(rows, list):
                raise ValueError("Invalid recent forward candidates")
            recent = []
            for row in rows[:20]:
                try:
                    source_id = _id(row.get("message_id")) if isinstance(row, dict) else None
                    segments = _checked_message(row, group, source_id, anchor_time, window)
                except (ValueError, TypeError):
                    continue
                recent.append((row["time"], source_id, segments))
            recent.sort(key=lambda item: item[0], reverse=True)
            for _, source_id, segments in recent:
                for segment in segments[:MAX_SEGMENTS]:
                    data = _card_data(segment)
                    if data:
                        snapshot.card(data, "recent", source_message_id=source_id)
                    if len(snapshot.cards) >= 2:
                        break
                if len(snapshot.cards) >= 2:
                    break
        except Exception as exc:
            snapshot.unavailable += 1
            snapshot.timeout |= isinstance(exc, TimeoutError)
    index = 0
    while index < len(snapshot.queue):
        item, resource, inline, depth = snapshot.queue[index]
        index += 1
        try:
            if snapshot.messages >= MAX_MESSAGES:
                item["truncated"] = snapshot.truncated = True
                continue
            if inline is None:
                response = await _call(bot, "get_forward_msg", {"message_id": resource}, deadline)
                inline = response.get("messages") if isinstance(response, dict) else None
            if not isinstance(inline, list):
                raise ValueError("Invalid merged forward")
            snapshot.parse(item, inline, depth)
        except Exception as exc:
            item["error"] = "timeout" if isinstance(exc, TimeoutError) else "unavailable"
            snapshot.timeout |= isinstance(exc, TimeoutError)
            snapshot.unavailable += 1
    result = snapshot.result()
    result["selection"] = selection
    if selection == "recent_candidates" and sum(item["parent_card_ref"] is None for item in snapshot.cards) > 1:
        result["instruction"] = "最近消息中有多个转发候选；请结合用户明确指代选择，不能确定时询问要看哪一张，不要擅自合并。"
    return result
