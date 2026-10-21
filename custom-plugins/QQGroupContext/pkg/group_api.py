"""Bind read-only group operations to the captured question, never model IDs."""
from __future__ import annotations

import asyncio
import time

from pkg.context import identity
from pkg.errors import VisionError

READ_ACTIONS = frozenset({"get_group_member_info", "get_group_member_list",
                          "get_group_root_files", "get_group_files_by_folder", "get_group_file_url"})


async def call_group_api(plugin, context, action, params):
    if action not in READ_ACTIONS or not isinstance(params, dict) or "group_id" in params:
        raise VisionError("群资料查询只接受当前群的只读操作。")
    try:
        async with asyncio.timeout(14):
            result = await plugin.call_platform_api(context.bot, "call_platform_api", {
                "action": action, "params": {**params, "group_id": identity(context.group)},
            })
        # Some OneBot bridges retain the standard action envelope.
        if isinstance(result, dict) and "status" in result and "data" in result:
            if result["status"] != "ok" or result.get("retcode", 0) != 0:
                raise ValueError("Unsuccessful action")
            result = result["data"]
        return result
    except Exception:
        raise VisionError("暂时无法读取本群资料，可能是 QQ 未连接、权限变化或接口不可用。") from None


async def validate_group_access(plugin, context):
    async with context.group_data_lock:
        checked = context.group_data.get("access_checked_at")
        if checked is not None and 0 <= time.monotonic() - checked < 30:
            return
        result = await call_group_api(plugin, context, "get_group_member_info", {
            "user_id": context.sender, "no_cache": True,
        })
        try:
            if (not isinstance(result, dict) or identity(result.get("user_id")) != context.sender
                    or identity(result.get("group_id")) != context.group
                    or result.get("role") not in {"owner", "admin", "member"}):
                raise ValueError
        except (ValueError, TypeError):
            raise VisionError("无法确认提问者仍在当前群，请在原群重新提问。") from None
        context.group_data["access_checked_at"] = time.monotonic()
