"""Persist and deliver provider output unchanged, including native GIFs."""
from __future__ import annotations

import base64
import json

from qunling.integration import query_context


async def prepare_context(plugin, api):
    context = await query_context(plugin, api)
    from qunling.assets import AssetStore
    AssetStore.from_env()
    return context


async def persist_image(context, image):
    import asyncio
    from main import image_data_url
    from qunling.assets import AssetStore, image_name
    from qunling.scope import Scope
    data = await image_data_url(image)
    raw = base64.b64decode(data.split(",", 1)[1], validate=True)
    scope, store = Scope(**context["scope"]), AssetStore.from_env()
    asset = await asyncio.to_thread(store.put, scope, image_name(raw), raw,
                                   origin="generated", metadata={"media_type": "image",
                                                                "request_key": context["request_key"]})
    store.grant(scope, context["request_key"], asset["asset_ref"])
    return scope, store, asset, raw


async def finish_static(plugin, api, context, image, state_key, *, sent_message, failed_message, timeout):
    """Durably save billed output before any QQ send; ambiguous delivery is not retried."""
    try:
        scope, store, asset, raw = await persist_image(context, image)
    except Exception:
        return "图片生成接口已返回，但共享素材保存失败；未发送图片。请勿重新生图，以免重复计费。"
    return await _deliver_saved(plugin, api, context, image, state_key, scope, store, asset, raw,
                                sent_message=sent_message, failed_message=failed_message, timeout=timeout)


async def _deliver_saved(plugin, api, context, image, state_key, scope, store, asset, raw,
                         *, sent_message, failed_message, timeout):
    import asyncio
    from langbot_plugin.api.entities.builtin.platform.message import Image, MessageChain
    from pkg.image_context import remember_output
    ref, request = asset["asset_ref"], context["request_key"]
    try:
        await api.set_query_var(state_key + ".asset", ref)
        await api.set_query_var(state_key, "asset_saved")
        await remember_output(plugin, api, image)
    except Exception:
        pass  # Shared asset remains discoverable after the query disappears.
    if not store.reserve_delivery(scope, request, ref):
        return f"图片已保存：{ref}。本轮交付已有记录，不重复发送；请用文件工作区查看。"
    try:
        await asyncio.wait_for(api.reply(MessageChain([
            Image(base64=base64.b64encode(raw).decode(), url="")
        ])), timeout=timeout)
    except BaseException as exc:
        store.mark_delivery(scope, request, ref, "unconfirmed")
        try:
            await api.set_query_var(state_key, "send_failed")
        except Exception:
            pass
        if isinstance(exc, asyncio.CancelledError):
            raise
        return failed_message + f" 已保存 asset_ref={ref}；交付未确认，可在用户明确要求后发送已有文件。"
    store.mark_delivery(scope, request, ref, "sent")
    try:
        await api.set_query_var(state_key, "sent")
    except Exception:
        pass
    return sent_message


async def finish_native(plugin, api, context, image, state_key, *, deliver, timeout):
    """Keep paid bytes even when validation or delivery fails; never render frames."""
    from qunling.vision_input import inspect_generated_gif
    from qunling.integration import task_hint
    asset = {}
    try:
        scope, store, asset, raw = await persist_image(context, image)
        try:
            await api.set_query_var(state_key + ".asset", asset["asset_ref"])
            await api.set_query_var(state_key, "asset_saved")
        except Exception:
            pass  # The pre-request claim and durable asset grant still prevent regeneration.
        metadata = await inspect_generated_gif("data:image/gif;base64," + base64.b64encode(raw).decode("ascii"))
    except Exception:
        return json.dumps({"status": "partial", **asset, "delivery_status": "not_sent",
                           "error_code": "native_gif_validation_failed",
                           "error": "生成接口已返回，但原生 GIF 保存或校验未完成，尚未发送。"
                                    "如有 asset_ref 可查看已保存产物；不要重新生图或改用其他生成方式。"}, ensure_ascii=False)
    result = {"status": "created", **asset, **metadata, "delivery_status": "not_requested",
              "instruction": "原生 GIF 已保存，可复用 asset_ref 加字、调速或打包，无需重新生图。",
              "_qunling_task": task_hint()}
    if deliver:
        result.pop("_qunling_task", None)
        sent = "原生 GIF 已发送。"
        message = await _deliver_saved(plugin, api, context, image, state_key, scope, store, asset, raw,
            sent_message=sent, failed_message="原生 GIF 已生成，但发送结果未确认。", timeout=timeout)
        result.update(status="sent" if message == sent else "partial",
                      delivery_status="sent" if message == sent else "unconfirmed", instruction=message)
    return json.dumps(result, ensure_ascii=False)


async def finish_asset(context, image):
    """Keep generated intermediates available to every shared-asset consumer."""
    import asyncio
    from main import image_data_url
    from qunling.assets import AssetStore, image_name
    from qunling.integration import task_hint
    from qunling.scope import Scope
    try:
        scope, store, asset, raw = await persist_image(context, image)
        result = {"status": "created", **asset, "delivery_status": "not_requested",
                  "instruction": "图片已保存为中间素材，尚未发到QQ。使用asset_ref继续改图、合成GIF、整理或打包；"
                                 "只交付用户需要的最终成品。生成要求不是成图已核验的证据，不要重复调用生图。",
                  "_qunling_task": task_hint()}
    except Exception:
        result = {"status": "partial", "error_code": "asset_output_failed",
                  "error": "生成接口已返回，但未能保存共享素材；不能宣称已发送，也不要重复生图。"}
    return json.dumps(result, ensure_ascii=False)
