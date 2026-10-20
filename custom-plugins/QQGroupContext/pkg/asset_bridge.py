"""Grant exact, already-shown history images to the current file workspace."""
from __future__ import annotations

import asyncio

from pkg.errors import VisionError
from pkg.media import prepare_image
from qunling.assets import AssetError, AssetStore, image_name
from qunling.integration import query_context, task_hint
from qunling.net_errors import NetworkError
from qunling.network import fetch_public
from qunling.scope import Scope


def _filename(raw):
    # Reuse the existing pixel/size/format validation, but retain original bytes.
    # The preview normalizer is not used as the exported image.
    prepare_image(raw)
    return image_name(raw)


async def import_images(plugin, context, params, api):
    if not isinstance(params, dict) or set(params) != {"image_refs"}:
        raise VisionError("只接受当前问题已展示的 image_refs，不接受网址或群号。")
    refs = params["image_refs"]
    if (not isinstance(refs, list) or not 1 <= len(refs) <= 4
            or any(not isinstance(ref, str) for ref in refs) or len(set(refs)) != len(refs)):
        raise VisionError("一次请选择 1 至 4 个不同的历史图片编号。")
    if not context.history or any(ref not in context.shown_images for ref in refs):
        raise VisionError("先用 read_recent_group_context 定位图片，再选择实际返回的 image_refs。")
    shared = await query_context(plugin, api)
    scope = Scope(**shared["scope"])
    if (scope.bot_id != context.bot or scope.conversation_id != f"group:{context.group}"
            or scope.actor_id != context.sender):
        raise AssetError("scope_mismatch", "图片与文件请求的会话不匹配。")
    store = AssetStore.from_env()
    pending = [ref for ref in refs if ref not in context.imported_assets]
    if pending:
        if context.import_inflight:
            raise VisionError("当前历史图片正在导入，请等待已有操作。")
        if context.import_attempts >= 3 or len(set(context.imported_assets) | set(refs)) > 8:
            raise VisionError("本轮历史图片导入已达到预算，请先使用已取得的图片。")
        context.import_inflight = True
        context.import_attempts += 1
    failures = []
    try:
        async def fetch(ref):
            fetched = await fetch_public(context.image_urls[ref], max_bytes=10 * 1024 * 1024, timeout=20)
            name = await asyncio.to_thread(_filename, fetched.body)
            item = await asyncio.to_thread(store.put, scope, name, fetched.body, origin="upload")
            store.grant(scope, shared["request_key"], item["asset_ref"])
            return {"image_ref": ref, **item, "filename_generated": True}

        results = await asyncio.gather(*(fetch(ref) for ref in pending), return_exceptions=True)
        for ref, result in zip(pending, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                failures.append({
                    "image_ref": ref,
                    "error_code": result.code if isinstance(result, (NetworkError, AssetError)) else "image_read_failed",
                })
            else:
                context.imported_assets[ref] = result
    finally:
        if pending:
            context.import_inflight = False
    value = {
        "status": "partial" if failures else "ok",
        "images": [context.imported_assets[ref] for ref in refs if ref in context.imported_assets],
        "failed_images": failures,
        "instruction": "这些 asset_ref 指向本群本轮选定图片的实际原始字节，可交给文件或漫画工具。"
                       "QQ 历史未提供原始文件名，已分配稳定图片名。尚未翻译，也未发送。",
    }
    if failures and context.import_attempts < 3:
        value["_qunling_task"] = task_hint(status="partial", next_action="continue",
                                         can_continue=True, error_code="network")
    return value
