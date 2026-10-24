"""Reference shared retrieved/generated pictures without accepting URLs or paths."""
from __future__ import annotations

import asyncio
import base64

from qunling.assets import AssetError, AssetStore
from qunling.integration import query_context
from qunling.scope import Scope


async def select_assets(plugin, api, refs):
    from main import GeneratedImage, MAX_IMAGE_BYTES, _base64_data_url
    if (not isinstance(refs, list) or not 1 <= len(refs) <= 4
            or any(not isinstance(ref, str) for ref in refs) or len(set(refs)) != len(refs)):
        raise AssetError("invalid_parameters", "asset_refs 请选择 1 至 4 个不同的共享图片编号。")
    shared = await query_context(plugin, api)
    scope, store = Scope(**shared["scope"]), AssetStore.from_env()
    images = []
    for ref in refs:
        store.require_visible(scope, ref, shared)
        item = store.get(scope, ref)
        if item["size"] > MAX_IMAGE_BYTES:
            raise AssetError("size_limit", "参考图超过 10 MiB，尚未提交编辑。")
        raw = await asyncio.to_thread(store.read, scope, ref)
        value = _base64_data_url(base64.b64encode(raw).decode("ascii"), reference=True)
        images.append(GeneratedImage(base64=value))
    return images
