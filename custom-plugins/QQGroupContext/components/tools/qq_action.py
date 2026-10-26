from langbot_plugin.api.definition.components.tool.tool import Tool
from pkg.qq_actions import call


class QQAction(Tool):
    async def call(self, params, session, query_id, query_uuid=None):
        return await call(self.plugin, params, query_id, query_uuid, catalog=False)
