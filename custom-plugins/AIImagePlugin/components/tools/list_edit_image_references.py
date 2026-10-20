from __future__ import annotations

import json

from langbot_plugin.api.definition.components.tool.tool import Tool
from langbot_plugin.api.proxies.query_based_api import QueryBasedAPIProxy

from main import ImageGenerationError
from pkg.edit_references import list_references


class ListEditImageReferences(Tool):
    """List references inside the trusted query; never edit or send anything."""

    async def call(self, params, session, query_id, query_uuid=None):
        try:
            if not isinstance(params, dict) or params:
                raise ImageGenerationError(
                    "列出编辑参考图只接受空对象，不接受图片地址、群号或收件人。"
                )
            api = QueryBasedAPIProxy(
                query_id=query_id,
                query_uuid=query_uuid,
                plugin_runtime_handler=self.plugin.plugin_runtime_handler,
            )
            result = await list_references(
                self.plugin,
                api,
                await api.get_query_vars(),
                query_id,
                query_uuid,
            )
        except ImageGenerationError as exc:
            result = {"status": "error", "error": str(exc)}
        except Exception:
            result = {
                "status": "error",
                "error": "本轮参考图列表不可用，请引用或补发目标图片；没有提交生成请求。",
            }
        return json.dumps(result, ensure_ascii=False)
