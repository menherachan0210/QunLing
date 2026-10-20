from langbot_plugin.api.definition.plugin import BasePlugin

from pkg.context import ContextStore


class QQGroupContext(BasePlugin):
    async def initialize(self):
        self.contexts = ContextStore()

    async def on_installation_revoked(self, binding):
        self.contexts.clear()
