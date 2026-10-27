"""Withdrawn ingress cancels its pending reply, independently of model output.

Only authenticated OneBot notices populate this runtime-local registry. It is
scoped to the current installation/workspace/bot placement and conversation;
plain text, quotes and model outputs cannot withdraw a request.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import os
import logging
import time


_current = ContextVar("qunling_reply_sources", default=())
SOURCE_IDS = "_qunling_ingress_message_ids"
RETENTION = 86400
MAX_RECALLED = 8192


class MessageRecalled(asyncio.CancelledError):
    """Silent request cancellation, never an error message to the chat."""


@dataclass(eq=False)
class Guard:
    state: object
    keys: frozenset
    task: object
    query: object = None
    revoked: bool = False

    def check(self):
        if self.revoked or any(key in self.state.recalled for key in self.keys):
            self.revoked = True
            if self.query is not None:
                object.__setattr__(self.query, "_qq_message_recalled", True)
            raise MessageRecalled()


@dataclass
class State:
    scope: tuple
    recalled: OrderedDict = field(default_factory=OrderedDict)
    active: set = field(default_factory=set)
    cleanup: set = field(default_factory=set)

    def settled(self, task):
        self.cleanup.discard(task)
        if not task.cancelled():
            # The synchronous tombstone remains authoritative if queue cleanup fails.
            if task.exception() is not None:
                logging.getLogger(__name__).warning('QQ recall queue cleanup failed; reply remains suppressed')


def _state(adapter, *, create=False):
    if not os.environ.get("QUNLING_DATA_DIR"):
        return None
    bot = getattr(adapter, "_runtime", None)
    ctx = getattr(bot, "execution_context", None)
    entity = getattr(bot, "bot_entity", None)
    if (ctx is None or getattr(bot, "adapter", None) is not adapter
            or getattr(entity, "adapter", None) != "onebot-v11"):
        return None
    scope = tuple(getattr(ctx, key, None) for key in
                  ("instance_uuid", "workspace_uuid", "placement_generation")) + (str(entity.uuid),)
    state = getattr(bot, "_qunling_recalls", None)
    if state is None or state.scope != scope:
        if not create:
            return None
        state = State(scope)
        bot._qunling_recalls = state
    now = time.monotonic()
    while state.recalled and next(iter(state.recalled.values())) < now - RETENTION:
        state.recalled.popitem(last=False)
    return state


def _id(value):
    return str(value) if type(value) in (int, str) and 0 < len(str(value)) <= 128 else None


def event_id(event):
    value = _id(getattr(event, "message_id", None))
    if value is None:
        value = _id(getattr(getattr(event, "message_chain", None), "message_id", None))
    if value is None:
        native = getattr(event, "source_platform_object", None)
        value = _id(native.get("message_id")) if isinstance(native, dict) else None
    return value


def event_key(event):
    mid = event_id(event)
    group = getattr(event, "group", None) or getattr(getattr(event, "sender", None), "group", None)
    group_id = _id(getattr(group, "id", None))
    native = getattr(event, "source_platform_object", None)
    if isinstance(native, dict):
        group_id = group_id or _id(native.get("group_id"))
    actor = _id(getattr(getattr(event, "sender", None), "id", None))
    if mid is None or (group_id or actor) is None:
        return None
    return ("group", group_id, mid) if group_id else ("person", actor, mid)


def query_keys(query):
    kind = getattr(getattr(query, "launcher_type", None), "value", getattr(query, "launcher_type", None))
    kind = "person" if kind == "private" else kind
    if kind not in {"group", "person"}:
        return frozenset()
    ids = [event_id(getattr(query, "message_event", None))]
    extra = (getattr(query, "variables", None) or {}).get(SOURCE_IDS, ())
    if isinstance(extra, (list, tuple)):
        ids.extend(_id(value) for value in extra)
    return frozenset((kind, str(query.launcher_id), mid) for mid in ids if mid is not None)


def query_recalled(query):
    if getattr(query, "_qq_message_recalled", False):
        return True
    state = _state(getattr(query, "adapter", None))
    return bool(state and any(key in state.recalled for key in query_keys(query)))


def event_recalled(adapter, event):
    state = _state(adapter)
    return bool(state and event_key(event) in state.recalled)


@contextmanager
def protect(adapter, keys, *, query=None):
    state = _state(adapter, create=True)
    if state is None or not keys:
        yield
        return
    guard = Guard(state, frozenset(keys), asyncio.current_task(), query)
    state.active.add(guard)
    token = _current.set((*_current.get(), guard))
    try:
        guard.check()
        yield
        guard.check()
    finally:
        _current.reset(token)
        state.active.discard(guard)


def protect_event(adapter, event):
    key = event_key(event)
    return protect(adapter, (key,) if key else ())


def check_current():
    for guard in _current.get():
        guard.check()


def before_action(adapter, action, params):
    # Runs after connection validation AND acquiring the socket send lock.
    # This also covers non-quoted replies and tools in the same request task.
    check_current()
    # Plugin-originated replies can arrive in a separate task. Their actual
    # reply segment still identifies the source even without our ContextVar.
    state = _state(adapter)
    if state is None or action not in {'send_msg', 'send_group_msg', 'send_private_msg'}:
        return
    kind, chat = ('group', _id(params.get('group_id'))) if params.get('group_id') is not None else ('person', _id(params.get('user_id')))
    message = params.get('message')
    if isinstance(message, list):
        for part in message:
            if isinstance(part, dict) and part.get('type') == 'reply' and isinstance(part.get('data'), dict):
                if (kind, chat, _id(part['data'].get('id'))) in state.recalled:
                    raise MessageRecalled()


def observe_native(adapter, payload):
    """Synchronous fast path after authenticated transport/account validation."""
    if payload.get("post_type") != "notice" or payload.get("notice_type") not in {"group_recall", "friend_recall"}:
        return
    if str(payload.get("self_id", "")) != str(getattr(adapter, "bot_account_id", "")):
        return
    kind = "group" if payload["notice_type"] == "group_recall" else "person"
    chat = _id(payload.get("group_id" if kind == "group" else "user_id"))
    mid = _id(payload.get("message_id"))
    if chat is None or mid is None:
        return
    state = _state(adapter, create=True)
    if state is None:
        return
    key = (kind, chat, mid)
    if key in state.recalled:
        return
    state.recalled[key] = time.monotonic()
    state.recalled.move_to_end(key)
    while len(state.recalled) > MAX_RECALLED:
        state.recalled.popitem(last=False)
    for guard in tuple(state.active):
        if key in guard.keys:
            guard.revoked = True
            if guard.query is not None:
                object.__setattr__(guard.query, "_qq_message_recalled", True)
            if guard.task is not None and not guard.task.done():
                guard.task.cancel()
    bot = adapter._runtime
    service = getattr(getattr(bot, "ap", None), "_qq_dispatch", None)
    if service is not None:
        for task in tuple(service.tasks.values()):
            if task.query.adapter is adapter and key in query_keys(task.query) and not task.done.is_set():
                object.__setattr__(task.query, "_qq_message_recalled", True)
                cleanup = asyncio.create_task(service.stop(task, "message_recalled", notify=False))
                state.cleanup.add(cleanup)
                cleanup.add_done_callback(state.settled)
    bridge = getattr(bot, "_qunling_proactive", None)
    if bridge is not None and kind == "group":
        bridge.engine.retract(chat, mid)
        bridge._request_contexts.pop((chat, mid), None)
        bridge.queued.discard((chat, mid))


def pending_messages(messages):
    """Drop withdrawn segments before aggregation, leaving live siblings intact."""
    return [item for item in messages if not event_recalled(item.adapter, item.message_event)]
