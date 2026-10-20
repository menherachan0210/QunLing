"""Short-lived attachment references, isolated by installation and sender."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

from langbot_plugin.api.entities.builtin.platform.message import File, Quote

from pkg.reader import MAX_BYTES, MAX_URL_CHARS, TextReadError, decode_inline

QUERY_CONTEXT_KEY = "qq_text_reader.attachment.v1"
CACHE_TTL_SECONDS = 1800
CACHE_MAX_ENTRIES = 64
CACHE_MAX_BYTES = 8 * 1024 * 1024
MISSING = "没有可读取的近期 TXT，请发送或引用一个 TXT 文件。"
AMBIGUOUS = "本条消息涉及多个文件，请单独发送或引用一个需要读取的 TXT。"
UNRESOLVED = "引用文件无法确认，请重新上传或重新引用；不会改读旧文件。"
EXPIRED = "TXT 的临时引用已过期或失效，请重新上传或引用。"
UNSUPPORTED = "当前附件不是可读取的 TXT，或文件超过 128 KiB，请重新发送。"


def _digest(values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode()).hexdigest()


def _binding_owner(binding):
    values = [
        getattr(binding, field, None)
        for field in (
            "instance_uuid",
            "workspace_uuid",
            "installation_uuid",
            "runtime_revision",
            "placement_generation",
        )
    ]
    if any(value is None or value == "" for value in values):
        raise ValueError("Incomplete installation scope")
    return _digest(values)


def _owner(plugin):
    getter = getattr(plugin, "get_installation_binding", None)
    binding = getter() if callable(getter) else None
    if binding is not None:
        return _binding_owner(binding)
    identity = getattr(plugin, "_text_dedicated_owner", None)
    if identity is None:
        identity = uuid.uuid4().hex
        plugin._text_dedicated_owner = identity
    return _digest(["dedicated", identity])


def _scope(plugin, source):
    owner = _owner(plugin)
    getter = getattr(plugin, "get_installation_binding", None)
    binding = getter() if callable(getter) else None
    for field in ("instance_uuid", "workspace_uuid", "placement_generation"):
        value = getattr(source, field, None)
        if (
            binding is not None
            and value is not None
            and value != getattr(binding, field, None)
        ):
            raise ValueError("Attachment scope does not match installation")
    kind = getattr(source, "launcher_type", None)
    kind = getattr(kind, "value", kind)
    values = [
        getattr(source, "bot_uuid", None),
        kind,
        getattr(source, "launcher_id", None),
        getattr(source, "sender_id", None),
    ]
    if kind not in {"group", "person"} or any(
        value is None or str(value) == "" for value in values
    ):
        raise ValueError("Incomplete attachment scope")
    # Dedicated processes serve one installation/workspace. Shared processes use
    # an owner containing the entire immutable installation binding above.
    return owner, _digest([owner, *[str(value) for value in values]])


@dataclass(frozen=True)
class Attachment:
    bot_uuid: str
    chat_type: str
    chat_id: str
    sender_id: str
    file_id: str
    name: str
    size: int
    url: str = ""
    inline: str = ""


def _attachment(source, file) -> Attachment | None:
    name = getattr(file, "name", "")
    file_id = getattr(file, "id", "")
    url = getattr(file, "url", "") or ""
    inline = getattr(file, "base64", "") or ""
    size = getattr(file, "size", 0)
    if (
        not isinstance(name, str)
        or not name.lower().endswith(".txt")
        or len(name) > 255
        or any(ord(char) < 32 for char in name)
        or not isinstance(file_id, str)
        or len(file_id) > 4096
        or not isinstance(url, str)
        or len(url) > MAX_URL_CHARS
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 <= size <= MAX_BYTES
        or not isinstance(inline, str)
    ):
        return None
    if inline:
        try:
            decode_inline(inline)
        except TextReadError:
            return None
    if not (file_id or url or inline):
        return None
    kind = getattr(source.launcher_type, "value", source.launcher_type)
    return Attachment(
        str(source.bot_uuid),
        kind,
        str(source.launcher_id),
        str(source.sender_id),
        file_id,
        name,
        size,
        url,
        inline,
    )


@dataclass(frozen=True)
class Entry:
    owner: str
    scope: str
    attachment: Attachment
    created: float
    cost: int


class AttachmentCache:
    def __init__(
        self,
        *,
        clock=time.monotonic,
        ttl=CACHE_TTL_SECONDS,
        max_entries=CACHE_MAX_ENTRIES,
        max_bytes=CACHE_MAX_BYTES,
    ):
        self.clock, self.ttl = clock, ttl
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.entries: OrderedDict[str, Entry] = OrderedDict()
        self.latest_tokens = {}
        self.bytes = 0
        self.lock = threading.RLock()

    def _remove(self, token):
        entry = self.entries.pop(token, None)
        if entry:
            self.bytes -= entry.cost
            if self.latest_tokens.get((entry.owner, entry.scope)) == token:
                self.latest_tokens.pop((entry.owner, entry.scope), None)

    def _prune(self):
        for token, entry in list(self.entries.items()):
            if self.clock() - entry.created >= self.ttl:
                self._remove(token)

    def clear(self, owner, scope):
        with self.lock:
            for token, entry in list(self.entries.items()):
                if (entry.owner, entry.scope) == (owner, scope):
                    self._remove(token)

    def put(self, owner, scope, attachment):
        cost = sum(len(str(value).encode()) for value in vars(attachment).values())
        if cost > self.max_bytes or self.max_entries < 1:
            return None
        with self.lock:
            self._prune()
            for previous, entry in self.entries.items():
                if (entry.owner, entry.scope) != (owner, scope):
                    continue
                old = entry.attachment
                if attachment.file_id and attachment.file_id == old.file_id:
                    # A second native report may add a URL; never extend its TTL.
                    merged = replace(
                        attachment,
                        url=attachment.url or old.url,
                        inline=attachment.inline or old.inline,
                    )
                    merged_cost = sum(
                        len(str(value).encode()) for value in vars(merged).values()
                    )
                    if self.bytes - entry.cost + merged_cost <= self.max_bytes:
                        self.entries[previous] = replace(
                            entry, attachment=merged, cost=merged_cost
                        )
                        self.bytes += merged_cost - entry.cost
                    return previous
            while self.entries and (
                len(self.entries) >= self.max_entries
                or self.bytes + cost > self.max_bytes
            ):
                self._remove(next(iter(self.entries)))
            token = uuid.uuid4().hex
            self.entries[token] = Entry(owner, scope, attachment, self.clock(), cost)
            self.latest_tokens[(owner, scope)] = token
            self.bytes += cost
            return token

    def latest(self, owner, scope):
        with self.lock:
            self._prune()
            return self.latest_tokens.get((owner, scope))

    def get(self, owner, scope, token):
        with self.lock:
            self._prune()
            entry = self.entries.get(token)
            return (
                entry.attachment
                if entry and (entry.owner, entry.scope) == (owner, scope)
                else None
            )

    def revoke(self, owner):
        with self.lock:
            for token, entry in list(self.entries.items()):
                if entry.owner == owner:
                    self._remove(token)


_CACHE = AttachmentCache()


def revoke_installation(binding):
    _CACHE.revoke(_binding_owner(binding))


def capture_context(plugin, source):
    owner, scope = _scope(plugin, source)
    direct, quoted = [], []
    has_quote = False
    unresolved = False
    for component in getattr(source, "message_chain", None) or []:
        if isinstance(component, File):
            direct.append(component)
        elif isinstance(component, Quote):
            has_quote = True
            kind = getattr(source.launcher_type, "value", source.launcher_type)
            group = getattr(component, "group_id", None)
            cross_chat = group not in (None, "", 0, "0") and (
                kind != "group" or str(group) != str(source.launcher_id)
            )
            target = getattr(component, "target_id", None)
            if kind == "person" and target not in (None, "", 0, "0"):
                cross_chat = cross_chat or str(target) != str(source.launcher_id)
            if not component.origin or cross_chat:
                unresolved = True
            else:
                quoted.extend(
                    item for item in component.origin or [] if isinstance(item, File)
                )
    files = direct or quoted
    descriptor = {"owner": owner, "scope": scope, "status": "missing", "token": None}
    if len(files) > 1:
        _CACHE.clear(owner, scope)
        descriptor["status"] = "ambiguous"
    elif not direct and (unresolved or (has_quote and not quoted)):
        _CACHE.clear(owner, scope)
        descriptor["status"] = "unresolved_quote"
    elif files:
        attachment = _attachment(source, files[0])
        if attachment is None:
            _CACHE.clear(owner, scope)
            descriptor["status"] = "unsupported"
        else:
            descriptor["token"] = _CACHE.put(owner, scope, attachment)
            descriptor["status"] = "current" if descriptor["token"] else "unavailable"
    else:
        descriptor["token"] = _CACHE.latest(owner, scope)
        if descriptor["token"]:
            descriptor["status"] = "recent"
    return descriptor


def capture_group_upload(
    plugin, bot_uuid, group_id, sender_id, file, **execution_scope
):
    if not isinstance(file, dict):
        return
    source = SimpleNamespace(
        bot_uuid=bot_uuid,
        launcher_type="group",
        launcher_id=group_id,
        sender_id=sender_id,
        **execution_scope,
    )
    owner, scope = _scope(plugin, source)
    try:
        component = File(
            id=file.get("id", ""), name=file.get("name", ""), size=file.get("size", 0)
        )
    except (TypeError, ValueError):
        return
    attachment = _attachment(source, component)
    if attachment:
        _CACHE.put(owner, scope, attachment)
    else:
        _CACHE.clear(owner, scope)


def select_attachment(plugin, variables: dict[str, Any]):
    descriptor = variables.get(QUERY_CONTEXT_KEY)
    if not isinstance(descriptor, dict) or descriptor.get("owner") != _owner(plugin):
        raise TextReadError(MISSING)
    status = descriptor.get("status")
    if status not in {"current", "recent"}:
        raise TextReadError(
            {
                "ambiguous": AMBIGUOUS,
                "unresolved_quote": UNRESOLVED,
                "unsupported": UNSUPPORTED,
            }.get(status, MISSING)
        )
    token, scope = descriptor.get("token"), descriptor.get("scope")
    if not isinstance(token, str) or not isinstance(scope, str):
        raise TextReadError(EXPIRED)
    attachment = _CACHE.get(descriptor["owner"], scope, token)
    if attachment is None:
        raise TextReadError(EXPIRED)
    return attachment
