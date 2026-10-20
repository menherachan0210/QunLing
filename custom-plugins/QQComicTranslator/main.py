from langbot_plugin.api.definition.plugin import BasePlugin


class QQComicTranslator(BasePlugin):
    async def initialize(self):
        from pkg.jobs import JobManager

        self.jobs = JobManager(self)

    def capture_group_upload(self, bot_uuid, group_id, sender_id, file, **scope):
        from pkg.attachment_context import capture_group_upload

        capture_group_upload(self, bot_uuid, group_id, sender_id, file, **scope)
