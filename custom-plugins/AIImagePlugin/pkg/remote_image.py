"""Bounded public reference downloads; callers still validate actual image bytes."""

from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urljoin, urlsplit

import aiohttp

from pkg.public_network import PublicNetworkError, PublicResolver, public_ip

MAX_URL_CHARS = 8192
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DOWNLOAD_TIMEOUT = 20
MAX_REDIRECTS = 3


class RemoteImageError(Exception):
    """Machine-readable diagnostics without source URLs or upstream error text."""

    def __init__(self, code: str, *, status: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status


def validate_reference_url(url: str) -> str:
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        raise RemoteImageError("invalid_url")
    if "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise RemoteImageError("invalid_url")
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
            # aiohttp may treat alternative numeric forms as IPs and skip the
            # resolver; reject ambiguous forms before starting any HTTP request.
            if ":" in host or host.replace(".", "").isdigit():
                raise ValueError from None
        else:
            if not public_ip(host):
                raise ValueError
    except ValueError:
        raise RemoteImageError("invalid_url") from None
    return url


async def _download_reference(url: str) -> bytes:
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT, connect=10),
        trust_env=False,
        auto_decompress=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        headers={
            "Accept": "image/png,image/jpeg,image/webp,image/gif,image/bmp;q=0.9",
            "Accept-Encoding": "identity",
        },
    ) as client:
        visited = set()
        for redirect in range(MAX_REDIRECTS + 1):
            url = validate_reference_url(url)
            if url in visited:
                raise RemoteImageError("invalid_response")
            visited.add(url)
            async with client.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if redirect == MAX_REDIRECTS or not location:
                        raise RemoteImageError("invalid_response")
                    url = urljoin(url, location)
                    continue
                if (
                    type(response.status) is not int
                    or not 100 <= response.status <= 599
                ):
                    raise RemoteImageError("invalid_response")
                if response.status != 200:
                    raise RemoteImageError("http_status", status=response.status)
                if (
                    response.headers.get("Content-Encoding", "identity").strip().lower()
                    != "identity"
                ):
                    raise RemoteImageError("invalid_response")
                declared = response.headers.get("Content-Length")
                size = None
                if declared is not None:
                    try:
                        size = int(declared)
                    except (TypeError, ValueError):
                        raise RemoteImageError("invalid_response") from None
                    if size < 0:
                        raise RemoteImageError("invalid_response")
                    if size > MAX_IMAGE_BYTES:
                        raise RemoteImageError("too_large")
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    if len(chunk) > MAX_IMAGE_BYTES - len(body):
                        raise RemoteImageError("too_large")
                    body.extend(chunk)
                if size is not None and len(body) != size:
                    raise RemoteImageError("invalid_response")
                if not body:
                    raise RemoteImageError("invalid_body")
                # MIME and filename are untrusted; the caller checks real image
                # signatures before sending these bytes to the editing service.
                return bytes(body)
    raise RemoteImageError("invalid_response")


async def fetch_reference_image(url: str) -> bytes:
    """Read at most 10 MiB in one 20-second window, including DNS and redirects."""
    validate_reference_url(url)
    try:
        async with asyncio.timeout(DOWNLOAD_TIMEOUT):
            return await _download_reference(url)
    except RemoteImageError:
        raise
    except TimeoutError:
        raise RemoteImageError("timeout") from None
    except PublicNetworkError as exc:
        raise RemoteImageError(
            "timeout" if exc.code == "dns_timeout" else "network"
        ) from None
    except Exception:
        raise RemoteImageError("network") from None
