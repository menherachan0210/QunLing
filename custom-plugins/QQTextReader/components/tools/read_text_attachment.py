from __future__ import annotations

import asyncio
import json

from langbot_plugin.api.definition.components.tool.tool import Tool
from langbot_plugin.api.proxies.query_based_api import QueryBasedAPIProxy

from pkg.reader import (
    TOTAL_TIMEOUT,
    TextReadError,
    decode_inline,
    download,
    result_json,
)
from pkg.text_context import select_attachment


class ReadTextAttachment(Tool):
    """Read one trusted attachment; the model cannot supply its address or scope."""

    async def call(self, params, session, query_id, query_uuid=None):
        if not isinstance(params, dict) or params:
            return self._error(
                "本工具不接受 URL、路径、文件 ID 或会话参数，请使用空对象调用。"
            )
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT):
                api = QueryBasedAPIProxy(
                    query_id=query_id,
                    query_uuid=query_uuid,
                    plugin_runtime_handler=self.plugin.plugin_runtime_handler,
                )
                variables = await api.get_query_vars()
                if not isinstance(variables, dict):
                    raise TextReadError("附件上下文不可用，请重新发送或引用 TXT。")
                attachment = select_attachment(self.plugin, variables)
                bot_uuid = await api.get_bot_uuid()
                if str(bot_uuid) != attachment.bot_uuid:
                    raise TextReadError("附件会话不匹配，请重新发送 TXT。")
                if attachment.inline:
                    raw = decode_inline(attachment.inline)
                else:
                    url = attachment.url
                    if not url:
                        if not attachment.file_id:
                            raise TextReadError(
                                "附件缺少可读取的地址，请重新上传 TXT。"
                            )
                        action = (
                            "get_group_file_url"
                            if attachment.chat_type == "group"
                            else "get_private_file_url"
                        )
                        parameters = {"file_id": attachment.file_id}
                        if attachment.chat_type == "group":
                            parameters["group_id"] = attachment.chat_id
                        response = await self.plugin.call_platform_api(
                            bot_uuid,
                            "call_platform_api",
                            {"action": action, "params": parameters},
                        )
                        url = (
                            response.get("url") if isinstance(response, dict) else None
                        )
                        if not isinstance(url, str) or not url:
                            raise TextReadError(
                                "无法获取附件下载地址，请重新上传或引用 TXT。"
                            )
                    raw = await download(url)
                return result_json(attachment.name, raw, attachment.size)
        except TextReadError as exc:
            return self._error(str(exc))
        except TimeoutError:
            return self._error("TXT 读取超过 30 秒，请稍后重新上传或引用。")
        except Exception:
            return self._error("TXT 读取暂不可用，请重新上传或引用；未读取到正文。")

    @staticmethod
    def _error(message):
        return json.dumps({"complete": False, "error": message}, ensure_ascii=False)
