"""Bounded, opt-in group participation. No model or platform dependencies.

All entry points run on the host's asyncio event loop. A failed or cancelled
attempt is never automatically retried: another human message is required.
"""
from __future__ import annotations

import asyncio
from collections import deque
import contextvars
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Awaitable, Callable, Mapping
from zoneinfo import ZoneInfo


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


@dataclass(frozen=True)
class Decision:
    action: str
    target_message_id: str | None


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


@dataclass(frozen=True)
class _Mode:
    debounce: float
    cooldown: int
    hourly: int
    daily: int
    reply_hourly: int
    reply_daily: int


_MODES = {
    "quiet": _Mode(10, 300, 12, 80, 2, 12),
    "natural": _Mode(6, 120, 24, 160, 6, 40),
    "active": _Mode(4, 45, 40, 240, 12, 80),
}


@dataclass
class _Group:
    messages: deque = field(default_factory=deque)
    seen: dict[str, float] = field(default_factory=dict)
    judgments: deque = field(default_factory=deque)
    sends: deque = field(default_factory=deque)
    explicit: set[str] = field(default_factory=set)
    generation: int = 0
    task: asyncio.Task | None = None
    last_bot_text: str = ""
    last_bot_id: str | None = None
    last_bot_at: float | None = None
    last_recipient: str | None = None
    last_proactive_at: float | None = None
    followup_streak: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    after_bot_ids: set[str] = field(default_factory=set)


def validate_decision(value: object, context: ConversationContext) -> Decision | None:
    """Closed schema: unknown fields, stale targets and invented IDs fail closed."""
    if not isinstance(value, dict) or set(value) != {"action", "target_message_id"}:
        return None
    action, target = value["action"], value["target_message_id"]
    if action not in ("silence", "wait", "reply", "followup"):
        return None
    if action in ("silence", "wait"):
        return Decision(action, None) if target is None else None
    if not isinstance(target, str) or not target:
        return None
    eligible = {m.message_id for m in context.messages if not m.explicit_request
                and not any(item != context.bot_id for item in m.mentions)}
    if target not in eligible or not context.messages or target != context.messages[-1].message_id:
        return None
    if action == "followup" and target not in context.followup_candidate_ids:
        return None
    if action == "reply" and context.mode == "quiet":
        return None
    return Decision(action, target)


class ProactiveEngine:
    """One engine per bot; callers may share a semaphore across bot instances."""

    def __init__(
        self, *, bot_id: str, config: Callable[[], EngineConfig],
        judge: Callable[[ConversationContext], Awaitable[object]],
        generate: Callable[[ConversationContext, Decision], Awaitable[str]],
        send: Callable[[str, str, str], Awaitable[str | None]],
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
        self._clock = clock
        self._semaphore = semaphore if semaphore is not None else asyncio.Semaphore(2)
        self._debounce_scale = max(0.0, debounce_scale)
        self._model_timeout = model_timeout
        self._send_timeout = send_timeout
        self._groups: dict[str, _Group] = {}
        self._tasks: set[asyncio.Task] = set()
        self._pending: dict[str, tuple[int, str]] = {}
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
                    "seen": {str(key): at for key, at in row["seen"].items()
                             if isinstance(at, (int, float)) and math.isfinite(at) and 0 <= now - at < 1200},
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

    def _state(self, group_id: str) -> _Group:
        if group_id not in self._groups:
            row = self._history.get(group_id, {})
            state = _Group(judgments=deque(row.get("judgments", [])),
                           sends=deque(row.get("sends", [])), seen=dict(row.get("seen", {})))
            if state.sends:
                state.last_proactive_at = state.sends[-1]
            self._groups[group_id] = state
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
                                       "seen": dict(list(state.seen.items())[-256:])}
        self._history = {
            group_id: {"judgments": self._timestamps(row["judgments"], now),
                       "sends": self._timestamps(row["sends"], now),
                       "seen": {key: at for key, at in row["seen"].items() if now - at < 1200}}
            for group_id, row in self._history.items()
            if row["judgments"] or row["sends"] or row["seen"]
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
    def _invalidate(state: _Group) -> None:
        state.generation += 1
        if state.task is not None and not state.task.done():
            state.task.cancel()

    def _prune(self, state: _Group, now: float) -> None:
        while state.messages and now - state.messages[0].timestamp > 240:
            state.messages.popleft()
        if state.last_bot_at is not None and now - state.last_bot_at > 240:
            state.last_bot_text = ""
            state.last_bot_id = None
            state.last_bot_at = None
            state.last_recipient = None
            state.after_bot_ids.clear()
            state.followup_streak = 0
        while len(state.messages) > 40 or sum(len(m.text) for m in state.messages) > 6000:
            state.messages.popleft()
        state.after_bot_ids.intersection_update(item.message_id for item in state.messages)
        state.seen = {key: at for key, at in state.seen.items() if now - at < 1200}
        while len(state.seen) > 2000:
            state.seen.pop(next(iter(state.seen)))
        for events in (state.judgments, state.sends):
            while events and now - events[0] >= 86400:
                events.popleft()

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
        if not message.text.strip():
            # An image/sticker can change the topic too, even though this text-only
            # path cannot inspect it. Never send an answer prepared before it arrived.
            self._invalidate(state)
            self._pending.pop(message.group_id, None)
            if message.explicit_request:
                state.explicit.add(message.message_id)
            return True
        # Event inputs are normalized, but an individual message must not defeat the window cap.
        message = Message(message.bot_id, message.group_id, message.message_id,
                          message.sender_id, message.text[:2000], message.timestamp,
                          message.reply_to, message.explicit_request, message.sender_name[:80],
                          tuple(str(item) for item in message.mentions[:20]), message.is_bot)
        state.messages.append(message)
        if state.last_bot_at is not None:
            state.after_bot_ids.add(message.message_id)
        self._prune(state, now)
        self._invalidate(state)
        self._pending.pop(message.group_id, None)
        if message.explicit_request:
            state.explicit.add(message.message_id)
            return True
        if (state.explicit or self._quiet(policy, now)
                or any(item != self.bot_id for item in message.mentions)):
            return True
        if len(self._pending) < 32:
            self._pending[message.group_id] = (state.generation, self._snapshot.revision)
            self._schedule_pump()
        return True

    def _schedule_pump(self) -> None:
        if self._pump_handle is None and not self._closed:
            self._pump_handle = asyncio.get_running_loop().call_soon(self._pump, context=contextvars.Context())

    def _pump(self) -> None:
        self._pump_handle = None
        self._tasks.difference_update(task for task in tuple(self._tasks) if task.done())
        while self._pending and len(self._tasks) < 32 and not self._closed:
            group_id = next(iter(self._pending))
            generation, revision = self._pending.pop(group_id)
            state = self._groups.get(group_id)
            if state is None or self._current(group_id, generation, revision) is None:
                continue
            task = asyncio.create_task(self._run(group_id, generation, revision), context=contextvars.Context())
            state.task = task
            self._tasks.add(task)
            task.add_done_callback(self._attempt_done, context=contextvars.Context())

    def _attempt_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if self._pending:
            self._schedule_pump()

    def mark_explicit(self, group_id: str, message_id: str) -> None:
        self.refresh_configuration()
        if self._policy(self._snapshot, group_id) is None:
            return
        state = self._state(group_id)
        self._invalidate(state)
        self._pending.pop(group_id, None)
        state.explicit.add(str(message_id))

    def explicit_done(self, group_id: str, message_id: str) -> None:
        state = self._groups.get(group_id)
        if state is not None:
            state.explicit.discard(str(message_id))
        # Do not resurrect messages received while an explicit request was being answered.

    def on_bot_message(self, group_id: str, text: str, *, message_id: str | None = None,
                       recipient_id: str | None = None, proactive: bool = False) -> None:
        self.refresh_configuration()
        if self._policy(self._snapshot, group_id) is None:
            return
        state = self._state(group_id)
        if not proactive:
            self._invalidate(state)
            self._pending.pop(group_id, None)
        state.last_bot_text = str(text)[:400]
        state.last_bot_id = str(message_id) if message_id is not None else None
        state.last_bot_at = self._clock()
        state.last_recipient = str(recipient_id) if recipient_id is not None else None
        state.after_bot_ids.clear()
        if not proactive:
            state.followup_streak = 0

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

    def _context(self, group_id: str, state: _Group, policy: GroupPolicy) -> ConversationContext:
        now = self._clock()
        recent_bot = state.last_bot_at is not None and 0 <= now - state.last_bot_at <= 120
        followups = tuple(
            m.message_id for m in state.messages
            if recent_bot and m.message_id in state.after_bot_ids and not m.explicit_request
            and ((state.last_bot_id is not None and m.reply_to == state.last_bot_id)
                 or (state.last_recipient is not None and m.sender_id == state.last_recipient))
        )
        return ConversationContext(self.bot_id, group_id, policy.mode, policy.persona,
                                   tuple(state.messages), followups, state.last_bot_text,
                                   state.last_bot_id, state.last_bot_at, policy.display_name)

    def _current(self, group_id: str, generation: int, revision: str):
        self.refresh_configuration()
        policy = self._policy(self._snapshot, group_id)
        state = self._groups.get(group_id)
        if (self._closed or not self._state_ok or policy is None or state is None or state.explicit
                or self._snapshot.revision != revision or state.generation != generation
                or self._quiet(policy, self._clock())):
            return None
        self._prune(state, self._clock())
        if not state.messages or self._clock() - state.messages[-1].timestamp > 90:
            return None
        return state, policy

    @staticmethod
    def _limit(value: int | None, default: int) -> int:
        # Administrators may lower limits; config errors cannot raise the hard budget.
        return min(default, max(0, value)) if isinstance(value, int) and not isinstance(value, bool) else default

    def _budget(self, state: _Group, policy: GroupPolicy, *, sending: bool = False) -> bool:
        now, mode = self._clock(), _MODES[policy.mode]
        events = state.sends if sending else state.judgments
        hourly = mode.reply_hourly if sending else self._limit(policy.judgments_per_hour, mode.hourly)
        daily = mode.reply_daily if sending else self._limit(policy.judgments_per_day, mode.daily)
        global_ok = sending or sum(now - at < 86400 for at in self._global_judgments) < 240
        return global_ok and len(events) < daily and sum(now - at < 3600 for at in events) < hourly

    def _can_speak(self, state: _Group, policy: GroupPolicy, action: str, *, quoted: bool = False) -> bool:
        now, mode = self._clock(), _MODES[policy.mode]
        if not self._budget(state, policy, sending=True):
            return False
        if action == "followup":
            return ((quoted or state.followup_streak < 2)
                    and (not state.sends or now - state.sends[-1] >= 30))
        cooldown = max(mode.cooldown, policy.cooldown_seconds or 0)
        # An ignored interjection should not be followed by another uninvited interjection.
        if state.last_proactive_at is not None and now - state.last_proactive_at < 600:
            return False
        return not state.sends or now - state.sends[-1] >= cooldown

    async def _run(self, group_id: str, generation: int, revision: str) -> None:
        try:
            current = self._current(group_id, generation, revision)
            if current is None:
                return
            state, policy = current
            context = self._context(group_id, state, policy)
            delay = 2 if context.followup_candidate_ids else _MODES[policy.mode].debounce
            await asyncio.sleep(delay * self._debounce_scale)
            async with state.lock, self._semaphore:
                current = self._current(group_id, generation, revision)
                if current is None:
                    return
                state, policy = current
                context = self._context(group_id, state, policy)
                followup = bool(context.followup_candidate_ids)
                quoted = state.last_bot_id is not None and context.messages[-1].reply_to == state.last_bot_id
                if ((policy.mode == "quiet" and not followup)
                        or not self._budget(state, policy)
                        or not self._can_speak(state, policy, "followup" if followup else "reply", quoted=quoted)):
                    return
                state.judgments.append(self._clock())  # Failed judgement still consumes budget.
                self._global_judgments.append(self._clock())
                if not self._persist():
                    return
                value = await asyncio.wait_for(self._judge(context), self._model_timeout)
                decision = validate_decision(value, context)
                current = self._current(group_id, generation, revision)
                if (current is None or decision is None or decision.action in ("silence", "wait")):
                    return
                state, policy = current
                if not self._can_speak(state, policy, decision.action, quoted=quoted):
                    return
                text = await asyncio.wait_for(self._generate(context, decision), self._model_timeout)
                if (not isinstance(text, str) or not text.strip() or len(text.strip()) > 400
                        or any((ord(char) < 32 and char not in "\n\t")
                               or 0xD800 <= ord(char) <= 0xDFFF for char in text)):
                    return
                current = self._current(group_id, generation, revision)
                if current is None:
                    return
                state, policy = current
                if not self._can_speak(state, policy, decision.action, quoted=quoted):
                    return
                # Reserve before the network call: an ambiguous delivery cannot trigger a resend.
                sent_at = self._clock()
                state.sends.append(sent_at)
                state.last_proactive_at = sent_at
                state.followup_streak = state.followup_streak + 1 if decision.action == "followup" and not quoted else 0
                if not self._persist():
                    return
                recipient = next(m.sender_id for m in context.messages
                                 if m.message_id == decision.target_message_id)
                message_id = await asyncio.wait_for(
                    self._send(group_id, text.strip(), decision.target_message_id), self._send_timeout)
                # The adapter may have already called on_bot_message; do not invalidate its observation.
                state.last_bot_text = text.strip()
                state.last_bot_id = str(message_id) if message_id is not None else None
                state.last_bot_at = sent_at
                state.last_recipient = recipient
                state.after_bot_ids.clear()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Passive participation must never disrupt the explicit request pipeline or reveal context.
            return

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
        for group_id, state in self._groups.items():
            self._prune(state, now)
            groups[group_id] = {
                "pending": group_id in self._pending or (state.task is not None and not state.task.done()),
                "explicit_busy": bool(state.explicit),
                "judgments_hour": sum(now - at < 3600 for at in state.judgments),
                "judgments_day": len(state.judgments),
                "replies_hour": sum(now - at < 3600 for at in state.sends),
                "replies_day": len(state.sends),
                "last_sent_at": state.sends[-1] if state.sends else None,
            }
        return {"enabled": self._snapshot is not None and self._snapshot.enabled is True,
                "storage_healthy": self._state_ok,
                "judgments_day": sum(now - at < 86400 for at in self._global_judgments),
                "judgments_day_limit": 240, "groups": groups}

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
