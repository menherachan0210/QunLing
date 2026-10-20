"""Bounded text decoding and public-network downloads with DNS pinning."""

from __future__ import annotations

import asyncio
import base64
import binascii
import codecs
import ipaddress
import json
import socket
import unicodedata
from urllib.parse import urljoin, urlsplit

import aiohttp

MAX_BYTES = 128 * 1024
MAX_CHARS = 16000
MAX_RESULT_CHARS = 19000
MAX_URL_CHARS = 8192
TOTAL_TIMEOUT = 30


class TextReadError(Exception):
    """A fixed, safe error that may be returned to the user."""


def public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and (
        address in ipaddress.ip_network("64:ff9b::/96")
        or address in ipaddress.ip_network("64:ff9b:1::/48")
        or address.sixtofour is not None
        or address.teredo is not None
    ):
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_global and (mapped is None or mapped.is_global)


def validate_url(url: str) -> str:
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        raise TextReadError("附件下载地址无效，请重新上传或引用 TXT。")
    if any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise TextReadError("附件下载地址无效，请重新上传或引用 TXT。")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or "%" in host
            or parsed.port not in {None, 80, 443}
        ):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # aiohttp treats numeric and colon-bearing hosts as literal IPs and
            # bypasses its resolver; reject non-canonical forms such as 127.1.
            if ":" in host or host.replace(".", "").isdigit():
                raise ValueError from None
        else:
            if not public_ip(host):
                raise ValueError
    except ValueError:
        raise TextReadError(
            "附件地址不允许访问，只支持公共 HTTP(S) 文件地址。"
        ) from None
    return url


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Resolve once, reject non-public results, then connect to those exact IPs."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        if not infos or any(not public_ip(info[4][0]) for info in infos):
            raise TextReadError("附件地址解析到非公共网络，已拒绝读取。")
        return [
            {
                "hostname": host,
                "host": info[4][0],
                "port": port,
                "family": info[0],
                "proto": info[2],
                "flags": socket.AI_NUMERICHOST,
            }
            for info in infos
        ]

    async def close(self):
        pass


def decode_inline(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > 4 * ((MAX_BYTES + 2) // 3) + 128:
        raise TextReadError("TXT 超过 128 KiB，请拆分后再发送。")
    payload = value
    if value.startswith("data:"):
        prefix, comma, payload = value.partition(",")
        if not comma or ";base64" not in prefix:
            raise TextReadError("TXT 附件编码无效，请重新上传。")
    elif value.startswith("base64://"):
        payload = value[len("base64://") :]
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise TextReadError("TXT 附件编码无效，请重新上传。") from None
    if len(raw) > MAX_BYTES:
        raise TextReadError("TXT 超过 128 KiB，请拆分后再发送。")
    return raw


async def _download(url: str) -> bytes:
    resolver = PublicResolver()
    connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
    timeout = aiohttp.ClientTimeout(total=TOTAL_TIMEOUT, connect=10)
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        trust_env=False,
        auto_decompress=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        headers={
            "Accept": "text/plain, application/octet-stream;q=0.9",
            "Accept-Encoding": "identity",
        },
    ) as client:
        for redirect in range(4):
            validate_url(url)
            async with client.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if redirect == 3 or not location:
                        raise TextReadError("附件下载重定向无效，请重新上传 TXT。")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise TextReadError("TXT 下载失败，请重新上传或引用文件。")
                if (
                    response.headers.get("Content-Encoding", "identity").lower()
                    != "identity"
                ):
                    raise TextReadError(
                        "附件下载返回了压缩内容，请重新上传未压缩的 TXT。"
                    )
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except ValueError:
                        raise TextReadError("附件下载大小信息无效。") from None
                    if declared_size < 0 or declared_size > MAX_BYTES:
                        raise TextReadError("TXT 超过 128 KiB，请拆分后再发送。")
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise TextReadError("TXT 超过 128 KiB，请拆分后再发送。")
                return bytes(body)
    raise TextReadError("TXT 下载失败，请重新上传或引用文件。")


async def download(url: str) -> bytes:
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT):
            return await _download(url)
    except TextReadError:
        raise
    except TimeoutError:
        raise TextReadError("TXT 读取超过 30 秒，请稍后重新上传或引用。") from None
    except Exception:
        raise TextReadError("TXT 下载失败，请重新上传或引用文件。") from None


def decode_text(raw: bytes) -> tuple[str, str]:
    if len(raw) > MAX_BYTES:
        raise TextReadError("TXT 超过 128 KiB，请拆分后再发送。")
    if raw.startswith(
        (
            b"PK\x03\x04",
            b"%PDF-",
            b"\x89PNG",
            b"GIF87a",
            b"GIF89a",
            b"\xff\xd8\xff",
            b"\x7fELF",
        )
    ):
        raise TextReadError("附件不是纯文本 TXT，请发送文本文件。")
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise TextReadError("暂不支持 UTF-32，请另存为 UTF-8 TXT。")
    encodings = (
        ["utf-8-sig"]
        if raw.startswith(codecs.BOM_UTF8)
        else ["utf-16"]
        if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
        else ["utf-8", "gb18030"]
    )
    for encoding in encodings:
        try:
            text = raw.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
        if any(
            unicodedata.category(char) == "Cc" and char not in "\t\n\r" for char in text
        ):
            raise TextReadError("附件包含二进制或控制字符，请另存为纯文本 TXT。")
        if len(text) > MAX_CHARS:
            raise TextReadError("TXT 正文超过 16000 字符，无法完整读取；请拆分文件。")
        return text, encoding
    raise TextReadError("TXT 编码无法可靠识别，请另存为 UTF-8 后发送。")


def result_json(name: str, raw: bytes, declared_size: int = 0) -> str:
    if declared_size > 0 and len(raw) != declared_size:
        raise TextReadError(
            "TXT 下载内容大小与附件信息不一致，无法确认完整性，请重新上传。"
        )
    text, encoding = decode_text(raw)
    result = json.dumps(
        {"name": name, "encoding": encoding, "text": text, "complete": True},
        ensure_ascii=False,
    )
    if len(result) > MAX_RESULT_CHARS:
        raise TextReadError("TXT 的完整工具结果超过长度限制，请拆分文件后发送。")
    return result
