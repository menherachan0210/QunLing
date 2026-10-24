from __future__ import annotations

import asyncio
import io
import random
import struct
import threading
from unittest.mock import AsyncMock
import zlib

import pytest
from PIL import Image, PngImagePlugin

from pkg import media
from pkg.errors import WebError
from pkg.network import Fetched


def fixture_image(
    format_name="PNG", *, size=(40, 20), color="red", mode="RGB", **kwargs
):
    output = io.BytesIO()
    with Image.new(mode, size, color) as image:
        image.save(output, format=format_name, **kwargs)
    return output.getvalue()


def decode_result(result):
    assert isinstance(result["data"], bytes)
    assert 0 < len(result["data"]) <= media.MAX_OUTPUT_BYTES
    assert result["mime"] in {"image/jpeg", "image/png"}
    image = Image.open(io.BytesIO(result["data"]))
    image.load()
    assert result["mime"] == Image.MIME[image.format]
    assert (result["width"], result["height"]) == image.size
    assert 0 < max(image.size) <= media.MAX_EDGE
    return image


@pytest.mark.parametrize("format_name", ["PNG", "JPEG", "WEBP", "GIF", "BMP"])
def test_supported_opaque_images_become_bounded_jpeg(format_name):
    with decode_result(media.prepare_image(fixture_image(format_name))) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"
        assert image.size == (40, 20)
        red, green, blue = image.getpixel((10, 10))
        assert red > 240 and green < 10 and blue < 10


def test_large_image_is_reduced_without_upscaling_small_images():
    with decode_result(media.prepare_image(fixture_image(size=(5000, 100)))) as image:
        assert image.size == (2400, 48)
    with decode_result(media.prepare_image(fixture_image(size=(10, 5)))) as image:
        assert image.size == (10, 5)


@pytest.mark.parametrize("mode,color", [("RGBA", (12, 34, 56, 0)), ("LA", (48, 0))])
def test_transparency_is_preserved_as_png(mode, color):
    raw = fixture_image(mode=mode, color=color)
    with decode_result(media.prepare_image(raw)) as image:
        assert image.format == "PNG"
        assert image.convert("RGBA").getpixel((10, 10))[3] == 0


def test_palette_transparency_is_preserved():
    raw = fixture_image("GIF", mode="P", color=0, transparency=0)
    with decode_result(media.prepare_image(raw)) as image:
        assert image.format == "PNG"
        assert image.convert("RGBA").getpixel((10, 10))[3] == 0


def test_gif_preserves_animation_bytes_and_timing():
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
    raw = output.getvalue()
    result = media.prepare_image(raw)
    assert result["animated"] and result["frames"] == 2
    assert result["duration_ms"] == 200
    assert result["data"] == raw and result["mime"] == "image/gif"
    with Image.open(io.BytesIO(result["data"])) as image:
        image.seek(1)
        assert image.convert("RGB").getpixel((10, 10)) == (0, 0, 255)


def test_exif_orientation_is_applied_and_jpeg_metadata_removed():
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "synthetic private metadata"
    raw = fixture_image(
        "JPEG",
        exif=exif,
        comment=b"synthetic private comment",
        icc_profile=b"synthetic private color profile",
    )
    with decode_result(media.prepare_image(raw)) as image:
        assert image.size == (20, 40)
        assert not image.getexif()
        assert "comment" not in image.info
        assert "icc_profile" not in image.info


def test_png_text_metadata_is_not_copied_to_transparent_result():
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Description", "synthetic private description")
    metadata.add_itxt("Author", "synthetic private author")
    raw = fixture_image(
        mode="RGBA",
        color=(255, 0, 0, 128),
        pnginfo=metadata,
        icc_profile=b"synthetic private color profile",
    )
    result = media.prepare_image(raw)
    with decode_result(result) as image:
        assert image.format == "PNG"
        assert image.convert("RGBA").getpixel((10, 10))[3] == 128
        assert not image.getexif()
        assert not image.text
        assert "icc_profile" not in image.info
    assert b"synthetic private" not in result["data"]


def test_output_byte_budget_can_reduce_dimensions(monkeypatch):
    monkeypatch.setattr(media, "MAX_OUTPUT_BYTES", 5000)
    output = io.BytesIO()
    pixels = random.Random(0).randbytes(480 * 480 * 3)
    with Image.frombytes("RGB", (480, 480), pixels) as image:
        image.save(output, format="PNG")
    with decode_result(media.prepare_image(output.getvalue())) as image:
        assert image.width < 480
        assert image.width == image.height


def test_unachievable_output_budget_fails_safely(monkeypatch):
    monkeypatch.setattr(media, "MAX_OUTPUT_BYTES", 1)
    with pytest.raises(WebError):
        media.prepare_image(fixture_image())


def test_pixel_limit_boundary_is_allowed(monkeypatch):
    monkeypatch.setattr(media, "MAX_PIXELS", 16)
    with decode_result(media.prepare_image(fixture_image(size=(4, 4)))) as image:
        assert image.size == (4, 4)


def test_oversized_dimensions_are_rejected_before_pixel_decode(monkeypatch):
    raw = fixture_image(size=(4, 4))
    monkeypatch.setattr(media, "MAX_PIXELS", 15)
    load = Image.Image.load
    decoded = []

    def tracked_load(image, *args, **kwargs):
        decoded.append(image.size)
        return load(image, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "load", tracked_load)
    with pytest.raises(WebError, match="像素"):
        media.prepare_image(raw)
    assert decoded == []


def test_real_pixel_budget_rejects_over_12_megapixels_without_decoding(monkeypatch):
    raw = bytearray(fixture_image(size=(4, 4)))
    header = raw.index(b"IHDR")
    raw[header + 4 : header + 12] = struct.pack(">II", 4000, 3001)
    raw[header + 17 : header + 21] = struct.pack(
        ">I", zlib.crc32(raw[header : header + 17])
    )
    decoded = []

    def unexpected_load(*args, **kwargs):
        decoded.append(True)
        raise AssertionError("oversized pixels reached decoding")

    monkeypatch.setattr(Image.Image, "load", unexpected_load)
    with pytest.raises(WebError, match="1200"):
        media.prepare_image(bytes(raw))
    assert decoded == []


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
        b"<html><body>private-download-token</body></html>",
        b"broken png",
        None,
        "C:/private.png",
    ],
)
def test_non_images_raise_safe_errors(raw):
    with pytest.raises(WebError) as exc:
        media.prepare_image(raw)
    assert "private.png" not in str(exc.value)
    assert "private-download-token" not in str(exc.value)


def test_tiff_is_rejected_even_when_pillow_can_read_it():
    with pytest.raises(WebError):
        media.prepare_image(fixture_image("TIFF"))


@pytest.mark.parametrize("format_name", ["PNG", "JPEG", "WEBP", "GIF", "BMP"])
def test_truncated_image_is_rejected(format_name):
    raw = fixture_image(format_name)
    with pytest.raises(WebError):
        media.prepare_image(raw[: len(raw) // 2])


def test_png_corrupt_chunk_checksum_is_rejected():
    raw = bytearray(fixture_image())
    idat = raw.index(b"IDAT")
    chunk_length = int.from_bytes(raw[idat - 4 : idat], "big")
    raw[idat + 4 + chunk_length] ^= 1
    with pytest.raises(WebError):
        media.prepare_image(bytes(raw))


def test_input_byte_budget_is_enforced_before_image_open(monkeypatch):
    raw = fixture_image()
    monkeypatch.setattr(media, "MAX_DOWNLOAD_BYTES", len(raw) - 1)
    opened = []

    def unexpected_open(*args, **kwargs):
        opened.append(True)
        raise AssertionError("oversized input reached Pillow")

    monkeypatch.setattr(media.Image, "open", unexpected_open)
    with pytest.raises(WebError):
        media.prepare_image(raw)
    assert opened == []


def test_input_byte_budget_boundary_is_allowed(monkeypatch):
    raw = fixture_image()
    monkeypatch.setattr(media, "MAX_DOWNLOAD_BYTES", len(raw))
    with decode_result(media.prepare_image(raw)) as image:
        assert image.size == (40, 20)


@pytest.mark.asyncio
async def test_fetch_image_uses_bounded_downloader_and_actual_image_bytes(monkeypatch):
    raw = fixture_image()
    fetch = AsyncMock(
        return_value=Fetched(
            url="https://images.example/redirected",
            body=raw,
            content_type="text/html",
        )
    )
    monkeypatch.setattr(media, "fetch_public", fetch)
    with decode_result(
        await media.fetch_image("https://images.example/start")
    ) as image:
        assert image.format == "JPEG"
    fetch.assert_awaited_once_with(
        "https://images.example/start", max_bytes=5 * 1024 * 1024, timeout=20
    )


@pytest.mark.asyncio
async def test_image_content_type_does_not_allow_html_payload(monkeypatch):
    fetch = AsyncMock(
        return_value=Fetched(
            url="https://images.example/blocked",
            body=b"<html>private-provider-error</html>",
            content_type="image/png",
        )
    )
    monkeypatch.setattr(media, "fetch_public", fetch)
    with pytest.raises(WebError) as exc:
        await media.fetch_image("https://images.example/blocked")
    assert "private-provider-error" not in str(exc.value)


@pytest.mark.asyncio
async def test_normalization_runs_outside_the_event_loop_thread(monkeypatch):
    raw = fixture_image()
    fetch = AsyncMock(
        return_value=Fetched(
            url="https://images.example/image", body=raw, content_type="image/png"
        )
    )
    monkeypatch.setattr(media, "fetch_public", fetch)
    event_loop_thread = threading.get_ident()
    seen_threads = []
    prepare_image = media.prepare_image

    def tracked_prepare(value):
        assert value == raw
        seen_threads.append(threading.get_ident())
        return prepare_image(value)

    monkeypatch.setattr(media, "prepare_image", tracked_prepare)
    with decode_result(await media.fetch_image("https://images.example/image")):
        pass
    assert len(seen_threads) == 1
    assert seen_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_downloader_safe_error_is_propagated_without_processing(monkeypatch):
    fetch = AsyncMock(side_effect=WebError("访问非公共地址已拒绝。"))
    monkeypatch.setattr(media, "fetch_public", fetch)
    prepared = []

    def unexpected_prepare(raw):
        prepared.append(raw)
        raise AssertionError("failed download reached decoder")

    monkeypatch.setattr(media, "prepare_image", unexpected_prepare)
    with pytest.raises(WebError, match="非公共"):
        await media.fetch_image("https://images.example/blocked")
    assert prepared == []
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_cancellation_is_preserved(monkeypatch):
    fetch = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(media, "fetch_public", fetch)
    with pytest.raises(asyncio.CancelledError):
        await media.fetch_image("https://images.example/image")
    fetch.assert_awaited_once()


class ControlledDecoder:
    """Real threads with synthetic bytes and explicit gates, never network I/O."""

    def __init__(self, count, *, fail_first=False):
        self.loop = asyncio.get_running_loop()
        self.started = []
        self.active = self.peak = 0
        self.guard = threading.Lock()
        self.entered = [asyncio.Event() for _ in range(count)]
        self.release = [threading.Event() for _ in range(count)]
        self.fail_first = fail_first

    async def fetch(self, url, **kwargs):
        return Fetched(url, url.rsplit("/", 1)[1].encode(), "image/png")

    def prepare(self, raw):
        index = int(raw)
        with self.guard:
            self.started.append(index)
            self.active += 1
            self.peak = max(self.peak, self.active)
        self.loop.call_soon_threadsafe(self.entered[index].set)
        try:
            if not self.release[index].wait(timeout=5):
                raise AssertionError("test failed to release decoder thread")
            if index == 0 and self.fail_first:
                raise WebError("synthetic decode failure")
            return {"data": raw}
        finally:
            with self.guard:
                self.active -= 1

    async def wait_started(self, *indices):
        await asyncio.wait_for(
            asyncio.gather(*(self.entered[index].wait() for index in indices)), 2
        )

    def unblock_all(self):
        for gate in self.release:
            gate.set()


def install_decoder(monkeypatch, decoder):
    monkeypatch.setattr(media, "fetch_public", decoder.fetch)
    monkeypatch.setattr(media, "prepare_image", decoder.prepare)


def image_task(index):
    return asyncio.create_task(media.fetch_image(f"https://images.example/{index}"))


@pytest.mark.asyncio
async def test_process_decode_pool_limits_six_candidates_to_two_threads(monkeypatch):
    decoder = ControlledDecoder(6)
    install_decoder(monkeypatch, decoder)
    tasks = [image_task(index) for index in range(6)]
    try:
        await decoder.wait_started(0, 1)
        await asyncio.sleep(0)
        assert set(decoder.started) == {0, 1}
        assert decoder.peak == 2
    finally:
        decoder.unblock_all()
        results = await asyncio.gather(*tasks)
    assert results == [{"data": str(index).encode()} for index in range(6)]
    assert decoder.peak == 2
    assert decoder.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_first", [False, True])
async def test_cancellation_returns_promptly_but_running_decode_retains_capacity(
    monkeypatch, fail_first
):
    decoder = ControlledDecoder(3, fail_first=fail_first)
    install_decoder(monkeypatch, decoder)
    tasks = [image_task(0), image_task(1)]
    try:
        await decoder.wait_started(0, 1)
        tasks.append(image_task(2))
        await asyncio.sleep(0)
        tasks[0].cancel()
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(tasks[0], 1)
        assert not decoder.entered[2].is_set()
        assert decoder.active == 2

        decoder.release[0].set()
        await decoder.wait_started(2)
        assert decoder.peak == 2
    finally:
        decoder.unblock_all()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert decoder.active == 0


@pytest.mark.asyncio
async def test_cancelled_queued_decode_never_starts(monkeypatch):
    decoder = ControlledDecoder(3)
    install_decoder(monkeypatch, decoder)
    tasks = [image_task(0), image_task(1)]
    try:
        await decoder.wait_started(0, 1)
        tasks.append(image_task(2))
        await asyncio.sleep(0)
        tasks[2].cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(tasks[2], 1)
        assert not decoder.entered[2].is_set()
        assert decoder.active == 2
    finally:
        decoder.unblock_all()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert set(decoder.started) == {0, 1}
    assert decoder.active == 0
