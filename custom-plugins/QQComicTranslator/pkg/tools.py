from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from langbot_plugin.api.proxies.query_based_api import QueryBasedAPIProxy

from pkg.attachment_context import select_attachment, select_session
from pkg.download import decode_inline, download
from pkg.jobs import JobError
from pkg.archive import validate_password


async def execute_assets(tool, params, query_id, query_uuid):
    """Only opaque asset references may come from the model."""
    from pkg.archive import ArchiveError
    from pkg.asset_input import asset_parameters, prepare_assets
    from pkg.attachment_context import _session
    from pkg.labelplus import TranslationError
    from qunling.assets import AssetError, AssetStore
    from qunling.integration import query_context
    from qunling.scope import Scope

    try:
        refs = asset_parameters(params)
        password = validate_password(params.get("password"))
        api = QueryBasedAPIProxy(
            query_id=query_id, query_uuid=query_uuid,
            plugin_runtime_handler=tool.plugin.plugin_runtime_handler,
        )
        context = await query_context(tool.plugin, api)
        scope = Scope(**context["scope"])
        kind, chat_id = scope.conversation_id.split(":", 1)
        session = _session(SimpleNamespace(
            bot_uuid=scope.bot_id, launcher_type=kind,
            launcher_id=chat_id, sender_id=scope.actor_id,
        ))
        model = tool.plugin.get_config().get("model_uuid", "")
        if not isinstance(model, str) or not model:
            raise JobError("漫画翻译模型尚未配置。")
        if not callable(getattr(tool.plugin, "spawn_background", None)):
            raise JobError("后台任务支持尚未安装，请应用本项目的 SDK 后台任务补丁。")
        prepared = await asyncio.to_thread(
            prepare_assets, AssetStore.from_env(), scope, refs, context
        )
        result = await tool.plugin.jobs.start(
            session, SimpleNamespace(name=prepared.name), prepared.raw, model,
            page_names=prepared.page_names, password=password,
        )
        return json.dumps(result, ensure_ascii=False)
    except (AssetError, ArchiveError, JobError, TranslationError) as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
    except Exception:
        return json.dumps({
            "status": "error",
            "error": "漫画资产处理暂不可用；未确认成功前不会声称已翻译或发送，请查询已有任务进度。",
        }, ensure_ascii=False)


async def execute(tool, action, params, query_id, query_uuid):
    allowed = {"password"} if action in {"start", "resume"} else set()
    if not isinstance(params, dict) or set(params) - allowed:
        return json.dumps(
            {
                "status": "error",
                "error": "开始或续做仅可提供 password；其他操作使用空对象，不接受地址、路径、收件人或会话参数。",
            },
            ensure_ascii=False,
        )
    try:
        password = validate_password(params.get("password"))
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
            options = {"password": password} if password is not None else {}
            result = await tool.plugin.jobs.start(session, attachment, raw, model, **options)
        else:
            options = {"password": password} if password is not None else {}
            result = await tool.plugin.jobs.operate(session, action, api, **options)
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
