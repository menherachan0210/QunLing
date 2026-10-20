from __future__ import annotations

import asyncio

import pytest

from pkg import pages
from pkg.errors import WebError
from pkg.network import Fetched


def parsed(html, *, url="https://example.test/article/page", content_type="text/html"):
    body = html.encode("utf-8") if isinstance(html, str) else html
    return pages._parse_page(Fetched(url, body, content_type))


def test_extracts_title_readable_text_and_decodes_entities_without_executing_scripts():
    value = parsed("""
        <html><head><title>Spring &amp; Notes</title>
        <style>private-css</style><script>private-script</script></head>
        <body><nav>private-navigation</nav><main><h1>中文介绍</h1>
        <p>A light <b>comedy</b> &amp; short stories.</p>
        <p hidden>private-hidden</p><p style="display: none">private-hidden2</p>
        <iframe>private-frame</iframe><template>private-template</template>
        </main><footer>private-footer</footer></body></html>
    """)
    assert value["title"] == "Spring & Notes"
    assert value["text"] == "中文介绍\nA light comedy & short stories."
    assert "private-" not in value["text"]
    assert value["url"] == "https://example.test/article/page"


def test_og_images_are_prioritized_and_deduplicated_with_body_images():
    value = parsed("""
        <head><meta property="og:image" content="/cover.jpg">
        <meta property="og:image:secure_url" content="/cover.jpg"></head>
        <img src="/cover.jpg"><img data-src="//cdn.example.test/second.png" alt="Second &amp; book">
        <img src="/third.png">
    """)
    assert value["images"] == [
        {"url": "https://example.test/cover.jpg", "alt": ""},
        {"url": "https://cdn.example.test/second.png", "alt": "Second & book"},
        {"url": "https://example.test/third.png", "alt": ""},
    ]


def test_repeated_images_do_not_consume_the_candidate_budget():
    value = parsed(
        '<img src="/repeated.jpg">' * 100
        + '<img src="/another.jpg">'
        + '<meta property="og:image" content="/og-repeated.jpg">' * 30
        + '<meta property="og:image" content="/og-another.jpg">'
    )
    assert [image["url"].rsplit("/", 1)[1] for image in value["images"]] == [
        "og-repeated.jpg",
        "og-another.jpg",
        "repeated.jpg",
        "another.jpg",
    ]


def test_srcset_prefers_largest_candidate_and_lazy_src_over_placeholder():
    value = parsed("""
        <img src="/small.jpg" srcset="/medium.jpg 640w, /large.jpg 1280w">
        <img src="/tiny.jpg" data-src="/full.jpg">
        <img src="/one.jpg" srcset="/one.jpg 1x, /two.jpg 2x">
    """)
    assert [x["url"] for x in value["images"]] == [
        "https://example.test/large.jpg",
        "https://example.test/full.jpg",
        "https://example.test/two.jpg",
    ]


def test_base_url_resolves_candidates_and_invalid_base_cannot_target_private_network():
    value = parsed('<base href="https://cdn.example.test/books/"><img src="cover.png">')
    assert value["images"][0]["url"] == "https://cdn.example.test/books/cover.png"
    value = parsed('<base href="http://127.0.0.1/private/"><img src="cover.png">')
    assert value["images"][0]["url"] == "https://example.test/article/cover.png"


@pytest.mark.parametrize(
    "markup",
    [
        '<img src="http://127.0.0.1/private.png">',
        '<img src="http://user:password@example.test/private.png">',
        '<img src="data:image/png;base64,AAAA">',
        '<img src="file:///private.png">',
        '<img src="/favicon.png">',
        '<img src="/assets/icon-small.png">',
        '<img src="/image.svg">',
        '<img src="/image.ico">',
        '<img src="/image.jpg" width="32" height="32">',
        '<img src="/tracking.gif" width="1">',
        '<img src="/image.jpg" style="width: 40px; height: 40px">',
        '<div aria-hidden="true"><img src="/hidden.jpg"></div>',
        '<template><img src="/template.jpg"></template>',
    ],
)
def test_private_embedded_nonbitmap_icon_and_tiny_candidates_are_excluded(markup):
    assert parsed(markup)["images"] == []


def test_logo_images_remain_candidates_for_explicit_logo_searches():
    value = parsed(
        '<img src="/company-logo.png" width="400" height="240" alt="Company logo">'
    )
    assert value["images"] == [
        {"url": "https://example.test/company-logo.png", "alt": "Company logo"}
    ]


def test_invalid_first_srcset_candidate_falls_back_to_valid_public_candidate():
    value = parsed(
        '<img src="/fallback.jpg" srcset="http://127.0.0.1/private.jpg 2000w, /large.jpg 1200w">'
    )
    assert value["images"] == [{"url": "https://example.test/large.jpg", "alt": ""}]


def test_text_title_alt_and_image_count_are_bounded():
    value = parsed(
        "<title>"
        + "t" * 1000
        + "</title><p>"
        + "x" * 20000
        + "</p>"
        + "".join(f'<img src="/cover-{i}.jpg" alt="{"a" * 500}">' for i in range(30))
    )
    assert len(value["title"]) == 300
    assert len(value["text"]) == 16000
    assert len(value["images"]) == 16
    assert all(len(item["alt"]) <= 200 for item in value["images"])


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        ("中文正文".encode("gb18030"), "text/plain; charset=gb18030", "中文正文"),
        (
            "日本語本文".encode("shift_jis"),
            "text/plain; charset=shift_jis",
            "日本語本文",
        ),
        ("中文正文".encode("utf-8-sig"), "text/plain", "中文正文"),
        (
            b"plain <script>literal</script>",
            "text/plain",
            "plain <script>literal</script>",
        ),
        (b"plain text", "text/plain; charset=invalid-encoding", "plain text"),
    ],
)
def test_plain_text_is_not_html_and_uses_declared_encoding(
    body, content_type, expected
):
    value = parsed(body, content_type=content_type)
    assert value["text"] == expected and value["images"] == []


def test_html_meta_charset_is_used_if_http_header_does_not_specify_one():
    html = '<meta charset="shift_jis"><p>日本語本文</p>'.encode("shift_jis")
    assert parsed(html)["text"] == "日本語本文"


@pytest.mark.parametrize(
    "content_type", ["image/png", "application/pdf", "application/json", ""]
)
def test_non_html_non_text_documents_are_rejected(content_type):
    with pytest.raises(WebError, match="不是"):
        parsed("<p>not relevant</p>", content_type=content_type)


def test_deep_or_malformed_html_does_not_allow_unbounded_parser_work():
    with pytest.raises(WebError, match="复杂"):
        parsed("<div>" * 300 + "text")


@pytest.mark.asyncio
async def test_read_page_preserves_final_source_url_and_uses_bounded_fetch(monkeypatch):
    async def fetch(url, **kwargs):
        assert url == "https://example.test/start"
        assert kwargs == {"max_bytes": 2 * 1024 * 1024, "timeout": 20}
        return Fetched("https://example.test/final", b"<p>Result</p>", "text/html")

    monkeypatch.setattr(pages, "fetch_public", fetch)
    value = await pages.read_page("https://example.test/start")
    assert value == {
        "url": "https://example.test/final",
        "title": "",
        "text": "Result",
        "full_text": "Result",
        "content_truncated": False,
        "quality": "readable",
        "links": [],
        "images": [],
    }


@pytest.mark.asyncio
async def test_network_errors_remain_safe_and_cancellation_propagates(monkeypatch):
    async def failed(*args, **kwargs):
        raise RuntimeError("https://private.example/?secret=never-log")

    monkeypatch.setattr(pages, "fetch_public", failed)
    with pytest.raises(WebError) as caught:
        await pages.read_page("https://example.test/")
    assert "never-log" not in str(caught.value)
    assert "private.example" not in str(caught.value)

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(pages, "fetch_public", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await pages.read_page("https://example.test/")
