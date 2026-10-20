"""Bounded, installation-scoped image context; no image bytes enter query vars."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from langbot_plugin.api.entities.builtin.platform.message import Image, Quote

from main import GeneratedImage, ImageGenerationError


QUERY_CONTEXT_KEY = "ai_image_plugin.image_context.v1"
CACHE_TTL_SECONDS = 30 * 60
CACHE_MAX_ENTRIES = 32
CACHE_MAX_BYTES = 64 * 1024 * 1024
MAX_INLINE_CHARS = 4 * ((10 * 1024 * 1024 + 2) // 3) + 128
MAX_URL_CHARS = 8192
MISSING_IMAGE = "没有可用的近期图片，请发送或引用一张要修改的图片。"
AMBIGUOUS_IMAGE = "本条消息包含多张图片，请单独发送或引用一张要修改的图片。"
UNRESOLVED_QUOTE = "引用图片暂时无法读取，请重新发送或重新引用要修改的图片。"
EXPIRED_IMAGE = "图片已过期或缓存不可用，请重新发送或引用一张图片。"


def _digest(parts: list[Any]) -> str:
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def _binding_owner(binding: Any) -> str:
    fields = [
        getattr(binding, name, None)
        for name in (
            "instance_uuid",
            "workspace_uuid",
            "installation_uuid",
            "runtime_revision",
        )
    ]
    if any(value is None or value == "" for value in fields):
        raise ValueError("Incomplete installation binding")
    return _digest(["installation", *fields])


def _owner(plugin: Any) -> str:
    getter = getattr(plugin, "get_installation_binding", None)
    binding = getter() if callable(getter) else None
    if binding is not None:
        return _binding_owner(binding)
    # A random identity prevents Python object-id reuse from reviving old data.
    identity = getattr(plugin, "_image_dedicated_context_owner", None)
    if identity is None:
        identity = uuid.uuid4().hex
        plugin._image_dedicated_context_owner = identity
    return _digest(["dedicated", identity])


def _scope(plugin: Any, query: Any) -> tuple[str, str]:
    owner = _owner(plugin)
    binding_getter = getattr(plugin, "get_installation_binding", None)
    binding = binding_getter() if callable(binding_getter) else None
    if binding is not None:
        for field in ("instance_uuid", "workspace_uuid"):
            value = getattr(query, field, None)
            if value is not None and value != getattr(binding, field, None):
                raise ValueError("Image scope does not match installation")
    launcher_type = getattr(query, "launcher_type", None)
    launcher_type = getattr(launcher_type, "value", launcher_type)
    identities = [
        getattr(query, "bot_uuid", None),
        launcher_type,
        getattr(query, "launcher_id", None),
        getattr(query, "sender_id", None),
    ]
    if launcher_type not in {"person", "group"} or any(
        value is None or str(value) == "" for value in identities
    ):
        raise ValueError("Incomplete platform image scope")
    return owner, _digest(
        [
            owner,
            getattr(query, "instance_uuid", None),
            getattr(query, "workspace_uuid", None),
            getattr(query, "placement_generation", None),
            *[str(value) for value in identities],
        ]
    )


def _usable_image(image: Any) -> GeneratedImage | None:
    inline = getattr(image, "base64", None)
    url = getattr(image, "url", None)
    if isinstance(inline, str) and inline:
        if len(inline) > MAX_INLINE_CHARS or not inline.startswith("data:image/"):
            return None
        return GeneratedImage(base64=inline)
    if isinstance(url, str) and url and len(url) <= MAX_URL_CHARS:
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
            ):
                return GeneratedImage(url=url)
        except ValueError:
            pass
    return None


@dataclass(frozen=True)
class _Entry:
    owner: str
    scope: str
    image: GeneratedImage
    created: float
    size: int


class ImageContextCache:
    """Keep one latest image per scope, with global count, byte and age bounds."""

    def __init__(
        self,
        *,
        clock=time.monotonic,
        ttl=CACHE_TTL_SECONDS,
        max_entries=CACHE_MAX_ENTRIES,
        max_bytes=CACHE_MAX_BYTES,
    ):
        self._clock = clock
        self._ttl = ttl
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._latest: dict[tuple[str, str], str] = {}
        self._bytes = 0
        self._lock = threading.RLock()

    def _remove(self, token: str) -> None:
        entry = self._entries.pop(token, None)
        if entry is not None:
            self._bytes -= entry.size
            if self._latest.get((entry.owner, entry.scope)) == token:
                self._latest.pop((entry.owner, entry.scope), None)

    def _prune(self) -> None:
        now = self._clock()
        for token, entry in list(self._entries.items()):
            if now - entry.created >= self._ttl:
                self._remove(token)

    def put(self, owner: str, scope: str, image: Any) -> str | None:
        image = _usable_image(image)
        if image is None:
            return None
        size = len((image.base64 or "").encode()) + len((image.url or "").encode())
        if size > self._max_bytes or self._max_entries < 1:
            return None
        with self._lock:
            self._prune()
            previous = self._latest.get((owner, scope))
            if previous:
                self._remove(previous)
            while self._entries and (
                len(self._entries) >= self._max_entries
                or self._bytes + size > self._max_bytes
            ):
                self._remove(next(iter(self._entries)))
            token = uuid.uuid4().hex
            self._entries[token] = _Entry(owner, scope, image, self._clock(), size)
            self._latest[(owner, scope)] = token
            self._bytes += size
            return token

    def latest(self, owner: str, scope: str) -> str | None:
        with self._lock:
            self._prune()
            return self._latest.get((owner, scope))

    def clear_scope(self, owner: str, scope: str) -> None:
        with self._lock:
            token = self._latest.get((owner, scope))
            if token:
                self._remove(token)

    def get(self, owner: str, scope: str, token: str) -> GeneratedImage | None:
        with self._lock:
            self._prune()
            entry = self._entries.get(token)
            if entry is None or (entry.owner, entry.scope) != (owner, scope):
                return None
            return entry.image

    def revoke(self, owner: str) -> None:
        with self._lock:
            for token, entry in list(self._entries.items()):
                if entry.owner == owner:
                    self._remove(token)


_CACHE = ImageContextCache()


def revoke_installation(binding: Any) -> None:
    """Called by the plugin's installation revocation hook."""
    _CACHE.revoke(_binding_owner(binding))


def query_claim_lock(plugin: Any) -> asyncio.Lock:
    """Share a data-free claim mutex across generation and editing components."""
    lock = getattr(plugin, "_image_query_claim_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        plugin._image_query_claim_lock = lock
    return lock


def capture_context(plugin: Any, query: Any) -> dict[str, str | None]:
    owner, scope = _scope(plugin, query)
    images = []
    direct_image_count = 0
    unresolved_quote = False
    for component in getattr(query, "message_chain", None) or []:
        if isinstance(component, Image):
            images.append(component)
            direct_image_count += 1
        elif isinstance(component, Quote):
            if not component.origin:
                unresolved_quote = True
            images.extend(
                item for item in (component.origin or []) if isinstance(item, Image)
            )
        if len(images) > 1:
            break
    descriptor: dict[str, str | None] = {
        "owner": owner,
        "scope": scope,
        "status": "missing",
        "token": None,
    }
    if unresolved_quote and direct_image_count != 1:
        # An explicit unreadable target must never silently select an older image.
        _CACHE.clear_scope(owner, scope)
        descriptor["status"] = "unresolved_quote"
    elif len(images) > 1:
        _CACHE.clear_scope(owner, scope)
        descriptor["status"] = "ambiguous"
    elif images:
        # A newer unusable source must never leave an older image as fallback.
        _CACHE.clear_scope(owner, scope)
        descriptor["token"] = _CACHE.put(owner, scope, images[0])
        descriptor["status"] = "current" if descriptor["token"] else "unavailable"
    else:
        descriptor["token"] = _CACHE.latest(owner, scope)
        if descriptor["token"]:
            descriptor["status"] = "recent"
    return descriptor


def _context(plugin: Any, variables: dict[str, Any]) -> dict[str, Any]:
    descriptor = variables.get(QUERY_CONTEXT_KEY)
    if not isinstance(descriptor, dict) or descriptor.get("owner") != _owner(plugin):
        raise ImageGenerationError(MISSING_IMAGE)
    scope = descriptor.get("scope")
    if not isinstance(scope, str) or len(scope) != 64:
        raise ImageGenerationError(MISSING_IMAGE)
    return descriptor


def select_image(plugin: Any, variables: dict[str, Any]) -> GeneratedImage:
    descriptor = _context(plugin, variables)
    if descriptor.get("status") == "unresolved_quote":
        raise ImageGenerationError(UNRESOLVED_QUOTE)
    if descriptor.get("status") == "ambiguous":
        raise ImageGenerationError(AMBIGUOUS_IMAGE)
    if descriptor.get("status") not in {"current", "recent"}:
        raise ImageGenerationError(MISSING_IMAGE)
    token = descriptor.get("token")
    image = (
        _CACHE.get(descriptor["owner"], descriptor["scope"], token)
        if isinstance(token, str)
        else None
    )
    if image is None:
        raise ImageGenerationError(EXPIRED_IMAGE)
    return image


async def remember_output(plugin: Any, reply_api: Any, image: GeneratedImage) -> None:
    """Call only after successful delivery; cache failures must not resend images."""
    try:
        variables = await reply_api.get_query_vars()
        descriptor = _context(plugin, variables)
        _CACHE.put(descriptor["owner"], descriptor["scope"], image)
    except Exception:
        pass
