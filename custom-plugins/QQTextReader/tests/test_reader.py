from __future__ import annotations

import asyncio
import base64
import codecs
import json
import socket
from types import SimpleNamespace

import pytest

from pkg import reader


@pytest.mark.parametrize(
    "encoding,bom",
    [
        ("utf-8", b""),
        ("utf-8", codecs.BOM_UTF8),
        ("utf-16-le", codecs.BOM_UTF16_LE),
        ("utf-16-be", codecs.BOM_UTF16_BE),
        ("gb18030", b""),
    ],
)
def test_exact_text_and_encoding(encoding, bom):
    text = '测试,半角标点.\r\n第二行:"内容"\\目录\t字。'
    raw = bom + text.encode(encoding)
    result = json.loads(reader.result_json("test.txt", raw, len(raw)))
    assert result["text"] == text
    assert result["complete"] is True
    assert set(result) == {"name", "encoding", "text", "complete"}


@pytest.mark.parametrize("prefix", ["", "data:text/plain;base64,", "base64://"])
def test_inline_formats(prefix):
    assert (
        reader.decode_inline(prefix + base64.b64encode(b"offline").decode())
        == b"offline"
    )


def test_bytes_and_char_boundaries():
    assert (
        len(reader.decode_inline(base64.b64encode(b"a" * reader.MAX_BYTES).decode()))
        == reader.MAX_BYTES
    )
    with pytest.raises(reader.TextReadError, match="128 KiB"):
        reader.decode_inline(base64.b64encode(b"a" * (reader.MAX_BYTES + 1)).decode())
    assert reader.decode_text(b"a" * reader.MAX_CHARS)[0] == "a" * reader.MAX_CHARS
    with pytest.raises(reader.TextReadError, match="16000"):
        reader.decode_text(b"a" * (reader.MAX_CHARS + 1))


def test_no_silent_serialized_truncation():
    with pytest.raises(reader.TextReadError, match="完整工具结果"):
        reader.result_json("test.txt", b'"' * reader.MAX_CHARS)
    with pytest.raises(reader.TextReadError, match="完整性"):
        reader.result_json("test.txt", b"abc", 4)
    assert json.loads(reader.result_json("empty.txt", b""))["complete"] is True


@pytest.mark.parametrize(
    "raw",
    [
        b"PK\x03\x04anything",
        b"%PDF-1.0",
        b"\x89PNGabc",
        b"a\x00b",
        b"a\x01b",
        codecs.BOM_UTF8 + b"\xff",
        codecs.BOM_UTF16_LE + b"a",
        codecs.BOM_UTF32_LE + b"abcd",
        b"\xff",
    ],
)
def test_binary_and_invalid_encoding_rejected(raw):
    with pytest.raises(reader.TextReadError):
        reader.decode_text(raw)


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/a.txt",
        "C:\\secret.txt",
        "http://127.0.0.1/a",
        "https://10.0.0.1/a",
        "https://169.254.169.254/a",
        "https://[::1]/a",
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
    with pytest.raises(reader.TextReadError):
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
    with pytest.raises(reader.TextReadError, match="非公共"):
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
    with pytest.raises(reader.TextReadError):
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
    with pytest.raises(reader.TextReadError):
        await reader.download("https://public.example/file")


@pytest.mark.asyncio
async def test_total_deadline_and_error_redaction(monkeypatch):
    async def slow(url):
        await asyncio.sleep(1)

    monkeypatch.setattr(reader, "TOTAL_TIMEOUT", 0.001)
    monkeypatch.setattr(reader, "_download", slow)
    with pytest.raises(reader.TextReadError, match="30 秒"):
        await reader.download("https://public.example/?secret=never-log")

    async def failing(url):
        raise RuntimeError(url)

    monkeypatch.setattr(reader, "_download", failing)
    with pytest.raises(reader.TextReadError) as exc:
        await reader.download("https://public.example/?secret=never-log")
    assert "secret" not in str(exc.value)
