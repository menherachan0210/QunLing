"""Turn animated references into bounded chronological model inputs, not first frames."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

from PIL import Image

from qunling.animation_preview import contact_sheet

MAX_BYTES = 10 * 1024 * 1024
MAX_ANIMATION_PIXELS = 12_000_000
MAX_PREVIEW_BYTES = 2 * 1024 * 1024
DECODE_TIMEOUT = 15
logger = logging.getLogger(__name__)


def decoder_memory_mib(value: str) -> int:
    """Budget from fixed image headers only; no pixel decoding in the Host.

    The child enforces the same cap, so malformed/understated headers cannot
    turn a small reservation into an unbounded decode. Unknown layouts retain
    the full budget. Encoded input copies and canvas working buffers count too.
    """
    start = value.find(",", 0, 128) + 1 if value.startswith("data:") else 0
    try:
        header = base64.b64decode(value[start:start + 44], validate=True)
    except ValueError:
        return 384
    width = height = 0
    if header.startswith((b"GIF87a", b"GIF89a")) and len(header) >= 10:
        width, height = int.from_bytes(header[6:8], "little"), int.from_bytes(header[8:10], "little")
    elif header.startswith(b"\x89PNG\r\n\x1a\n") and header[12:16] == b"IHDR" and len(header) >= 24:
        width, height = int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
    elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        if header[12:16] == b"VP8X" and len(header) >= 30:
            width = 1 + int.from_bytes(header[24:27], "little")
            height = 1 + int.from_bytes(header[27:30], "little")
        elif header[12:16] == b"VP8L" and len(header) >= 25 and header[20] == 0x2f:
            packed = int.from_bytes(header[21:25], "little")
            width, height = 1 + (packed & 0x3fff), 1 + ((packed >> 14) & 0x3fff)
        elif header[12:16] == b"VP8 " and len(header) >= 30 and header[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(header[26:28], "little") & 0x3fff
            height = int.from_bytes(header[28:30], "little") & 0x3fff
    if not width or not height:
        return 384
    estimate = 96 * 1024**2 + width * height * 16 + (len(value) - start) * 3
    return next((size for size in (128, 256) if estimate <= size * 1024**2), 384)


def animation_data_url(value: str) -> tuple[str, bool]:
    """Keep originals outside this adapter; only the model gets a sampled sheet."""
    if not isinstance(value, str):
        raise ValueError("Invalid image input")
    payload = value.partition(",")[2] if value.startswith("data:") else value
    # PNG and WebP may be animated too. Leave ordinary JPEGs and legacy fixtures alone.
    if not payload.startswith(("R0lGOD", "iVBORw0KGgo", "UklGR")):
        return value, False
    if len(payload) > 4 * ((MAX_BYTES + 2) // 3):
        raise ValueError("Image exceeds input budget")
    raw = base64.b64decode(payload, validate=True)
    if len(raw) > MAX_BYTES:
        raise ValueError("Image exceeds input budget")
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > 40_000_000:
            raise ValueError("Image exceeds pixel budget")
        if not getattr(image, "is_animated", False):
            return value, False
        if image.width * image.height > MAX_ANIMATION_PIXELS:
            raise ValueError("Animation canvas exceeds decoder budget")
        with contact_sheet(image) as sheet:
            output = io.BytesIO()
            sheet.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii"), True


async def _stop_decoder(process):
    if process.returncode is None:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    await process.wait()


async def _isolated_animation(value: str, *, memory_mib: int = 384) -> str:
    from qunling.resource_gate import finish_thread

    with tempfile.TemporaryDirectory(prefix="qunling-vision-") as temporary:
        root = Path(temporary)
        await finish_thread((root / "input.txt").write_text, value, "ascii")
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
        }}
        env.update(PYTHONUTF8="1", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
        options = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
                   else {"start_new_session": True})
        launch = asyncio.create_task(asyncio.create_subprocess_exec(
            sys.executable, "-I", str(Path(__file__).with_name("_vision_worker.py")), str(memory_mib),
            cwd=temporary, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options))
        try:
            process = await asyncio.shield(launch)
        except asyncio.CancelledError:
            process = await launch
            await _stop_decoder(process)
            raise
        try:
            async with asyncio.timeout(DECODE_TIMEOUT):
                await process.wait()
        except BaseException:
            await _stop_decoder(process)
            raise
        result_path = root / "result.json"
        if (process.returncode or not result_path.is_file()
                or result_path.stat().st_size > 1024):
            raise ValueError("Image decoder reached its resource limit")
        result = json.loads(result_path.read_text(encoding="ascii"))
        if not result.get("ok"):
            raise ValueError("Image could not be decoded within its resource limit")
        if not result.get("animated"):
            return value
        preview = root / "preview.png"
        if not preview.is_file() or preview.stat().st_size > MAX_PREVIEW_BYTES:
            raise ValueError("Image preview exceeds output budget")
        body = await finish_thread(preview.read_bytes)
        return "data:image/png;base64," + base64.b64encode(body).decode("ascii")


async def model_image_base64(value: str) -> str:
    from qunling.resource_gate import resource_slot

    if not isinstance(value, str):
        raise ValueError("Invalid image input")
    # Inspect a small prefix without copying a multi-megabyte data URI.
    start = value.find(",", 0, 128) + 1 if value.startswith("data:") else 0
    if not value.startswith(("R0lGOD", "iVBORw0KGgo", "UklGR"), start):
        return value
    if len(value) > 4 * ((MAX_BYTES + 2) // 3) + 128:
        raise ValueError("Image exceeds input budget")
    from qunling.image_header import static_image
    if static_image(value):
        # The provider consumes compressed static images directly. Do not
        # reserve animation buffers or launch a pixel decoder for a screenshot.
        return value
    memory_mib = decoder_memory_mib(value)
    kind = {128: "vision_small", 256: "vision_medium", 384: "vision"}[memory_mib]
    async with resource_slot(kind, wait_seconds=3):
        return await _isolated_animation(value, memory_mib=memory_mib)


async def model_image_content(value: str):
    from langbot_plugin.api.entities.builtin.provider.message import ContentElement
    from qunling.resource_gate import ResourceBusy
    try:
        return ContentElement.from_image_base64(await model_image_base64(value))
    except ResourceBusy:
        logger.warning("vision_input_unavailable reason=capacity")
        return ContentElement.from_text(
            "[图片暂未分析：服务器暂时繁忙，原图已保留，不代表图片损坏。"
            "本轮不要重复读图、导入或查历史重试，也不能声称已看清内容或动作。]")
    except (ValueError, OSError, TimeoutError) as error:
        logger.warning("vision_input_unavailable reason=%s",
                       "deadline" if isinstance(error, TimeoutError) else "decode")
        return ContentElement.from_text("[图片或动画读取失败；原始引用仍保留，不能声称已看清内容或动作。]")
