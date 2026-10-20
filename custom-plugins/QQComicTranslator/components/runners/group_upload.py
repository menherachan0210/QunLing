"""Capture bounded QQ upload metadata without reading or replying to the file."""

from __future__ import annotations

from langbot_plugin.api.definition.components.runner.runner import Runner
from langbot_plugin.api.entities.builtin.platform.events import PlatformSpecificEvent
from langbot_plugin.api.entities.builtin.runner.context import RunnerContext


def _platform_id(value) -> str | None:
    if type(value) not in {str, int}:
        return None
    text = str(value)
    if not (1 <= len(text) <= 20 and text.isascii() and text.isdecimal()):
        return None
    return text if int(text) > 0 else None


def _metadata(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = {}
    for field in ("id", "name"):
        text = value.get(field)
        limit = 4096 if field == "id" else 255
        if not isinstance(text, str) or not text.strip() or len(text) > limit:
            return None
        if any(character in text for character in "\x00\r\n"):
            return None
        result[field] = text
    for field in ("size", "busid"):
        number = value.get(field)
        if number is not None:
            if type(number) is not int or not 0 <= number <= 2**63 - 1:
                return None
            result[field] = number
    return result


class GroupUpload(Runner):
    """An event-only Runner; the plugin core owns scoped retention and deduplication."""

    async def run(self, ctx: RunnerContext) -> None:
        if (
            ctx.event.source != "platform"
            or ctx.event.event_type != "platform.specific"
        ):
            return
        try:
            event = ctx.platform_event
        except (TypeError, ValueError):
            return
        if not isinstance(event, PlatformSpecificEvent):
            return
        if event.adapter_name != "onebot-v11" or event.action != "notice.group_upload":
            return
        bot_uuid = event.bot_uuid
        if not isinstance(bot_uuid, str) or not bot_uuid.strip() or len(bot_uuid) > 128:
            return
        data = event.data
        group_id = _platform_id(data.get("group_id"))
        sender_id = _platform_id(data.get("user_id"))
        file = _metadata(data.get("file"))
        if group_id is None or sender_id is None or file is None:
            return

        # These values are Host metadata, never fields supplied inside the notice.
        metadata = ctx.runtime.metadata
        if metadata.get("bot_id") not in (None, bot_uuid):
            return
        getter = getattr(self.plugin, "get_installation_binding", None)
        binding = getter() if callable(getter) else None
        workspace = metadata.get("workspace_id")
        if binding is not None:
            if workspace not in (None, binding.workspace_uuid):
                return
            instance = binding.instance_uuid
            workspace = binding.workspace_uuid
            generation = binding.placement_generation
        else:
            instance = generation = None
        self.plugin.capture_group_upload(
            bot_uuid,
            group_id,
            sender_id,
            file,
            instance_uuid=instance,
            workspace_uuid=workspace,
            placement_generation=generation,
        )
