"""Ephemeral image references belong only to the trusted current query."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import secrets
import time

from pkg.errors import WebError


KEY = "qqwebsearch.query.v1"
TTL = 900
MAX_QUERIES = 32
MAX_BYTES = 64 * 1024 * 1024


@dataclass
class QueryContext:
    query_id: int
    query_uuid: str | None
    owner: object = None
    created: float = field(default_factory=time.monotonic)
    counts: dict = field(default_factory=dict)
    images: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)
    pages: dict = field(default_factory=dict)
    searches: dict = field(default_factory=dict)
    image_queries: set = field(default_factory=set)
    page_urls: dict = field(default_factory=dict)
    page_failures: dict = field(default_factory=dict)
    read_urls: set = field(default_factory=set)
    page_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    page_fetch_locks: dict = field(default_factory=dict)
    page_slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2))
    file_delivery: dict = field(default_factory=dict)
    delivery: str | None = None
    active: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def claim(self, action, limit):
        count = self.counts.get(action, 0)
        if count >= limit:
            raise WebError("本轮该工具已达到调用上限，请使用已有结果；不要重复调用。",
                           code="tool_budget_exhausted", next_action="use_existing_evidence")
        self.counts[action] = count + 1


class QueryStore:
    def __init__(self):
        self.entries = {}
        self.lock = asyncio.Lock()

    def clear(self):
        self.entries.clear()

    def prune(self):
        now = time.monotonic()
        for token, context in list(self.entries.items()):
            if now - context.created > TTL and not context.active:
                del self.entries[token]

    async def get(self, api, query_id, query_uuid, owner=None):
        async with self.lock:
            self.prune()
            variables = await api.get_query_vars()
            if not isinstance(variables, dict):
                raise WebError("无法确认当前会话，未执行联网操作。")
            token = variables.get(KEY)
            if token:
                context = self.entries.get(token) if isinstance(token, str) else None
                if (
                    context is None
                    or context.query_id != query_id
                    or context.query_uuid != query_uuid
                    or context.owner != owner
                ):
                    raise WebError("本轮联网结果已过期或不属于当前问题，请重新提问。")
                return context
            token = secrets.token_urlsafe(24)
            context = QueryContext(query_id, query_uuid, owner=owner)
            if len(self.entries) >= MAX_QUERIES:
                disposable = next(
                    (key for key, item in self.entries.items() if not item.active), None
                )
                if disposable is None:
                    raise WebError("当前联网请求较多，请稍后再试。")
                self.entries.pop(disposable)
            await api.set_query_var(KEY, token)
            self.entries[token] = context
            return context

    def remember(self, context, image):
        import hashlib
        digest = hashlib.sha256(image["data"]).digest()
        for ref, existing in context.images.items():
            if hashlib.sha256(existing["data"]).digest() == digest:
                return ref
        self.reserve_bytes(len(image["data"]))
        ref = "webimg_" + secrets.token_urlsafe(9)
        context.images[ref] = image
        return ref

    def reserve_bytes(self, added):
        size = sum(
            len(value["data"])
            for c in self.entries.values()
            for collection in (c.images, c.files)
            for value in collection.values()
        )
        size += sum(
            len(json.dumps(page, ensure_ascii=False).encode("utf-8"))
            for context in self.entries.values()
            for collection in (context.pages, context.searches) for page in collection.values()
        )
        if size + added > MAX_BYTES:
            raise WebError("临时文件或图片缓存已满，请稍后重新提问。")

    def remember_page(self, context, url, page):
        self.reserve_bytes(len(json.dumps(page, ensure_ascii=False).encode("utf-8")))
        ref = "webpage_" + secrets.token_urlsafe(12)
        context.pages[ref] = page
        context.page_urls[url] = ref
        context.page_urls[page["url"]] = ref
        return ref

    def remember_file(self, context, item):
        for ref, existing in context.files.items():
            if existing["sha256"] == item["sha256"]:
                return ref
        self.reserve_bytes(len(item["data"]))
        ref = "webfile_" + secrets.token_urlsafe(12)
        context.files[ref] = item
        return ref

    def remember_search(self, context, query, result):
        item = {"query": query, **result}
        self.reserve_bytes(len(json.dumps(item, ensure_ascii=False).encode("utf-8")))
        ref = "websearch_" + secrets.token_urlsafe(12)
        context.searches[ref] = item
        return ref
