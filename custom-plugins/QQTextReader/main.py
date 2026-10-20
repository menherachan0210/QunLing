from langbot_plugin.api.definition.plugin import BasePlugin


class QQTextReader(BasePlugin):
    """Read bounded, user-supplied TXT attachments without a sandbox."""

    async def initialize(self):
        pass

    def capture_group_upload(
        self, bot_uuid, group_id, sender_id, file, **execution_scope
    ):
        from pkg.text_context import capture_group_upload

        capture_group_upload(
            self, bot_uuid, group_id, sender_id, file, **execution_scope
        )

    async def on_installation_revoked(self, binding):
        from pkg.text_context import revoke_installation

        revoke_installation(binding)
