from __future__ import annotations

import asyncio
import gzip
import json
import socket
import ssl
from types import SimpleNamespace
import zlib

import pytest
from unittest.mock import AsyncMock

from pkg import network
from pkg.errors import WebError
from qunling import network as shared_network
from qunling.net_errors import NetworkError


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "file:///tmp/private.png",
        "C:\\private.png",
        "data:image/png;base64,AA==",
        "http://127.0.0.1/a",
        "https://10.0.0.1/a",
        "https://169.254.169.254/a",
        "http://192.168.1.1/a",
        "http://100.64.0.1/a",
        "http://198.18.1.12/a",
        "https://[::1]/a",
        "http://224.0.0.1/a",
        "http://[ff0e::1]/a",
        "http://[::ffff:127.0.0.1]/a",
        "http://[64:ff9b::7f00:1]/a",
        "http://[2002:7f00:1::]/a",
        "http://127.1/a",
        "http://2130706433/a",
        "http://0177.0.0.1/a",
        "http://u:p@example.test/a",
        "http://example.test:9090/a",
        "http://example.test:bad/a",
        "http://example.test/a#fragment",
        "http://%31%32%37.0.0.1/a",
        "http://example.test/\r\nsecret",
        "http://example.test\\private",
        "http://[fe80::1%25zone]/a",
        "https://example.test/" + "a" * network.MAX_URL_CHARS,
    ],
)
def test_unsafe_or_ambiguous_urls_rejected_without_dns(url, monkeypatch):
    def never_dns(*args, **kwargs):
        pytest.fail("syntax validation must not resolve DNS")

    monkeypatch.setattr(socket, "getaddrinfo", never_dns)
    with pytest.raises(WebError):
        network.validate_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/image",
        "http://8.8.8.8:80/image",
        "https://[2606:4700:4700::1111]/image",
    ],
)
def test_public_http_urls_are_accepted(url):
    assert network.validate_public_url(url) == url


def test_shared_transport_preserves_plugin_patch_points_and_error_identity():
    assert network is shared_network
    assert WebError is NetworkError


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_transient_get_failure_retries_once_then_returns_bytes(monkeypatch, status):
    calls = fake_session(monkeypatch, [Response(status=status), Response(b"recovered")])
    result = await network.fetch_public("https://example.test/retry")
    assert result.body == b"recovered"
    assert calls == ["https://example.test/retry"] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [
    (401, "authentication_required"), (403, "access_denied"), (404, "not_found"),
    (429, "rate_limited"), (501, "http_server_error"),
])
async def test_auth_denied_quota_and_permanent_status_do_not_retry(monkeypatch, status, code):
    calls = fake_session(monkeypatch, [Response(status=status)])
    with pytest.raises(WebError) as caught:
        await network.fetch_public("https://example.test/denied")
    assert caught.value.code == code and caught.value.retryable is False
    assert caught.value.attempts == 1 and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    TimeoutError(), network.aiohttp.ClientConnectionError("synthetic-secret"),
    network.aiohttp.ClientPayloadError("synthetic-secret"),
])
async def test_get_transport_transients_retry_once_with_shared_deadline(monkeypatch, failure):
    result = network.Fetched("https://example.test/file", b"okay", "text/plain")
    fetch = AsyncMock(side_effect=[failure, result])
    monkeypatch.setattr(network, "_fetch", fetch)
    assert await network.fetch_public(result.url, timeout=2) == result
    assert fetch.await_count == 2
    assert all(0 < call.kwargs["timeout"] <= 2 for call in fetch.await_args_list)


@pytest.mark.asyncio
async def test_get_exhausts_two_attempts_without_leaking_transport_exception(monkeypatch):
    fetch = AsyncMock(side_effect=network.aiohttp.ClientConnectionError("private-token"))
    monkeypatch.setattr(network, "_fetch", fetch)
    with pytest.raises(WebError) as caught:
        await network.fetch_public("https://example.test/file", timeout=2)
    assert fetch.await_count == caught.value.attempts == 2
    assert caught.value.retryable is False and caught.value.code == "connection_failed"
    assert caught.value.next_action == "try_other_source"
    assert "private-token" not in json.dumps(caught.value.result())


@pytest.mark.asyncio
async def test_tls_verification_failure_is_not_retried(monkeypatch):
    fetch = AsyncMock(side_effect=network.aiohttp.ServerFingerprintMismatch(
        b"expected", b"actual", "example.test", 443
    ))
    monkeypatch.setattr(network, "_fetch", fetch)
    with pytest.raises(WebError) as caught:
        await network.fetch_public("https://example.test/file")
    assert caught.value.code == "tls_error"
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_short_deadline_does_not_create_retry_or_outlive_total_budget(monkeypatch):
    async def slow(*args, **kwargs):
        await asyncio.sleep(5)
    fetch = AsyncMock(side_effect=slow)
    monkeypatch.setattr(network, "_fetch", fetch)
    start = asyncio.get_running_loop().time()
    with pytest.raises(WebError) as caught:
        await network.fetch_public("https://example.test/file", timeout=0.03)
    assert asyncio.get_running_loop().time() - start < 1
    assert caught.value.code == "network_timeout"
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_document_transport_accepts_40mib_budget_but_file_default_stays_20mib(monkeypatch):
    fetched = network.Fetched("https://example.test/doc", b"document", "application/pdf")
    fetch = AsyncMock(return_value=fetched)
    monkeypatch.setattr(network, "_fetch", fetch)
    await network.fetch_public_file(fetched.url)
    assert fetch.await_args.kwargs["max_bytes"] == 20 * 1024 * 1024
    await network.fetch_public_file(fetched.url, max_bytes=40 * 1024 * 1024)
    assert fetch.await_args.kwargs["max_bytes"] == 40 * 1024 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("addresses", [[], ["127.0.0.1"], ["8.8.8.8", "127.0.0.1"]])
async def test_dns_rejects_empty_private_or_mixed_answers(monkeypatch, addresses):
    async def resolve(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 443))
            for host in addresses
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(WebError, match="非公共"):
        await network.PublicResolver().resolve("images.example.test", 443)


@pytest.mark.asyncio
async def test_dns_pins_only_exact_validated_addresses(monkeypatch):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))] * 2

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)

    async def never_doh(*args):
        pytest.fail("normal public DNS must not use DoH")

    monkeypatch.setattr(network, "_doh_addresses", never_doh)
    assert await network.PublicResolver().resolve("images.example.test", 443) == [
        {
            "hostname": "images.example.test",
            "host": "8.8.8.8",
            "port": 443,
            "family": socket.AF_INET,
            "proto": 6,
            "flags": socket.AI_NUMERICHOST,
        }
    ]


@pytest.mark.asyncio
async def test_dns_failure_is_safe(monkeypatch):
    async def resolve(*args, **kwargs):
        raise OSError("private-source.example?key=never-log")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(WebError) as caught:
        await network.PublicResolver().resolve("private-source.example", 443)
    assert "private-source" not in str(caught.value)
    assert "never-log" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("errno,retryable", [
    (socket.EAI_AGAIN, True), (socket.EAI_NONAME, False),
])
async def test_dns_only_retries_temporary_resolution_failure(monkeypatch, errno, retryable):
    resolve = AsyncMock(side_effect=socket.gaierror(errno, "private-host-name"))
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(WebError) as caught:
        await network.PublicResolver().resolve("example.test", 443)
    assert caught.value.code == "dns_error" and caught.value.retryable is retryable
    assert "private-host-name" not in str(caught.value)


class Response:
    def __init__(self, body=b"synthetic", *, status=200, headers=None):
        self.status, self.headers = status, headers or {}
        self.content = self
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]


def fake_session(monkeypatch, responses):
    calls = []

    def connector(**kwargs):
        assert isinstance(kwargs["resolver"], network.PublicResolver)
        assert kwargs["use_dns_cache"] is False
        return SimpleNamespace()

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["auto_decompress"] is False
            assert isinstance(kwargs["cookie_jar"], network.aiohttp.DummyCookieJar)
            assert kwargs["headers"]["Accept-Encoding"] == "identity"
            assert not any(
                key.lower() in {"authorization", "cookie"} for key in kwargs["headers"]
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            assert kwargs == {"allow_redirects": False}
            calls.append(url)
            return responses.pop(0)

    monkeypatch.setattr(network.aiohttp, "TCPConnector", connector)
    monkeypatch.setattr(network.aiohttp, "ClientSession", Session)
    return calls


@pytest.mark.asyncio
async def test_redirect_returns_final_url_body_and_charset_header(monkeypatch):
    calls = fake_session(
        monkeypatch,
        [
            Response(status=302, headers={"Location": "/final"}),
            Response(b"hello", headers={"Content-Type": "text/html; charset=utf-8"}),
        ],
    )
    result = await network.fetch_public("https://example.test/start")
    assert result == network.Fetched(
        "https://example.test/final", b"hello", "text/html; charset=utf-8"
    )
    assert calls == ["https://example.test/start", "https://example.test/final"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/private",
        "//169.254.169.254/metadata",
        "https://secret:token@example.test/image",
        "file:///private",
    ],
)
async def test_each_redirect_target_is_checked_before_network(monkeypatch, target):
    calls = fake_session(
        monkeypatch, [Response(status=302, headers={"Location": target})]
    )
    with pytest.raises(WebError):
        await network.fetch_public("https://example.test/start")
    assert calls == ["https://example.test/start"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        Response(headers={"Content-Length": "17"}),
        Response(headers={"Content-Encoding": "gzip"}),
        Response(status=404),
        Response(status=206),
        Response(headers={"Content-Length": "invalid"}),
        Response(headers={"Content-Length": "-1"}),
        Response(headers={"Content-Length": "16"}),
        Response(headers={"Content-Type": "x" * 257}),
    ],
)
async def test_invalid_size_encoding_status_or_type_is_rejected(monkeypatch, response):
    fake_session(monkeypatch, [response])
    with pytest.raises(WebError):
        await network.fetch_public("https://example.test/file", max_bytes=16)


@pytest.mark.asyncio
async def test_stream_budget_is_checked_even_without_content_length(monkeypatch):
    fake_session(monkeypatch, [Response(body=b"x" * 17)])
    with pytest.raises(WebError, match="大小"):
        await network.fetch_public("https://example.test/file", max_bytes=16)


@pytest.mark.asyncio
async def test_exact_stream_limit_is_allowed(monkeypatch):
    fake_session(
        monkeypatch, [Response(body=b"x" * 16, headers={"Content-Length": "16"})]
    )
    result = await network.fetch_public("https://example.test/file", max_bytes=16)
    assert result.body == b"x" * 16


@pytest.mark.asyncio
async def test_redirect_limit_and_loop_prevention(monkeypatch):
    calls = fake_session(
        monkeypatch,
        [Response(status=302, headers={"Location": f"/next-{i}"}) for i in range(4)],
    )
    with pytest.raises(WebError, match="重定向"):
        await network.fetch_public("https://example.test/start")
    assert len(calls) == 4
    calls = fake_session(
        monkeypatch, [Response(status=302, headers={"Location": "/start"})]
    )
    with pytest.raises(WebError, match="循环"):
        await network.fetch_public("https://example.test/start")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_total_deadline_is_shared_across_all_redirects(monkeypatch):
    cancelled = asyncio.Event()

    async def slow(url, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(network, "_fetch", slow)
    with pytest.raises(WebError, match="超时"):
        await network.fetch_public("https://example.test/file", timeout=0.001)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_error_redaction_and_cancellation(monkeypatch, caplog):
    async def failing(url, **kwargs):
        raise RuntimeError(url)

    monkeypatch.setattr(network, "_fetch", failing)
    with pytest.raises(WebError) as caught:
        await network.fetch_public("https://example.test/?key=never-log")
    assert "never-log" not in str(caught.value) + caplog.text

    async def cancelled(url, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(network, "_fetch", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await network.fetch_public("https://example.test/file")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 0},
        {"max_bytes": True},
        {"max_bytes": 41 * 1024 * 1024},
        {"timeout": 0},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"timeout": True},
    ],
)
async def test_invalid_limits_rejected_before_network(kwargs, monkeypatch):
    async def never(*args, **kwargs):
        pytest.fail("invalid limits must not reach the network")

    monkeypatch.setattr(network, "_fetch", never)
    with pytest.raises(WebError):
        await network.fetch_public("https://example.test/file", **kwargs)


def doh_document(addresses=("8.8.8.8",), *, record_type=1, host="images.example.test"):
    return {
        "Status": 0,
        "TC": False,
        "Question": [{"name": host, "type": record_type}],
        "Answer": [
            {"name": host, "type": record_type, "TTL": 60, "data": address}
            for address in addresses
        ],
    }


def doh_response(document=None, **kwargs):
    body = json.dumps(document if document is not None else doh_document()).encode()
    return Response(
        body,
        headers={"Content-Type": "application/dns-json", **kwargs.pop("headers", {})},
        **kwargs,
    )


def fake_doh_session(monkeypatch, responses):
    calls = []

    def connector(**kwargs):
        context = kwargs["ssl"]
        assert isinstance(context, ssl.SSLContext)
        assert context.check_hostname is True
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert kwargs["use_dns_cache"] is False
        assert kwargs["limit"] == 1
        assert "resolver" not in kwargs  # Fixed literal endpoint; no recursive DoH.
        return SimpleNamespace()

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["auto_decompress"] is False
            assert isinstance(kwargs["cookie_jar"], network.aiohttp.DummyCookieJar)
            assert kwargs["headers"]["Accept"] == "application/dns-json"
            assert kwargs["headers"]["Accept-Encoding"] == "identity"
            assert kwargs["timeout"].total == network.DOH_TIMEOUT
            assert not any(
                key.lower() in {"authorization", "cookie"} for key in kwargs["headers"]
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            assert url == "https://1.1.1.1/dns-query"
            assert kwargs["allow_redirects"] is False
            assert set(kwargs) == {"params", "allow_redirects"}
            calls.append((url, kwargs["params"]))
            return responses.pop(0)

    monkeypatch.setattr(network.aiohttp, "TCPConnector", connector)
    monkeypatch.setattr(network.aiohttp, "ClientSession", Session)
    return calls


def fake_os_dns(monkeypatch, addresses):
    async def resolve(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 443))
            for host in addresses
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)


@pytest.mark.asyncio
async def test_all_tun_fake_ips_use_verified_fixed_doh_and_pin_public_result(
    monkeypatch,
):
    fake_os_dns(monkeypatch, ["198.18.1.12", "198.19.255.254"])
    document = doh_document()
    document["Answer"].insert(
        0, {"name": "images.example.test", "type": 5, "data": "cdn.example.test."}
    )
    calls = fake_doh_session(monkeypatch, [doh_response(document)])
    result = await network.PublicResolver().resolve(
        "images.example.test", 443, socket.AF_UNSPEC
    )
    assert result == [
        {
            "hostname": "images.example.test",
            "host": "8.8.8.8",
            "port": 443,
            "family": socket.AF_INET,
            "proto": socket.IPPROTO_TCP,
            "flags": socket.AI_NUMERICHOST,
        }
    ]
    assert calls == [
        ("https://1.1.1.1/dns-query", {"name": "images.example.test", "type": "A"})
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [
        ["198.18.1.12", "127.0.0.1"],
        ["198.18.1.12", "8.8.8.8"],
        ["198.17.255.255", "127.0.0.1"],
        ["198.20.0.1", "10.0.0.1"],
        ["10.0.0.1"],
        [],
    ],
)
async def test_non_fake_or_mixed_private_dns_never_uses_fallback(
    monkeypatch, addresses
):
    fake_os_dns(monkeypatch, addresses)

    async def never(*args):
        pytest.fail("DoH is only allowed for exclusively 198.18.0.0/15 answers")

    monkeypatch.setattr(network, "_doh_addresses", never)
    with pytest.raises(WebError, match="非公共"):
        await network.PublicResolver().resolve("images.example.test", 443)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["198.18.1.12"],
        ["8.8.8.8", "169.254.169.254"],
        ["not-an-address"],
        ["10.0.0.1"],
        ["224.0.0.1"],
    ],
)
async def test_doh_cannot_turn_private_or_invalid_answers_into_allowed_ips(
    monkeypatch, addresses
):
    fake_os_dns(monkeypatch, ["198.18.1.12"])
    calls = fake_doh_session(monkeypatch, [doh_response(doh_document(addresses))])
    with pytest.raises(WebError):
        await network.PublicResolver().resolve("images.example.test", 443)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_empty_a_can_fall_back_to_aaaa_within_the_same_deadline(monkeypatch):
    calls = fake_doh_session(
        monkeypatch,
        [
            doh_response(doh_document([])),
            doh_response(doh_document(["2606:4700:4700::1111"], record_type=28)),
        ],
    )
    assert await network._doh_addresses("images.example.test", socket.AF_UNSPEC) == [
        (socket.AF_INET6, "2606:4700:4700::1111")
    ]
    assert [item[1]["type"] for item in calls] == ["A", "AAAA"]


@pytest.mark.asyncio
async def test_ipv6_family_requests_only_aaaa(monkeypatch):
    calls = fake_doh_session(
        monkeypatch,
        [doh_response(doh_document(["2606:4700:4700::1111"], record_type=28))],
    )
    assert await network._doh_addresses("images.example.test", socket.AF_INET6) == [
        (socket.AF_INET6, "2606:4700:4700::1111")
    ]
    assert [item[1]["type"] for item in calls] == ["AAAA"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        [],
        {"Status": 3},
        {"Status": True},
        {**doh_document(), "TC": True},
        {**doh_document(), "Question": []},
        doh_document(host="different.example.test"),
        doh_document(record_type=28),
        {**doh_document(), "Answer": "not-a-list"},
        {**doh_document(), "Answer": [{}]},
        {**doh_document(), "Answer": [{"type": 1, "data": "2606:4700:4700::1111"}]},
        {**doh_document(), "Answer": [{"type": 16, "data": "synthetic-private-token"}]},
    ],
)
async def test_bad_doh_documents_are_safely_rejected(monkeypatch, document):
    fake_doh_session(monkeypatch, [doh_response(document)])
    with pytest.raises(WebError) as caught:
        await network._doh_addresses("images.example.test", socket.AF_INET)
    assert "synthetic-private-token" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        doh_response(status=302, headers={"Location": "http://127.0.0.1/private"}),
        doh_response(status=500),
        doh_response(headers={"Content-Encoding": "gzip"}),
        doh_response(headers={"Content-Type": "text/html"}),
        doh_response(headers={"Content-Length": "not-a-number"}),
        doh_response(headers={"Content-Length": "-1"}),
        doh_response(headers={"Content-Length": str(network.DOH_MAX_BYTES + 1)}),
        doh_response(headers={"Content-Length": "1"}),
        Response(b"not-json SECRET", headers={"Content-Type": "application/json"}),
        Response(
            b"x" * (network.DOH_MAX_BYTES + 1),
            headers={"Content-Type": "application/dns-json"},
        ),
    ],
)
async def test_doh_status_redirect_compression_size_and_json_fail_closed(
    monkeypatch, response
):
    calls = fake_doh_session(monkeypatch, [response])
    with pytest.raises(WebError) as caught:
        await network._doh_addresses("images.example.test", socket.AF_INET)
    assert "SECRET" not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ssl.SSLCertVerificationError("synthetic-secret-host"),
        OSError("synthetic-secret-host"),
        TimeoutError("synthetic-secret-host"),
    ],
)
async def test_doh_network_and_tls_errors_are_safe_and_never_retried(
    monkeypatch, caplog, error
):
    class Failure(Response):
        async def __aenter__(self):
            raise error

    calls = fake_doh_session(monkeypatch, [Failure()])
    with pytest.raises(WebError) as caught:
        await network._doh_addresses("synthetic-secret-host", socket.AF_INET)
    assert "synthetic-secret-host" not in str(caught.value) + caplog.text
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_doh_has_own_deadline_and_preserves_fetch_cancellation(monkeypatch):
    cancelled = asyncio.Event()

    class Slow(Response):
        async def __aenter__(self):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(network, "DOH_TIMEOUT", 0.001)
    fake_doh_session(monkeypatch, [Slow()])
    with pytest.raises(WebError, match="DNS 解析超时"):
        await network._doh_addresses("images.example.test", socket.AF_INET)
    assert cancelled.is_set()

    cancelled.clear()
    monkeypatch.setattr(network, "DOH_TIMEOUT", 5)
    fake_doh_session(monkeypatch, [Slow()])

    async def fetch_resolves(url, **kwargs):
        await network._doh_addresses("images.example.test", socket.AF_INET)

    monkeypatch.setattr(network, "_fetch", fetch_resolves)
    with pytest.raises(WebError, match="网页或图片读取超时"):
        await network.fetch_public("https://images.example.test/", timeout=0.001)
    assert cancelled.is_set()


def compressed_body(body, encoding):
    return gzip.compress(body, mtime=0) if encoding == "gzip" else zlib.compress(body)


class SmallChunks(Response):
    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), 7):
            yield self.body[offset : offset + 7]


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_bounded_streaming_compression_returns_decoded_body(
    monkeypatch, encoding
):
    raw = ("<p>中文与日本語</p>" * 200).encode()
    compressed = compressed_body(raw, encoding)
    fake_session(
        monkeypatch,
        [
            SmallChunks(
                compressed,
                headers={
                    "Content-Encoding": encoding,
                    "Content-Length": str(len(compressed)),
                    "Content-Type": "text/html; charset=utf-8",
                },
            )
        ],
    )
    result = await network.fetch_public(
        "https://example.test/compressed", max_bytes=len(raw)
    )
    assert result.body == raw
    assert result.content_type == "text/html; charset=utf-8"


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_compression_bomb_stops_at_bounded_per_chunk_output(
    monkeypatch, encoding
):
    compressed = compressed_body(b"x" * 1_000_000, encoding)
    assert len(compressed) <= 1024
    fake_session(
        monkeypatch, [Response(compressed, headers={"Content-Encoding": encoding})]
    )
    original = network.zlib.decompressobj
    limits = []

    class BoundedDecoder:
        def __init__(self, wbits):
            self.decoder = original(wbits)

        def decompress(self, data, max_length):
            assert 0 < max_length <= 1025
            limits.append(max_length)
            return self.decoder.decompress(data, max_length)

        def __getattr__(self, name):
            return getattr(self.decoder, name)

    monkeypatch.setattr(network.zlib, "decompressobj", BoundedDecoder)
    with pytest.raises(WebError, match="解压后超过"):
        await network.fetch_public("https://example.test/bomb", max_bytes=1024)
    assert limits == [1025]


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
@pytest.mark.parametrize("declared", [False, True])
async def test_compressed_input_budget_is_independent_of_output(
    monkeypatch, encoding, declared
):
    compressed = compressed_body(b"", encoding)
    headers = {"Content-Encoding": encoding}
    if declared:
        headers["Content-Length"] = str(len(compressed))
    fake_session(monkeypatch, [Response(compressed, headers=headers)])
    with pytest.raises(WebError, match="下载大小"):
        await network.fetch_public(
            "https://example.test/compressed", max_bytes=len(compressed) - 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_missing_compressed_eof_is_rejected(monkeypatch, encoding):
    compressed = compressed_body(b"synthetic text", encoding)[:-1]
    fake_session(
        monkeypatch, [SmallChunks(compressed, headers={"Content-Encoding": encoding})]
    )
    with pytest.raises(WebError, match="压缩内容不完整"):
        await network.fetch_public("https://example.test/truncated")


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
@pytest.mark.parametrize("tail", ["garbage", "second-stream"])
async def test_concatenated_or_trailing_compressed_data_is_rejected(
    monkeypatch, encoding, tail
):
    compressed = compressed_body(b"first", encoding)
    compressed += (
        b"synthetic-secret-garbage"
        if tail == "garbage"
        else compressed_body(b"second", encoding)
    )
    fake_session(
        monkeypatch, [SmallChunks(compressed, headers={"Content-Encoding": encoding})]
    )
    with pytest.raises(WebError, match="尾随或拼接") as caught:
        await network.fetch_public("https://example.test/concatenated")
    assert "synthetic-secret" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_invalid_compressed_checksum_is_rejected(monkeypatch, encoding):
    compressed = bytearray(compressed_body(b"synthetic text", encoding))
    compressed[-1] ^= 255
    fake_session(
        monkeypatch,
        [Response(bytes(compressed), headers={"Content-Encoding": encoding})],
    )
    with pytest.raises(WebError, match="压缩内容损坏"):
        await network.fetch_public("https://example.test/invalid")


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["br", "zstd", "gzip, deflate"])
async def test_other_or_stacked_content_encodings_remain_rejected(
    monkeypatch, encoding
):
    fake_session(monkeypatch, [Response(headers={"Content-Encoding": encoding})])
    with pytest.raises(WebError, match="不支持的压缩"):
        await network.fetch_public("https://example.test/unsupported")


@pytest.mark.asyncio
async def test_attachment_fetch_preserves_final_content_disposition(monkeypatch):
    disposition = "attachment; filename*=UTF-8''%E5%AD%97%E4%BD%93.ttf"
    calls = fake_session(
        monkeypatch,
        [
            Response(status=302, headers={"Location": "/font"}),
            Response(
                b"font bytes",
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Disposition": disposition,
                },
            ),
        ],
    )
    result = await network.fetch_public_file("https://example.test/start")
    assert result.url == "https://example.test/font"
    assert result.body == b"font bytes"
    assert result.content_disposition == disposition
    assert calls == ["https://example.test/start", "https://example.test/font"]


@pytest.mark.asyncio
async def test_shared_fetch_allows_explicit_large_documents_but_keeps_small_default(
    monkeypatch,
):
    raw = b"x" * (5 * 1024 * 1024 + 1)
    calls = fake_session(monkeypatch, [Response(raw), Response(raw), Response(raw)])
    result = await network.fetch_public_file("https://example.test/package.bin")
    assert result.body == raw
    assert (await network.fetch_public(
        "https://example.test/package.bin", max_bytes=len(raw)
    )).body == raw
    with pytest.raises(WebError, match="大小"):
        await network.fetch_public(
            "https://example.test/package.bin"
        )
    assert calls == ["https://example.test/package.bin"] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 40 * 1024 * 1024 + 1},
        {"max_bytes": 0},
        {"max_bytes": True},
        {"timeout": 61},
        {"timeout": 0},
        {"timeout": float("nan")},
        {"timeout": True},
    ],
)
async def test_attachment_fetch_rejects_invalid_limits_before_request(
    monkeypatch, kwargs
):
    async def forbidden(*args, **kw):
        pytest.fail("invalid file limits must not start a request")

    monkeypatch.setattr(network, "_fetch", forbidden)
    with pytest.raises(WebError):
        await network.fetch_public_file("https://example.test/file.bin", **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["x" * 2049, "attachment\r\nInjected: true", 12])
async def test_unbounded_or_invalid_filename_headers_are_rejected(monkeypatch, header):
    fake_session(monkeypatch, [Response(headers={"Content-Disposition": header})])
    with pytest.raises(WebError, match="文件名"):
        await network.fetch_public_file("https://example.test/file")


@pytest.mark.asyncio
async def test_attachment_redirect_to_private_address_is_blocked_before_connection(
    monkeypatch,
):
    calls = fake_session(
        monkeypatch,
        [
            Response(
                status=302, headers={"Location": "http://169.254.169.254/metadata"}
            ),
        ],
    )
    with pytest.raises(WebError, match="公共网络"):
        await network.fetch_public_file("https://example.test/download")
    assert calls == ["https://example.test/download"]


@pytest.mark.asyncio
async def test_attachment_stream_limit_is_enforced_without_content_length(monkeypatch):
    fake_session(monkeypatch, [Response(b"x" * 17)])
    with pytest.raises(WebError, match="下载大小"):
        await network.fetch_public_file("https://example.test/file", max_bytes=16)
    fake_session(monkeypatch, [Response(b"x" * 16)])
    assert (
        len(
            (
                await network.fetch_public_file(
                    "https://example.test/file", max_bytes=16
                )
            ).body
        )
        == 16
    )


@pytest.mark.asyncio
async def test_attachment_compression_expansion_is_also_bounded(monkeypatch):
    compressed = gzip.compress(b"x" * 1024)
    assert len(compressed) < 100
    fake_session(
        monkeypatch, [Response(compressed, headers={"Content-Encoding": "gzip"})]
    )
    with pytest.raises(WebError, match="解压后"):
        await network.fetch_public_file("https://example.test/file", max_bytes=100)


@pytest.mark.asyncio
async def test_attachment_shared_deadline_cancels_download_and_does_not_retry(
    monkeypatch,
):
    finished = asyncio.Event()
    calls = []

    async def stalled(url, **kwargs):
        calls.append(url)
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    monkeypatch.setattr(network, "_fetch", stalled)
    with pytest.raises(WebError, match="超时"):
        await network.fetch_public_file("https://example.test/file", timeout=0.001)
    assert finished.is_set()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_attachment_errors_hide_request_secrets_and_cancellation_is_preserved(
    monkeypatch, caplog
):
    async def fail(url, **kwargs):
        raise OSError(url)

    monkeypatch.setattr(network, "_fetch", fail)
    with pytest.raises(WebError) as caught:
        await network.fetch_public_file("https://example.test/file?secret=never-log")
    assert "never-log" not in str(caught.value) + caplog.text

    async def cancelled(url, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(network, "_fetch", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await network.fetch_public_file("https://example.test/file")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "size",
    [
        int(12.3 * 1024 * 1024),
        20 * 1024 * 1024,
        20 * 1024 * 1024 + 1,
    ],
)
@pytest.mark.parametrize("with_length", [False, True])
async def test_attachment_twenty_mib_boundary_is_enforced_in_real_stream(
    monkeypatch, size, with_length
):
    raw = b"\0\xff" + b"x" * (size - 2)
    headers = {"Content-Length": str(size)} if with_length else {}
    calls = fake_session(monkeypatch, [Response(raw, headers=headers)])
    if size > 20 * 1024 * 1024:
        with pytest.raises(WebError, match="下载大小"):
            await network.fetch_public_file("https://example.test/file.custom")
    else:
        fetched = await network.fetch_public_file("https://example.test/file.custom")
        assert fetched.body == raw
    assert calls == ["https://example.test/file.custom"]
