"""Offline multi-step browsing regressions: real parser, frozen query cache."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pkg import context as context_module, pages, search, tools
from pkg.context import QueryContext, QueryStore
from pkg.errors import WebError
from pkg.network import Fetched
from pkg.page_tools import read_cached_page, source_read_status


def page(url="https://one.example/article", text="正文", **extra):
    return {
        "url": url, "title": "Article", "text": text[:pages.MAX_TEXT_CHARS],
        "full_text": text, "quality": "readable", "links": [],
        "content_truncated": False, **extra,
    }


def environment():
    context = QueryContext(1, "query-a")
    store = QueryStore()
    store.entries["query-a"] = context
    plugin = SimpleNamespace(
        web_queries=store, plugin_runtime_handler=object(), get_config=lambda: {}
    )
    return plugin, context


def parsed(html):
    return pages._parse_page(
        Fetched("https://one.example/list", html.encode(), "text/html; charset=utf-8")
    )


def test_page_exposes_only_observed_safe_links_with_titles_and_base():
    value = parsed("""
    <head><base href="/catalog/"></head>
    <p>资料 <a href="detail#section"><strong>完整详情</strong></a></p>
    <a href="detail">重复链接</a><a href="#local">本地锚点</a>
    <a href="https://two.example/compare">比较</a>
    <a href="http://127.0.0.1/secret">私网</a>
    <a href="javascript:alert(1)">脚本</a><a hidden href="/hidden">隐藏</a>
    <div style="display:none"><a href="/hidden-parent">隐藏父节点</a></div>
    """)
    assert value["links"] == [
        {"url": "https://one.example/catalog/detail", "title": "完整详情"},
        {"url": "https://two.example/compare", "title": "比较"},
    ]
    assert value["quality"] == "readable"


@pytest.mark.parametrize("html,quality", [
    ("<div id='app'></div><script src='/bundle.js'></script>", "javascript_required"),
    ("<p>Loading...</p><script>render()</script>", "javascript_required"),
    ("<p>You need to enable JavaScript to run this app.</p>", "javascript_required"),
    ("<title>Just a moment</title><p>Verify that you are human</p>", "access_challenge"),
    ("<title>Empty</title>", "no_readable_text"),
    ("<p>简短但完整的天气报道。</p><script>analytics()</script>", "readable"),
])
def test_quality_distinguishes_shell_challenge_and_real_short_content(html, quality):
    assert parsed(html)["quality"] == quality


def test_links_and_stored_page_text_are_bounded():
    value = parsed(
        "<p>" + "字" * (pages.MAX_STORED_TEXT_CHARS + 10) + "</p>"
        + "".join(f'<a href="/{n}">详情{n}</a>' for n in range(100))
    )
    assert len(value["links"]) == pages.MAX_LINKS
    assert len(value["text"]) == pages.MAX_TEXT_CHARS
    assert len(value["full_text"]) == pages.MAX_STORED_TEXT_CHARS
    assert value["content_truncated"] is True


def test_pdf_is_structured_handoff_instead_of_false_html_success():
    with pytest.raises(WebError) as caught:
        pages._parse_page(Fetched("https://one.example/report.pdf", b"%PDF", "application/pdf"))
    assert caught.value.code == "unsupported_content_type"
    assert caught.value.next_action == "use_document_reader_or_other_source"


@pytest.mark.asyncio
async def test_long_page_paging_is_stable_and_marks_read_only_after_complete():
    plugin, context = environment()
    text = "".join(f"第{i}节：内容" for i in range(7000))
    reader = AsyncMock(return_value=page(text=text))
    result = await read_cached_page(plugin, context, {"url": "https://one.example/article"}, reader)
    ref, pieces = result["page_ref"], [result["text"]]
    assert result["read_status"] == "partial"
    # Simulate the remote page changing; cached offsets must stay on this snapshot.
    reader.return_value = page(text="网页已更新")
    while result["next_offset"] is not None:
        result = await read_cached_page(
            plugin, context, {"page_ref": ref, "offset": result["next_offset"]}, reader
        )
        pieces.append(result["text"])
    assert "".join(pieces) == text
    assert result["read_status"] == "read"
    assert source_read_status(context, "https://one.example/article") == "read"
    assert context.counts["page"] == 1
    reader.assert_awaited_once()


@pytest.mark.asyncio
async def test_find_locates_late_content_using_original_unicode_offsets():
    plugin, context = environment()
    text = "ßİ" * 14000 + "证据 KEYWORD 正文" + "尾" * 1000
    reader = AsyncMock(return_value=page(text=text))
    found = await read_cached_page(
        plugin, context, {"url": "https://one.example/article", "find_text": "keyword"}, reader
    )
    assert found["match_offset"] == text.index("KEYWORD")
    assert found["text"] == text[found["offset"]:found["offset"] + pages.MAX_TEXT_CHARS]
    assert found["read_status"] == "partial"
    miss = await read_cached_page(
        plugin, context, {"page_ref": found["page_ref"], "find_text": "不存在"}, reader
    )
    assert miss["status"] == "not_found" and not miss["text"]
    assert source_read_status(context, "https://one.example/article") == "partial"
    assert reader.await_count == 1


@pytest.mark.asyncio
async def test_cross_query_reference_rejected_without_fetch():
    plugin, first = environment()
    reader = AsyncMock(return_value=page())
    result = await read_cached_page(plugin, first, {"url": "https://one.example/article"}, reader)
    second = QueryContext(2, "query-b")
    with pytest.raises(WebError) as caught:
        await read_cached_page(plugin, second, {"page_ref": result["page_ref"]}, reader)
    assert caught.value.code == "unknown_page_ref"
    assert not second.pages
    assert reader.await_count == 1


@pytest.mark.asyncio
async def test_redirect_alias_and_concurrent_reads_share_one_frozen_fetch():
    plugin, context = environment()
    reader = AsyncMock(return_value=page(url="https://one.example/final"))
    results = await asyncio.gather(*[
        read_cached_page(plugin, context, {"url": "https://one.example/start#part"}, reader)
        for _ in range(2)
    ])
    final = await read_cached_page(
        plugin, context, {"url": "https://one.example/final"}, reader
    )
    assert len({r["page_ref"] for r in [*results, final]}) == 1
    reader.assert_awaited_once_with("https://one.example/start")


@pytest.mark.asyncio
async def test_failed_url_is_not_retried_by_repeated_tool_calls():
    plugin, context = environment()
    reader = AsyncMock(side_effect=WebError(
        "访问被拒绝。", code="access_denied", next_action="try_other_source"
    ))
    for _ in range(2):
        with pytest.raises(WebError) as caught:
            await read_cached_page(plugin, context, {"url": "https://one.example/denied"}, reader)
        assert caught.value.code == "access_denied"
        assert caught.value.retryable is False
    assert context.counts["page"] == 1
    reader.assert_awaited_once()


@pytest.mark.asyncio
async def test_challenge_keeps_observed_links_but_never_marks_page_as_read():
    plugin, context = environment()
    reader = AsyncMock(return_value=parsed(
        '<title>Just a moment</title><p>Verify you are human</p><a href="/public">公开资料</a>'
    ))
    result = await read_cached_page(plugin, context, {"url": "https://one.example/list"}, reader)
    assert result["status"] == "error" and result["code"] == "access_challenge"
    assert result["links"][0]["url"] == "https://one.example/public"
    assert result["page_ref"] in context.pages
    assert source_read_status(context, "https://one.example/list") == "not_read"


@pytest.mark.asyncio
async def test_new_fetch_limit_does_not_prevent_cached_paging_and_memory_is_bounded(monkeypatch):
    plugin, context = environment()
    reader = AsyncMock(side_effect=lambda url: page(url=url, text="文" * 20000))
    values = [
        await read_cached_page(plugin, context, {"url": f"https://one.example/{i}"}, reader)
        for i in range(4)
    ]
    with pytest.raises(WebError) as caught:
        await read_cached_page(plugin, context, {"url": "https://one.example/5"}, reader)
    assert caught.value.code == "tool_budget_exhausted"
    result = await read_cached_page(
        plugin, context, {"page_ref": values[0]["page_ref"], "offset": 16000}, reader
    )
    assert result["read_status"] == "read" and reader.await_count == 4
    before = len(context.pages)
    monkeypatch.setattr(context_module, "MAX_BYTES", 20)
    with pytest.raises(WebError, match="缓存"):
        plugin.web_queries.remember_page(context, "https://one.example/extra", page())
    assert len(context.pages) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {}, {"url": "https://one.example", "page_ref": "x"},
    {"page_ref": 1}, {"url": "https://one.example", "offset": True},
    {"url": "https://one.example", "offset": -1},
    {"url": "https://one.example", "find_text": ""},
    {"url": "https://one.example", "find_text": "x\nsecret"},
    {"url": "https://one.example", "recipient": "other"},
])
async def test_bad_page_arguments_do_not_start_network(params):
    plugin, context = environment()
    reader = AsyncMock()
    with pytest.raises(WebError):
        await read_cached_page(plugin, context, params, reader)
    reader.assert_not_awaited()


def search_output():
    return [
        {"type": "web_search_call", "status": "completed", "action": {"sources": [
            {"url": "https://two.example/list", "title": "原始资料"},
            {"url": "https://three.example/statement", "title": "另一机构"},
            {"url": "http://127.0.0.1/private", "title": "不可用"},
        ]}},
        {"type": "message", "content": [{"type": "output_text", "text": "服务商摘要",
         "annotations": [
             {"type": "url_citation", "url": f"https://www.one.example/{i}", "title": f"引用{i}"}
             for i in range(20)
         ]}]},
    ]


def test_actual_extra_sources_survive_citation_crowding_and_domain_prioritization():
    result = search.parse_results(search_output(), True)
    assert len(result["sources"]) == search.MAX_SOURCES
    assert [v["domain"] for v in result["sources"][:3]] == [
        "one.example", "two.example", "three.example"
    ]
    assert result["sources"][0]["cited"] is True
    assert result["sources"][1]["cited"] is False
    assert all(v["read_status"] == "not_read" for v in result["sources"])
    assert result["sources_truncated"] is True and result["source_domains"] == 3
    assert result["answer_kind"] == "provider_search_summary"


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["403", "script_shell"])
async def test_offline_chain_failed_first_site_to_observed_detail_and_conflicting_source(
    monkeypatch, first_failure
):
    plugin, context = environment()
    plugin.web_queries.get = AsyncMock(return_value=context)
    monkeypatch.setattr(tools, "QueryBasedAPIProxy", lambda **kwargs: object())
    monkeypatch.setattr(tools, "search_web", AsyncMock(
        side_effect=lambda *a, **kw: search.parse_results(search_output(), True)
    ))
    detail_text = "前文" * 10000 + "原始公告：发布日期 2026 年 10 月 20 日。"

    async def reader(url):
        if "one.example" in url:
            if first_failure == "403":
                raise WebError("HTTP 403", code="access_denied", next_action="try_other_source")
            return page(url, text="", quality="javascript_required")
        if url.endswith("/list"):
            return page(url, text="目录", links=[
                {"url": "https://two.example/detail", "title": "完整公告"}
            ])
        if url.endswith("/detail"):
            return page(url, text=detail_text)
        return page(url, text="第三方转载称发布日期是 2026 年 10 月 21 日，尚未解释差异。")

    fetch = AsyncMock(side_effect=reader)
    monkeypatch.setattr(tools, "read_page", fetch)

    async def call(action, params):
        return json.loads(await tools.execute(SimpleNamespace(plugin=plugin), action, params, 1, "query-a"))

    sources = (await call("search", {"query": "公告发布时间"}))["sources"]
    failed = await call("page", {"url": sources[0]["url"]})
    assert failed["status"] == "error" and failed["retryable"] is False
    listing = await call("page", {"url": sources[1]["url"]})
    detail = await call("page", {"url": listing["links"][0]["url"], "find_text": "原始公告"})
    assert "10 月 20 日" in detail["text"] and detail["read_status"] == "partial"
    opposing = await call("page", {"url": sources[2]["url"]})
    assert "10 月 21 日" in opposing["text"] and opposing["read_status"] == "read"
    # The evidence stays separate; the test does not pretend a model has judged it.
    refreshed = await call("search", {"query": "公告来源比较"})
    statuses = {v["domain"]: v["read_status"] for v in refreshed["sources"]}
    assert statuses["two.example"] == statuses["three.example"] == "read"
    assert statuses["one.example"] == "not_read"
    assert fetch.await_count == 4 and context.active == 0
