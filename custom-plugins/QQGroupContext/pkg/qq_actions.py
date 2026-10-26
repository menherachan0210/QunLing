"""Model parameters never supply the host query, actor, bot or destination."""
import json

from langbot_plugin.api.proxies.query_based_api import QueryBasedAPIProxy


async def call(plugin, params, query_id, query_uuid, *, catalog):
    allowed = {"category"} if catalog else {"operation", "arguments"}
    if not isinstance(params, dict) or set(params) - allowed:
        return json.dumps({"status": "error", "error": "不支持的 QQ 工具参数。"}, ensure_ascii=False)
    api = QueryBasedAPIProxy(query_id=query_id, query_uuid=query_uuid, plugin_runtime_handler=plugin.plugin_runtime_handler)
    result = await plugin.call_platform_api(await api.get_bot_uuid(), "qunling_qq", {
        **params, "query_id": query_id, "query_uuid": query_uuid, "catalog": catalog,
    })
    return json.dumps(result, ensure_ascii=False)
