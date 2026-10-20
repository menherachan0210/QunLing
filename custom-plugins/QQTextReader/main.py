from langbot_plugin.api.definition.plugin import BasePlugin


class QQTextReader(BasePlugin):
    """Scoped file reading, artifact creation and reliable attachment delivery."""

    async def initialize(self):
        pass

    def capture_group_upload(
        self, bot_uuid, group_id, sender_id, file, **execution_scope
    ):
        from pkg.text_context import capture_group_upload

        capture_group_upload(
            self, bot_uuid, group_id, sender_id, file, **execution_scope
        )
        from qunling.capture import capture_group_upload as capture_shared
        from qunling.assets import AssetError
        try:
            capture_shared(self, bot_uuid, group_id, sender_id, file, **execution_scope)
        except (AssetError, ValueError, TypeError):
            pass

    async def on_installation_revoked(self, binding):
        from pkg.text_context import revoke_installation

        revoke_installation(binding)
