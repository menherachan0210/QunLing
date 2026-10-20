"""Host-owned file outbox transport. No model calls or direct platform sockets."""
from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

from qunling.assets import AssetError, AssetStore, data_root
from qunling.outbox import Outbox, validate_route
from qunling.tasks import TaskStore


class DeliveryUnavailable(Exception):
    """No request has been sent; safely wait for the existing Host connection."""


class DeliveryStopped(Exception):
    """The saved route is no longer authorized."""


async def resolve_bot(ap, scope, route):
    """Resolve both persisted enable state and runtime identity under tenant UoW."""
    from langbot.pkg.api.http.context import ExecutionContext
    from langbot.pkg.core.task_boundary import run_in_workspace_uow

    route = validate_route(scope, route)
    if ap.workspace_service.instance_uuid != route["instance_uuid"]:
        raise DeliveryStopped
    context = ExecutionContext(**route, bot_uuid=scope.bot_id)

    async def lookup():
        info = await ap.bot_service.get_bot(context, scope.bot_id, include_secret=False)
        if not info or info.get("enable") is not True:
            raise DeliveryStopped
        if info.get("adapter") != "onebot-v11":
            raise DeliveryStopped
        return await ap.platform_mgr.get_bot_by_uuid(context, scope.bot_id)
    bot = await run_in_workspace_uow(ap, route["workspace_uuid"], lookup)
    if bot is None:
        raise DeliveryUnavailable
    runtime_context = getattr(bot, "execution_context", None)
    if (bot.enable is not True or getattr(bot.bot_entity, "enable", None) is not True
            or getattr(bot.bot_entity, "uuid", None) != scope.bot_id
            or getattr(bot.bot_entity, "workspace_uuid", None) != route["workspace_uuid"]
            or runtime_context is None
            or any(getattr(runtime_context, key, None) != value for key, value in route.items())):
        raise DeliveryStopped
    # The pinned OneBot adapter exposes its existing authenticated connection.
    # A disconnected/restarting adapter is not an attempted upload.
    connection = getattr(getattr(bot.adapter, "bot", None), "connection", None)
    if connection is None or getattr(connection, "closed", True):
        raise DeliveryUnavailable
    return bot


class HostDelivery:
    def __init__(self, ap, root: str | Path, *, resolver=resolve_bot):
        self.ap, self.resolver = ap, resolver
        self.tasks = TaskStore(Path(root) / "tasks.sqlite")
        self.assets = AssetStore(Path(root) / "assets")
        self.outbox = Outbox(self.tasks)

    async def tick(self) -> bool:
        instance = self.ap.workspace_service.instance_uuid
        await asyncio.to_thread(self.outbox.reconcile_sent, instance, self.assets)
        claim = await asyncio.to_thread(self.outbox.claim, instance)
        if claim is None:
            return False
        reserved, ref = False, None
        try:
            async with asyncio.timeout(60):
                validate_route(claim.scope, claim.route)
                task = await asyncio.to_thread(self.tasks.get, claim.scope, claim.task_id)
                if len(task.result_refs) != 1:
                    raise AssetError("delivery_result", "任务没有唯一的可交付产物。")
                ref = task.result_refs[0]
                item = await asyncio.to_thread(self.assets.get, claim.scope, ref)
                if item["origin"] not in {"generated", "archive"} or item["size"] > 20 * 1024 * 1024:
                    raise AssetError("delivery_limit", "产物超过后台交付限制。")
                raw = await asyncio.to_thread(self.assets.read, claim.scope, ref)
                from langbot_plugin.api.entities.builtin.platform.message import File, MessageChain
                from langbot_plugin.runtime.io.connection import MAX_MESSAGE_BYTES
                if ((len(raw) + 2) // 3) * 4 + 65536 > min(MAX_MESSAGE_BYTES, 32 * 1024 * 1024):
                    raise AssetError("delivery_limit", "产物超过消息传输上限。")
                chain = MessageChain([File(name=item["name"], size=len(raw), base64=base64.b64encode(raw).decode("ascii"))])
                # Resolve/revalidate the actual bot immediately before the
                # synchronous reservation section, with no shared tenant state.
                bot = await self.resolver(self.ap, claim.scope, claim.route)
                if not self.outbox.update(claim, "started", asset_ref=ref):
                    return True
                if not self.outbox.active(claim) or bot.enable is not True:
                    self.outbox.update(claim, "cancelled", error_code="cancelled")
                    return True
                reserved = self.assets.reserve_delivery(claim.scope, claim.request_key, ref)
                if not reserved:
                    previous = self.assets.delivery(claim.scope, claim.request_key, ref)
                    self.outbox.update(claim, "sent" if previous == "sent" else "unconfirmed",
                                       error_code=None if previous == "sent" else "existing_attempt")
                    return True
                if not self.outbox.active(claim) or bot.enable is not True:
                    self.assets.mark_delivery(claim.scope, claim.request_key, ref, "unconfirmed")
                    self.outbox.update(claim, "unconfirmed", error_code="cancelled")
                    return True
                kind, target = claim.scope.conversation_id.split(":", 1)
                async with asyncio.timeout(45):
                    await bot.adapter.send_message(kind, target, chain)
                self.assets.mark_delivery(claim.scope, claim.request_key, ref, "sent")
                self.outbox.update(claim, "sent")
        except DeliveryStopped:
            self.outbox.update(claim, "cancelled", error_code="route_inactive")
        except DeliveryUnavailable:
            self.outbox.update(claim, "queued", error_code="transport_offline", delay=5)
        except AssetError:
            if reserved:
                self._unconfirmed(claim, ref)
            else:
                self.outbox.update(claim, "failed", error_code="asset_unavailable")
        except BaseException as error:
            if reserved and ref is not None:
                self._unconfirmed(claim, ref)
            else:
                self.outbox.update(claim, "queued", error_code="host_unavailable", delay=5)
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
        return True

    def _unconfirmed(self, claim, ref):
        try:
            self.assets.mark_delivery(claim.scope, claim.request_key, ref, "unconfirmed")
        except AssetError:
            pass  # An explicitly deleted asset must not prevent outbox fencing.
        self.outbox.update(claim, "unconfirmed", error_code="transport_unconfirmed")

    async def run(self):
        while True:
            try:
                worked = await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                worked = False  # No payload, route, URL or traceback in logs.
            await asyncio.sleep(0.1 if worked else 1)


async def run_forever(ap):
    """Optional Host lifecycle hook; standalone processing daemon never calls it."""
    if not os.environ.get("QUNLING_DATA_DIR"):
        return
    await HostDelivery(ap, data_root()).run()
