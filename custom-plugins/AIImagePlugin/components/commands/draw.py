# Modified by qq-ai-bot contributors on 2026-10-20; local changes, see NOTICE.
from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from langbot_plugin.api.definition.components.command.command import Command
from langbot_plugin.api.entities.builtin.command.context import (
    CommandReturn,
    ExecuteContext,
)
from langbot_plugin.api.entities.builtin.platform.message import Image, MessageChain

from main import ImageGenerationError
from pkg.image_context import remember_output
from qunling.assets import AssetError
from qunling.integration import query_context
from qunling.image_access import ImageAccessDenied, ImageAccessError, require_allowed, request_scope
from qunling.scope import Scope


class Draw(Command):
    async def _execute(
        self, context: ExecuteContext
    ) -> AsyncGenerator[CommandReturn, None]:
        if not context.params:
            yield CommandReturn(
                text="用法: !draw <描述>\n示例: !draw 一只坐在彩虹上的猫"
            )
            return
        try:
            trusted = await query_context(self.plugin, context)
            require_allowed(Scope(**trusted["scope"]))
            yield CommandReturn(text="正在生成图片，请稍候。")
            # Commands bypass the chat model, but must share the same live ban
            # and recheck it after any upstream resource queue wait.
            with request_scope(trusted):
                result = await self.plugin.generate_image(" ".join(context.params))
        except ImageAccessDenied as exc:
            reply = exc.result["reply"]
            yield CommandReturn(text=reply.get("text", "生图资格都没了，还搁这儿点菜呢。"))
            return
        except (ImageGenerationError, ImageAccessError, AssetError) as exc:
            yield CommandReturn(text=str(exc))
            return
        try:
            await asyncio.wait_for(
                context.reply(
                    MessageChain(
                        [Image(base64=result.base64 or "", url=result.url or "")]
                    )
                ),
                timeout=15,
            )
        except Exception:
            yield CommandReturn(
                text="图片已生成，但发送失败；请勿重新生成，以免重复计费。"
            )
            return
        await remember_output(self.plugin, context, result)
        yield CommandReturn(text="图片已生成并发送。")
