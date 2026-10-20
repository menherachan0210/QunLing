from langbot_plugin.api.definition.components.tool.tool import Tool
from pkg.tools import execute


class CancelComicTranslation(Tool):
    async def call(self, params, session, query_id, query_uuid=None):
        return await execute(self, "cancel", params, query_id, query_uuid)
