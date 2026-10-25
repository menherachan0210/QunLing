"""Extract bounded source text and image candidates without executing scripts."""

from __future__ import annotations

import asyncio
from email.message import Message
from html.parser import HTMLParser
import re
from urllib.parse import urldefrag, urljoin, urlsplit

from qunling.net_errors import NetworkError as WebError
from qunling.network import Fetched, fetch_public, validate_public_url

MAX_TEXT_CHARS = 16000
MAX_STORED_TEXT_CHARS = 128000
MAX_LINKS = 80
MAX_IMAGES = 16
MAX_TITLE_CHARS = 300
MAX_ALT_CHARS = 200
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
    "head",
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "object",
    "form",
    "button",
    "select",
    "textarea",
    "nav",
    "footer",
}
BREAK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "div",
    "dl",
    "dt",
    "dd",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tr",
    "ul",
}
SMALL_IMAGE_NAME = re.compile(
    r"(?:^|[/_.-])(?:favicon|icon|avatar|sprite|spacer|pixel|loading|loader|badge)"
    r"(?:[/_.-]|$)",
    re.I,
)


def clean_text(value: str) -> str:
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def _decode(fetched: Fetched) -> str:
    header = Message()
    header["content-type"] = fetched.content_type
    charset = header.get_content_charset()
    if fetched.body.startswith(b"\xef\xbb\xbf"):
        charset = "utf-8-sig"
    elif fetched.body.startswith((b"\xff\xfe", b"\xfe\xff")):
        charset = "utf-16"
    if not charset:
        match = re.search(
            rb"<meta\b[^>]*\bcharset\s*=\s*[\"']?\s*([A-Za-z0-9._-]{1,40})",
            fetched.body[:8192],
            re.I,
        )
        if match:
            charset = match.group(1).decode("ascii")
    try:
        return fetched.body.decode(charset or "utf-8", errors="replace")
    except (LookupError, UnicodeError):
        return fetched.body.decode("utf-8", errors="replace")


def _image_url(base: str, value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        url = urldefrag(urljoin(base, value.strip()))[0]
        validate_public_url(url)
        path = urlsplit(url).path
        if path.lower().endswith((".svg", ".ico")) or SMALL_IMAGE_NAME.search(path):
            return None
        return url
    except (WebError, ValueError):
        return None


def _dimension(value) -> float | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:px)?\s*", value, re.I)
    return float(match.group(1)) if match else None


def _small(attrs: dict) -> bool:
    width, height = _dimension(attrs.get("width")), _dimension(attrs.get("height"))
    style = attrs.get("style", "") or ""
    for field in ("width", "height"):
        match = re.search(
            r"(?:^|;)\s*" + field + r"\s*:\s*([\d.]+px)\s*(?:;|$)", style, re.I
        )
        if match:
            if field == "width" and width is None:
                width = _dimension(match.group(1))
            elif field == "height" and height is None:
                height = _dimension(match.group(1))
    return (
        (width is not None and width <= 2)
        or (height is not None and height <= 2)
        or (width is not None and height is not None and max(width, height) <= 96)
    )


def _srcset(value: str | None) -> list[str]:
    if not isinstance(value, str) or len(value) > 16384 or "data:" in value.lower():
        return []
    candidates = []
    for part in value.split(",")[:32]:
        fields = part.strip().split()
        if not fields or len(fields) > 2:
            continue
        score = (0, 1.0)
        if len(fields) == 2:
            match = re.fullmatch(r"(\d+(?:\.\d+)?)(w|x)", fields[1])
            if not match:
                continue
            score = (1 if match.group(2) == "w" else 0, float(match.group(1)))
        candidates.append((score, fields[0]))
    return [value for _, value in sorted(candidates, key=lambda x: x[0], reverse=True)]


class PageParser(HTMLParser):
    def __init__(self, url: str):
        super().__init__(convert_charrefs=True)
        self.url = self.base = url
        self.base_seen = False
        self.stack = []
        self.parts = []
        self.text_size = 0
        self.title_parts = []
        self.published_at = None
        self.title_size = 0
        self.title_active = False
        self.og_images = []
        self.other_images = []
        self.og_seen = set()
        self.other_seen = set()
        self.elements = 0
        self.scripts = 0
        self.links = []
        self.link_seen = set()
        self.active_link = None
        self.text_clipped = False

    def count_element(self):
        self.elements += 1
        if self.elements > 50000 or len(self.stack) > 256:
            raise WebError("网页结构过于复杂，无法安全提取正文。")

    def muted(self):
        return bool(self.stack and self.stack[-1][1])

    def append_text(self, text):
        room = MAX_STORED_TEXT_CHARS * 2 - self.text_size
        self.text_clipped |= len(text) > room
        if room > 0:
            self.parts.append(text[:room])
            self.text_size += min(len(text), room)

    def handle_starttag(self, tag, values):
        self.count_element()
        attrs = {key.lower(): value for key, value in values}
        hidden = (
            "hidden" in attrs
            or str(attrs.get("aria-hidden", "")).lower() == "true"
            or re.search(
                r"(?:display\s*:\s*none|visibility\s*:\s*hidden)",
                attrs.get("style", "") or "",
                re.I,
            )
        )
        parent_muted = self.muted()
        if tag == "script":
            self.scripts += 1
        if tag == "a":
            self.finish_link()
            href = attrs.get("href")
            if (
                not parent_muted and not hidden and isinstance(href, str)
                and href.strip() and not href.lstrip().startswith("#")
            ):
                try:
                    target = validate_public_url(urldefrag(urljoin(self.base, href.strip()))[0])
                except (WebError, ValueError):
                    target = None
                if target and target not in self.link_seen and len(self.links) < MAX_LINKS:
                    self.active_link = {"url": target, "parts": []}
                    if (urlsplit(target).path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
                            and target not in self.other_seen and len(self.other_images) < MAX_IMAGES * 4):
                        self.other_seen.add(target)
                        self.other_images.append({"url": target, "alt": clean_text(attrs.get("title", "") or "")[:MAX_ALT_CHARS]})
        if tag == "base" and not self.base_seen:
            self.base_seen = True
            try:
                self.base = validate_public_url(
                    urldefrag(urljoin(self.url, attrs.get("href", "") or ""))[0]
                )
            except (WebError, ValueError):
                pass
        if tag == "meta":
            property_name = (attrs.get("property") or attrs.get("name") or "").lower()
            if property_name in {"article:published_time", "datepublished", "date"}:
                from datetime import datetime
                value = attrs.get("content") or ""
                if isinstance(value, str) and len(value) <= 40:
                    try:
                        datetime.fromisoformat(value.replace("Z", "+00:00"))
                        self.published_at = value
                    except ValueError:
                        pass
            if property_name in {"og:image", "og:image:secure_url", "twitter:image"}:
                image_url = _image_url(self.base, attrs.get("content"))
                if (
                    image_url
                    and image_url not in self.og_seen
                    and len(self.og_images) < MAX_IMAGES
                ):
                    self.og_seen.add(image_url)
                    self.og_images.append({"url": image_url, "alt": ""})
        if tag == "img" and not parent_muted and not hidden and not _small(attrs):
            candidates = _srcset(attrs.get("srcset") or attrs.get("data-srcset")) + [
                attrs.get("data-original"),
                attrs.get("data-lazy-src"),
                attrs.get("data-url"),
                attrs.get("data-src"),
                attrs.get("src"),
            ]
            for candidate in candidates:
                image_url = _image_url(self.base, candidate)
                if image_url:
                    if (
                        image_url not in self.other_seen
                        and len(self.other_images) < MAX_IMAGES * 4
                    ):
                        self.other_seen.add(image_url)
                        self.other_images.append(
                            {
                                "url": image_url,
                                "alt": clean_text(attrs.get("alt", "") or "")[
                                    :MAX_ALT_CHARS
                                ],
                            }
                        )
                    break
        if tag == "title":
            self.title_active = True
        if tag in BREAK_TAGS and not parent_muted:
            self.append_text("\n")
        if tag not in VOID_TAGS:
            self.stack.append((tag, bool(parent_muted or hidden or tag in SKIP_TAGS)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        self.count_element()
        if tag == "a":
            self.finish_link()
        if tag == "title":
            self.title_active = False
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in BREAK_TAGS and not self.muted():
            self.append_text("\n")

    def handle_data(self, data):
        if self.active_link is not None and not self.muted():
            parts = self.active_link["parts"]
            room = 240 - sum(map(len, parts))
            if room > 0:
                parts.append(data[:room])
        if self.title_active:
            room = MAX_TITLE_CHARS * 2 - self.title_size
            if room > 0:
                self.title_parts.append(data[:room])
                self.title_size += min(len(data), room)
        elif not self.muted():
            self.append_text(data)

    def finish_link(self):
        link, self.active_link = self.active_link, None
        if link is None:
            return
        title = clean_text("".join(link["parts"]))[:240] or urlsplit(link["url"]).path[:240]
        self.links.append({"url": link["url"], "title": title})
        self.link_seen.add(link["url"])

    def result(self):
        self.finish_link()
        images = []
        seen = set()
        for image in self.og_images + self.other_images:
            if image["url"] not in seen:
                seen.add(image["url"])
                images.append(image)
                if len(images) == MAX_IMAGES:
                    break
        text = clean_text("".join(self.parts))
        title = clean_text("".join(self.title_parts))[:MAX_TITLE_CHARS]
        quality = "readable"
        challenge = re.search(
            r"just a moment|attention required|verify (?:that )?you are human|"
            r"checking your browser|access denied|请完成.*验证|安全验证|人机验证",
            title + "\n" + text[:300], re.I,
        )
        if challenge and len(text) < 2000:
            quality = "access_challenge"
        elif not text:
            quality = "javascript_required" if self.scripts else "no_readable_text"
        elif len(text) < 500 and re.search(
            r"(?:enable|turn on|requires?) javascript|javascript (?:is )?(?:required|disabled)|"
            r"请(?:开启|启用|允许).{0,15}javascript|you need to enable javascript",
            text, re.I,
        ):
            quality = "javascript_required"
        elif self.scripts and len(text) < 120 and re.fullmatch(
            r"(?:loading(?:\.\.\.|…)?|正在加载(?:中)?[.…。]*|加载中[.…。]*)",
            text.strip(), re.I,
        ):
            quality = "javascript_required"
        return {
            "url": self.url,
            "title": title,
            **({"published_at": self.published_at} if self.published_at else {}),
            "text": text[:MAX_TEXT_CHARS],
            "full_text": text[:MAX_STORED_TEXT_CHARS],
            "content_truncated": self.text_clipped or len(text) > MAX_STORED_TEXT_CHARS,
            "quality": quality,
            "links": self.links,
            "images": images,
        }


def _parse_page(fetched: Fetched) -> dict:
    content_type = fetched.content_type.split(";", 1)[0].strip().lower()
    if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
        raise WebError("该链接不是可读取的 HTML 网页或纯文本。",
                       code="unsupported_content_type", next_action="use_document_reader_or_other_source")
    decoded = _decode(fetched)
    if content_type == "text/plain":
        text = clean_text(decoded)
        return {
            "url": fetched.url,
            "title": "",
            "text": text[:MAX_TEXT_CHARS],
            "full_text": text[:MAX_STORED_TEXT_CHARS],
            "content_truncated": len(text) > MAX_STORED_TEXT_CHARS,
            "quality": "readable" if text else "no_readable_text",
            "links": [],
            "images": [],
        }
    parser = PageParser(fetched.url)
    parser.feed(decoded)
    parser.close()
    return parser.result()


async def read_page(url: str) -> dict:
    try:
        fetched = await fetch_public(url, max_bytes=2 * 1024 * 1024, timeout=20)
        return await asyncio.to_thread(_parse_page, fetched)
    except WebError:
        raise
    except Exception:
        raise WebError("网页内容解析失败，无法确认正文或图片。",
                       code="page_parse_failed", next_action="try_other_source") from None
