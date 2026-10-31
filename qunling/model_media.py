"""Bound image history per provider request without changing stored originals."""
from __future__ import annotations

from contextlib import asynccontextmanager

from qunling.incoming_media import MAX_MODEL_IMAGE_CHARS, MAX_MODEL_IMAGES
from qunling.resource_gate import resource_slot

OMITTED_IMAGE = "[较早的图片未包含在本次视觉输入中；不能声称已重新查看。]"


def _url(part):
    if not isinstance(part, dict) or part.get("type") != "image_url":
        return None
    value = part.get("image_url")
    return value.get("url") if isinstance(value, dict) else value


def bounded_images(messages):
    """Prefer recent images across the entire history, not a fresh budget per turn."""
    remaining, count, result = MAX_MODEL_IMAGE_CHARS, 0, list(messages)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        content = message.get("content")
        if not isinstance(content, list):
            continue
        revised = list(content)
        changed = False
        for offset in range(len(content) - 1, -1, -1):
            url = _url(content[offset])
            if not isinstance(url, str):
                continue
            size = len(url) if url.startswith("data:") else 0
            if count >= MAX_MODEL_IMAGES or size > remaining:
                revised[offset] = {"type": "text", "text": OMITTED_IMAGE}
                changed = True
            else:
                remaining -= size
                count += 1
        if changed:
            result[index] = {**message, "content": revised}
    return result


@asynccontextmanager
async def model_request_slot(messages):
    """Reserve serialization/HTTP buffer space for substantial inline media."""
    size = sum(len(url) for message in messages
               if isinstance(message.get("content"), list)
               for part in message["content"]
               if isinstance(url := _url(part), str) and url.startswith("data:"))
    if size < 256 * 1024:
        yield
        return
    async with resource_slot("model_media", wait_seconds=5):
        yield
