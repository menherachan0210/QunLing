"""Public DNS pinning with a narrow TUN fake-IP fallback.

Self-contained so other local image clients can use the same policy. Never
connect to fake/private answers; only all-fake system answers enable DoH.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import ssl

import aiohttp

FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
DOH_URL = "https://1.1.1.1/dns-query"
DOH_TIMEOUT = 5
DOH_MAX_BYTES = 64 * 1024


class PublicNetworkError(Exception):
    """Fixed diagnostic code and safe text, never source addresses or payloads."""

    def __init__(self, message: str, *, code: str = "dns_invalid_response"):
        super().__init__(message)
        self.code = code


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


def _fake_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return isinstance(address, ipaddress.IPv4Address) and address in FAKE_IP_NETWORK


def _dns_name(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise PublicNetworkError("公网 DNS 返回的数据无效。")
    try:
        return value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise PublicNetworkError("公网 DNS 返回的数据无效。") from None


def _doh_answer(body: bytes, host: str, record_type: int) -> list[tuple[int, str]]:
    try:
        document = json.loads(body)
    except (ValueError, UnicodeError):
        raise PublicNetworkError("公网 DNS 返回的数据无效。") from None
    if (
        not isinstance(document, dict)
        or type(document.get("Status")) is not int
        or document["Status"] != 0
        or document.get("TC", False) is not False
    ):
        raise PublicNetworkError("公网 DNS 未能返回完整的解析结果。")
    questions = document.get("Question")
    if (
        not isinstance(questions, list)
        or len(questions) != 1
        or not isinstance(questions[0], dict)
        or type(questions[0].get("type")) is not int
        or questions[0]["type"] != record_type
        or _dns_name(questions[0].get("name")) != _dns_name(host)
    ):
        raise PublicNetworkError("公网 DNS 返回的问题与请求不匹配。")
    answers = document.get("Answer", [])
    if not isinstance(answers, list) or len(answers) > 128:
        raise PublicNetworkError("公网 DNS 返回的数据无效。")
    result = []
    for answer in answers:
        if not isinstance(answer, dict) or type(answer.get("type")) is not int:
            raise PublicNetworkError("公网 DNS 返回的数据无效。")
        kind, value = answer["type"], answer.get("data")
        if kind == 5:  # CNAME is informational; only literal addresses are connected.
            _dns_name(value)
            continue
        if kind not in {1, 28} or not isinstance(value, str):
            raise PublicNetworkError("公网 DNS 返回的数据无效。")
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise PublicNetworkError("公网 DNS 返回的地址无效。") from None
        if not public_ip(str(address)):
            raise PublicNetworkError(
                "公网 DNS 返回了非公共地址，已拒绝读取。", code="dns_non_public"
            )
        expected_version = 4 if kind == 1 else 6
        if kind != record_type or address.version != expected_version:
            raise PublicNetworkError("公网 DNS 返回的地址类型不匹配。")
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
                    "User-Agent": "QQGroupContext/0.1",
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
                            raise PublicNetworkError("公网 DNS 解析服务暂时不可用。")
                        if (
                            response.headers.get("Content-Encoding", "identity")
                            .strip()
                            .lower()
                            != "identity"
                        ):
                            raise PublicNetworkError(
                                "公网 DNS 返回了不支持的压缩内容。"
                            )
                        content_type = response.headers.get("Content-Type", "")
                        if content_type.split(";", 1)[0].strip().lower() not in {
                            "application/dns-json",
                            "application/json",
                        }:
                            raise PublicNetworkError("公网 DNS 返回的内容类型无效。")
                        declared = response.headers.get("Content-Length")
                        size = None
                        if declared is not None:
                            try:
                                size = int(declared)
                            except (TypeError, ValueError):
                                raise PublicNetworkError(
                                    "公网 DNS 返回的内容大小无效。"
                                ) from None
                            if not 0 <= size <= DOH_MAX_BYTES:
                                raise PublicNetworkError("公网 DNS 响应超过允许大小。")
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(4096):
                            if len(chunk) > DOH_MAX_BYTES - len(body):
                                raise PublicNetworkError("公网 DNS 响应超过允许大小。")
                            body.extend(chunk)
                        if size is not None and len(body) != size:
                            raise PublicNetworkError("公网 DNS 响应不完整。")
                        addresses = _doh_answer(bytes(body), host, record_type)
                        if addresses:
                            return addresses
        raise PublicNetworkError("公网 DNS 未找到可用的公共地址。")
    except PublicNetworkError:
        raise
    except TimeoutError:
        raise PublicNetworkError(
            "公网 DNS 解析超时，未自动重试。", code="dns_timeout"
        ) from None
    except Exception:
        raise PublicNetworkError(
            "公网 DNS 安全连接或解析失败，未自动重试。", code="dns_request_failed"
        ) from None


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Return only the exact public addresses that were just validated."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, family=family, type=socket.SOCK_STREAM
            )
        except Exception:
            raise PublicNetworkError(
                "图片地址解析失败，请稍后重试。", code="dns_lookup_failed"
            ) from None
        if infos and all(_fake_ip(info[4][0]) for info in infos):
            addresses = await _doh_addresses(host, family)
        else:
            addresses = [(info[0], info[4][0]) for info in infos]
        if not addresses or any(not public_ip(address) for _, address in addresses):
            raise PublicNetworkError(
                "图片地址解析到非公共网络，已拒绝读取。", code="dns_non_public"
            )
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
