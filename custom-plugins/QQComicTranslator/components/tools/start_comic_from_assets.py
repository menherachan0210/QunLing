from langbot_plugin.api.definition.components.tool.tool import Tool
from pkg.tools import execute_assets


class StartComicFromAssets(Tool):
    async def call(self, params, session, query_id, query_uuid=None):
        return await execute_assets(self, params, query_id, query_uuid)
