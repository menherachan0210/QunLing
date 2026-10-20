"""Synthetic public listings only: no GitHub, file downloads or QQ sends."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from pkg import file_links
from pkg.errors import WebError
from pkg.network import Fetched


ROOT_URL = "https://github.com/example/fonts"
API_URL = "https://api.github.com/repos/example/fonts/contents"


def file_item(name="Example.ttf", **overrides):
    item = {
        "name": name,
        "type": "file",
        "size": 1234,
        "download_url": f"https://raw.githubusercontent.com/example/fonts/main/{name}",
        "html_url": f"https://github.com/example/fonts/blob/main/{name}",
        "content": "base64-synthetic-secret",
        "encoding": "base64",
        "sha": "internal-sha-not-returned",
    }
    item.update(overrides)
    return item


def github_fetch(monkeypatch, document, *, final_url=API_URL):
    fetch = AsyncMock(
        return_value=Fetched(
            final_url, json.dumps(document).encode(), "application/json"
        )
    )
    monkeypatch.setattr(file_links, "fetch_public", fetch)
    return fetch


def html(body, *, url="https://example.test/files/", content_type="text/html"):
    raw = body.encode() if isinstance(body, str) else body
    return file_links._html_listing(Fetched(url, raw, content_type))


@pytest.mark.asyncio
async def test_repo_root_uses_contents_without_guessing_default_branch(monkeypatch):
    fetch = github_fetch(monkeypatch, [file_item()])
    result = await file_links.list_file_links(ROOT_URL)
    fetch.assert_awaited_once_with(API_URL, max_bytes=1024 * 1024, timeout=20)
    assert result == {
        "url": ROOT_URL,
        "links": [
            {
                "name": "Example.ttf",
                "url": "https://raw.githubusercontent.com/example/fonts/main/Example.ttf",
                "kind": "file",
                "size": 1234,
            }
        ],
        "truncated": False,
    }
    assert "base64" not in json.dumps(result)
    assert "internal-sha" not in json.dumps(result)


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            ROOT_URL + "/tree/main/字体",
            API_URL + "/%E5%AD%97%E4%BD%93?ref=main",
        ),
        (
            ROOT_URL + "/blob/v1.0/字体/示例.ttf",
            API_URL + "/%E5%AD%97%E4%BD%93/%E7%A4%BA%E4%BE%8B.ttf?ref=v1.0",
        ),
        (
            ROOT_URL + "/tree/feature%2Ffonts/folder",
            API_URL + "/folder?ref=feature%2Ffonts",
        ),
        (ROOT_URL + "/tree/main", API_URL + "?ref=main"),
        (
            ROOT_URL + "/tree/main/%E5%AD%97%E4%BD%93",
            API_URL + "/%E5%AD%97%E4%BD%93?ref=main",
        ),
    ],
)
@pytest.mark.asyncio
async def test_explicit_tree_blob_paths_and_refs_are_encoded(
    monkeypatch, url, expected
):
    fetch = github_fetch(monkeypatch, file_item("字体.ttf"))
    result = await file_links.list_file_links(url)
    assert fetch.call_args.args == (expected,)
    assert "%E5%AD%97%E4%BD%93.ttf" in result["links"][0]["url"]
    assert len(result["links"]) == 1


@pytest.mark.parametrize(
    "suffix",
    [
        "/tree/main/../secret",
        "/tree/main/%2e%2e/secret",
        "/tree/main/%252e%252e/secret",
        "/tree/main/a%2f..%2fsecret",
        "/tree/%2e%2e/folder",
        "/tree/feature%2F..%2Fmain/folder",
        "/tree/main/a%5cb",
        "/tree/main/%00secret",
        "/tree/main/%invalid",
        "/tree/main/%ff",
        "/tree/main//folder",
        "/tree",
        "/blob/main",
    ],
)
@pytest.mark.asyncio
async def test_path_traversal_or_ambiguous_encoding_rejected_before_fetch(
    monkeypatch, suffix
):
    fetch = github_fetch(monkeypatch, [])
    with pytest.raises(WebError):
        await file_links.list_file_links(ROOT_URL + suffix)
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_directories_and_files_project_only_safe_fields(monkeypatch):
    document = [
        {
            "type": "dir",
            "name": "ofl",
            "html_url": ROOT_URL + "/tree/main/ofl",
            "download_url": None,
        },
        file_item(),
        file_item("NoDownload.bin", download_url=None),
        {"type": "symlink", "name": "link", "target": "../secret"},
        file_item("Submodule", submodule_git_url="git://example.test/private.git"),
        file_item("Symbolic", target="private-path"),
        {"type": "submodule", "name": "Submodule2", "html_url": ROOT_URL},
        None,
        "not-an-entry",
    ]
    github_fetch(monkeypatch, document)
    result = await file_links.list_file_links(ROOT_URL)
    assert [(item["name"], item["kind"]) for item in result["links"]] == [
        ("Example.ttf", "file"),
        ("ofl", "directory"),
        ("NoDownload.bin", "page"),
    ]
    assert set(result["links"][0]) == {"name", "url", "kind", "size"}
    assert "size" not in result["links"][-1]
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    "bad",
    [
        "file:///private/font.ttf",
        "http://127.0.0.1/font.ttf",
        "https://user:password@example.test/font.ttf",
        "https://169.254.169.254/secret",
        "data:font/ttf;base64,AAAA",
        "http://example.test:9000/font.ttf",
        "javascript:alert(1)",
    ],
)
@pytest.mark.asyncio
async def test_github_unsafe_link_fields_are_not_exposed(monkeypatch, bad):
    github_fetch(monkeypatch, [file_item(download_url=bad, html_url=bad)])
    result = await file_links.list_file_links(ROOT_URL)
    assert result["links"] == []


@pytest.mark.asyncio
async def test_safe_html_fallback_does_not_claim_an_unreadable_download_is_a_file(
    monkeypatch,
):
    github_fetch(monkeypatch, [file_item(download_url="http://127.0.0.1/private")])
    result = await file_links.list_file_links(ROOT_URL)
    assert result["links"][0]["kind"] == "page"
    assert result["links"][0]["url"] == ROOT_URL + "/blob/main/Example.ttf"


@pytest.mark.parametrize("size", [-1, True, "100", 2**64])
@pytest.mark.asyncio
async def test_untrusted_size_is_omitted(monkeypatch, size):
    github_fetch(monkeypatch, [file_item(size=size)])
    result = await file_links.list_file_links(ROOT_URL)
    assert "size" not in result["links"][0]


@pytest.mark.asyncio
async def test_github_maximum_and_projection_limits(monkeypatch):
    github_fetch(monkeypatch, [file_item(f"{i}.ttf") for i in range(1000)])
    result = await file_links.list_file_links(ROOT_URL)
    assert len(result["links"]) == 60
    assert result["truncated"]
    github_fetch(monkeypatch, [file_item(f"{i}.ttf") for i in range(1001)])
    with pytest.raises(WebError, match="1000"):
        await file_links.list_file_links(ROOT_URL)


@pytest.mark.parametrize(
    "document", [None, 42, "invalid", {"message": "private-secret"}]
)
@pytest.mark.asyncio
async def test_invalid_github_response_is_safe(monkeypatch, document):
    github_fetch(monkeypatch, document)
    with pytest.raises(WebError) as caught:
        await file_links.list_file_links(ROOT_URL)
    assert "private-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_github_redirect_must_still_be_api_origin(monkeypatch):
    github_fetch(monkeypatch, [file_item()], final_url="https://other.example.test/")
    with pytest.raises(WebError, match="来源"):
        await file_links.list_file_links(ROOT_URL)


def test_html_extracts_visible_links_and_uses_public_base():
    result = html("""
        <head><base href="https://cdn.example.test/fonts/"><title>Files</title>
        <a href="/hidden-head.ttf">hidden head</a></head>
        <body><a href="Example.ttf">示例 <strong>字体</strong></a>
        <a href="../index/">父目录</a>
        <a href="details">说明</a>
        <script><a href="/script.ttf">secret</a></script>
        <template><a href="/template.ttf">secret</a></template>
        <div hidden><a href="/hidden.ttf">secret</a></div>
        <a href="/hidden2.ttf" aria-hidden="true">secret</a>
        <a href="/hidden3.ttf" style="display: none">secret</a>
        <a href="/hidden4.ttf" style="visibility:hidden">secret</a>
        </body>
    """)
    assert result["links"] == [
        {
            "name": "示例 字体",
            "url": "https://cdn.example.test/fonts/Example.ttf",
            "kind": "file",
        },
        {
            "name": "父目录",
            "url": "https://cdn.example.test/index/",
            "kind": "directory",
        },
        {
            "name": "说明",
            "url": "https://cdn.example.test/fonts/details",
            "kind": "page",
        },
    ]
    assert "secret" not in json.dumps(result)


def test_hidden_base_cannot_redirect_visible_links():
    result = html("""
        <template><base href="https://hidden.example.test/"></template>
        <base href="http://127.0.0.1/private/">
        <a href="font.ttf">Font</a>
    """)
    assert result["links"][0]["url"] == "https://example.test/files/font.ttf"


def test_html_file_downloads_and_download_pages_prioritized_before_navigation():
    result = html(
        "".join(f'<a href="/navigation-{i}">Nav {i}</a>' for i in range(80))
        + '<a href="/fonts/">字体目录</a>'
        + '<a href="/download-page">下载页面</a>'
        + '<a href="/download?id=3" download="Example.ttf">Download font</a>'
        + '<a href="/real.otf">真实字体</a>'
    )
    assert [(item["name"], item["kind"]) for item in result["links"][:4]] == [
        ("Download font", "file"),
        ("真实字体", "file"),
        ("下载页面", "page"),
        ("字体目录", "directory"),
    ]
    assert len(result["links"]) == 60
    assert result["truncated"]


def test_html_nested_hidden_labels_are_excluded_and_duplicates_collapse():
    result = html("""
        <a href="font.ttf">Open <span hidden>private-secret</span> Font</a>
        <a href="font.ttf#download">Duplicate</a>
        <a href="#local">Anchor</a>
        <a href="mailto:test@example.test">Mail</a>
        <a href="file:///local.ttf">Local</a>
        <a href="javascript:alert(1)">Script</a>
        <a href="http://127.0.0.1/local.ttf">Private</a>
    """)
    assert len(result["links"]) == 1
    assert result["links"][0]["name"] == "Open Font"
    assert result["links"][0]["url"] == "https://example.test/files/font.ttf"


def test_html_names_use_visible_alt_title_or_filename_and_are_bounded():
    result = html(
        '<a href="one.ttf"><img alt="One font"></a>'
        '<a href="two.ttf" title="Two font"></a>'
        '<a href="字体.ttf"></a>'
        '<a href="long.ttf">' + "x" * 2000 + "</a>"
    )
    assert [item["name"] for item in result["links"][:3]] == [
        "One font",
        "Two font",
        "字体.ttf",
    ]
    assert "%E5%AD%97%E4%BD%93.ttf" in result["links"][2]["url"]
    assert len(result["links"][3]["name"]) == 200


def test_malformed_unclosed_anchors_do_not_mix_link_labels():
    result = html('<a href="one.ttf">One<a href="two.ttf">Two</div>')
    assert [
        (item["name"], item["url"].rsplit("/", 1)[-1]) for item in result["links"]
    ] == [("One", "one.ttf"), ("Two", "two.ttf")]


def test_html_item_budget_and_complexity_are_bounded():
    result = html("".join(f'<a href="/{i}.ttf">Font {i}</a>' for i in range(1100)))
    assert len(result["links"]) == 60 and result["truncated"]
    with pytest.raises(WebError, match="复杂"):
        html("<div>" * 300 + '<a href="font.ttf">Font</a>')


@pytest.mark.parametrize("mime", ["application/zip", "font/ttf", "application/json"])
def test_non_html_is_not_treated_as_a_file_list(mime):
    with pytest.raises(WebError, match="不是网页"):
        html("<a href='font.ttf'>Font</a>", content_type=mime)


@pytest.mark.asyncio
async def test_other_github_pages_use_html_without_guessing_api_path(monkeypatch):
    source = ROOT_URL + "/releases"
    fetch = AsyncMock(
        return_value=Fetched(
            source,
            b'<a href="/example/fonts/releases/download/v1/font.zip">ZIP</a>',
            "text/html",
        )
    )
    monkeypatch.setattr(file_links, "fetch_public", fetch)
    result = await file_links.list_file_links(source)
    fetch.assert_awaited_once_with(source, max_bytes=1024 * 1024, timeout=20)
    assert result["links"][0]["kind"] == "file"


@pytest.mark.asyncio
async def test_listing_fetches_one_page_only_and_uses_final_url_for_relative_links(
    monkeypatch,
):
    fetch = AsyncMock(
        return_value=Fetched(
            "https://cdn.example.test/directory/",
            b'<a href="font.ttf">Font</a>',
            "text/html",
        )
    )
    monkeypatch.setattr(file_links, "fetch_public", fetch)
    result = await file_links.list_file_links("https://example.test/start")
    fetch.assert_awaited_once()
    assert result["url"] == "https://cdn.example.test/directory/"
    assert result["links"][0]["url"] == "https://cdn.example.test/directory/font.ttf"


@pytest.mark.asyncio
async def test_response_byte_limit_is_rechecked(monkeypatch):
    monkeypatch.setattr(
        file_links,
        "fetch_public",
        AsyncMock(
            return_value=Fetched(ROOT_URL, b"x" * (1024 * 1024 + 1), "text/html")
        ),
    )
    with pytest.raises(WebError, match="1 MiB"):
        await file_links.list_file_links("https://example.test/")


@pytest.mark.asyncio
async def test_total_deadline_and_external_cancellation(monkeypatch):
    timeout = asyncio.timeout
    managers = []
    cancelled = asyncio.Event()

    def record_timeout(seconds):
        manager = timeout(seconds)
        managers.append(manager)
        return manager

    async def slow(*args, **kwargs):
        managers[0].reschedule(asyncio.get_running_loop().time() - 1)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(file_links.asyncio, "timeout", record_timeout)
    monkeypatch.setattr(file_links, "fetch_public", slow)
    with pytest.raises(WebError, match="20 秒"):
        await file_links.list_file_links(ROOT_URL)
    assert cancelled.is_set()
    monkeypatch.setattr(
        file_links, "fetch_public", AsyncMock(side_effect=asyncio.CancelledError())
    )
    with pytest.raises(asyncio.CancelledError):
        await file_links.list_file_links(ROOT_URL)


@pytest.mark.asyncio
async def test_unexpected_failure_does_not_leak_source_or_payload(monkeypatch):
    monkeypatch.setattr(
        file_links,
        "fetch_public",
        AsyncMock(side_effect=RuntimeError("private-secret")),
    )
    with pytest.raises(WebError) as caught:
        await file_links.list_file_links(ROOT_URL)
    assert "private-secret" not in str(caught.value)
