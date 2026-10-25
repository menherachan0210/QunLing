"""Bot-lifetime collection and administrator endpoints. Never sends QQ messages."""
import asyncio
import logging

from qunling.scope import Scope
from .config import ConfigError, requires_live
from .store import Store

logger = logging.getLogger(__name__)


def scope_for(bot, group="1", actor="*"):
    from qunling.proactive.host import _knowledge_scope, ProactiveUnavailable
    workspace = _knowledge_scope(bot)
    if not workspace:
        raise ProactiveUnavailable("知识库所属机器人已失效。")
    return Scope(workspace, str(bot.bot_entity.uuid), "group:" + str(group), str(actor))


async def groups_for(bot):
    async with asyncio.timeout(10):
        rows = await bot.adapter.get_group_list()
    return {str(row.id) for row in rows[:2000]}


async def cycle(bot, store, reader):
    from qunling.proactive.host import _bridge
    identity = scope_for(bot)
    await asyncio.to_thread(store.prune)
    config = await asyncio.to_thread(store.settings, identity)
    if not config["enabled"] or not config["sources"]:
        return
    bridge = _bridge(bot, create=True)
    if bridge is None:
        return
    await bridge._active()
    groups = await groups_for(bot)
    # One background GET at a time, independent of foreground tool slots.
    for source in config["sources"]:
        if source["group_id"] not in groups:
            continue
        await bridge._active()
        scope = scope_for(bot, source["group_id"])
        if scope.workspace_id != identity.workspace_id or scope.bot_id != identity.bot_id:
            return
        token = await asyncio.to_thread(store.claim, scope, source, config["revision"])
        if token is None:
            continue
        page, error = None, "fetch_failed"
        try:
            async with asyncio.timeout(25):
                page = await reader(source["url"])
            # Revocation, replacement and leaving the group invalidate results
            # collected under an earlier authority, including during a fetch.
            await bridge._active()
            if scope != scope_for(bot, source["group_id"]) or source["group_id"] not in await groups_for(bot):
                continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            page, error = None, getattr(exc, "code", "fetch_failed")
        await asyncio.to_thread(store.complete, scope, source, config["revision"], token, page, error)


async def collect(bot):
    from qunling.web_pages import read_page
    store = Store.from_env()
    while True:
        try:
            await cycle(bot, store, read_page)
        except asyncio.CancelledError:
            raise
        except Exception:
            # No URLs, tokens or page bodies enter runtime logs.
            logger.debug("Public knowledge collection paused; bot or storage unavailable")
        await asyncio.sleep(30)


def start(bot):
    import os
    if not os.environ.get("QUNLING_DATA_DIR") or getattr(bot.bot_entity, "adapter", None) != "onebot-v11":
        return
    task = getattr(bot, "_qunling_knowledge_task", None)
    if task is None or task.done():
        bot._qunling_knowledge_task = asyncio.create_task(collect(bot), name="qunling-public-knowledge")


async def stop(bot):
    task = getattr(bot, "_qunling_knowledge_task", None)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        bot._qunling_knowledge_task = None


async def recall(bot, group, actor, text):
    if requires_live(text):
        return []
    try:
        return await asyncio.to_thread(Store.from_env().recall, scope_for(bot, group, actor), text)
    except Exception:
        return []


async def get_config(ap, context, bot_uuid):
    from qunling.proactive.host import _authorized_bot
    bot = await _authorized_bot(ap, context, bot_uuid)
    store, scope = Store.from_env(), scope_for(bot)
    start(bot)
    return {"config": await asyncio.to_thread(store.settings, scope),
            "status": await asyncio.to_thread(store.status, scope)}


async def save_config(ap, context, bot_uuid, values):
    from qunling.proactive.host import _authorized_bot, _bridge
    bot = await _authorized_bot(ap, context, bot_uuid)
    bridge = _bridge(bot, create=True)
    if bridge is None:
        raise ConfigError("知识库存储尚未配置。")
    await bridge._active()
    known = await groups_for(bot)
    scope, store = scope_for(bot), Store.from_env()
    result = await asyncio.to_thread(store.save, scope, values, known)
    start(bot)
    return {"config": result, "status": await asyncio.to_thread(store.status, scope)}


def register_routes(router):
    import quart
    from langbot.pkg.api.http.authz import Permission
    from langbot.pkg.api.http.context import RequestContext
    from langbot.pkg.api.http.controller import group
    from qunling.proactive.host import ProactiveUnavailable

    @router.route('/<bot_uuid>/knowledge', methods=['GET', 'POST'],
                  auth_type=group.AuthType.USER_TOKEN_OR_API_KEY, permission=Permission.RESOURCE_MANAGE)
    async def knowledge(bot_uuid: str, request_context: RequestContext):
        try:
            if quart.request.method == 'POST':
                result = await save_config(router.ap, request_context, bot_uuid, await quart.request.get_json(silent=True))
            else:
                result = await get_config(router.ap, request_context, bot_uuid)
            return router.success(data=result)
        except (ConfigError, ProactiveUnavailable) as exc:
            return router.http_status(400, 'knowledge_unavailable', str(exc))
        except Exception:
            return router.http_status(503, 'knowledge_unavailable', '知识库暂不可用，请确认连接和存储后重试。')
