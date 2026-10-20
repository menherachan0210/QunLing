from langbot_plugin.api.definition.plugin import BasePlugin

from pkg.context import QueryStore


class QQWebSearch(BasePlugin):
    async def initialize(self):
        self.web_queries = QueryStore()

    async def on_installation_revoked(self, binding):
        self.web_queries.clear()
