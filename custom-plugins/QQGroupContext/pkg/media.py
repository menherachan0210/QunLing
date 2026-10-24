"""Bounded public image loading for selected, trusted group-history sources."""

from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
from urllib.parse import urljoin, urlsplit

import aiohttp
from PIL import Image, ImageOps

from pkg.errors import ImageReadError, VisionError
from pkg.public_network import PublicNetworkError, PublicResolver, public_ip

MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_URL_CHARS = 8192
MAX_PIXELS = 40_000_000
MAX_EDGE = 2400
DOWNLOAD_TIMEOUT = 20
IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP")
DATA_PREFIX = "data:image/jpeg;base64,"


def validate_url(url: str) -> str:
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        raise VisionError("图片下载地址无效，请重新发送或引用图片。")
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise VisionError("图片下载地址无效，请重新发送或引用图片。")
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
            # Numeric alternatives may bypass aiohttp's custom resolver.
            if ":" in host or host.replace(".", "").isdigit():
                raise ValueError from None
        else:
            if not public_ip(host):
                raise ValueError
    except ValueError:
        raise VisionError("图片地址不允许访问，只支持公共 HTTP(S) 图片地址。") from None
    return url


async def _download(url: str) -> bytes:
    validate_url(url)
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT, connect=10)
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        trust_env=False,
        auto_decompress=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        headers={
            "Accept": "image/png,image/jpeg,image/webp,image/gif,image/bmp;q=0.9",
            "Accept-Encoding": "identity",
        },
    ) as client:
        for redirect in range(4):
            validate_url(url)
            async with client.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if redirect == 3 or not location:
                        raise VisionError("图片下载重定向无效，请重新发送图片。")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise VisionError("图片下载失败，请重新发送或引用图片。")
                if (
                    response.headers.get("Content-Encoding", "identity").strip().lower()
                    != "identity"
                ):
                    raise VisionError("图片下载返回了不支持的内容编码。")
                declared = response.headers.get("Content-Length")
                declared_size = None
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except ValueError:
                        raise VisionError("图片下载大小信息无效。") from None
                    if declared_size < 0 or declared_size > MAX_DOWNLOAD_BYTES:
                        raise VisionError("单张图片超过 10 MiB，请压缩后重新发送。")
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > MAX_DOWNLOAD_BYTES:
                        raise VisionError("单张图片超过 10 MiB，请压缩后重新发送。")
                if declared_size is not None and len(body) != declared_size:
                    raise VisionError("图片下载不完整，请重新发送或引用图片。")
                return bytes(body)
    raise VisionError("图片下载失败，请重新发送或引用图片。")


def _prepare_image(raw: bytes) -> str:
    if not isinstance(raw, bytes) or not raw:
        raise VisionError("图片内容为空或无效，请重新发送。")
    if len(raw) > MAX_DOWNLOAD_BYTES:
        raise VisionError("单张图片超过 10 MiB，请压缩后重新发送。")
    with Image.open(io.BytesIO(raw), formats=list(IMAGE_FORMATS)) as image:
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
            raise VisionError("图片像素超过 4000 万，请缩小后重新发送。")
        if getattr(image, "is_animated", False):
            from qunling.animation_preview import contact_sheet
            with contact_sheet(image) as preview:
                output = io.BytesIO()
                preview.save(output, format="PNG")
            return _prepare_image(output.getvalue())
        image.seek(0)
        image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
        with ImageOps.exif_transpose(image) as frame:
            if frame.mode in {"RGBA", "LA"} or "transparency" in frame.info:
                with frame.convert("RGBA") as rgba:
                    rgb = Image.new("RGB", frame.size, "white")
                    rgb.paste(rgba, mask=rgba.getchannel("A"))
            else:
                rgb = frame.convert("RGB")
            with rgb:
                # Pillow may inherit a JPEG comment from Image.info even when
                # no save keyword is supplied. Retain pixel data only.
                rgb.info.clear()
                for _ in range(12):
                    output = io.BytesIO()
                    # Do not preserve EXIF, comments, filenames, or other metadata.
                    rgb.save(output, format="JPEG", quality=85, optimize=True)
                    encoded = base64.b64encode(output.getvalue()).decode("ascii")
                    result = DATA_PREFIX + encoded
                    if len(result) <= MAX_OUTPUT_BYTES:
                        return result
                    target = (
                        max(1, int(rgb.width * 0.8)),
                        max(1, int(rgb.height * 0.8)),
                    )
                    if target == rgb.size:
                        break
                    rgb.thumbnail(target, Image.Resampling.LANCZOS)
    raise VisionError("图片处理后仍然过大，请缩小后重新发送。")


def prepare_image(raw: bytes) -> str:
    """Normalize one image to a metadata-free JPEG data URI, at most 2 MiB."""
    try:
        return _prepare_image(raw)
    except VisionError:
        raise
    except Exception:
        raise VisionError(
            "图片格式不支持或内容损坏，请重新发送 PNG、JPEG、WEBP、GIF 或 BMP。"
        ) from None


async def load_image(url: str) -> str:
    """Download within one 20-second window, then normalize off the event loop."""
    try:
        async with asyncio.timeout(DOWNLOAD_TIMEOUT):
            raw = await _download(url)
    except ImageReadError:
        raise
    except PublicNetworkError as exc:
        raise ImageReadError(str(exc), stage="dns", code=exc.code) from None
    except VisionError as exc:
        raise ImageReadError(
            str(exc), stage="download", code="image_download_rejected"
        ) from None
    except TimeoutError:
        raise ImageReadError(
            "图片读取超过 20 秒，请稍后重试或重新发送。",
            stage="download",
            code="image_download_timeout",
        ) from None
    except Exception:
        raise ImageReadError(
            "图片下载失败，请重新发送或引用图片。",
            stage="download",
            code="image_download_failed",
        ) from None
    try:
        return await asyncio.to_thread(prepare_image, raw)
    except VisionError as exc:
        raise ImageReadError(
            str(exc), stage="decode", code="image_decode_failed"
        ) from None
    except Exception:
        raise ImageReadError(
            "图片处理失败，请重新发送支持的图片格式。",
            stage="decode",
            code="image_decode_failed",
        ) from None
