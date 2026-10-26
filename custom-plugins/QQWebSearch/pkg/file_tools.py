"""Download public attachments, then deliver only to the trusted current query."""

from __future__ import annotations

import asyncio
import base64

from langbot_plugin.api.entities.builtin.platform.message import (
    File,
    MessageChain,
    Plain,
)
from langbot_plugin.runtime.io.connection import MAX_MESSAGE_BYTES

from pkg.errors import WebError
from pkg.files import download_file
from pkg.materials import register_material
from qunling.assets import AssetError, AssetStore
from qunling.integration import query_context
from qunling.scope import Scope
from qunling.transfers import send_attachment


async def prepare_file(plugin, context, params, *, api=None):
    if (
        not isinstance(params, dict)
        or "url" not in params
        or not set(params) <= {"url", "filename"}
        or not isinstance(params["url"], str)
        or not 1 <= len(params["url"]) <= 8192
        or (
            "filename" in params
            and (
                not isinstance(params["filename"], str)
                or not 1 <= len(params["filename"]) <= 180
            )
        )
    ):
        raise WebError("下载只接受公网文件url和可选的filename，不接受路径或收件人。")
    context.claim("file_download", 6)
    item = await download_file(params["url"], params.get("filename"))
    ref = plugin.web_queries.remember_file(context, item)
    item = context.files[ref]
    result = {
        "status": "downloaded",
        "file_ref": ref,
        "name": item["name"],
        "size": item["size"],
        "sha256": item["sha256"],
        "source_url": item["source_url"],
        "instruction": (
            "文件已实际下载到本轮临时内存，尚未发送。用户要求上传或发送时，"
            "调用send_web_file并使用此file_ref；不要只回复下载链接或声称已上传。"
            "未执行文件，也未审查压缩包内容，不能宣称文件经过安全扫描。"
        ),
    }
    # A shared asset can be parsed by other tools, but must never be registered
    # using a model-supplied identity or an inferred chat/sender.
    try:
        if api is None:
            raise WebError("未提供可信的当前问题上下文。")
        shared_context = await query_context(plugin, api)
        scope = Scope(**shared_context["scope"])
        asset = await register_material(plugin, api, item, shared=shared_context)
        shared_store = AssetStore.from_env()
        shared_store.grant(scope, shared_context["request_key"], asset["asset_ref"])
        # A prior legacy send in this query must also block the new asset entry.
        previous = context.file_delivery.get(item["sha256"])
        if previous and shared_store.reserve_delivery(scope, shared_context["request_key"], asset["asset_ref"]):
            shared_store.mark_delivery(
                scope, shared_context["request_key"], asset["asset_ref"],
                "sent" if previous == "sent" else "unconfirmed",
            )
        for candidate in context.files.values():
            if candidate["sha256"] == item["sha256"]:
                candidate["_shared_asset"] = asset["asset_ref"]
        result["asset_ref"] = asset["asset_ref"]
        result["instruction"] += "需要阅读或加工文件时可把asset_ref交给附件工具。"
    except asyncio.CancelledError:
        raise
    except Exception:
        result["shared_asset_unavailable"] = {
            "code": "shared_asset_unavailable",
            "message": "未能登记到共享文件服务；本轮file_ref仍可用于发送，不能宣称已解析文件。",
        }
    return result


async def send_file(context, api, params, *, plugin=None):
    if not isinstance(params, dict) or set(params) != {"file_ref"}:
        raise WebError("发附件只接受本轮file_ref，不接受URL、路径或收件人。")
    ref = params["file_ref"]
    if not isinstance(ref, str) or ref not in context.files:
        raise WebError("请选择本轮download_web_file返回的有效file_ref。")
    item = context.files[ref]
    digest = item["sha256"]
    if item.get("_shared_asset"):
        if plugin is None:
            raise WebError("共享文件发送缺少可信查询上下文。", code="scope_unavailable")
        claimed = False
        try:
            shared_context = await query_context(plugin, api)
            scope = Scope(**shared_context["scope"])
            async with context.lock:
                if digest in context.file_delivery:
                    raise WebError("本轮已经尝试发送该附件，不要重复发送。")
                context.claim("file_send", 2)
                context.file_delivery[digest] = "started"
                claimed = True
            result = await send_attachment(
                AssetStore.from_env(), scope, shared_context, api, item["_shared_asset"],
                caption=f"附件：{item['name']}\n来源：{item['source_url']}\n",
            )
        except AssetError as error:
            if claimed:
                context.file_delivery[digest] = "unconfirmed"
            raise WebError(str(error), code=error.code) from None
        except BaseException:
            if claimed:
                context.file_delivery[digest] = "unconfirmed"
            raise
        context.file_delivery[digest] = "sent"
        return {**result, "instruction": "附件已发送到当前聊天；不要通过其他发送工具重复发送。"}
    # Conservative room for the SDK action envelope and source text.
    encoded_bytes = ((item["size"] + 2) // 3) * 4
    if encoded_bytes + 64 * 1024 > MAX_MESSAGE_BYTES:
        raise WebError(
            "当前插件运行时的消息容量不足，请应用附件传输补丁并重启后再上传此文件。"
        )
    async with context.lock:
        if digest in context.file_delivery:
            raise WebError("本轮已经尝试发送该附件，不要重复发送。")
        context.claim("file_send", 2)
        # Mark before any await: cancellation or an ambiguous QQ result must
        # never trigger a duplicate upload.
        context.file_delivery[digest] = "started"
    try:
        chain = MessageChain(
            [
                Plain(text=f"附件：{item['name']}\n来源：{item['source_url']}\n"),
                File(
                    name=item["name"],
                    size=item["size"],
                    base64=base64.b64encode(item["data"]).decode("ascii"),
                ),
            ]
        )
        async with asyncio.timeout(45):
            await api.reply(chain, quote_origin=False)
    except asyncio.CancelledError:
        context.file_delivery[digest] = "unconfirmed"
        raise
    except Exception:
        context.file_delivery[digest] = "unconfirmed"
        raise WebError(
            "附件已下载，但QQ上传结果未确认；未自动重发，不能声称已发送。"
        ) from None
    context.file_delivery[digest] = "sent"
    return {
        "status": "sent",
        "name": item["name"],
        "size": item["size"],
        "instruction": "附件与来源已发送到当前聊天，不要再次发送；交流表达采用当前群设置。",
    }
