from langbot_plugin.api.definition.components.tool.tool import Tool
from pkg.file_tools import call_tool


class FileTask(Tool):
    async def call(self, params, session, query_id, query_uuid=None):
        return await call_tool(self, "file_task", params, query_id, query_uuid)
