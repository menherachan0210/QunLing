"""Bounded archive downloads with pinned public DNS resolution."""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp

from pkg.archive import ArchiveError

MAX_BYTES = 40 * 1024 * 1024
MAX_URL_CHARS = 8192
TOTAL_TIMEOUT = 60


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
    return (
        address.is_global
        and not (address.is_multicast or address.is_reserved)
        and (mapped is None or mapped.is_global)
    )


def validate_url(url: str) -> str:
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        raise ArchiveError("附件下载地址无效，请重新上传或引用 压缩包。")
    if any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ArchiveError("附件下载地址无效，请重新上传或引用 压缩包。")
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
        raise ArchiveError(
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
            raise ArchiveError("附件地址解析到非公共网络，已拒绝读取。")
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
        raise ArchiveError("压缩包 超过 40 MiB，请拆分后再发送。")
    payload = value
    if value.startswith("data:"):
        prefix, comma, payload = value.partition(",")
        if not comma or ";base64" not in prefix:
            raise ArchiveError("压缩包 附件编码无效，请重新上传。")
    elif value.startswith("base64://"):
        payload = value[len("base64://") :]
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise ArchiveError("压缩包 附件编码无效，请重新上传。") from None
    if len(raw) > MAX_BYTES:
        raise ArchiveError("压缩包 超过 40 MiB，请拆分后再发送。")
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
            "Accept": "application/zip, application/vnd.rar, application/x-7z-compressed, application/octet-stream;q=0.9",
            "Accept-Encoding": "identity",
        },
    ) as client:
        for redirect in range(4):
            validate_url(url)
            async with client.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if redirect == 3 or not location:
                        raise ArchiveError("附件下载重定向无效，请重新上传 压缩包。")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise ArchiveError("压缩包 下载失败，请重新上传或引用文件。")
                if (
                    response.headers.get("Content-Encoding", "identity").lower()
                    != "identity"
                ):
                    raise ArchiveError(
                        "附件下载返回了压缩内容，请重新上传未压缩的 压缩包。"
                    )
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except ValueError:
                        raise ArchiveError("附件下载大小信息无效。") from None
                    if declared_size < 0 or declared_size > MAX_BYTES:
                        raise ArchiveError("压缩包 超过 40 MiB，请拆分后再发送。")
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise ArchiveError("压缩包 超过 40 MiB，请拆分后再发送。")
                return bytes(body)
    raise ArchiveError("压缩包 下载失败，请重新上传或引用文件。")


async def download(url: str) -> bytes:
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT):
            return await _download(url)
    except ArchiveError:
        raise
    except TimeoutError:
        raise ArchiveError("压缩包 读取超过 60 秒，请稍后重新上传或引用。") from None
    except Exception:
        raise ArchiveError("压缩包 下载失败，请重新上传或引用文件。") from None
