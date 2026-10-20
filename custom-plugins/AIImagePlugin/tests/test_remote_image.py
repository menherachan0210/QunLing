"""No live requests: exercise the real download flow with synthetic transports."""

import asyncio
import json
import socket
import ssl
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pkg import public_network, remote_image
from pkg.remote_image import RemoteImageError

PNG = b"\x89PNG\r\n\x1a\nsynthetic"
URL = "https://images.example.test/reference?token=synthetic-secret"


class Response:
    def __init__(self, body=PNG, *, status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]


def fake_http(monkeypatch, responses):
    calls = []

    def connector(**kwargs):
        assert kwargs["use_dns_cache"] is False
        assert isinstance(kwargs["resolver"], public_network.PublicResolver)
        return SimpleNamespace()

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["auto_decompress"] is False
            assert isinstance(kwargs["cookie_jar"], remote_image.aiohttp.DummyCookieJar)
            assert kwargs["headers"]["Accept-Encoding"] == "identity"
            assert not {"authorization", "cookie"} & {
                name.lower() for name in kwargs["headers"]
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            assert kwargs == {"allow_redirects": False}
            calls.append(url)
            return responses.pop(0)

    monkeypatch.setattr(remote_image.aiohttp, "TCPConnector", connector)
    monkeypatch.setattr(remote_image.aiohttp, "ClientSession", Session)
    return calls


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "file:///tmp/private.png",
        "data:image/png;base64,AA==",
        "https://user:password@example.test/a",
        "https://example.test:9443/a",
        "https://example.test:invalid/a",
        "https://example.test/a#fragment",
        "https://example.test/a\nprivate",
        "https://example.test\\@127.0.0.1/a",
        "https://127.0.0.1/a",
        "https://10.0.0.1/a",
        "https://169.254.169.254/a",
        "https://198.18.0.1/a",
        "https://[::1]/a",
        "https://[::ffff:127.0.0.1]/a",
        "https://[64:ff9b::7f00:1]/a",
        "https://[2002:7f00:1::]/a",
        "https://127.1/a",
        "https://2130706433/a",
        "https://0177.0.0.1/a",
        "https://%31%32%37.0.0.1/a",
        "https://example.test/" + "a" * remote_image.MAX_URL_CHARS,
    ],
)
@pytest.mark.asyncio
async def test_unsafe_input_rejected_before_network(monkeypatch, url):
    download = AsyncMock()
    monkeypatch.setattr(remote_image, "_download_reference", download)
    with pytest.raises(RemoteImageError) as caught:
        await remote_image.fetch_reference_image(url)
    assert caught.value.code == "invalid_url"
    download.assert_not_called()


@pytest.mark.asyncio
async def test_public_redirect_preserves_pixels_and_ignores_claimed_mime(monkeypatch):
    calls = fake_http(
        monkeypatch,
        [
            Response(status=302, headers={"Location": "/next"}),
            Response(PNG, headers={"Content-Type": "text/plain"}),
        ],
    )
    assert await remote_image.fetch_reference_image(URL) == PNG
    assert calls == [URL, "https://images.example.test/next"]


@pytest.mark.asyncio
async def test_redirect_to_private_address_never_connects(monkeypatch):
    calls = fake_http(
        monkeypatch,
        [Response(status=302, headers={"Location": "http://127.0.0.1/private"})],
    )
    with pytest.raises(RemoteImageError) as caught:
        await remote_image.fetch_reference_image(URL)
    assert caught.value.code == "invalid_url"
    assert calls == [URL]


@pytest.mark.asyncio
async def test_redirect_count_and_loops_are_bounded(monkeypatch):
    calls = fake_http(
        monkeypatch,
        [
            Response(status=302, headers={"Location": f"/next-{i}"})
            for i in range(remote_image.MAX_REDIRECTS + 1)
        ],
    )
    with pytest.raises(RemoteImageError) as caught:
        await remote_image.fetch_reference_image(URL)
    assert caught.value.code == "invalid_response"
    assert len(calls) == 4
    calls = fake_http(monkeypatch, [Response(status=302, headers={"Location": URL})])
    with pytest.raises(RemoteImageError) as caught:
        await remote_image.fetch_reference_image(URL)
    assert caught.value.code == "invalid_response"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response,code,status",
    [
        (Response(status=403), "http_status", 403),
        (Response(status=500), "http_status", 500),
        (Response(status=302), "invalid_response", None),
        (Response(headers={"Content-Length": "secret"}), "invalid_response", None),
        (Response(headers={"Content-Length": "-1"}), "invalid_response", None),
        (Response(headers={"Content-Length": "1"}), "invalid_response", None),
        (
            Response(headers={"Content-Length": str(10 * 1024 * 1024 + 1)}),
            "too_large",
            None,
        ),
        (Response(headers={"Content-Encoding": "gzip"}), "invalid_response", None),
        (Response(b""), "invalid_body", None),
    ],
)
@pytest.mark.asyncio
async def test_http_size_encoding_and_body_errors_are_structured(
    monkeypatch, response, code, status
):
    fake_http(monkeypatch, [response])
    with pytest.raises(RemoteImageError) as caught:
        await remote_image.fetch_reference_image(URL)
    assert caught.value.code == code
    assert caught.value.status == status
    assert str(caught.value) == code


@pytest.mark.asyncio
async def test_stream_cap_enforced_even_if_length_is_missing_or_false(monkeypatch):
    monkeypatch.setattr(remote_image, "MAX_IMAGE_BYTES", 16)
    for headers in [{}, {"Content-Length": "1"}]:
        fake_http(monkeypatch, [Response(b"x" * 17, headers=headers)])
        with pytest.raises(RemoteImageError) as caught:
            await remote_image.fetch_reference_image(URL)
        assert caught.value.code == "too_large"
    fake_http(monkeypatch, [Response(b"x" * 16)])
    assert await remote_image.fetch_reference_image(URL) == b"x" * 16


@pytest.mark.asyncio
async def test_deadline_covers_response_body_and_cancels_network(monkeypatch):
    cancelled = asyncio.Event()
    timeout = asyncio.timeout
    managers = []

    def record_timeout(seconds):
        manager = timeout(seconds)
        managers.append(manager)
        return manager

    class Slow(Response):
        async def iter_chunked(self, size):
            managers[0].reschedule(asyncio.get_running_loop().time() - 1)
            try:
                await asyncio.Event().wait()
                yield b"unreachable"
            finally:
                cancelled.set()

    monkeypatch.setattr(remote_image.asyncio, "timeout", record_timeout)
    fake_http(monkeypatch, [Slow()])
    with pytest.raises(RemoteImageError) as caught:
        await remote_image.fetch_reference_image(URL)
    assert caught.value.code == "timeout"
    assert cancelled.is_set()


@pytest.mark.parametrize(
    "error",
    [
        OSError("synthetic-secret"),
        ssl.SSLCertVerificationError("synthetic-secret"),
        TimeoutError("synthetic-secret"),
        public_network.PublicNetworkError("safe DNS error", code="dns_timeout"),
        public_network.PublicNetworkError("safe DNS error", code="dns_non_public"),
    ],
)
@pytest.mark.asyncio
async def test_transport_errors_do_not_leak_url_or_underlying_error(
    monkeypatch, caplog, error
):
    download = AsyncMock(side_effect=error)
    monkeypatch.setattr(remote_image, "_download_reference", download)
    with pytest.raises(RemoteImageError) as caught:
        await remote_image.fetch_reference_image(URL)
    output = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert "synthetic-secret" not in output
    assert "images.example.test" not in output
    assert caught.value.code in {"network", "timeout"}
    download.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_propagates_without_retry(monkeypatch):
    download = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(remote_image, "_download_reference", download)
    with pytest.raises(asyncio.CancelledError):
        await remote_image.fetch_reference_image(URL)
    download.assert_awaited_once()


@pytest.mark.asyncio
async def test_pure_fake_ip_uses_doh_and_pins_real_ip(monkeypatch):
    async def dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.42", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
    doh = AsyncMock(return_value=[(socket.AF_INET, "8.8.8.8")])
    monkeypatch.setattr(public_network, "_doh_addresses", doh)
    result = await remote_image.PublicResolver().resolve("images.example.test", 443)
    assert result[0]["host"] == "8.8.8.8"
    assert result[0]["hostname"] == "images.example.test"
    doh.assert_awaited_once()


@pytest.mark.parametrize(
    "addresses",
    [
        ["198.18.0.42", "127.0.0.1"],
        ["198.18.0.42", "8.8.8.8"],
        ["10.0.0.1"],
        [],
    ],
)
@pytest.mark.asyncio
async def test_mixed_fake_and_private_dns_remains_rejected(monkeypatch, addresses):
    async def dns(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            for address in addresses
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
    doh = AsyncMock()
    monkeypatch.setattr(public_network, "_doh_addresses", doh)
    with pytest.raises(public_network.PublicNetworkError):
        await remote_image.PublicResolver().resolve("images.example.test", 443)
    doh.assert_not_called()


def test_doh_rejects_private_answer_and_wrong_question():
    document = {
        "Status": 0,
        "Question": [{"name": "images.example.test", "type": 1}],
        "Answer": [{"name": "images.example.test", "type": 1, "data": "127.0.0.1"}],
    }
    with pytest.raises(public_network.PublicNetworkError) as caught:
        public_network._doh_answer(
            json.dumps(document).encode(), "images.example.test", 1
        )
    assert caught.value.code == "dns_non_public"
    document["Answer"][0]["data"] = "8.8.8.8"
    with pytest.raises(public_network.PublicNetworkError):
        public_network._doh_answer(
            json.dumps(document).encode(), "other.example.test", 1
        )
