"""Turn animated references into bounded chronological model inputs, not first frames."""
from __future__ import annotations

import asyncio
import base64
import io

from PIL import Image

from qunling.animation_preview import contact_sheet

MAX_BYTES = 10 * 1024 * 1024


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
        with contact_sheet(image) as sheet:
            output = io.BytesIO()
            sheet.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii"), True


async def model_image_base64(value: str) -> str:
    normalized, _ = await asyncio.to_thread(animation_data_url, value)
    return normalized


async def model_image_content(value: str):
    from langbot_plugin.api.entities.builtin.provider.message import ContentElement
    try:
        return ContentElement.from_image_base64(await model_image_base64(value))
    except (ValueError, OSError):
        return ContentElement.from_text("[动画关键帧读取失败；原始引用仍保留，不能声称已查看动作。]")
