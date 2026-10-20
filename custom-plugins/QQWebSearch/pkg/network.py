"""Bounded public HTTP reads with DNS pinning and no ambient credentials."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import json
import math
import socket
import ssl
from urllib.parse import urljoin, urlsplit
import zlib

import aiohttp

from pkg.errors import WebError

MAX_URL_CHARS = 8192
MAX_FETCH_BYTES = 5 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 3
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
DOH_URL = "https://1.1.1.1/dns-query"
DOH_TIMEOUT = 5
DOH_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class Fetched:
    url: str
    body: bytes
    content_type: str
    content_disposition: str = ""


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


def validate_public_url(url: str) -> str:
    """Check syntax and literal IPs without DNS; fetching rechecks resolved IPs."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        raise WebError("网页或图片地址无效。")
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise WebError("网页或图片地址无效。")
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
            # Reject abbreviated/decimal IP forms before an HTTP library can
            # interpret them without going through our resolver.
            if ":" in host or host.replace(".", "").isdigit():
                raise ValueError from None
        else:
            if not public_ip(host):
                raise ValueError
    except ValueError:
        raise WebError("仅允许访问公共网络的 HTTP(S) 地址。") from None
    return url


def _fake_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return isinstance(address, ipaddress.IPv4Address) and address in FAKE_IP_NETWORK


def _dns_name(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise WebError("公网 DNS 返回的数据无效。")
    try:
        return value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise WebError("公网 DNS 返回的数据无效。") from None


def _doh_answer(body: bytes, host: str, record_type: int) -> list[tuple[int, str]]:
    try:
        document = json.loads(body)
    except (ValueError, UnicodeError):
        raise WebError("公网 DNS 返回的数据无效。") from None
    if (
        not isinstance(document, dict)
        or type(document.get("Status")) is not int
        or document["Status"] != 0
        or document.get("TC", False) is not False
    ):
        raise WebError("公网 DNS 未能返回完整的解析结果。")
    questions = document.get("Question")
    if (
        not isinstance(questions, list)
        or len(questions) != 1
        or not isinstance(questions[0], dict)
        or type(questions[0].get("type")) is not int
        or questions[0]["type"] != record_type
        or _dns_name(questions[0].get("name")) != _dns_name(host)
    ):
        raise WebError("公网 DNS 返回的问题与请求不匹配。")
    answers = document.get("Answer", [])
    if not isinstance(answers, list) or len(answers) > 128:
        raise WebError("公网 DNS 返回的数据无效。")
    result = []
    for answer in answers:
        if not isinstance(answer, dict) or type(answer.get("type")) is not int:
            raise WebError("公网 DNS 返回的数据无效。")
        kind, value = answer["type"], answer.get("data")
        if kind == 5:  # CNAME is informational; only literal addresses are connected.
            _dns_name(value)
            continue
        if kind not in {1, 28} or not isinstance(value, str):
            raise WebError("公网 DNS 返回的数据无效。")
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise WebError("公网 DNS 返回的地址无效。") from None
        if not public_ip(str(address)):
            raise WebError("公网 DNS 返回了非公共地址，已拒绝读取。")
        expected_version = 4 if kind == 1 else 6
        if kind != record_type or address.version != expected_version:
            raise WebError("公网 DNS 返回的地址类型不匹配。")
        result.append(
            (socket.AF_INET if address.version == 4 else socket.AF_INET6, str(address))
        )
    return result


async def _doh_addresses(host: str, family: int) -> list[tuple[int, str]]:
    """Resolve TUN fake-IP answers via one fixed, verified HTTPS endpoint."""
    record_types = [28] if family == socket.AF_INET6 else [1]
    if family == socket.AF_UNSPEC:
        record_types.append(28)
    try:
        # This nested deadline includes both A and optional AAAA requests. The
        # caller's fetch deadline still covers this resolver and the final GET.
        async with asyncio.timeout(DOH_TIMEOUT):
            connector = aiohttp.TCPConnector(
                ssl=ssl.create_default_context(), use_dns_cache=False, limit=1
            )
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=DOH_TIMEOUT, connect=DOH_TIMEOUT),
                trust_env=False,
                auto_decompress=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                headers={
                    "Accept": "application/dns-json",
                    "Accept-Encoding": "identity",
                    "User-Agent": "QQWebSearch/0.1",
                },
            ) as client:
                for record_type in record_types:
                    async with client.get(
                        DOH_URL,
                        params={
                            "name": _dns_name(host),
                            "type": "AAAA" if record_type == 28 else "A",
                        },
                        allow_redirects=False,
                    ) as response:
                        if response.status != 200:
                            raise WebError("公网 DNS 解析服务暂时不可用。")
                        if (
                            response.headers.get("Content-Encoding", "identity")
                            .strip()
                            .lower()
                            != "identity"
                        ):
                            raise WebError("公网 DNS 返回了不支持的压缩内容。")
                        content_type = response.headers.get("Content-Type", "")
                        if content_type.split(";", 1)[0].strip().lower() not in {
                            "application/dns-json",
                            "application/json",
                        }:
                            raise WebError("公网 DNS 返回的内容类型无效。")
                        declared = response.headers.get("Content-Length")
                        size = None
                        if declared is not None:
                            try:
                                size = int(declared)
                            except (TypeError, ValueError):
                                raise WebError(
                                    "公网 DNS 返回的内容大小无效。"
                                ) from None
                            if not 0 <= size <= DOH_MAX_BYTES:
                                raise WebError("公网 DNS 响应超过允许大小。")
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(4096):
                            if len(chunk) > DOH_MAX_BYTES - len(body):
                                raise WebError("公网 DNS 响应超过允许大小。")
                            body.extend(chunk)
                        if size is not None and len(body) != size:
                            raise WebError("公网 DNS 响应不完整。")
                        addresses = _doh_answer(bytes(body), host, record_type)
                        if addresses:
                            return addresses
        raise WebError("公网 DNS 未找到可用的公共地址。")
    except WebError:
        raise
    except TimeoutError:
        raise WebError("公网 DNS 解析超时，未自动重试。") from None
    except Exception:
        raise WebError("公网 DNS 安全连接或解析失败，未自动重试。") from None


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Return only the exact public addresses that were just validated."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, family=family, type=socket.SOCK_STREAM
            )
        except Exception:
            raise WebError("网页或图片地址解析失败。") from None
        if infos and all(_fake_ip(info[4][0]) for info in infos):
            addresses = await _doh_addresses(host, family)
        else:
            addresses = [(info[0], info[4][0]) for info in infos]
        if not addresses or any(not public_ip(address) for _, address in addresses):
            raise WebError("地址解析到非公共网络，已拒绝读取。")
        unique = {}
        for address_family, address in addresses:
            unique[(address_family, address)] = {
                "hostname": host,
                "host": address,
                "port": port,
                "family": address_family,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST,
            }
        return list(unique.values())[:16]

    async def close(self):
        pass


async def _fetch(url: str, *, max_bytes: int, timeout: float) -> Fetched:
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=timeout, connect=min(10, timeout)),
        trust_env=False,
        auto_decompress=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        headers={
            "Accept": "text/html,text/plain,image/*;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "identity",
            "User-Agent": "QQWebSearch/0.1",
        },
    ) as client:
        visited = set()
        for redirect in range(MAX_REDIRECTS + 1):
            url = validate_public_url(url)
            if url in visited:
                raise WebError("网页或图片重定向形成循环。")
            visited.add(url)
            async with client.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if redirect == MAX_REDIRECTS or not location:
                        raise WebError("网页或图片重定向次数过多或地址无效。")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise WebError(
                        f"网页或图片服务器拒绝读取（HTTP {response.status}）。"
                    )
                encoding = (
                    response.headers.get("Content-Encoding", "identity").strip().lower()
                )
                if encoding not in {"identity", "gzip", "deflate"}:
                    raise WebError("服务器返回了不支持的压缩内容。")
                decoder = None
                if encoding != "identity":
                    decoder = zlib.decompressobj(
                        zlib.MAX_WBITS + 16 if encoding == "gzip" else zlib.MAX_WBITS
                    )
                declared = response.headers.get("Content-Length")
                size = None
                if declared is not None:
                    try:
                        size = int(declared)
                    except (TypeError, ValueError):
                        raise WebError("服务器返回的内容大小无效。") from None
                    if size < 0:
                        raise WebError("服务器返回的内容大小无效。")
                    if size > max_bytes:
                        raise WebError("网页或图片超过允许的下载大小。")
                body = bytearray()
                received_size = 0
                async for chunk in response.content.iter_chunked(16384):
                    if len(chunk) > max_bytes - received_size:
                        raise WebError("网页或图片超过允许的下载大小。")
                    received_size += len(chunk)
                    if decoder is None:
                        output = chunk
                    else:
                        try:
                            # +1 detects overflow without allowing unbounded
                            # expansion; max_length must never be zero.
                            output = decoder.decompress(
                                chunk, max_bytes - len(body) + 1
                            )
                        except zlib.error:
                            raise WebError("服务器返回的压缩内容损坏。") from None
                    if len(output) > max_bytes - len(body):
                        raise WebError("网页或图片解压后超过允许的下载大小。")
                    body.extend(output)
                    if decoder is not None:
                        if decoder.unused_data:
                            raise WebError("服务器返回的压缩内容含有尾随或拼接数据。")
                        if decoder.unconsumed_tail:
                            raise WebError("网页或图片解压后超过允许的下载大小。")
                if decoder is not None and not decoder.eof:
                    raise WebError("服务器返回的压缩内容不完整。")
                if size is not None and received_size != size:
                    raise WebError("网页或图片下载不完整。")
                content_type = response.headers.get("Content-Type", "")
                if not isinstance(content_type, str) or len(content_type) > 256:
                    raise WebError("服务器返回的内容类型无效。")
                disposition = response.headers.get("Content-Disposition", "")
                if (
                    not isinstance(disposition, str)
                    or len(disposition) > 2048
                    or any(ord(char) < 32 or ord(char) == 127 for char in disposition)
                ):
                    raise WebError("服务器返回的文件名信息无效。")
                return Fetched(url, bytes(body), content_type, disposition)
    raise WebError("网页或图片读取失败。")


async def fetch_public(
    url: str, *, max_bytes: int = 2 * 1024 * 1024, timeout: float = 20
) -> Fetched:
    """Fetch at most 5 MiB within one deadline, rechecking each redirect."""
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_FETCH_BYTES:
        raise WebError("下载大小限制无效。")
    if (
        type(timeout) not in {int, float}
        or not math.isfinite(timeout)
        or not 0 < timeout <= 120
    ):
        raise WebError("下载超时设置无效。")
    validate_public_url(url)
    try:
        async with asyncio.timeout(timeout):
            return await _fetch(url, max_bytes=max_bytes, timeout=timeout)
    except WebError:
        raise
    except TimeoutError:
        raise WebError("网页或图片读取超时，未自动重试。") from None
    except Exception:
        raise WebError("网页或图片网络读取失败，未自动重试。") from None


async def fetch_public_file(
    url: str, *, max_bytes: int = MAX_FILE_BYTES, timeout: float = 60
) -> Fetched:
    """Read one public attachment with its own 20 MiB / 60 second limits."""
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_FILE_BYTES:
        raise WebError("附件下载大小限制无效，单文件最多 20 MiB。")
    if (
        type(timeout) not in {int, float}
        or not math.isfinite(timeout)
        or not 0 < timeout <= 60
    ):
        raise WebError("附件下载超时设置无效。")
    validate_public_url(url)
    try:
        async with asyncio.timeout(timeout):
            return await _fetch(url, max_bytes=max_bytes, timeout=timeout)
    except WebError:
        raise
    except TimeoutError:
        raise WebError("附件下载超时，未自动重试。") from None
    except Exception:
        raise WebError("附件网络读取失败，未自动重试。") from None
