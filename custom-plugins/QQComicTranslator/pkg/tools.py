from __future__ import annotations

import asyncio
import json

from langbot_plugin.api.proxies.query_based_api import QueryBasedAPIProxy

from pkg.attachment_context import select_attachment, select_session
from pkg.download import decode_inline, download
from pkg.jobs import JobError


async def execute(tool, action, params, query_id, query_uuid):
    if not isinstance(params, dict) or params:
        return json.dumps(
            {
                "status": "error",
                "error": "本工具仅接受空对象，不接受文件地址、路径、收件人或会话参数。",
            },
            ensure_ascii=False,
        )
    try:
        api = QueryBasedAPIProxy(
            query_id=query_id,
            query_uuid=query_uuid,
            plugin_runtime_handler=tool.plugin.plugin_runtime_handler,
        )
        variables = await api.get_query_vars()
        session = select_session(tool.plugin, variables)
        if str(await api.get_bot_uuid()) != session.bot_uuid:
            raise JobError("当前文件和会话不匹配，请重新发送。")
        if action == "start":
            model = tool.plugin.get_config().get("model_uuid", "")
            if not isinstance(model, str) or not model:
                raise JobError("漫画翻译模型尚未配置。")
            if not callable(getattr(tool.plugin, "spawn_background", None)):
                raise JobError(
                    "后台任务支持尚未安装，请应用本项目的 SDK 后台任务补丁。"
                )
            attachment = select_attachment(tool.plugin, variables)
            async with asyncio.timeout(75):
                if attachment.inline:
                    raw = decode_inline(attachment.inline)
                else:
                    url = attachment.url
                    if not url:
                        action_name = (
                            "get_group_file_url"
                            if attachment.chat_type == "group"
                            else "get_private_file_url"
                        )
                        arguments = {"file_id": attachment.file_id}
                        if attachment.chat_type == "group":
                            arguments["group_id"] = attachment.chat_id
                        result = await tool.plugin.call_platform_api(
                            attachment.bot_uuid,
                            "call_platform_api",
                            {"action": action_name, "params": arguments},
                        )
                        url = result.get("url") if isinstance(result, dict) else None
                        if not url:
                            raise JobError("无法取得压缩包下载地址，请重新上传或引用。")
                    raw = await download(url)
            if attachment.size and len(raw) != attachment.size:
                raise JobError("压缩包下载大小与附件信息不一致，请重新上传。")
            result = await tool.plugin.jobs.start(session, attachment, raw, model)
        else:
            result = await tool.plugin.jobs.operate(session, action, api)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        # Known safety modules expose fixed diagnostic strings. Arbitrary Host,
        # network and provider exceptions may contain signed URLs or credentials.
        safe = isinstance(exc, JobError) or type(exc).__module__ in {
            "pkg.archive",
            "pkg.download",
            "pkg.attachment_context",
        }
        message = (
            str(exc)
            if safe
            else "漫画翻译操作暂时失败；请查询进度，未确认成功前不会声称已经翻译或发送。"
        )
        return json.dumps({"status": "error", "error": message}, ensure_ascii=False)
