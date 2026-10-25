"""Optional animation output for the existing single billed image request."""
from __future__ import annotations

import base64
import json

from qunling.ai_animation import assemble, frame_plan, storyboard_prompt
from qunling.assets import AssetError
from qunling.integration import query_context


def prepare_prompt(params):
    if type(params.get("deliver", True)) is not bool:
        raise AssetError("invalid_parameters", "deliver 必须为 true 或 false。")
    plan = frame_plan(params)
    if plan is not None and params.get("deliver", True) is False:
        plan["deliver"] = False
    return plan, storyboard_prompt(params["prompt"], plan)


async def prepare_context(plugin, api, plan, *, deliver=True):
    context = await query_context(plugin, api)
    from qunling.assets import AssetStore
    AssetStore.from_env()
    route = context.get("host_route")
    if plan is not None and deliver and (not isinstance(route, dict) or not all(route.get(key) for key in ("instance_uuid", "workspace_uuid", "placement_generation"))):
        raise AssetError("scope_unavailable", "动画交付服务暂不可用，尚未发起生图请求。")
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
    import asyncio
    from langbot_plugin.api.entities.builtin.platform.message import Image, MessageChain
    from pkg.image_context import remember_output
    try:
        scope, store, asset, raw = await persist_image(context, image)
    except Exception:
        return "图片生成接口已返回，但共享素材保存失败；未发送图片。请勿重新生图，以免重复计费。"
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


async def finish(context, plan, image):
    from main import image_data_url

    try:
        data = await image_data_url(image)
        raw = base64.b64decode(data.split(",", 1)[1], validate=True)
        result = await assemble(context, raw, plan)
    except Exception:
        result = {"status": "partial", "error_code": "animation_output_failed",
                  "error": "生图接口已返回，但分镜读取或合成失败；不代表生成未发生，不要重复生图。"}
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
