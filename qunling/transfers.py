"""Import QQ attachments and send registered artifacts to the captured query only."""
from __future__ import annotations

import asyncio
import base64

from qunling.assets import AssetError, AssetStore, MAX_ASSET_BYTES, image_name
from qunling.inbox import Inbox
from qunling.network import fetch_public_file
from qunling.scope import Scope


async def import_attachment(plugin, store: AssetStore, scope: Scope, incoming_ref: str,
                            *, before: float, current_refs=()) -> dict:
    inbox = Inbox(store)
    source = inbox.get(scope, incoming_ref, before=before, include_refs=current_refs)
    if source.get("asset_ref"):
        return store.get(scope, source["asset_ref"])
    url = source.get("url")
    if not url:
        kind, chat_id = scope.conversation_id.split(":", 1)
        parameters = {"file_id": source["file_id"]}
        if kind == "group":
            parameters["group_id"] = chat_id
        async with asyncio.timeout(15):
            response = await plugin.call_platform_api(
                scope.bot_id, "call_platform_api",
                {"action": "get_group_file_url" if kind == "group" else "get_private_file_url",
                 "params": parameters},
            )
        url = response.get("url") if isinstance(response, dict) else None
        if not isinstance(url, str) or not url:
            raise AssetError("source_unavailable", "QQ 未返回附件地址，可重新引用原文件后再试。")
    fetched = await fetch_public_file(url, max_bytes=MAX_ASSET_BYTES, timeout=60)
    if source.get("size") and source["size"] != len(fetched.body):
        raise AssetError("integrity_error", "附件下载长度与 QQ 提供的大小不一致。")
    name = image_name(fetched.body) if source.get("media_type") == "image" else source["name"]
    item = store.put(scope, name, fetched.body, origin="upload")
    inbox.attach(scope, incoming_ref, item["asset_ref"])
    return item


async def send_attachment(store, scope, context, api, ref, *, caption=None):
    from langbot_plugin.api.entities.builtin.platform.message import File, MessageChain, Plain
    from langbot_plugin.runtime.io.connection import MAX_MESSAGE_BYTES
    item = store.get(scope, ref)
    store.require_visible(scope, ref, context)
    if item["size"] > 20 * 1024 * 1024 or ((item["size"] + 2) // 3) * 4 + 65536 > MAX_MESSAGE_BYTES:
        raise AssetError("delivery_limit", "当前 QQ 附件发送上限为 20 MiB，请拆分文件。")
    content = store.read(scope, ref)
    request = context["request_key"]
    # Reserve before the first await. A cancelled/ambiguous upload is never retried.
    if not store.reserve_delivery(scope, request, ref):
        previous = store.delivery(scope, request, ref)
        if previous == "sent":
            return {"status": "sent", "name": item["name"], "already_sent": True}
        raise AssetError("delivery_unconfirmed", "该附件已尝试发送，但 QQ 回执未确认；请先查看聊天，避免重复发送。")
    try:
        async with asyncio.timeout(45):
            encoded = base64.b64encode(content).decode("ascii")
            image_bytes = (content.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a"))
                           or (content.startswith(b"RIFF") and content[8:12] == b"WEBP"))
            if ((store.metadata(scope, ref).get("media_type") == "image" and image_bytes)
                    or (item["name"].lower().endswith(".gif") and content[:6] in {b"GIF87a", b"GIF89a"})):
                from langbot_plugin.api.entities.builtin.platform.message import Image
                component = Image(base64=encoded)
            else:
                component = File(name=item["name"], size=item["size"], base64=encoded)
            chain = ([Plain(text=caption)] if caption else []) + [component]
            await api.reply(MessageChain(chain), quote_origin=False)
    except BaseException as error:
        store.mark_delivery(scope, request, ref, "unconfirmed")
        if isinstance(error, asyncio.CancelledError):
            raise
        raise AssetError("delivery_unconfirmed", "QQ 上传回执未确认；保留了文件，不自动重复发送。") from None
    store.mark_delivery(scope, request, ref, "sent")
    return {"status": "sent", "name": item["name"], "size": item["size"]}
