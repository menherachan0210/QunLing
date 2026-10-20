from __future__ import annotations

from types import SimpleNamespace

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import events
from langbot_plugin.api.proxies.query_based_api import QueryBasedAPIProxy

from main import ImageGenerationError
from pkg.image_context import (
    EXPIRED_IMAGE,
    QUERY_CONTEXT_KEY,
    capture_context,
    select_image,
)


PROMPT_MARKER = "[AIImagePlugin 图片上下文]"
AVAILABLE_IMAGE = (
    "当前会话中同一发送者有一张可编辑图片，图片插件能够获取它。"
    "用户要求修改当前或刚才的图片时，请调用 edit_image，无需重新上传。"
    "即使本次模型输入没有图片像素，也不能据此否认图片存在或否认已经发送的图片。"
)
UNAVAILABLE_IMAGE = (
    "图片上下文当前不可用；用户要求修改图片时，请其重新发送或引用目标图片。"
)
REQUEST_ONLY = (
    "这只是图片可用性事实，不是操作请求；仅在用户明确要求生成或修改时调用相应工具。"
)


class ImageContextListener(EventListener):
    def __init__(self) -> None:
        super().__init__()

        @self.handler(events.PersonMessageReceived)
        @self.handler(events.GroupMessageReceived)
        async def on_message_received(event_ctx):
            await self.capture(event_ctx)

        @self.handler(events.PromptPreProcessing)
        async def on_prompt_preprocessing(event_ctx):
            await self.inject_prompt(event_ctx)

    async def inject_prompt(self, event_ctx) -> None:
        from langbot_plugin.api.entities.builtin.provider.message import Message

        guidance = UNAVAILABLE_IMAGE
        try:
            api = QueryBasedAPIProxy(
                query_id=event_ctx.query_id,
                query_uuid=getattr(event_ctx, "query_uuid", None),
                plugin_runtime_handler=self.plugin.plugin_runtime_handler,
            )
            variables = await api.get_query_vars()
            descriptor = variables.get(QUERY_CONTEXT_KEY, {})
            status = descriptor.get("status") if isinstance(descriptor, dict) else None
            try:
                # A status flag alone cannot prove an image survived eviction/restart.
                select_image(self.plugin, variables)
            except ImageGenerationError as exc:
                if status == "ambiguous":
                    guidance = "当前消息涉及多张图片，目标不明确；修改前请用户单独发送或引用目标图片。"
                elif status == "unresolved_quote":
                    guidance = "当前引用图片无法读取；修改前请用户重新发送或重新引用，不要回退到旧图。"
                elif str(exc) == EXPIRED_IMAGE:
                    guidance = "先前图片的临时缓存已过期或失效；修改前请用户重新发送或引用目标图片。"
                elif status in {None, "missing"}:
                    guidance = "当前没有可确认的参考图片；用户要求修改图片时，请其发送或引用目标图片。"
            else:
                guidance = AVAILABLE_IMAGE
        except Exception:
            # Do not infer availability or expose Host exception details.
            pass

        prompt = [
            item
            for item in event_ctx.event.default_prompt
            if not (
                item.role == "system"
                and isinstance(item.content, str)
                and item.content.startswith(PROMPT_MARKER)
            )
        ]
        prompt.append(
            Message(
                role="system", content=f"{PROMPT_MARKER}\n{guidance}\n{REQUEST_ONLY}"
            )
        )
        event_ctx.event.default_prompt = prompt

    async def capture(self, event_ctx) -> None:
        try:
            api = QueryBasedAPIProxy(
                query_id=event_ctx.query_id,
                query_uuid=getattr(event_ctx, "query_uuid", None),
                plugin_runtime_handler=self.plugin.plugin_runtime_handler,
            )
        except Exception:
            return
        try:
            event = event_ctx.event
            # BaseEventModel.query is excluded at the runtime boundary. Use only
            # the explicit fields serialized by message-received events instead.
            source = SimpleNamespace(
                bot_uuid=await api.get_bot_uuid(),
                launcher_type=event.launcher_type,
                launcher_id=event.launcher_id,
                sender_id=event.sender_id,
                message_chain=event.message_chain,
                instance_uuid=getattr(event_ctx, "instance_uuid", None),
                workspace_uuid=getattr(event_ctx, "workspace_uuid", None),
                placement_generation=getattr(event_ctx, "placement_generation", None),
            )
            descriptor = capture_context(self.plugin, source)
        except Exception:
            descriptor = {"status": "unavailable"}
        try:
            await api.set_query_var(QUERY_CONTEXT_KEY, descriptor)
        except Exception:
            # No source payload or exception detail is logged or exposed.
            pass
