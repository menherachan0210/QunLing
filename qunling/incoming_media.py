"""Keep received image references small; load pixels only for an admitted turn."""
from __future__ import annotations

import asyncio
import os


MAX_MODEL_IMAGE_CHARS = 14 * 1024 * 1024
MAX_MODEL_IMAGES = 8


def deferred_image(segment):
    if (not os.environ.get("QUNLING_DATA_DIR") or segment.type != "image"
            or segment.data.get("emoji_package_id")):
        return None
    from langbot_plugin.api.entities.builtin.platform.message import Image
    return Image(image_id=str(segment.data.get("file", "")),
                 url=segment.data.get("url") or segment.data.get("file") or "")


async def model_message_chain(chain, keep_images, include_quotes):
    """Copy only model inputs. Original references remain available for editing."""
    if not keep_images:
        return chain
    from langbot_plugin.api.entities.builtin.platform.message import Image, MessageChain, Quote
    from langbot.pkg.utils.image import qq_image_url_to_base64

    remaining = MAX_MODEL_IMAGE_CHARS
    count = 0

    async def load(item):
        nonlocal remaining, count
        if not isinstance(item, Image):
            return item
        value = item.base64 or ""
        if count >= MAX_MODEL_IMAGES or remaining <= 0:
            return item.model_copy(update={"base64": ""})
        count += 1
        if not value and (item.url or "").startswith(("https://", "http://")):
            try:
                async with asyncio.timeout(15):
                    payload, image_format = await qq_image_url_to_base64(item.url)
                value = f"data:image/{image_format};base64,{payload}"
            except (TimeoutError, ValueError, OSError):
                value = ""
            except Exception:
                # Match the existing adapter fallback without logging signed URLs.
                value = ""
        if len(value) > remaining:
            value = ""
        remaining -= len(value)
        return item.model_copy(update={"base64": value})

    result = []
    for item in chain:
        if isinstance(item, Quote) and include_quotes:
            origin = MessageChain([await load(part) for part in item.origin])
            result.append(item.model_copy(update={"origin": origin}))
        else:
            result.append(await load(item))
    return MessageChain(result)
