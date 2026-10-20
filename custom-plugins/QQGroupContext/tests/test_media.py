from __future__ import annotations

import asyncio
import base64
import io
import socket
from types import SimpleNamespace

import pytest
from PIL import Image

from pkg import media
from pkg.errors import ImageReadError, VisionError
from pkg.public_network import PublicNetworkError


def fixture_image(
    format_name="PNG", *, size=(40, 20), color="red", mode="RGB", **kwargs
):
    output = io.BytesIO()
    with Image.new(mode, size, color) as image:
        image.save(output, format=format_name, **kwargs)
    return output.getvalue()


def decode_result(value):
    assert value.startswith(media.DATA_PREFIX)
    assert len(value) <= media.MAX_OUTPUT_BYTES
    result = Image.open(
        io.BytesIO(base64.b64decode(value.split(",", 1)[1], validate=True))
    )
    result.load()
    assert result.format == "JPEG"
    assert result.mode == "RGB"
    assert max(result.size) <= media.MAX_EDGE
    return result


@pytest.mark.parametrize("format_name", ["PNG", "JPEG", "WEBP", "GIF", "BMP"])
def test_supported_images_are_normalized_to_bounded_jpeg(format_name):
    with decode_result(media.prepare_image(fixture_image(format_name))) as image:
        assert image.size == (40, 20)
        red, green, blue = image.getpixel((10, 10))
        assert red > 240 and green < 10 and blue < 10


def test_image_long_edge_is_reduced_without_upscaling_small_images():
    with decode_result(media.prepare_image(fixture_image(size=(5000, 100)))) as image:
        assert image.size == (2400, 48)


def test_transparency_uses_white_background():
    raw = fixture_image(mode="RGBA", color=(0, 0, 0, 0))
    with decode_result(media.prepare_image(raw)) as image:
        assert all(channel > 245 for channel in image.getpixel((10, 10)))


def test_gif_uses_first_frame_only():
    output = io.BytesIO()
    with (
        Image.new("RGB", (20, 20), "red") as first,
        Image.new("RGB", (20, 20), "blue") as second,
    ):
        first.save(
            output,
            format="GIF",
            save_all=True,
            append_images=[second],
            duration=100,
            loop=0,
        )
    with decode_result(media.prepare_image(output.getvalue())) as image:
        red, green, blue = image.getpixel((10, 10))
        assert red > 240 and green < 10 and blue < 10


def test_exif_orientation_is_applied_and_metadata_removed():
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "synthetic private metadata"
    raw = fixture_image("JPEG", exif=exif, comment=b"synthetic private comment")
    with decode_result(media.prepare_image(raw)) as image:
        assert image.size == (20, 40)
        assert not image.getexif()
        assert "comment" not in image.info


def test_output_budget_includes_base64_overhead(monkeypatch):
    monkeypatch.setattr(media, "MAX_OUTPUT_BYTES", 5000)
    output = io.BytesIO()
    with Image.effect_noise((480, 480), 100).convert("RGB") as image:
        image.save(output, format="PNG")
    with decode_result(media.prepare_image(output.getvalue())) as normalized:
        assert normalized.width < 480


def test_pixel_limit_is_checked_before_decoding(monkeypatch):
    raw = fixture_image(size=(4, 4))
    monkeypatch.setattr(media, "MAX_PIXELS", 16)
    decode_result(media.prepare_image(raw)).close()
    monkeypatch.setattr(media, "MAX_PIXELS", 15)
    with pytest.raises(VisionError, match="像素"):
        media.prepare_image(raw)


@pytest.mark.parametrize(
    "raw",
    [b"", b"<svg>not an allowed image</svg>", b"broken png", None, "C:/private.png"],
)
def test_invalid_image_is_a_safe_error(raw):
    with pytest.raises(VisionError) as exc:
        media.prepare_image(raw)
    assert "private.png" not in str(exc.value)


def test_tiff_is_rejected_even_when_pillow_can_read_it():
    with pytest.raises(VisionError, match="格式"):
        media.prepare_image(fixture_image("TIFF"))


def test_raw_byte_budget_is_enforced_before_image_open(monkeypatch):
    raw = fixture_image()
    monkeypatch.setattr(media, "MAX_DOWNLOAD_BYTES", len(raw) - 1)
    with pytest.raises(VisionError, match="10 MiB"):
        media.prepare_image(raw)


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
        "https://[::1]/a",
        "http://224.0.0.1/a",
        "http://[ff0e::1]/a",
        "http://[::ffff:127.0.0.1]/a",
        "http://[64:ff9b::7f00:1]/a",
        "http://[2002:7f00:1::]/a",
        "http://127.1/a",
        "http://2130706433/a",
        "http://0177.0.0.1/a",
        "http://u:p@example.com/a",
        "http://example.com:9090/a",
        "http://example.com:bad/a",
        "http://example.com/a#fragment",
        "http://%31%32%37.0.0.1/a",
        "http://example.com/\r\nprivate",
        "http://[fe80::1%25zone]/a",
        "https://example.com/" + "a" * media.MAX_URL_CHARS,
    ],
)
def test_local_unsafe_or_ambiguous_urls_rejected(url):
    with pytest.raises(VisionError):
        media.validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/image",
        "http://8.8.8.8:80/image",
        "https://[2606:4700:4700::1111]/image",
    ],
)
def test_public_http_urls_are_accepted(url):
    assert media.validate_url(url) == url


@pytest.mark.asyncio
async def test_dns_pin_rejects_mixed_public_private_answers(monkeypatch):
    async def resolve(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 443))
            for host in ["8.8.8.8", "127.0.0.1"]
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(PublicNetworkError, match="非公共"):
        await media.PublicResolver().resolve("images.example", 443)


@pytest.mark.asyncio
async def test_dns_returns_only_validated_exact_addresses(monkeypatch):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    answers = await media.PublicResolver().resolve("images.example", 443)
    assert answers == [
        {
            "hostname": "images.example",
            "host": "8.8.8.8",
            "port": 443,
            "family": socket.AF_INET,
            "proto": 6,
            "flags": socket.AI_NUMERICHOST,
        }
    ]


@pytest.mark.asyncio
async def test_dns_error_does_not_include_hostname(monkeypatch):
    async def resolve(*args, **kwargs):
        raise OSError("private-source.example")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(PublicNetworkError) as exc:
        await media.PublicResolver().resolve("private-source.example", 443)
    assert "private-source" not in str(exc.value)


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
        assert isinstance(kwargs["resolver"], media.PublicResolver)
        assert kwargs["use_dns_cache"] is False
        return SimpleNamespace()

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["auto_decompress"] is False
            assert isinstance(kwargs["cookie_jar"], media.aiohttp.DummyCookieJar)
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

    monkeypatch.setattr(media.aiohttp, "TCPConnector", connector)
    monkeypatch.setattr(media.aiohttp, "ClientSession", Session)
    return calls


@pytest.mark.asyncio
async def test_public_redirect_and_download_normalization(monkeypatch):
    calls = fake_session(
        monkeypatch,
        [Response(status=302, headers={"Location": "/new"}), Response(fixture_image())],
    )
    decode_result(await media.load_image("https://images.example/old")).close()
    assert calls == ["https://images.example/old", "https://images.example/new"]


@pytest.mark.asyncio
async def test_redirect_to_private_is_never_requested(monkeypatch):
    calls = fake_session(
        monkeypatch,
        [Response(status=302, headers={"Location": "http://127.0.0.1/private"})],
    )
    with pytest.raises(VisionError):
        await media.load_image("https://images.example/old")
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        Response(headers={"Content-Length": str(media.MAX_DOWNLOAD_BYTES + 1)}),
        Response(headers={"Content-Encoding": "gzip"}),
        Response(status=404),
        Response(headers={"Content-Length": "invalid"}),
        Response(headers={"Content-Length": "-1"}),
        Response(headers={"Content-Length": "100"}),
    ],
)
async def test_download_rejects_invalid_size_encoding_and_status(monkeypatch, response):
    fake_session(monkeypatch, [response])
    with pytest.raises(VisionError):
        await media.load_image("https://images.example/file")


@pytest.mark.asyncio
async def test_stream_budget_does_not_trust_content_length(monkeypatch):
    monkeypatch.setattr(media, "MAX_DOWNLOAD_BYTES", 16)
    fake_session(monkeypatch, [Response(body=b"x" * 17)])
    with pytest.raises(VisionError, match="10 MiB"):
        await media.load_image("https://images.example/file")


@pytest.mark.asyncio
async def test_exact_stream_limit_is_allowed(monkeypatch):
    monkeypatch.setattr(media, "MAX_DOWNLOAD_BYTES", 16)
    fake_session(
        monkeypatch, [Response(body=b"x" * 16, headers={"Content-Length": "16"})]
    )
    assert await media._download("https://images.example/file") == b"x" * 16


@pytest.mark.asyncio
async def test_redirect_count_is_bounded(monkeypatch):
    calls = fake_session(
        monkeypatch,
        [Response(status=302, headers={"Location": "/next"}) for _ in range(4)],
    )
    with pytest.raises(VisionError, match="重定向"):
        await media.load_image("https://images.example/file")
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_total_deadline_and_error_redaction(monkeypatch):
    async def slow(url):
        await asyncio.sleep(1)

    monkeypatch.setattr(media, "DOWNLOAD_TIMEOUT", 0.001)
    monkeypatch.setattr(media, "_download", slow)
    with pytest.raises(VisionError, match="20 秒"):
        await media.load_image("https://images.example/?secret=never-log")

    async def failing(url):
        raise RuntimeError(url)

    monkeypatch.setattr(media, "_download", failing)
    with pytest.raises(VisionError) as exc:
        await media.load_image("https://images.example/?secret=never-log")
    assert "secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_cancellation_is_not_hidden(monkeypatch):
    async def cancelled(url):
        raise asyncio.CancelledError

    monkeypatch.setattr(media, "_download", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await media.load_image("https://images.example/file")


@pytest.mark.asyncio
async def test_safe_dns_code_survives_media_boundary(monkeypatch):
    async def failed(url):
        raise PublicNetworkError("图片地址解析到非公共网络。", code="dns_non_public")

    monkeypatch.setattr(media, "_download", failed)
    with pytest.raises(ImageReadError) as caught:
        await media.load_image("https://images.example/?secret=never-log")
    assert caught.value.stage == "dns"
    assert caught.value.code == "dns_non_public"
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_corrupt_image_is_reported_as_decode_not_download(monkeypatch):
    async def downloaded(url):
        return b"synthetic broken image"

    monkeypatch.setattr(media, "_download", downloaded)
    with pytest.raises(ImageReadError) as caught:
        await media.load_image("https://images.example/?secret=never-log")
    assert caught.value.stage == "decode"
    assert caught.value.code == "image_decode_failed"
    assert "secret" not in str(caught.value)
