"""Only opaque query tokens cross into query variables; destinations stay private."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import asyncio
import json
import time
import uuid

from langbot_plugin.api.entities.builtin.platform.message import Source

from pkg.errors import VisionError

KEY = "qq_group_context.query.v1"
TTL = 900


def identity(value, *, signed=False):
    if type(value) not in {str, int}:
        raise ValueError("Invalid platform identity")
    text = str(value)
    digits = text[1:] if signed and text.startswith("-") else text
    if not digits.isascii() or not digits.isdecimal() or len(digits) > 20:
        raise ValueError("Invalid platform identity")
    if int(text) == 0 or (not signed and int(text) < 0):
        raise ValueError("Invalid platform identity")
    return str(int(text))


def owner(plugin):
    getter = getattr(plugin, "get_installation_binding", None)
    binding = getter() if callable(getter) else None
    if binding is None:
        return plugin.contexts.owner
    return tuple(
        getattr(binding, key)
        for key in (
            "instance_uuid",
            "workspace_uuid",
            "installation_uuid",
            "runtime_revision",
        )
    )


@dataclass
class QueryContext:
    owner: object
    query_id: int
    query_uuid: str | None
    bot: str
    group: str
    sender: str
    anchor: str
    timestamp: int
    created: float
    history: dict | None = None
    image_urls: dict[str, str] = field(default_factory=dict)
    shown_images: set[str] = field(default_factory=set)
    vision_used: bool = False
    vision_inflight: bool = False
    image_read_attempts: int = 0
    active: bool = False
    history_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ContextStore:
    def __init__(self, clock=time.monotonic):
        self.owner = uuid.uuid4().hex
        self.clock = clock
        self.entries = OrderedDict()

    def clear(self):
        self.entries.clear()
        self.owner = uuid.uuid4().hex

    def prune(self):
        for key, value in list(self.entries.items()):
            if self.clock() - value.created >= TTL:
                self.entries.pop(key, None)

    def save_history(self, item, history):
        size = len(json.dumps(history, ensure_ascii=False).encode("utf-8"))
        if size > 1024 * 1024:
            raise VisionError("近期群聊上下文过大，请引用需要讨论的消息。")
        saved = [
            (key, entry)
            for key, entry in self.entries.items()
            if entry is not item and entry.history is not None
        ]
        # Bound full snapshots separately from the small pre-trigger anchors.
        while len(saved) >= 16:
            key, _ = saved.pop(0)
            self.entries.pop(key, None)
        item.history = history

    def capture(self, plugin, event_ctx, bot):
        event = event_ctx.event
        kind = getattr(event.launcher_type, "value", event.launcher_type)
        if kind != "group":
            raise ValueError("Group only")
        source = next((x for x in event.message_chain if isinstance(x, Source)), None)
        if source is None or not source.time:
            raise ValueError("Missing trusted message anchor")
        getter = getattr(plugin, "get_installation_binding", None)
        binding = getter() if callable(getter) else None
        if binding:
            for key in ("instance_uuid", "workspace_uuid", "placement_generation"):
                actual = getattr(event_ctx, key, None)
                if actual is not None and actual != getattr(binding, key, None):
                    raise ValueError("Mismatched installation")
        item = QueryContext(
            owner(plugin),
            event_ctx.query_id,
            getattr(event_ctx, "query_uuid", None),
            str(bot),
            identity(event.launcher_id),
            identity(event.sender_id),
            identity(source.id, signed=True),
            int(source.time.timestamp()),
            self.clock(),
        )
        self.prune()
        while len(self.entries) >= 256:
            victim = next(
                (key for key, value in self.entries.items() if not value.active),
                next(iter(self.entries)),
            )
            self.entries.pop(victim)
        token = uuid.uuid4().hex
        self.entries[token] = item
        return token

    def get(self, plugin, variables, query_id, query_uuid):
        self.prune()
        token = variables.get(KEY)
        item = self.entries.get(token) if isinstance(token, str) else None
        if (
            item is None
            or item.owner != owner(plugin)
            or item.query_id != query_id
            or item.query_uuid != query_uuid
        ):
            raise VisionError("当前群聊上下文不可用，请在原群重新 @ 我提出问题。")
        for key, expected in (
            ("launcher_type", "group"),
            ("launcher_id", item.group),
            ("sender_id", item.sender),
        ):
            if key in variables and str(variables[key]) != expected:
                raise VisionError("群聊上下文与当前请求不匹配，请重新提问。")
        item.active = True
        return item
