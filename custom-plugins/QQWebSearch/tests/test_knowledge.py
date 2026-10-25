import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from pkg import knowledge
from pkg.context import QueryContext, QueryStore
from pkg.errors import WebError
from pkg.page_tools import read_pages, read_cached_page
from qunling.knowledge.store import Store
from qunling.scope import Scope

URL = "https://public.example/article"


@pytest.mark.asyncio
async def test_batch_reads_concurrently_once_and_tracks_only_returned_ranges():
    plugin = NS(web_queries=QueryStore())
    context = QueryContext(1, "one")
    active = maximum = 0
    calls = []
    async def reader(url):
        nonlocal active, maximum
        calls.append(url)
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(.01)
        active -= 1
        if url.endswith("bad"):
            raise WebError("unavailable", code="fetch_failed")
        return {"url": url, "title": "test", "text": "x"*9000, "quality": "readable"}
    result = await read_pages(plugin, context, {"urls": [URL, URL, URL+"2", URL+"bad"]}, reader)
    assert maximum == 2 and len(calls) == 3
    assert result["status"] == "partial" and len(result["pages"]) == 3
    first = result["pages"][0]
    assert len(first["text"]) == 4000 and first["next_offset"] == 4000 and first["read_status"] == "partial"
    rest = await read_cached_page(plugin, context, {"page_ref": first["page_ref"], "offset": 4000}, reader)
    assert rest["read_status"] == "read" and len(calls) == 3
    with pytest.raises(WebError):
        await read_cached_page(plugin, QueryContext(2, "two"), {"page_ref": first["page_ref"]}, reader)


@pytest.mark.asyncio
async def test_search_cache_reuse_freshness_failure_and_scope(tmp_path, monkeypatch):
    store = Store(tmp_path / "evidence.db")
    scope = Scope("workspace", "bot", "group:1", "actor")
    binding = [store, scope, False]
    async def access(*args):
        return binding
    monkeypatch.setattr(knowledge, "access", access)
    plugin = NS(get_config=lambda: {})
    searcher = AsyncMock(return_value={"answer": "Evidence", "sources": [{"url": URL}]})
    first = await knowledge.search(plugin, None, "stable topic", "auto", searcher)
    second = await knowledge.search(plugin, None, "stable  topic", "auto", searcher)
    assert searcher.await_count == 1 and second["cache"]["hit"]
    assert "cache" not in first
    await knowledge.search(plugin, None, "stable topic", "live", searcher)
    binding[2] = True
    await knowledge.search(plugin, None, "stable topic", "auto", searcher)
    binding[:] = [store, replace(scope, actor_id="other"), False]
    await knowledge.search(plugin, None, "stable topic", "auto", searcher)
    assert searcher.await_count == 4
    searcher.side_effect = WebError("failed")
    for _ in range(2):
        with pytest.raises(WebError):
            await knowledge.search(plugin, None, "failing topic", "auto", searcher)
    assert searcher.await_count == 6


@pytest.mark.asyncio
async def test_live_url_refreshes_older_evidence_but_keeps_old_reference_frozen():
    plugin, context = NS(web_queries=QueryStore()), QueryContext(1, "one")
    reader = AsyncMock(side_effect=[{"url": URL, "title": "old", "text": "old", "cache": {"hit": True}},
                                   {"url": URL, "title": "new", "text": "new"}])
    old = await read_cached_page(plugin, context, {"url": URL}, reader)
    new = await read_cached_page(plugin, context, {"url": URL, "freshness": "live"}, reader)
    frozen = await read_cached_page(plugin, context, {"page_ref": old["page_ref"]}, reader)
    assert new["text"] == "new" and frozen["text"] == "old" and reader.await_count == 2
