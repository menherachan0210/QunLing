"""Bounded, opt-in group participation. No model or platform dependencies.

Each participant has one running batch and a merged successor on the host's
asyncio loop. Public discussion can share evidence and claim related candidates
across participants; task authority remains separate. Uncertain sends are never retried.
"""
from __future__ import annotations

import asyncio
from collections import deque
import contextvars
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
import hashlib
import json
import math
import os
from pathlib import Path
import time
import unicodedata
from typing import Awaitable, Callable, Mapping
from zoneinfo import ZoneInfo

from qunling.group_context import MAX_CHARACTERS, MAX_MESSAGES, WINDOW_SECONDS, metadata, topic_roots


@dataclass(frozen=True)
class GroupPolicy:
    mode: str = "natural"
    persona: str = ""
    quiet_start: str | None = None
    quiet_end: str | None = None
    timezone: str = "Asia/Shanghai"
    cooldown_seconds: int | None = None
    judgments_per_hour: int | None = None
    judgments_per_day: int | None = None
    display_name: str = "群灵"
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class EngineConfig:
    revision: str
    enabled: bool
    groups: Mapping[str, GroupPolicy]


@dataclass(frozen=True)
class Message:
    bot_id: str
    group_id: str
    message_id: str
    sender_id: str
    text: str
    timestamp: float
    reply_to: str | None = None
    explicit_request: bool = False
    sender_name: str = ""
    mentions: tuple[str, ...] = ()
    is_bot: bool = False
    has_media: bool = False
    reply_recipient_id: str | None = None


@dataclass(frozen=True)
class Decision:
    action: str
    target_message_id: str | None
    covered_message_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConversationContext:
    bot_id: str
    group_id: str
    mode: str
    persona: str
    messages: tuple[Message, ...]
    followup_candidate_ids: tuple[str, ...]
    last_bot_text: str
    last_bot_message_id: str | None
    last_bot_at: float | None
    display_name: str = "群灵"
    addressed_candidate_ids: tuple[str, ...] = ()
    reply_candidate_ids: tuple[str, ...] = ()
    conversation_id: str = ""
    participant_id: str = ""
    focus_message_ids: tuple[str, ...] = ()
    topic_id: str = ""
    topic_message_ids: tuple[str, ...] = ()
    participant_ids: tuple[str, ...] = ()
    covered_message_ids: tuple[str, ...] = ()
    continuation_anchors: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class _Mode:
    debounce: float
    cooldown: int
    hourly: int
    daily: int
    reply_hourly: int
    reply_daily: int
    unanswered_backoff: int
    followup_interval: int
    followup_limit: int


_MODES = {
    "quiet": _Mode(6, 120, 60, 600, 12, 120, 300, 15, 4),
    "natural": _Mode(3, 45, 120, 1200, 30, 300, 90, 10, 6),
    "active": _Mode(2, 15, 240, 2400, 60, 600, 30, 5, 8),
}
GLOBAL_JUDGMENTS_PER_DAY = 4800


def _repeated_reply(first: str, second: str) -> bool:
    """Recognize text repetition, never infer that two corrections mean the same.

    Keep numbers, negations, names, question marks and code punctuation intact.
    A changed fact or an additional sentence must survive even when most of a
    long reply is copied. Only harmless spacing/punctuation and a few spoken
    particles may differ; paraphrases deliberately fail open.
    """
    if any(char in first + second for char in "`=<>\\{}[]()"):
        return first.strip() == second.strip() and len(first.strip()) >= 24

    def normalize(value):
        value = " ".join(unicodedata.normalize("NFKC", value).split())
        return "".join(char for index, char in enumerate(value)
                       if char not in ",;!。、" or
                       (char == "," and 0 < index < len(value) - 1
                        and value[index - 1].isascii() and value[index - 1].isalnum()
                        and value[index + 1].isascii() and value[index + 1].isalnum()))

    first, second = normalize(first), normalize(second)
    if min(len(first), len(second)) < 24:
        return False  # Short acknowledgments may legitimately answer two people.
    if first == second:
        return True
    matcher = SequenceMatcher(None, first, second, autojunk=False)
    if matcher.ratio() < .96:
        return False
    changed = "".join(first[a:b] + second[c:d] for tag, a, b, c, d in matcher.get_opcodes()
                      if tag != "equal")
    return bool(changed) and len(changed) <= 3 and set(changed) <= set("啊呀呢吧嗯呃哦哎唉")


@dataclass
class _Lane:
    sender_id: str
    last_seen: float = 0
    explicit: set[str] = field(default_factory=set)
    generation: int = 0
    task: asyncio.Task | None = None
    last_bot_text: str = ""
    last_bot_id: str | None = None
    last_bot_at: float | None = None
    last_recipient: str | None = None
    last_interjection_at: float | None = None
    followup_streak: int = 0
    after_bot_ids: set[str] = field(default_factory=set)
    candidates: set[str] = field(default_factory=set)
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    sending: bool = False
    batch_participants: set[str] = field(default_factory=set)
    handoff_target: str | None = None


@dataclass(frozen=True)
class _BotReply:
    text: str
    at: float
    recipient_id: str | None
    target_id: str | None


@dataclass
class _Group:
    messages: deque = field(default_factory=deque)
    seen: dict[str, float] = field(default_factory=dict)
    judgments: deque = field(default_factory=deque)
    sends: deque = field(default_factory=deque)
    handoffs: deque = field(default_factory=deque)
    last_proactive_at: float | None = None
    last_interjection_at: float | None = None
    last_event: dict | None = None
    lanes: dict[str, _Lane] = field(default_factory=dict)
    reply_index: dict[str, _BotReply] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    model_slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2))
    covered: dict[str, float] = field(default_factory=dict)
    coverage_receipts: dict[str, str] = field(default_factory=dict)
    invalid_targets: set[str] = field(default_factory=set)


def reply_character_limit(context: ConversationContext, decision: Decision) -> int:
    """Explicitly addressed text and genuine followups may need a complete answer."""
    return 2000 if (decision.action == "followup"
                    or decision.target_message_id in context.addressed_candidate_ids) else 400


def validate_decision(value: object, context: ConversationContext) -> Decision | None:
    """Closed schema: unknown fields, stale targets and invented IDs fail closed."""
    if (not isinstance(value, dict) or not {"action", "target_message_id"} <= set(value)
            or set(value) - {"action", "target_message_id", "covered_message_ids"}):
        return None
    action, target = value["action"], value["target_message_id"]
    covered = value.get("covered_message_ids", [])
    if (not isinstance(covered, list) or len(covered) > MAX_MESSAGES
            or any(not isinstance(key, str) for key in covered) or len(set(covered)) != len(covered)):
        return None
    if action not in ("silence", "wait", "reply", "followup", "tool_followup"):
        return None
    if action in ("silence", "wait"):
        return Decision(action, None) if target is None and not covered else None
    if not isinstance(target, str) or not target:
        return None
    eligible = {m.message_id for m in context.messages if m.text.strip() and not m.is_bot and not m.explicit_request
                and not any(item != context.bot_id for item in m.mentions)}
    candidates = context.reply_candidate_ids or tuple(m.message_id for m in context.messages[-1:])
    if target not in eligible or target not in candidates:
        return None
    if (any(key not in eligible or key not in candidates or key in context.covered_message_ids for key in covered)
            or target in context.covered_message_ids or (covered and target not in covered)):
        return None
    if action == "followup" and target not in context.followup_candidate_ids:
        return None
    if action == "tool_followup":
        anchor_id = dict(context.continuation_anchors).get(target)
        anchor = next((message for message in context.messages if message.message_id == anchor_id), None)
        candidate = next((message for message in context.messages if message.message_id == target), None)
        if (not context.reply_candidate_ids or anchor is None or not anchor.is_bot
                or anchor.bot_id != context.bot_id or anchor.group_id != context.group_id
                or str(anchor.message_id).startswith("bot-observed-")
                or not (anchor.text.strip() or anchor.has_media)
                or candidate.bot_id != context.bot_id or candidate.group_id != context.group_id
                or context.messages.index(anchor) >= context.messages.index(candidate)
                or covered not in ([], [target])):
            return None
    if (action == "reply" and context.mode == "quiet"
            and target not in context.addressed_candidate_ids):
        return None
    return Decision(action, target, tuple(covered) or (target,))


class ProactiveEngine:
    """One engine per bot; callers may share a semaphore across bot instances."""

    def __init__(
        self, *, bot_id: str, config: Callable[[], EngineConfig],
        judge: Callable[[ConversationContext], Awaitable[object]],
        generate: Callable[[ConversationContext, Decision], Awaitable[str]],
        send: Callable[[str, str, str], Awaitable[str | None]],
        handoff: Callable[[ConversationContext, Decision], Awaitable[bool]] | None = None,
        clock: Callable[[], float] = time.time,
        semaphore: asyncio.Semaphore | None = None,
        debounce_scale: float = 1.0,
        model_timeout: float = 25.0,
        send_timeout: float = 10.0,
        state_dir: str | Path | None = None,
    ):
        self.bot_id = str(bot_id)
        self._config = config
        self._judge = judge
        self._generate = generate
        self._send = send
        self._handoff = handoff
        self._clock = clock
        self._semaphore = semaphore if semaphore is not None else asyncio.Semaphore(2)
        self._debounce_scale = max(0.0, debounce_scale)
        self._model_timeout = model_timeout
        self._send_timeout = send_timeout
        self._groups: dict[str, _Group] = {}
        self._tasks: set[asyncio.Task] = set()
        self._pending: dict[tuple[str, str], tuple[int, str]] = {}
        self._pump_handle: asyncio.Handle | None = None
        self._snapshot: EngineConfig | None = None
        self._closed = False
        self._state_path = Path(state_dir) / "participation-state.json" if state_dir is not None else None
        self._history: dict[str, dict] = {}
        self._global_judgments: deque = deque()
        self._state_ok = True
        self._load_state()

    def _load_state(self) -> None:
        if self._state_path is None:
            return
        try:
            if not self._state_path.exists():
                return
            if self._state_path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("budget state too large")
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
            if value.get("version") != 1 or value.get("bot_id") != self.bot_id:
                raise ValueError("invalid budget state")
            now = self._clock()
            self._global_judgments = deque(self._timestamps(value["global_judgments"], now))
            if not isinstance(value["groups"], dict) or len(value["groups"]) > 2000:
                raise ValueError("invalid group budgets")
            for group_id, row in value["groups"].items():
                self._history[group_id] = {
                    "judgments": self._timestamps(row["judgments"], now),
                    "sends": self._timestamps(row["sends"], now),
                    "handoffs": self._timestamps(row.get("handoffs", []), now),
                    "seen": {str(key): at for key, at in row["seen"].items()
                             if isinstance(at, (int, float)) and math.isfinite(at) and 0 <= now - at < 1200},
                    # Old ledgers cannot distinguish an ignored interjection from a
                    # direct answer. Apply a conservative backoff only on that upgrade.
                    "last_interjection_at": row.get("last_interjection_at", max(row["sends"], default=None)),
                }
        except Exception:
            self._state_ok = False

    @staticmethod
    def _timestamps(values: object, now: float) -> list[float]:
        if not isinstance(values, list) or len(values) > 10000:
            raise ValueError("invalid budget events")
        if any(not isinstance(at, (int, float)) or not math.isfinite(at) for at in values):
            raise ValueError("invalid budget time")
        return sorted(at for at in values if -30 <= now - at < 86400)

    def _restored_state(self, group_id: str) -> _Group:
        row = self._history.get(group_id, {})
        state = _Group(judgments=deque(row.get("judgments", [])),
                       sends=deque(row.get("sends", [])), handoffs=deque(row.get("handoffs", [])),
                       seen=dict(row.get("seen", {})))
        state.last_proactive_at = self._last_attempt(state)
        last_interjection = row.get("last_interjection_at")
        if (isinstance(last_interjection, (int, float)) and math.isfinite(last_interjection)
                and 0 <= self._clock() - last_interjection < 600):
            state.last_interjection_at = last_interjection
        return state

    def _state(self, group_id: str) -> _Group:
        if group_id not in self._groups:
            self._groups[group_id] = self._restored_state(group_id)
        return self._groups[group_id]

    def _persist(self) -> bool:
        if not self._state_ok:
            return False
        now = self._clock()
        while self._global_judgments and now - self._global_judgments[0] >= 86400:
            self._global_judgments.popleft()
        for group_id, state in self._groups.items():
            self._prune(state, now)
            self._history[group_id] = {"judgments": list(state.judgments),
                                       "sends": list(state.sends),
                                       "handoffs": list(state.handoffs),
                                       "last_interjection_at": state.last_interjection_at,
                                       "seen": dict(list(state.seen.items())[-256:])}
        self._history = {
            group_id: {"judgments": self._timestamps(row["judgments"], now),
                       "sends": self._timestamps(row["sends"], now),
                       "handoffs": self._timestamps(row.get("handoffs", []), now),
                       "last_interjection_at": row.get("last_interjection_at"),
                       "seen": {key: at for key, at in row["seen"].items() if now - at < 1200}}
            for group_id, row in self._history.items()
            if row["judgments"] or row["sends"] or row.get("handoffs") or row["seen"]
        }
        if self._state_path is None:
            return self._state_ok
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            value = {"version": 1, "bot_id": self.bot_id, "groups": self._history,
                     "global_judgments": list(self._global_judgments)}
            temporary = self._state_path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self._state_path)
            return self._state_ok
        except Exception:
            self._state_ok = False
            return False

    def _read_config(self) -> EngineConfig | None:
        try:
            result = self._config()
            if not isinstance(result, EngineConfig) or not isinstance(result.revision, str):
                return None
            return result
        except Exception:
            return None

    @staticmethod
    def _policy(snapshot: EngineConfig | None, group_id: str) -> GroupPolicy | None:
        if snapshot is None or snapshot.enabled is not True:
            return None
        policy = snapshot.groups.get(group_id)
        if not isinstance(policy, GroupPolicy) or policy.mode not in _MODES:
            return None
        return policy

    def refresh_configuration(self) -> None:
        """Host calls on configuration changes; denial destroys buffered text."""
        snapshot = self._read_config()
        if snapshot != self._snapshot:
            self._persist()
            self._pending.clear()
            for group_id, state in list(self._groups.items()):
                self._invalidate(state)
                if self._policy(snapshot, group_id) is None:
                    del self._groups[group_id]
            self._snapshot = snapshot

    @staticmethod
    def _invalidate_lane(lane: _Lane, *, cancel_send=True) -> None:
        lane.generation += 1
        lane.candidates.clear()
        lane.wakeup.set()
        if lane.task is not None and not lane.task.done() and (cancel_send or not lane.sending):
            lane.task.cancel()

    def _invalidate(self, state: _Group) -> None:
        for lane in state.lanes.values():
            self._invalidate_lane(lane)

    def _lane(self, state: _Group, sender_id: str) -> _Lane | None:
        if sender_id in state.lanes:
            return state.lanes[sender_id]
        if len(state.lanes) >= 16:
            idle = [(lane.last_seen, key) for key, lane in state.lanes.items()
                    if not lane.explicit and not lane.candidates
                    and (lane.task is None or lane.task.done())]
            if not idle:
                return None
            del state.lanes[min(idle)[1]]
        lane = _Lane(sender_id, last_seen=self._clock())
        state.lanes[sender_id] = lane
        return lane

    def _prune(self, state: _Group, now: float) -> None:
        while state.messages and now - state.messages[0].timestamp > WINDOW_SECONDS:
            state.messages.popleft()
        while len(state.messages) > MAX_MESSAGES or sum(len(m.text) for m in state.messages) > MAX_CHARACTERS:
            state.messages.popleft()
        message_ids = {item.message_id for item in state.messages}
        candidate_ids = {item.message_id for item in state.messages if now - item.timestamp <= 90}
        state.covered = {key: at for key, at in state.covered.items() if key in message_ids and now - at <= WINDOW_SECONDS}
        state.coverage_receipts = {key: receipt for key, receipt in state.coverage_receipts.items()
                                   if key in state.covered and receipt in message_ids}
        state.invalid_targets.intersection_update(message_ids)
        for sender, lane in tuple(state.lanes.items()):
            if lane.last_bot_at is not None and now - lane.last_bot_at > 240:
                lane.last_bot_text = ""
                lane.last_bot_id = None
                lane.last_bot_at = None
                lane.last_recipient = None
                lane.after_bot_ids.clear()
                lane.followup_streak = 0
            lane.after_bot_ids.intersection_update(message_ids)
            lane.candidates.intersection_update(candidate_ids)
            if (now - lane.last_seen > 240 and not lane.explicit and not lane.candidates
                    and (lane.task is None or lane.task.done())):
                del state.lanes[sender]
        state.reply_index = {key: value for key, value in state.reply_index.items()
                             if 0 <= now - value.at <= WINDOW_SECONDS}
        while len(state.reply_index) > MAX_MESSAGES:
            state.reply_index.pop(next(iter(state.reply_index)))
        state.seen = {key: at for key, at in state.seen.items() if now - at < 1200}
        while len(state.seen) > 2000:
            state.seen.pop(next(iter(state.seen)))
        for events in (state.judgments, state.sends, state.handoffs):
            while events and now - events[0] >= 86400:
                events.popleft()

    def _record(self, state: _Group, code: str) -> None:
        # Never put model output, message text, user names or raw exceptions here.
        state.last_event = {"code": code, "at": self._clock()}

    def get_observed_message(self, group_id: str, message_id: str) -> Message | None:
        """Read an actual scoped event, including already-claimed continuations.

        This is evidence lookup, not permission to dispatch it again. Unlike
        resolve_target, it deliberately preserves explicit/claimed events so
        trusted ingress can bind its native event to the observed message.
        """
        self.refresh_configuration()
        if self._closed or self._policy(self._snapshot, group_id) is None:
            return None
        state = self._groups.get(group_id)
        if state is None:
            return None
        self._prune(state, self._clock())
        return next((message for message in state.messages
                     if message.message_id == message_id
                     and message.bot_id == self.bot_id and message.group_id == group_id), None)

    def snapshot(self, group_id: str, *, before_message_id: str | None = None,
                 before_timestamp: float | None = None) -> tuple[Message, ...]:
        """Freeze preceding, scoped short-term context for an explicit request.

        The ingress caller captures this immediately after observing the request.
        A known message ID preserves arrival order even when OneBot timestamps
        have only second precision. With no known ID, the strict time boundary
        deliberately omits ambiguous simultaneous messages.
        """
        self.refresh_configuration()
        if self._policy(self._snapshot, group_id) is None:
            return ()
        state = self._groups.get(group_id)
        if state is None:
            return ()
        self._prune(state, self._clock())
        messages = tuple(state.messages)
        has_timestamp = isinstance(before_timestamp, (int, float)) and math.isfinite(before_timestamp)
        if before_message_id is not None:
            for index, message in enumerate(messages):
                if message.message_id == before_message_id:
                    # QQ may round a request down to whole seconds while a
                    # previously observed bot receipt retains host fractions.
                    # Only that same-second predecessor is exempt from the
                    # time bound; delayed requests still exclude future turns.
                    rounded_anchor = (has_timestamp and message.timestamp == before_timestamp
                                      and before_timestamp == math.floor(before_timestamp))
                    return tuple(item for item in messages[:index]
                                 if not has_timestamp or item.timestamp <= before_timestamp
                                 or (rounded_anchor and item.is_bot
                                     and math.floor(item.timestamp) == before_timestamp))
        if not has_timestamp:
            return ()
        return tuple(message for message in messages if message.timestamp < before_timestamp)

    def on_message(self, message: Message) -> bool:
        """Observe a trusted incoming event without waiting for model execution."""
        if (self._closed or message.bot_id != self.bot_id or message.sender_id == self.bot_id
                or message.is_bot):
            return False
        self.refresh_configuration()
        policy = self._policy(self._snapshot, message.group_id)
        if policy is None:
            return False  # In particular: no buffer, dedupe cache, task or model call.
        now = self._clock()
        if (not isinstance(message.message_id, str) or not 0 < len(message.message_id) <= 256
                or not isinstance(message.sender_id, str) or not 0 < len(message.sender_id) <= 128
                or not isinstance(message.text, str)
                or not isinstance(message.timestamp, (int, float))
                or not math.isfinite(message.timestamp) or not -30 <= now - message.timestamp <= 120):
            return False
        state = self._state(message.group_id)
        self._prune(state, now)
        message_key = hashlib.sha256(message.message_id.encode("utf-8")).hexdigest()
        if message_key in state.seen:
            return False
        state.seen[message_key] = now
        self._record(state, "observed")
        # Event inputs are normalized, but an individual message must not defeat the window cap.
        message = Message(message.bot_id, message.group_id, message.message_id,
                          message.sender_id, message.text[:2000], message.timestamp,
                          message.reply_to, message.explicit_request, message.sender_name[:80],
                          tuple(str(item) for item in message.mentions[:20]), message.is_bot,
                          message.has_media or not bool(message.text.strip()))
        state.messages.append(message)
        self._prune(state, now)
        lane = self._lane(state, message.sender_id)
        if lane is None:
            self._record(state, "conversation_limit")
            return True
        lane.last_seen = now
        if lane.last_bot_at is not None:
            lane.after_bot_ids.add(message.message_id)
        if message.explicit_request:
            self.mark_explicit(message.group_id, message.message_id, message.sender_id)
            return True
        if self._quiet(policy, now) or any(item != self.bot_id for item in message.mentions):
            if self._quiet(policy, now):
                self._record(state, "quiet_hours")
            return True
        if not message.text.strip():
            self._record(state, "media_observed")
            return True
        lane.candidates.add(message.message_id)
        lane.wakeup.set()
        self._queue_lane(message.group_id, lane)
        return True

    def _queue_lane(self, group_id, lane):
        if not lane.candidates or lane.explicit or self._closed:
            return
        key = (group_id, lane.sender_id)
        if key in self._pending or len(self._pending) < 32:
            self._pending[key] = (lane.generation, self._snapshot.revision)
            if lane.task is not None and not lane.task.done():
                self._record(self._groups[group_id], "queued")
            self._schedule_pump()

    def _schedule_pump(self) -> None:
        if self._pump_handle is None and not self._closed:
            self._pump_handle = asyncio.get_running_loop().call_soon(self._pump, context=contextvars.Context())

    def _pump(self) -> None:
        self._pump_handle = None
        self._tasks.difference_update(task for task in tuple(self._tasks) if task.done())
        # Previously full queues are reconsidered when an existing task ends.
        for group_id, state in self._groups.items():
            self._prune(state, self._clock())
            for sender, lane in state.lanes.items():
                key = (group_id, sender)
                if lane.candidates and not lane.explicit and key not in self._pending and len(self._pending) < 32:
                    self._pending[key] = (lane.generation, self._snapshot.revision)
        for key in tuple(self._pending):
            if len(self._tasks) >= 32 or self._closed:
                break
            group_id, sender = key
            state = self._groups.get(group_id)
            lane = state.lanes.get(sender) if state else None
            if lane is None or not lane.candidates or lane.explicit:
                self._pending.pop(key, None)
                continue
            if lane.task is not None and not lane.task.done():
                continue
            generation, revision = self._pending.pop(key)
            if self._current(group_id, sender, generation, revision) is None:
                continue
            task = asyncio.create_task(self._run(group_id, sender, generation, revision), context=contextvars.Context())
            lane.task = task
            self._tasks.add(task)
            task.add_done_callback(self._attempt_done, context=contextvars.Context())

    def _attempt_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not self._closed:
            self._schedule_pump()

    def mark_explicit(self, group_id: str, message_id: str, sender_id: str | None = None) -> None:
        self.refresh_configuration()
        if self._policy(self._snapshot, group_id) is None:
            return
        state = self._state(group_id)
        if sender_id is None:
            sender_id = next((m.sender_id for m in state.messages if m.message_id == str(message_id) and not m.is_bot), None)
        if not isinstance(sender_id, str) or not 0 < len(sender_id) <= 128:
            return  # An unassociated lifecycle callback must never pause the group.
        lane = self._lane(state, sender_id)
        if lane is None or str(message_id) in lane.explicit:
            return
        state.invalid_targets.update(m.message_id for m in state.messages
                                     if m.sender_id == sender_id and not m.is_bot and not m.explicit_request)
        if lane.handoff_target == str(message_id) and lane.task is asyncio.current_task():
            # The trusted handoff callback promotes its own target to a normal
            # request. Invalidate old candidates without cancelling this very
            # admission call; unrelated/revoked tasks still cancel normally.
            lane.generation += 1
            lane.candidates.clear()
            lane.wakeup.set()
        elif lane.batch_participants - {sender_id}:
            # The shared discussion may already be answering another member.
            # Supersede this owner's old candidates without cancelling others.
            lane.candidates.clear()
            lane.wakeup.set()
        else:
            self._invalidate_lane(lane, cancel_send=False)
        self._pending.pop((group_id, sender_id), None)
        lane.explicit.add(str(message_id))
        lane.last_seen = self._clock()
        self._record(state, "explicit_started")

    def explicit_done(self, group_id: str, message_id: str) -> None:
        state = self._groups.get(group_id)
        if state is None:
            return
        self._prune(state, self._clock())
        for lane in state.lanes.values():
            if str(message_id) not in lane.explicit:
                continue
            lane.explicit.discard(str(message_id))
            self._record(state, "explicit_finished")
            lane.wakeup.set()
            self._queue_lane(group_id, lane)

    def on_bot_message(self, group_id: str, text: str, *, message_id: str | None = None,
                       recipient_id: str | None = None, proactive: bool = False,
                       reply_to: str | None = None, has_media: bool = False) -> None:
        self.refresh_configuration()
        if self._policy(self._snapshot, group_id) is None:
            return
        has_media = has_media is True and message_id is not None
        if not str(text).strip() and not has_media:
            return
        state = self._state(group_id)
        if recipient_id is None and reply_to is not None:
            recipient_id = next((m.sender_id for m in state.messages
                                 if m.message_id == str(reply_to) and not m.is_bot), None)
        recipient = str(recipient_id) if recipient_id is not None else None
        lane = self._lane(state, recipient) if recipient else None
        if lane is not None:
            lane.last_seen = self._clock()
            lane.last_bot_text = str(text)[:2000]
            lane.last_bot_id = str(message_id) if message_id is not None else None
            lane.last_bot_at = self._clock()
            lane.last_recipient = recipient
            lane.after_bot_ids.clear()
            if not proactive:
                lane.followup_streak = 0
                lane.last_interjection_at = None
                state.last_interjection_at = None
        if message_id is not None:
            state.reply_index[str(message_id)] = _BotReply(str(text)[:2000], self._clock(), recipient,
                                                          str(reply_to) if reply_to is not None else None)
            if reply_to is not None and not proactive:
                state.covered[str(reply_to)] = self._clock()
                state.coverage_receipts[str(reply_to)] = str(message_id)
        self._append_bot_message(group_id, state, text, message_id, reply_to, recipient, has_media)

    def _append_bot_message(self, group_id: str, state: _Group, text: str,
                            message_id: str | None, reply_to: str | None = None,
                            recipient_id: str | None = None, has_media: bool = False) -> None:
        text = str(text).strip()[:2000]
        if not text and not has_media:
            return
        key = str(message_id) if message_id is not None else f"bot-observed-{self._clock()}-{len(state.messages)}"
        if any(item.is_bot and item.message_id == key for item in state.messages):
            return
        policy = self._policy(self._snapshot, group_id)
        state.messages.append(Message(self.bot_id, group_id, key, self.bot_id,
                                      text, self._clock(), reply_to=reply_to,
                                      sender_name=policy.display_name if policy else "", is_bot=True,
                                      reply_recipient_id=recipient_id, has_media=has_media))
        self._prune(state, self._clock())

    def resolve_target(self, group_id: str, message_id: str) -> Message | None:
        self.refresh_configuration()
        if self._closed or self._policy(self._snapshot, group_id) is None:
            return None
        state = self._groups.get(group_id)
        if state is None:
            return None
        self._prune(state, self._clock())
        return next((m for m in state.messages if m.message_id == message_id
                     and m.bot_id == self.bot_id and m.group_id == group_id and not m.is_bot
                     and not m.explicit_request and m.text.strip()
                     and not any(item != self.bot_id for item in m.mentions)
                     and m.message_id not in state.covered and m.message_id not in state.invalid_targets
                     and not (state.lanes.get(m.sender_id) and state.lanes[m.sender_id].explicit)
                     and -30 <= self._clock() - m.timestamp <= 90), None)

    def focus_message_ids(self, group_id: str, participant_id: str,
                          messages: tuple[Message, ...], reply_to: str | None = None) -> tuple[str, ...]:
        return self.context_metadata(group_id, messages, participant_id, reply_to)["focus_message_ids"]

    def context_metadata(self, group_id: str, messages: tuple[Message, ...],
                         participant_id: str = "", reply_to: str | None = None) -> dict:
        """Shared public evidence, never private executor state or newer messages."""
        self.refresh_configuration()
        empty = {"topic_id": "", "topic_message_ids": (), "participant_ids": (),
                 "focus_message_ids": (), "covered_message_ids": ()}
        if self._closed or self._policy(self._snapshot, group_id) is None:
            return empty
        state = self._groups.get(group_id)
        if state is None:
            return empty
        scoped = tuple(m for m in messages if m.bot_id == self.bot_id and m.group_id == group_id)
        policy = self._policy(self._snapshot, group_id)
        result = metadata(self.bot_id, group_id, scoped, participant_id=participant_id,
                          reply_to=reply_to, ignored_terms=(policy.display_name, *policy.aliases))
        snapshot_ids = {m.message_id for m in scoped}
        result["covered_message_ids"] = tuple(m.message_id for m in scoped
                                              if state.coverage_receipts.get(m.message_id) in snapshot_ids)
        return result

    @staticmethod
    def _quiet(policy: GroupPolicy, now: float) -> bool:
        if policy.quiet_start is None and policy.quiet_end is None:
            return False
        try:
            start = datetime.strptime(policy.quiet_start, "%H:%M").time()
            end = datetime.strptime(policy.quiet_end, "%H:%M").time()
            current = datetime.fromtimestamp(now, ZoneInfo(policy.timezone)).time()
        except (TypeError, ValueError, KeyError):
            return True  # Broken quiet-hour configuration never enables unsolicited speech.
        if start == end:
            return True
        return start <= current < end if start < end else current >= start or current < end

    def _context(self, group_id: str, state: _Group, lane: _Lane, policy: GroupPolicy) -> ConversationContext:
        from qunling.group_context import continuation_anchors

        now = self._clock()
        recent_bot = lane.last_bot_at is not None and 0 <= now - lane.last_bot_at <= 120
        candidates = tuple(m.message_id for m in state.messages if m.message_id in lane.candidates)
        roots = topic_roots(tuple(state.messages), ignored_terms=(policy.display_name, *policy.aliases))
        followups = tuple(
            m.message_id for m in state.messages if m.sender_id == lane.sender_id
            and not m.explicit_request and not m.is_bot
            and ((m.reply_to in state.reply_index and 0 <= now - state.reply_index[m.reply_to].at <= 120)
                 or (recent_bot and m.message_id in lane.after_bot_ids))
        )
        names = {name.strip().casefold() for name in (policy.display_name, *policy.aliases)
                 if isinstance(name, str) and 1 <= len(name.strip()) <= 80}

        def mentions_name(text: str) -> bool:
            folded = text.casefold().lstrip()
            for name in names:
                if len(name) > 1 and name in folded:
                    return True
                if (len(name) == 1 and folded.startswith(name)
                        and (len(folded) == 1 or folded[1].isspace() or folded[1] in ",，:：!！?？、")):
                    return True
            return False

        addressed = tuple(m.message_id for m in state.messages if m.sender_id == lane.sender_id
                          and not m.is_bot and not m.explicit_request
                          and not any(item != self.bot_id for item in m.mentions)
                          and (m.reply_to in state.reply_index or mentions_name(m.text)))
        # Coalesce a public topic across participants. Individually addressed
        # calls and members busy with an explicit task retain their own queue.
        if candidates and not (set(candidates) & set(addressed)):
            selected_roots = {roots[key] for key in candidates}
            extras = set()
            for message in state.messages:
                other = state.lanes.get(message.sender_id)
                if (other is None or other is lane or other.explicit or message.explicit_request
                        or message.is_bot or message.message_id not in other.candidates
                        or roots[message.message_id] not in selected_roots
                        or message.reply_to in state.reply_index or mentions_name(message.text)
                        or not self._can_speak(state, other, policy, "reply")):
                    continue
                extras.add(message.message_id)
            candidates = tuple(m.message_id for m in state.messages
                               if m.message_id in set(candidates) | extras)
        candidates = tuple(key for key in candidates if key not in state.covered and key not in state.invalid_targets)
        continuations = continuation_anchors(tuple(state.messages),
                                             ignored_terms=(policy.display_name, *policy.aliases))
        continuations = {key: anchor for key, anchor in continuations.items() if key in candidates}
        participant_anchor = lane.last_bot_id if candidates and candidates[-1] in followups else None
        # A public continuation belongs to the shared discussion even when a
        # different member asks it. Both text-only replies and tool handoffs
        # must use the same grounded candidates and real bot receipt.
        followup_ids = set(followups) | set(continuations)
        followups = tuple(m.message_id for m in state.messages if m.message_id in followup_ids)
        # An exact quote wins over the most recent reply to this participant.
        quoted = next((m.reply_to for m in reversed(state.messages)
                       if m.message_id in candidates and m.reply_to in state.reply_index), None)
        shared_anchor = next((continuations[key] for key in reversed(candidates) if key in continuations), None)
        anchor_id = quoted or participant_anchor or shared_anchor or lane.last_bot_id
        anchor = state.reply_index.get(anchor_id)
        shared = self.context_metadata(group_id, tuple(state.messages), lane.sender_id,
                                       candidates[-1] if candidates else anchor_id)
        conversation_id = hashlib.sha256((self.bot_id + "\0" + group_id + "\0" + lane.sender_id).encode()).hexdigest()[:24]
        return ConversationContext(self.bot_id, group_id, policy.mode, policy.persona,
                                   tuple(state.messages), followups,
                                   anchor.text if anchor else lane.last_bot_text,
                                   anchor_id, anchor.at if anchor else lane.last_bot_at,
                                   policy.display_name, addressed, candidates, conversation_id,
                                   lane.sender_id, shared["focus_message_ids"], shared["topic_id"],
                                   shared["topic_message_ids"], shared["participant_ids"], shared["covered_message_ids"],
                                   tuple(continuations.items()))

    def _current(self, group_id: str, sender_id: str, generation: int, revision: str):
        self.refresh_configuration()
        policy = self._policy(self._snapshot, group_id)
        state = self._groups.get(group_id)
        lane = state.lanes.get(sender_id) if state else None
        if (self._closed or not self._state_ok or policy is None or state is None or lane is None
                or (lane.explicit and not (lane.batch_participants - {sender_id}))
                or self._snapshot.revision != revision or lane.generation != generation
                or self._quiet(policy, self._clock())):
            return None
        self._prune(state, self._clock())
        return state, lane, policy

    @staticmethod
    def _limit(value: int | None, default: int) -> int:
        # Administrators may lower limits; config errors cannot raise the hard budget.
        return min(default, max(0, value)) if isinstance(value, int) and not isinstance(value, bool) else default

    def _budget(self, state: _Group, policy: GroupPolicy, *, sending: bool = False) -> bool:
        now, mode = self._clock(), _MODES[policy.mode]
        events = (*state.sends, *state.handoffs) if sending else state.judgments
        hourly = mode.reply_hourly if sending else self._limit(policy.judgments_per_hour, mode.hourly)
        daily = mode.reply_daily if sending else self._limit(policy.judgments_per_day, mode.daily)
        global_ok = sending or sum(now - at < 86400 for at in self._global_judgments) < GLOBAL_JUDGMENTS_PER_DAY
        return global_ok and len(events) < daily and sum(now - at < 3600 for at in events) < hourly

    @staticmethod
    def _last_attempt(state: _Group) -> float | None:
        return max((events[-1] for events in (state.sends, state.handoffs) if events), default=None)

    def _cooldown_until(self, state: _Group, policy: GroupPolicy, action: str, *, addressed: bool = False,
                        lane: _Lane | None = None) -> float:
        last = self._last_attempt(state)
        if addressed:
            return last + 5 if last is not None else 0
        if action == "followup":
            return last + _MODES[policy.mode].followup_interval if last is not None else 0
        mode = _MODES[policy.mode]
        cooldown = max(mode.cooldown, policy.cooldown_seconds or 0)
        return max(last + cooldown if last is not None else 0,
                   lane.last_interjection_at + mode.unanswered_backoff
                   if lane is not None and lane.last_interjection_at is not None else 0)

    def _can_speak(self, state: _Group, lane: _Lane, policy: GroupPolicy, action: str, *, addressed: bool = False) -> bool:
        now = self._clock()
        if not self._budget(state, policy, sending=True):
            return False
        if action == "followup" and not addressed and lane.followup_streak >= _MODES[policy.mode].followup_limit:
            return False
        return now >= self._cooldown_until(state, policy, action, addressed=addressed, lane=lane)

    @staticmethod
    def _candidate_timing(context, target):
        return ("followup" if target in context.followup_candidate_ids or target in dict(context.continuation_anchors) else "reply",
                target in context.addressed_candidate_ids)

    def _consume(self, group_id, lane, context):
        # Own frozen batch is consumed once. Borrowed public-topic candidates
        # are claimed separately and returned unless a confirmed reply covers them.
        lane.candidates.difference_update(context.reply_candidate_ids)
        self._pending.pop((group_id, lane.sender_id), None)

    def _fresh(self, context, target):
        return any(m.message_id == target and -30 <= self._clock() - m.timestamp <= 90
                   for m in context.messages)

    async def _wait_delay(self, lane, delay):
        lane.wakeup.clear()
        try:
            await asyncio.wait_for(lane.wakeup.wait(), delay * self._debounce_scale)
        except asyncio.TimeoutError:
            pass

    async def _wait_for_turn(self, group_id, sender, generation, revision):
        while True:
            current = self._current(group_id, sender, generation, revision)
            if current is None:
                return None
            state, lane, policy = current
            context = self._context(group_id, state, lane, policy)
            if not context.reply_candidate_ids:
                self._pending.pop((group_id, sender), None)
                return None
            eligible = [target for target in context.reply_candidate_ids
                        if policy.mode != "quiet" or target in context.addressed_candidate_ids
                        or target in context.followup_candidate_ids or target in dict(context.continuation_anchors)]
            if not eligible:
                self._consume(group_id, lane, context)
                self._record(state, "silence")
                return None
            if not self._budget(state, policy) or not self._budget(state, policy, sending=True):
                self._consume(group_id, lane, context)
                self._record(state, "budget_exhausted")
                return None
            timings = [self._candidate_timing(context, target) for target in eligible]
            if any(self._can_speak(state, lane, policy, action, addressed=addressed)
                   for action, addressed in timings):
                return current
            delay = min(self._cooldown_until(state, policy, action, addressed=addressed, lane=lane)
                        for action, addressed in timings) - self._clock()
            if delay <= 0 or self._debounce_scale == 0:
                self._consume(group_id, lane, context)
                self._record(state, "cooldown")
                return None
            expiry = max(m.timestamp + 90 for m in context.messages if m.message_id in eligible)
            if self._clock() + delay >= expiry:
                self._consume(group_id, lane, context)
                self._record(state, "batch_expired")
                return None
            self._record(state, "cooldown")
            await self._wait_delay(lane, delay)

    async def _handoff_once(self, group_id, sender, generation, revision, context, decision):
        """Claim a genuine continuation once, then admit it to the normal chain.

        Admission is not delivery: it consumes a participation attempt but never
        creates a receipt or marks a question answered. An uncertain acceptance
        is not retried, even if the callback fails after submitting the request.
        """
        addressed = decision.target_message_id in context.addressed_candidate_ids
        while True:
            current = self._current(group_id, sender, generation, revision)
            if current is None:
                return
            state, lane, policy = current
            async with state.send_lock:
                current = self._current(group_id, sender, generation, revision)
                if current is None:
                    return
                state, lane, policy = current
                if not self._fresh(context, decision.target_message_id):
                    self._record(state, "batch_expired")
                    return
                target = self.resolve_target(group_id, decision.target_message_id)
                if target is None:
                    self._record(state, "stale")
                    return
                if not self._budget(state, policy, sending=True):
                    self._record(state, "budget_exhausted")
                    return
                pacing_lane = state.lanes.get(target.sender_id, lane)
                if self._can_speak(state, pacing_lane, policy, "followup", addressed=addressed):
                    # Claim before yielding to the callback. Neither a second
                    # lane nor a failed/uncertain callback can claim this again.
                    state.invalid_targets.add(decision.target_message_id)
                    state.handoffs.append(self._clock())
                    state.last_proactive_at = self._clock()
                    pacing_lane.followup_streak += 1
                    if not self._persist():
                        self._record(state, "handoff_failed")
                        return
                    lane.handoff_target = decision.target_message_id
                    break
                delay = self._cooldown_until(state, policy, "followup", addressed=addressed, lane=pacing_lane) - self._clock()
                if delay <= 0 or self._debounce_scale == 0:
                    self._record(state, "cooldown")
                    return
                if self._clock() + delay >= target.timestamp + 90:
                    self._record(state, "batch_expired")
                    return
                self._record(state, "cooldown")
            await self._wait_delay(lane, delay)
        try:
            self._record(state, "handing_off")
            # timeout(), unlike wait_for(), keeps admission on this exact task
            # so mark_explicit can recognize its own controlled promotion.
            async with asyncio.timeout(self._model_timeout):
                accepted = self._handoff is not None and await self._handoff(context, decision) is True
            self._record(state, "handed_off" if accepted else "handoff_failed")
        except asyncio.CancelledError:
            self._record(state, "handoff_failed")
            raise
        except Exception:
            self._record(state, "handoff_failed")
        finally:
            lane.handoff_target = None
            for other in state.lanes.values():
                other.wakeup.set()

    @staticmethod
    def _new_duplicate_reply(state, policy, target_id, text, previous_receipts):
        """Find a confirmed same-topic reply sent while this text was generated.

        Receipt IDs, rather than timestamps, also work for simultaneous sends
        within one clock tick. Nothing in a new user message can suppress a
        pending answer, and an already-known reply never becomes a global ban.
        """
        roots = topic_roots(tuple(state.messages), ignored_terms=(policy.display_name, *policy.aliases))
        root = roots.get(target_id)
        if root is None:
            return None
        observed = {message.message_id for message in state.messages if message.is_bot}
        for receipt_id, reply in reversed(tuple(state.reply_index.items())):
            if (receipt_id in previous_receipts or receipt_id not in observed
                    or reply.target_id is None or roots.get(reply.target_id) != root):
                continue
            if _repeated_reply(reply.text, text):
                return receipt_id, roots
        return None

    async def _deliver(self, group_id, sender, generation, revision, context, decision, text,
                       previous_receipts):
        addressed = decision.target_message_id in context.addressed_candidate_ids
        while True:
            current = self._current(group_id, sender, generation, revision)
            if current is None:
                return
            state, lane, policy = current
            if not self._fresh(context, decision.target_message_id):
                self._record(state, "batch_expired")
                return
            target = self.resolve_target(group_id, decision.target_message_id)
            if target is None:
                self._record(state, "stale")
                return
            # Models never hold this lock. A ready answer waits outside it for
            # group pacing rather than being discarded when someone else sends.
            async with state.send_lock:
                current = self._current(group_id, sender, generation, revision)
                if current is None:
                    return
                state, lane, policy = current
                if not self._fresh(context, decision.target_message_id):
                    self._record(state, "batch_expired")
                    return
                target = self.resolve_target(group_id, decision.target_message_id)
                if target is None:
                    self._record(state, "stale")
                    return
                duplicate = self._new_duplicate_reply(
                    state, policy, decision.target_message_id, text, previous_receipts)
                if duplicate is not None:
                    receipt_id, roots = duplicate
                    root = roots[decision.target_message_id]
                    # Reuse only a real successful receipt for the same topic.
                    # Unrelated borrowed candidates retain their next attempt.
                    for key in decision.covered_message_ids or (decision.target_message_id,):
                        if roots.get(key) == root and key not in state.invalid_targets:
                            state.covered[key] = self._clock()
                            state.coverage_receipts[key] = receipt_id
                    self._record(state, "duplicate_reply")
                    return
                if not self._budget(state, policy, sending=True):
                    self._record(state, "budget_exhausted")
                    return
                pacing_lane = state.lanes.get(target.sender_id, lane)
                if self._can_speak(state, pacing_lane, policy, decision.action, addressed=addressed):
                    sent_at = self._clock()
                    state.sends.append(sent_at)
                    state.last_proactive_at = sent_at
                    state.last_interjection_at = None if addressed or decision.action == "followup" else sent_at
                    pacing_lane.last_interjection_at = state.last_interjection_at
                    pacing_lane.followup_streak = pacing_lane.followup_streak + 1 if decision.action == "followup" and not addressed else 0
                    if not self._persist():
                        return
                    before_send_ids = {m.message_id for m in state.messages}
                    lane.sending = True
                    try:
                        message_id = await asyncio.wait_for(
                            self._send(group_id, text, decision.target_message_id), self._send_timeout)
                    except asyncio.CancelledError:
                        state.invalid_targets.update(decision.covered_message_ids or (decision.target_message_id,))
                        raise
                    except Exception:
                        state.invalid_targets.update(decision.covered_message_ids or (decision.target_message_id,))
                        self._record(state, "delivery_failed")
                        return
                    finally:
                        lane.sending = False
                    if message_id is None:
                        state.invalid_targets.update(decision.covered_message_ids or (decision.target_message_id,))
                        self._record(state, "delivery_failed")
                        return
                    self.on_bot_message(group_id, text, message_id=message_id, recipient_id=target.sender_id,
                                        proactive=True, reply_to=decision.target_message_id)
                    # Never claim coverage from a judgment/generation alone.
                    for key in decision.covered_message_ids or (decision.target_message_id,):
                        if key not in state.invalid_targets:
                            state.covered[key] = self._clock()
                            state.coverage_receipts[key] = str(message_id)
                    pacing_lane.after_bot_ids.update(m.message_id for m in state.messages
                                              if m.sender_id == target.sender_id and not m.is_bot
                                              and m.message_id not in before_send_ids)
                    self._record(state, "sent")
                    for other in state.lanes.values():
                        other.wakeup.set()
                    return
                delay = self._cooldown_until(state, policy, decision.action, addressed=addressed, lane=pacing_lane) - self._clock()
                if delay <= 0 or self._debounce_scale == 0:
                    self._record(state, "cooldown")
                    return
                expiry = next(m.timestamp + 90 for m in context.messages if m.message_id == decision.target_message_id)
                if self._clock() + delay >= expiry:
                    self._record(state, "batch_expired")
                    return
                self._record(state, "cooldown")
            await self._wait_delay(lane, delay)

    async def _run(self, group_id: str, sender: str, generation: int, revision: str) -> None:
        state = None
        lane = None
        borrowed = ()
        handing_off = False
        try:
            current = self._current(group_id, sender, generation, revision)
            if current is None:
                return
            state, lane, policy = current
            context = self._context(group_id, state, lane, policy)
            delay = 1 if (set(context.reply_candidate_ids) &
                          (set(context.followup_candidate_ids) | set(context.addressed_candidate_ids)
                           | set(dict(context.continuation_anchors)))) else _MODES[policy.mode].debounce
            await asyncio.sleep(delay * self._debounce_scale)
            if await self._wait_for_turn(group_id, sender, generation, revision) is None:
                return
            async with state.model_slots, self._semaphore:
                current = self._current(group_id, sender, generation, revision)
                if current is None:
                    return
                state, lane, policy = current
                context = self._context(group_id, state, lane, policy)
                if not context.reply_candidate_ids:
                    return
                batch_ids = set(context.reply_candidate_ids)
                lane.batch_participants = {m.sender_id for m in context.messages if m.message_id in batch_ids}
                borrowed = tuple(m for m in context.messages if m.message_id in batch_ids and m.sender_id != sender)
                for message in borrowed:
                    state.lanes[message.sender_id].candidates.discard(message.message_id)
                self._consume(group_id, lane, context)
                if not self._budget(state, policy) or not self._budget(state, policy, sending=True):
                    self._record(state, "budget_exhausted")
                    return
                state.judgments.append(self._clock())
                self._global_judgments.append(self._clock())
                if not self._persist():
                    return
                self._record(state, "judging")
                value = await asyncio.wait_for(self._judge(context), self._model_timeout)
                decision = validate_decision(value, context)
                if self._current(group_id, sender, generation, revision) is None:
                    self._record(state, "stale")
                    return
                if decision is None:
                    self._record(state, "invalid_decision")
                    return
                if decision.action in ("silence", "wait"):
                    # This is one deliberate decision about the complete public
                    # batch, not an error requiring every member to judge again.
                    borrowed = ()
                    self._record(state, decision.action)
                    return
                if self.resolve_target(group_id, decision.target_message_id) is None:
                    self._record(state, "stale")
                    return
                if not self._fresh(context, decision.target_message_id):
                    self._record(state, "batch_expired")
                    return
                if decision.action != "tool_followup":
                    self._record(state, "generating")
                    previous_receipts = frozenset(state.reply_index)
                    text = await asyncio.wait_for(self._generate(context, decision), self._model_timeout)
                    if (not isinstance(text, str) or not text.strip()
                            or len(text.strip()) > reply_character_limit(context, decision)
                            or any((ord(char) < 32 and char not in "\n\t")
                                   or 0xD800 <= ord(char) <= 0xDFFF for char in text)):
                        self._record(state, "invalid_generation")
                        return
            if decision.action == "tool_followup":
                handing_off = True
                await self._handoff_once(group_id, sender, generation, revision, context, decision)
                return
            await self._deliver(group_id, sender, generation, revision, context, decision, text.strip(),
                                previous_receipts)
        except asyncio.CancelledError:
            if (not handing_off and state is not None and lane is not None
                    and self._groups.get(group_id) is state and not lane.explicit):
                self._record(state, "cancelled")
            raise
        except Exception:
            if state is not None:
                self._record(state, "model_error")
        finally:
            if lane is not None:
                lane.batch_participants.clear()
            if state is not None and self._groups.get(group_id) is state and not self._closed:
                # A different member's unaddressed contribution is not silently
                # discarded just because this batch answered only its owner.
                for message in borrowed:
                    other = state.lanes.get(message.sender_id)
                    if (other is not None and not other.explicit
                            and self.resolve_target(group_id, message.message_id) is not None):
                        other.candidates.add(message.message_id)
                        self._queue_lane(group_id, other)

    async def drain(self) -> None:
        """Wait for existing attempts; useful for orderly host shutdown and offline tests."""
        while self._tasks or self._pending or self._pump_handle is not None:
            if self._pending and self._pump_handle is None:
                self._schedule_pump()
            if self._pump_handle is not None:
                await asyncio.sleep(0)
            pending = tuple(task for task in self._tasks if not task.done())
            self._tasks.difference_update(task for task in tuple(self._tasks) if task.done())
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    def status(self) -> dict:
        """Operational counters only: this API never exposes observed conversation text."""
        self.refresh_configuration()
        now = self._clock()
        groups = {}
        policies = self._snapshot.groups if self._snapshot is not None else {}
        for group_id, policy in policies.items():
            if not isinstance(policy, GroupPolicy) or policy.mode not in _MODES:
                continue
            state = self._groups.get(group_id)
            if state is None:
                state = self._restored_state(group_id)
            self._prune(state, now)
            mode = _MODES[policy.mode]
            active = sum(lane.task is not None and not lane.task.done() for lane in state.lanes.values())
            queued = sum(bool(lane.candidates) for lane in state.lanes.values())
            explicit = sum(len(lane.explicit) for lane in state.lanes.values())
            pending = bool(active or queued)
            cooldown_until = self._cooldown_until(state, policy, "reply")
            if self._policy(self._snapshot, group_id) is None:
                code = "off"
            elif not self._state_ok:
                code = "storage_error"
            elif pending:
                code = "pending"
            elif self._quiet(policy, now):
                code = "quiet_hours"
            elif not self._budget(state, policy) or not self._budget(state, policy, sending=True):
                code = "budget_exhausted"
            elif cooldown_until > now:
                code = "cooldown"
            else:
                code = "ready"
            groups[group_id] = {
                "status_code": code,
                "pending": pending,
                "explicit_busy": bool(explicit),
                "active_conversations": active, "queued_conversations": queued, "explicit_requests": explicit,
                "judgments_hour": sum(now - at < 3600 for at in state.judgments),
                "judgments_day": len(state.judgments),
                "judgments_hour_limit": self._limit(policy.judgments_per_hour, mode.hourly),
                "judgments_day_limit": self._limit(policy.judgments_per_day, mode.daily),
                "replies_hour": sum(now - at < 3600 for at in state.sends),
                "replies_day": len(state.sends),
                "handoff_attempts_hour": sum(now - at < 3600 for at in state.handoffs),
                "handoff_attempts_day": len(state.handoffs),
                "replies_hour_limit": mode.reply_hourly,
                "replies_day_limit": mode.reply_daily,
                "last_sent_at": state.sends[-1] if state.sends else None,
                "last_handoff_at": state.handoffs[-1] if state.handoffs else None,
                "cooldown_until": cooldown_until if cooldown_until > now else None,
                "last_event": state.last_event or {"code": "initial", "at": now},
            }
        return {"enabled": self._snapshot is not None and self._snapshot.enabled is True,
                "generated_at": now,
                "storage_healthy": self._state_ok,
                "judgments_day": sum(now - at < 86400 for at in self._global_judgments),
                "judgments_day_limit": GLOBAL_JUDGMENTS_PER_DAY, "groups": groups}

    @property
    def configuration_revision(self) -> str | None:
        """Revision last validated by this engine, without reading newer settings."""
        return self._snapshot.revision if self._snapshot is not None else None

    async def close(self) -> None:
        self._closed = True
        self._pending.clear()
        if self._pump_handle is not None:
            self._pump_handle.cancel()
            self._pump_handle = None
        for state in self._groups.values():
            self._invalidate(state)
        await self.drain()
        self._groups.clear()
