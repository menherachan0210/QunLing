from __future__ import annotations

import asyncio
from typing import Any

from langbot_plugin.api.definition.components.tool.tool import Tool
from langbot_plugin.api.entities.builtin.platform.message import Image, MessageChain
from langbot_plugin.api.proxies.query_based_api import QueryBasedAPIProxy

from main import ImageGenerationError
from pkg.image_context import query_claim_lock, remember_output


QUERY_STATE_KEY = "ai_image_plugin.generate_image.v1"
MAX_PROMPT_CHARS = 8000
REPLY_TIMEOUT_SECONDS = 15
INVALID_PROMPT = "请仅提供非空的图片描述 prompt，长度最多 8000 字符。"
GUARD_UNAVAILABLE = "暂时无法确认本条消息的绘图状态，未发起生成请求；请勿重复调用。"
GENERATING = "本条消息已提交绘图请求，结果尚未确认；请勿重复调用或重新生成。"
GENERATION_FAILED = "本条消息的图片生成未成功；请勿重复调用，以免重复计费。"
SEND_FAILED = "图片已生成，但发送失败；请勿重新生成或重复调用本工具。"
SENT = "图片已生成并发送。不要重复调用，也不要输出 Markdown 图片链接。"

_STATE_MESSAGES = {
    "started": GENERATING,
    "generation_failed": GENERATION_FAILED,
    "send_failed": SEND_FAILED,
    "sent": SENT,
}


class GenerateImage(Tool):
    """Generate one image and reply to the current query at most once."""

    async def call(
        self,
        params: dict[str, Any],
        session: Any,
        query_id: int,
        query_uuid: str | None = None,
    ) -> str:
        if not isinstance(params, dict) or set(params) != {"prompt"}:
            return INVALID_PROMPT
        prompt = params["prompt"]
        if (
            not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt) > MAX_PROMPT_CHARS
        ):
            return INVALID_PROMPT

        try:
            reply_api = QueryBasedAPIProxy(
                query_id=query_id,
                query_uuid=query_uuid,
                plugin_runtime_handler=self.plugin.plugin_runtime_handler,
            )
            async with query_claim_lock(self.plugin):
                # get_query_var raises for a missing key; the mapping API does not.
                variables = await reply_api.get_query_vars()
                if not isinstance(variables, dict):
                    return GUARD_UNAVAILABLE
                if QUERY_STATE_KEY in variables:
                    state = variables[QUERY_STATE_KEY]
                    return (
                        _STATE_MESSAGES.get(state, GENERATING)
                        if isinstance(state, str)
                        else GENERATING
                    )
                # Persist before any billable request, including possible timeouts.
                await reply_api.set_query_var(QUERY_STATE_KEY, "started")
        except Exception:
            return GUARD_UNAVAILABLE

        try:
            result = await self.plugin.generate_image(prompt.strip())
            if not result.base64 and not result.url:
                raise ImageGenerationError("绘图服务未返回可发送的图片。")
        except ImageGenerationError as exc:
            await self._record_state(reply_api, "generation_failed")
            return f"{exc} 请勿重复调用本工具。"
        except Exception:
            await self._record_state(reply_api, "generation_failed")
            return GENERATION_FAILED

        try:
            await asyncio.wait_for(
                reply_api.reply(
                    MessageChain(
                        [Image(base64=result.base64 or "", url=result.url or "")]
                    )
                ),
                timeout=REPLY_TIMEOUT_SECONDS,
            )
        except Exception:
            await self._record_state(reply_api, "send_failed")
            return SEND_FAILED

        await self._record_state(reply_api, "sent")
        await remember_output(self.plugin, reply_api, result)
        return SENT

    @staticmethod
    async def _record_state(reply_api: QueryBasedAPIProxy, state: str) -> None:
        try:
            await reply_api.set_query_var(QUERY_STATE_KEY, state)
        except Exception:
            # The pre-request claim remains, so a later call cannot regenerate.
            pass
