"""Turn animated references into bounded chronological model inputs, not first frames."""
from __future__ import annotations

import asyncio
import base64
import io
import json
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


async def _isolated_animation(value: str) -> str:
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
            sys.executable, "-I", str(Path(__file__).with_name("_vision_worker.py")),
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
    async with resource_slot("vision", wait_seconds=3):
        return await _isolated_animation(value)


async def model_image_content(value: str):
    from langbot_plugin.api.entities.builtin.provider.message import ContentElement
    from qunling.resource_gate import ResourceBusy
    try:
        return ContentElement.from_image_base64(await model_image_base64(value))
    except (ValueError, OSError, TimeoutError, ResourceBusy):
        return ContentElement.from_text("[图片或动画读取失败；原始引用仍保留，不能声称已看清内容或动作。]")
