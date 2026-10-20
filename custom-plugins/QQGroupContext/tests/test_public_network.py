"""Only synthetic DNS/HTTP responses; no network traffic or real image sources."""

import asyncio
import json
import socket
import ssl
from types import SimpleNamespace

import pytest

from pkg import public_network as network
from pkg.public_network import PublicNetworkError


class Response:
    def __init__(self, body=b"", *, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self.body = body
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]


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
async def test_ordinary_public_dns_does_not_use_doh(monkeypatch):
    fake_os_dns(monkeypatch, ["8.8.8.8", "8.8.8.8"])

    async def never(*args):
        pytest.fail("normal public DNS must not use DoH")

    monkeypatch.setattr(network, "_doh_addresses", never)
    result = await network.PublicResolver().resolve("images.example.test", 443)
    assert len(result) == 1
    assert result[0]["host"] == "8.8.8.8"


@pytest.mark.asyncio
async def test_external_cancellation_is_not_hidden_by_doh(monkeypatch):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class Slow(Response):
        async def __aenter__(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    fake_doh_session(monkeypatch, [Slow()])
    task = asyncio.create_task(
        network._doh_addresses("images.example.test", socket.AF_INET)
    )
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


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
    with pytest.raises(PublicNetworkError, match="非公共"):
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
    with pytest.raises(PublicNetworkError):
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
    with pytest.raises(PublicNetworkError) as caught:
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
    with pytest.raises(PublicNetworkError) as caught:
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
    with pytest.raises(PublicNetworkError) as caught:
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
    with pytest.raises(PublicNetworkError, match="DNS 解析超时"):
        await network._doh_addresses("images.example.test", socket.AF_INET)
    assert cancelled.is_set()

    cancelled.clear()
    monkeypatch.setattr(network, "DOH_TIMEOUT", 5)
    fake_doh_session(monkeypatch, [Slow()])

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.001):
            await network._doh_addresses("images.example.test", socket.AF_INET)
    assert cancelled.is_set()
