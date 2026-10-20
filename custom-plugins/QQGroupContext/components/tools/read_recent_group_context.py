from langbot_plugin.api.definition.components.tool.tool import Tool
from pkg.tools import execute


class ReadRecentGroupContext(Tool):
    async def call(self, params, session, query_id, query_uuid=None):
        return await execute(self, "context", params, query_id, query_uuid)
