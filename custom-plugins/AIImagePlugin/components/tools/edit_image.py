from __future__ import annotations

import asyncio
from typing import Any

from langbot_plugin.api.definition.components.tool.tool import Tool
from langbot_plugin.api.entities.builtin.platform.message import Image, MessageChain
from langbot_plugin.api.proxies.query_based_api import QueryBasedAPIProxy

from components.tools.generate_image import (
    GENERATING,
    GENERATION_FAILED,
    GUARD_UNAVAILABLE,
    INVALID_PROMPT,
    MAX_PROMPT_CHARS,
    QUERY_STATE_KEY,
    REPLY_TIMEOUT_SECONDS,
    SEND_FAILED,
    SENT,
    GenerateImage,
    _STATE_MESSAGES,
)
from main import ImageGenerationError
from pkg.image_context import query_claim_lock, remember_output, select_image
from pkg.gif_output import prepare_prompt, prepare_context, finish, finish_asset
from qunling.assets import AssetError


class EditImage(Tool):
    """Edit scoped references without trusting model-provided image URLs."""

    async def call(
        self,
        params: dict[str, Any],
        session: Any,
        query_id: int,
        query_uuid: str | None = None,
    ) -> str:
        if (
            not isinstance(params, dict)
            or "prompt" not in params
            or set(params) - {"prompt", "image_refs", "asset_refs", "gif_frames", "frame_duration_ms", "deliver"}
            or ("image_refs" in params and "asset_refs" in params)
        ):
            return INVALID_PROMPT
        prompt = params["prompt"]
        if (
            not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt) > MAX_PROMPT_CHARS
        ):
            return INVALID_PROMPT
        try:
            plan, prompt = prepare_prompt(params)
        except AssetError as exc:
            return str(exc)
        try:
            reply_api = QueryBasedAPIProxy(
                query_id=query_id,
                query_uuid=query_uuid,
                plugin_runtime_handler=self.plugin.plugin_runtime_handler,
            )
            async with query_claim_lock(self.plugin):
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
                if "asset_refs" in params:
                    from pkg.asset_input import select_assets
                    image = await select_assets(self.plugin, reply_api, params["asset_refs"])
                elif "image_refs" in params:
                    from pkg.edit_references import select_references

                    image = select_references(
                        self.plugin,
                        variables,
                        query_id,
                        query_uuid,
                        params["image_refs"],
                        bot=await reply_api.get_bot_uuid(),
                    )
                else:
                    image = select_image(self.plugin, variables)
                context = await prepare_context(self.plugin, reply_api, plan, deliver=params.get("deliver", True))
                await reply_api.set_query_var(QUERY_STATE_KEY, "started")
        except ImageGenerationError as exc:
            return str(exc)
        except AssetError as exc:
            return str(exc)
        except Exception:
            return GUARD_UNAVAILABLE

        try:
            result = await self.plugin.edit_image(prompt.strip(), image)
            if not result.base64 and not result.url:
                raise ImageGenerationError("绘图服务未返回可发送的图片。")
        except ImageGenerationError as exc:
            await GenerateImage._record_state(reply_api, "generation_failed")
            return f"{exc} 请勿重复调用本工具。"
        except Exception:
            await GenerateImage._record_state(reply_api, "generation_failed")
            return GENERATION_FAILED
        if plan is not None:
            await GenerateImage._record_state(reply_api, "gif_saved")
            return await finish(context, plan, result)
        if params.get("deliver", True) is False:
            await GenerateImage._record_state(reply_api, "asset_saved")
            return await finish_asset(context, result)
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
            await GenerateImage._record_state(reply_api, "send_failed")
            return SEND_FAILED
        await GenerateImage._record_state(reply_api, "sent")
        await remember_output(self.plugin, reply_api, result)
        return SENT
