"""Bounded public file listings; never download linked files or execute HTML."""

from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import json
import re
from urllib.parse import (
    quote,
    unquote,
    urldefrag,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

from pkg.errors import WebError
from pkg.network import fetch_public, validate_public_url
from pkg.pages import _decode

MAX_LISTING_BYTES = 1024 * 1024
LISTING_TIMEOUT = 20
MAX_ITEMS = 1000
MAX_LINKS = 60
MAX_NAME_CHARS = 200
FILE_SUFFIXES = (
    ".ttf",
    ".otf",
    ".ttc",
    ".woff",
    ".woff2",
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".pdf",
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".xml",
    ".epub",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".mp3",
    ".wav",
    ".flac",
    ".ogg",
    ".mp4",
    ".webm",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
    ".ico",
)
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "object",
    "select",
    "textarea",
}
HIDDEN_STYLE = re.compile(
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden|content-visibility\s*:\s*hidden)",
    re.I,
)


def _name(value: str) -> str:
    printable = "".join(
        " " if c.isspace() else c for c in value if c.isprintable() or c.isspace()
    )
    return re.sub(r"\s+", " ", printable).strip()[:MAX_NAME_CHARS]


def _public_url(value: str) -> str:
    # Validate before urlsplit, which otherwise strips some control characters.
    value = validate_public_url(value)
    parsed = urlsplit(value)
    encoded = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            quote(parsed.path, safe="/:@!$&'()*+,;=-._~%"),
            quote(parsed.query, safe="=&?/:;+,$@!()*[]'-._~%"),
            "",
        )
    )
    return validate_public_url(encoded)


def _link_url(base: str, value: str | None) -> str | None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value.lstrip().startswith("#")
    ):
        return None
    try:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            return None
        return _public_url(urldefrag(urljoin(base, value.strip()))[0])
    except (WebError, ValueError):
        return None


def _github_segment(value: str, *, ref: bool = False) -> str:
    try:
        if re.search(r"%(?![0-9a-fA-F]{2})", value):
            raise ValueError
        decoded = unquote(value, errors="strict")
        if (
            not decoded
            or len(decoded) > 1024
            or "\\" in decoded
            or any(not char.isprintable() for char in decoded)
            or re.search(r"%[0-9a-fA-F]{2}", decoded)
            or any(part in {"", ".", ".."} for part in decoded.split("/"))
            or (not ref and "/" in decoded)
        ):
            raise ValueError
        return decoded
    except (ValueError, UnicodeError):
        raise WebError("GitHub 地址含有无效或不明确的路径。") from None


def _github_api_url(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.hostname not in {"github.com", "www.github.com"}:
        return None
    segments = parsed.path.strip("/").split("/")
    if len(segments) < 2:
        return None
    owner, repo = (_github_segment(value) for value in segments[:2])
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo)
        or repo in {".", ".."}
    ):
        raise WebError("GitHub 仓库地址无效。")
    path, ref = [], None
    if len(segments) > 2:
        if segments[2] not in {"tree", "blob"}:
            return None
        if len(segments) < 4:
            raise WebError("GitHub 地址缺少分支或版本。")
        ref = _github_segment(segments[3], ref=True)
        path = [_github_segment(value) for value in segments[4:]]
        if segments[2] == "blob" and not path:
            raise WebError("GitHub 文件地址缺少文件路径。")
    api = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/contents"
    if path:
        api += "/" + "/".join(quote(value, safe="") for value in path)
    if ref is not None:
        api += "?" + urlencode({"ref": ref})
    return api


def _finish(url: str, links: list[dict], *, truncated: bool = False) -> dict:
    unique = {}
    for item in links:
        existing = unique.get(item["url"])
        if existing is None or (existing["kind"] == "page" and item["kind"] != "page"):
            unique[item["url"]] = item

    def rank(item):
        if item["kind"] == "file":
            return 0
        if re.search(r"\bdownloads?\b|下载", item["name"], re.I):
            return 1
        return 2 if item["kind"] == "directory" else 3

    ordered = sorted(unique.values(), key=rank)
    return {
        "url": url,
        "links": ordered[:MAX_LINKS],
        "truncated": truncated or len(ordered) > MAX_LINKS,
    }


def _github_listing(url: str, body: bytes) -> dict:
    try:
        document = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise WebError("GitHub 文件列表不是有效的公开目录数据。") from None
    if isinstance(document, list):
        if len(document) > MAX_ITEMS:
            raise WebError("GitHub 文件列表超过 1000 项，请选择更具体的目录。")
        entries = document
    elif isinstance(document, dict) and "type" in document:
        entries = [document]
    else:
        raise WebError("GitHub 文件列表不可用，请使用公开仓库的目录或文件地址。")
    links = []
    for item in entries:
        if not isinstance(item, dict) or item.get("type") not in {"file", "dir"}:
            continue
        if item.get("submodule_git_url") or item.get("target"):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not _name(name):
            continue
        download = (
            _link_url(url, item.get("download_url")) if item["type"] == "file" else None
        )
        html = _link_url(url, item.get("html_url"))
        target = download or html
        if target is None:
            continue
        kind = (
            "directory" if item["type"] == "dir" else ("file" if download else "page")
        )
        projected = {"name": _name(name), "url": target, "kind": kind}
        size = item.get("size")
        if kind == "file" and type(size) is int and 0 <= size <= 2**63 - 1:
            projected["size"] = size
        links.append(projected)
    # GitHub's contents endpoint caps directory listings at 1000 entries.
    return _finish(
        url, links, truncated=isinstance(document, list) and len(entries) == MAX_ITEMS
    )


class FileLinkParser(HTMLParser):
    def __init__(self, url: str):
        super().__init__(convert_charrefs=True)
        self.url = self.base = url
        self.base_seen = False
        self.stack = []
        self.active = None
        self.links = []
        self.seen = set()
        self.truncated = False
        self.elements = 0

    def muted(self):
        return bool(self.stack and self.stack[-1][1])

    def finish_link(self):
        active, self.active = self.active, None
        if active is None:
            return
        name = _name("".join(active.pop("text")))
        parsed = urlsplit(active["url"])
        name = (
            name
            or _name(active.pop("fallback"))
            or _name(unquote(parsed.path.rsplit("/", 1)[-1]))
            or parsed.hostname
        )
        active.pop("fallback", None)
        active["name"] = name
        if active["url"] not in self.seen:
            if len(self.links) >= MAX_ITEMS:
                self.truncated = True
                return
            self.seen.add(active["url"])
            self.links.append(active)

    def handle_starttag(self, tag, values):
        self.elements += 1
        if self.elements > 50000 or len(self.stack) > 256:
            raise WebError("网页结构过于复杂，无法安全提取文件链接。")
        attrs = dict(values)
        hidden = (
            "hidden" in attrs
            or str(attrs.get("aria-hidden", "")).lower() == "true"
            or HIDDEN_STYLE.search(attrs.get("style") or "") is not None
        )
        parent_muted = self.muted()
        muted = parent_muted or hidden or tag in SKIP_TAGS
        if tag == "base" and not self.base_seen and not muted:
            self.base_seen = True
            self.base = _link_url(self.url, attrs.get("href")) or self.base
        if tag == "a":
            self.finish_link()
            if not muted and not any(frame[0] == "head" for frame in self.stack):
                target = _link_url(self.base, attrs.get("href"))
                if target is not None:
                    parsed = urlsplit(target)
                    path = unquote(parsed.path).lower()
                    kind = (
                        "file"
                        if "download" in attrs or path.endswith(FILE_SUFFIXES)
                        else "directory"
                        if path.endswith("/")
                        else "page"
                    )
                    self.active = {
                        "url": target,
                        "kind": kind,
                        "text": [],
                        "fallback": attrs.get("title") or attrs.get("download") or "",
                    }
        if tag == "img" and self.active and not muted and attrs.get("alt"):
            self.handle_data(attrs["alt"][:MAX_NAME_CHARS])
        if tag not in VOID_TAGS:
            self.stack.append((tag, muted))

    def handle_data(self, data):
        if self.active is not None and not self.muted():
            available = MAX_NAME_CHARS - sum(len(part) for part in self.active["text"])
            if available > 0:
                self.active["text"].append(data[:available])

    def handle_endtag(self, tag):
        if tag == "a":
            self.finish_link()
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                if any(frame[0] == "a" for frame in self.stack[i:]):
                    self.finish_link()
                del self.stack[i:]
                break

    def handle_startendtag(self, tag, values):
        self.handle_starttag(tag, values)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def result(self):
        self.finish_link()
        return _finish(self.url, self.links, truncated=self.truncated)


def _html_listing(fetched) -> dict:
    mime = fetched.content_type.partition(";")[0].strip().lower()
    if mime not in {"text/html", "application/xhtml+xml", ""}:
        raise WebError("该地址不是网页文件列表，请选择目录页或下载页。")
    parser = FileLinkParser(_public_url(fetched.url))
    parser.feed(_decode(fetched))
    parser.close()
    return parser.result()


async def list_file_links(url: str) -> dict:
    """List at most 60 public candidates from one page or GitHub contents call."""
    try:
        url = _public_url(url)
        api = _github_api_url(url)
        async with asyncio.timeout(LISTING_TIMEOUT):
            fetched = await fetch_public(
                api or url, max_bytes=MAX_LISTING_BYTES, timeout=LISTING_TIMEOUT
            )
            if len(fetched.body) > MAX_LISTING_BYTES:
                raise WebError("文件列表超过 1 MiB，请选择更具体的目录或网页。")
            if api:
                if urlsplit(fetched.url).hostname != "api.github.com":
                    raise WebError("GitHub 文件列表返回了无效的来源地址。")
                return await asyncio.to_thread(_github_listing, url, fetched.body)
            return await asyncio.to_thread(_html_listing, fetched)
    except WebError:
        raise
    except TimeoutError:
        raise WebError("文件列表读取超过 20 秒，未自动重试。") from None
    except Exception:
        raise WebError("公开文件列表读取失败，请稍后重试或提供目录页地址。") from None
