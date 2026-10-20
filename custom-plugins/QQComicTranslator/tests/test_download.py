from __future__ import annotations
import asyncio
import base64
import socket
from types import SimpleNamespace
import pytest
from pkg import download as reader


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/a.txt",
        "C:\\secret.txt",
        "http://127.0.0.1/a",
        "https://10.0.0.1/a",
        "https://169.254.169.254/a",
        "https://[::1]/a",
        "http://224.0.0.1/a",
        "http://[ff0e::1]/a",
        "http://[::ffff:127.0.0.1]/a",
        "http://[64:ff9b::7f00:1]/a",
        "http://127.1/a",
        "http://2130706433/a",
        "http://0177.0.0.1/a",
        "http://u:p@example.com/a",
        "http://example.com:9090/a",
        "http://example.com/a#fragment",
        "http://%31%32%37.0.0.1/a",
        "http://example.com/\r\nprivate",
    ],
)
def test_untrusted_or_nonpublic_urls_rejected(url):
    with pytest.raises(reader.ArchiveError):
        reader.validate_url(url)


@pytest.mark.asyncio
async def test_dns_pin_rejects_mixed_public_private_answers(monkeypatch):
    loop = asyncio.get_running_loop()

    async def resolve(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 443))
            for host in ["8.8.8.8", "127.0.0.1"]
        ]

    monkeypatch.setattr(loop, "getaddrinfo", resolve)
    with pytest.raises(reader.ArchiveError, match="非公共"):
        await reader.PublicResolver().resolve("attachment.example", 443)


@pytest.mark.asyncio
async def test_dns_returns_only_validated_exact_addresses(monkeypatch):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    answers = await reader.PublicResolver().resolve("attachment.example", 443)
    assert answers[0]["host"] == "8.8.8.8"
    assert answers[0]["hostname"] == "attachment.example"
    assert answers[0]["flags"] == socket.AI_NUMERICHOST


class Response:
    def __init__(self, body=b"offline", *, status=200, headers=None):
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

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["auto_decompress"] is False
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

    monkeypatch.setattr(
        reader.aiohttp, "TCPConnector", lambda **kwargs: SimpleNamespace()
    )
    monkeypatch.setattr(reader.aiohttp, "ClientSession", Session)
    return calls


@pytest.mark.asyncio
async def test_public_redirect_and_no_auth(monkeypatch):
    calls = fake_session(
        monkeypatch,
        [
            Response(status=302, headers={"Location": "https://public.example/new"}),
            Response(),
        ],
    )
    assert await reader.download("https://public.example/old") == b"offline"
    assert calls == ["https://public.example/old", "https://public.example/new"]


@pytest.mark.asyncio
async def test_redirect_to_private_is_never_requested(monkeypatch):
    calls = fake_session(
        monkeypatch,
        [Response(status=302, headers={"Location": "http://127.0.0.1/private"})],
    )
    with pytest.raises(reader.ArchiveError):
        await reader.download("https://public.example/old")
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        Response(headers={"Content-Length": str(reader.MAX_BYTES + 1)}),
        Response(body=b"a" * (reader.MAX_BYTES + 1)),
        Response(headers={"Content-Encoding": "gzip"}),
        Response(status=404),
        Response(headers={"Content-Length": "invalid"}),
    ],
)
async def test_download_rejects_size_encoding_and_status(monkeypatch, response):
    fake_session(monkeypatch, [response])
    with pytest.raises(reader.ArchiveError):
        await reader.download("https://public.example/file")


@pytest.mark.asyncio
async def test_total_deadline_and_error_redaction(monkeypatch):
    async def slow(url):
        await asyncio.sleep(1)

    monkeypatch.setattr(reader, "TOTAL_TIMEOUT", 0.001)
    monkeypatch.setattr(reader, "_download", slow)
    with pytest.raises(reader.ArchiveError, match="60 秒"):
        await reader.download("https://public.example/?secret=never-log")

    async def failing(url):
        raise RuntimeError(url)

    monkeypatch.setattr(reader, "_download", failing)
    with pytest.raises(reader.ArchiveError) as exc:
        await reader.download("https://public.example/?secret=never-log")
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("prefix", ["", "base64://", "data:application/zip;base64,"])
def test_inline_formats_and_exact_boundary(monkeypatch, prefix):
    assert reader.MAX_BYTES == 40 * 1024 * 1024
    assert reader.TOTAL_TIMEOUT == 60
    monkeypatch.setattr(reader, "MAX_BYTES", 16)
    assert (
        reader.decode_inline(prefix + base64.b64encode(b"x" * 16).decode()) == b"x" * 16
    )
    with pytest.raises(reader.ArchiveError):
        reader.decode_inline(prefix + base64.b64encode(b"x" * 17).decode())


@pytest.mark.parametrize(
    "value", [None, "data:application/zip,not base64", "not base64!", "base64://a"]
)
def test_invalid_inline_encoding_is_rejected(value):
    with pytest.raises(reader.ArchiveError):
        reader.decode_inline(value)


@pytest.mark.asyncio
async def test_redirect_limit_is_bounded(monkeypatch):
    calls = fake_session(
        monkeypatch,
        [Response(status=302, headers={"Location": "/next"}) for _ in range(4)],
    )
    with pytest.raises(reader.ArchiveError, match="重定向"):
        await reader.download("https://public.example/zip")
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_exact_stream_limit_is_allowed(monkeypatch):
    monkeypatch.setattr(reader, "MAX_BYTES", 16)
    fake_session(
        monkeypatch, [Response(body=b"x" * 16, headers={"Content-Length": "16"})]
    )
    assert await reader.download("https://public.example/zip") == b"x" * 16
