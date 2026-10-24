from langbot_plugin.api.definition.components.tool.tool import Tool
from pkg.file_tools import call_tool


class GifAnimation(Tool):
    async def call(self, params, session, query_id, query_uuid=None):
        return await call_tool(self, "gif_animation", params, query_id, query_uuid)
