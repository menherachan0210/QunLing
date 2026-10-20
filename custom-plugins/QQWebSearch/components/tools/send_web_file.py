from langbot_plugin.api.definition.components.tool.tool import Tool
from pkg.tools import execute


class SendWebFile(Tool):
    async def call(self, params, session, query_id, query_uuid=None):
        return await execute(self, "file_send", params, query_id, query_uuid)
