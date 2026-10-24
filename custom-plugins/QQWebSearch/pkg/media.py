"""Decode real bitmap bytes and return bounded, metadata-free PNG or JPEG."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import io

from PIL import Image, ImageOps

from pkg.errors import WebError
from pkg.network import fetch_public

MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 12_000_000
MAX_EDGE = 2400
IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP")
# Each worker process owns two decoder threads, including work whose caller was
# cancelled. A separate pool also leaves SDK blocking workers free for other APIs.
_DECODE_EXECUTOR = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="qqwebsearch-image"
)


def _prepare_image(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not raw:
        raise WebError("图片内容为空或无效。")
    if len(raw) > MAX_DOWNLOAD_BYTES:
        raise WebError("单张图片超过 5 MiB，无法读取。")
    with Image.open(io.BytesIO(raw), formats=list(IMAGE_FORMATS)) as probe:
        width, height = probe.size
        if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
            raise WebError("图片像素超过 1200 万，无法读取。")
        # Verify container integrity before reopening for actual pixel decoding.
        probe.verify()
    with Image.open(io.BytesIO(raw), formats=list(IMAGE_FORMATS)) as image:
        if getattr(image, "is_animated", False):
            # Validate all frames within a finite decode budget, then retain the
            # animation byte-for-byte. Never silently turn an animated meme into a still.
            frame_limit = min(240, 80_000_000 // (width * height))
            duration = 0
            for index in range(frame_limit + 1):
                try:
                    image.seek(index)
                except EOFError:
                    return {"data": raw, "mime": Image.MIME[image.format],
                            "width": width, "height": height, "animated": True,
                            "frames": index, "duration_ms": duration}
                if index == frame_limit:
                    raise WebError("动图超出解码帧数或像素预算，未将它冒充静态图。", code="animation_limit")
                image.load()
                duration += max(20, min(60000, int(image.info.get("duration", 100) or 100)))
        image.seek(0)
        image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
        with ImageOps.exif_transpose(image) as frame:
            transparent = frame.mode in {"RGBA", "LA"} or "transparency" in frame.info
            with frame.convert("RGBA" if transparent else "RGB") as output_image:
                output_image.info.clear()
                format_name, mime = (
                    ("PNG", "image/png") if transparent else ("JPEG", "image/jpeg")
                )
                for _ in range(14):
                    output = io.BytesIO()
                    options = {"optimize": True}
                    if format_name == "JPEG":
                        options["quality"] = 85
                    output_image.save(output, format=format_name, **options)
                    data = output.getvalue()
                    if len(data) <= MAX_OUTPUT_BYTES:
                        return {
                            "data": data,
                            "mime": mime,
                            "width": output_image.width,
                            "height": output_image.height,
                        }
                    target = (
                        max(1, int(output_image.width * 0.8)),
                        max(1, int(output_image.height * 0.8)),
                    )
                    if target == output_image.size:
                        break
                    output_image.thumbnail(target, Image.Resampling.LANCZOS)
    raise WebError("图片处理后仍然过大，无法发送。")


def prepare_image(raw: bytes) -> dict:
    try:
        return _prepare_image(raw)
    except WebError:
        raise
    except Exception:
        raise WebError("图片不是有效的 PNG、JPEG、WebP、GIF 或 BMP 位图。") from None


async def _prepare_image_async(raw: bytes) -> dict:
    pending = _DECODE_EXECUTOR.submit(prepare_image, raw)
    result = asyncio.wrap_future(pending)
    try:
        return await asyncio.shield(result)
    except asyncio.CancelledError:
        # Cancel queued work. A running Pillow decoder cannot be interrupted:
        # the executor keeps that thread occupied until it actually finishes.
        # Return cancellation promptly so cleanup cannot overrun the tool budget.
        pending.cancel()

        def discard_finished(future):
            if not future.cancelled():
                future.exception()

        result.add_done_callback(discard_finished)
        raise


async def fetch_image(url: str) -> dict:
    try:
        fetched = await fetch_public(url, max_bytes=MAX_DOWNLOAD_BYTES, timeout=20)
        return await _prepare_image_async(fetched.body)
    except WebError:
        raise
    except Exception:
        raise WebError("图片读取或处理失败。") from None
