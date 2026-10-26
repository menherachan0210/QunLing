"""Authenticated, group-scoped memory purge. Never resets configuration or sends QQ messages."""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import re
import sqlite3

from qunling import group_reset
from qunling.group_memory import MEMORY_PLUGIN


class MemoryResetError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.status = code, status


def log_failure(ap, stage, error):
    # Record the failing step without exception messages, SQL or memory text.
    logger = getattr(ap, 'logger', None)
    if logger is not None:
        logger.error(f'Group memory reset failed: {stage} ({type(error).__name__})')


def validate(values):
    required = {"group_id", "confirmation", "request_id"}
    if (not isinstance(values, dict) or not required.issubset(values)
            or set(values) - required - {"resume_generation"}
            or not isinstance(values["group_id"], str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", values["group_id"])
            or values["confirmation"] != values["group_id"]
            or not isinstance(values["request_id"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", values["request_id"])
            or ("resume_generation" in values and (not isinstance(values["resume_generation"], str)
                or not re.fullmatch(r"[a-f0-9]{32}", values["resume_generation"])))):
        raise MemoryResetError("invalid_memory_reset", "请确认群号后再清空记忆。", 400)
    return values["group_id"], values["request_id"]


def _matches(owner, context, bot, group):
    return isinstance(owner, tuple) and len(owner) >= 5 and owner[:5] == (
        context.instance_uuid, context.workspace_uuid, context.placement_generation, bot, group)


def assert_idle(ap, runtime, bridge, group):
    """No await between this check and the durable admission fence."""
    context, bot = runtime.execution_context, str(runtime.bot_entity.uuid)
    busy = False
    dispatch = getattr(ap, "_qq_dispatch", None)
    if dispatch:
        busy |= any(_matches(owner, context, bot, group) for owner in dispatch.preparing.values())
        busy |= any(_matches(task.owner, context, bot, group) for task in dispatch.tasks.values())
        items = list(dispatch.observations) + ([dispatch.observing] if dispatch.observing else [])
        busy |= any(str(item[0].bot_uuid) == bot and str(item[0].launcher_id) == group
                    and getattr(item[0], "workspace_uuid", None) == context.workspace_uuid for item in items)
    busy |= any(key[0] == group for key in bridge.queued)
    state = bridge.engine._groups.get(group)
    if state:
        busy |= any(lane.task is not None and not lane.task.done() for lane in state.lanes.values())
        busy |= any(lane.explicit or lane.sending for lane in state.lanes.values())
    for session in getattr(getattr(ap, "sess_mgr", None), "session_list", []):
        key = getattr(session, "_langbot_session_key", ())
        if (isinstance(key, tuple) and len(key) == 6 and key[:4] == (
                context.instance_uuid, context.workspace_uuid, context.placement_generation, bot)
                and key[4] == "group" and str(session.launcher_id) == group):
            busy |= not ap.sess_mgr._session_is_idle(session)
    if busy:
        raise MemoryResetError("memory_reset_busy", "这个群还有请求正在处理，请等回复或工具任务结束后再清空。")
    from qunling.assets import data_root
    path = data_root() / "tasks.sqlite"
    if path.is_symlink():
        raise RuntimeError("Invalid task storage")
    if path.exists():
        with closing(sqlite3.connect(path)) as db:
            rows = list(db.execute("SELECT scope_json FROM ql_tasks WHERE status NOT IN ('completed','failed','cancelled')"))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ql_file_outbox'").fetchone():
                rows += list(db.execute("SELECT scope_json FROM ql_file_outbox WHERE state IN ('queued','started')"))
            for row in rows:
                owner = json.loads(row[0])
                if (owner.get("workspace_id") == group_reset.scope(context, bot, group)[0]
                        and owner.get("bot_id") == bot and owner.get("conversation_id") == f"group:{group}"):
                    raise MemoryResetError("memory_reset_busy", "这个群还有后台工具任务，请等任务结束后再清空。")


def group_conversations(workspace, bot, group):
    import sqlalchemy as sa
    from langbot.pkg.entity.persistence.agent_run import AgentRun
    from langbot.pkg.entity.persistence.monitoring import MonitoringMessage
    return sa.select(AgentRun.conversation_id).join(MonitoringMessage, sa.and_(
        MonitoringMessage.run_id == AgentRun.run_id,
        MonitoringMessage.workspace_uuid == AgentRun.workspace_id,
        MonitoringMessage.bot_id == AgentRun.bot_id)).where(
        AgentRun.workspace_id == workspace, AgentRun.bot_id == bot,
        MonitoringMessage.session_id.in_([f"group_{group}", f"GROUP_{group}"]),
        AgentRun.conversation_id.is_not(None))


async def memory_bases(ap, context):
    import sqlalchemy as sa
    from langbot.pkg.entity.persistence.rag import KnowledgeBase

    rows = (await ap.persistence_mgr.execute_async(sa.select(
        KnowledgeBase.uuid, KnowledgeBase.creation_settings).where(
            KnowledgeBase.workspace_uuid == context.workspace_uuid,
            KnowledgeBase.knowledge_engine_plugin_id == MEMORY_PLUGIN))).all()
    # Bot-wide memory has no proven group ownership. Never erase another group
    # by broadening the filter to bot_uuid or deleting an entire collection.
    if any((settings or {}).get("isolation", "session") != "session" for _, settings in rows):
        raise MemoryResetError("memory_reset_shared", "当前长期记忆使用跨群共享存储，无法安全地只清空一个群。", 400)
    return [row[0] for row in rows]


async def preflight(ap, context, bot, group):
    import sqlalchemy as sa
    from langbot.pkg.entity.persistence.monitoring import MonitoringMessage as M
    from langbot.pkg.entity.persistence.agent_run import AgentRun as R
    from langbot.pkg.entity.persistence.agent_interaction import AgentInteraction as I
    execute = ap.persistence_mgr.execute_async
    conversations = group_conversations(context.workspace_uuid, bot, group)
    pending = (await execute(sa.select(M.id).where(
        M.workspace_uuid == context.workspace_uuid, M.bot_id == bot,
        M.session_id.in_([f"group_{group}", f"GROUP_{group}"]), M.status == "pending").limit(1))).first()
    for table, statuses in ((R, ["created", "queued", "claimed", "running"]), (I, ["pending", "submitted"])):
        busy = (await execute(sa.select(table.id).where(
            table.workspace_id == context.workspace_uuid, table.bot_id == bot,
            sa.or_(table.conversation_id.in_(conversations), table.conversation_id == f"group_{group}"),
            table.status.in_(statuses)).limit(1))).first()
        pending = pending or busy
    if pending:
        raise MemoryResetError("memory_reset_busy", "这个群还有未结束的请求，请等任务完成或取消后再清空。")
    return await memory_bases(ap, context)


def memory_rows(table, workspace, bot, group):
    import sqlalchemy as sa
    scope = f"{bot}:group_{group}"
    return sa.and_(table.workspace_uuid == workspace, table.owner_type == "plugin",
                   table.owner == MEMORY_PLUGIN, sa.or_(
                       table.key.in_([f"{prefix}:{scope}" for prefix in ("ps", "audit", "candidates", "inj")]),
                       table.key.startswith(f"pp:{scope}:", autoescape=True)))


async def purge_persistent(ap, context, bot, group, bases):
    import sqlalchemy as sa
    from langbot.pkg.entity.persistence.bstorage import BinaryStorage
    from langbot.pkg.entity.persistence.runner_state import RunnerState
    from langbot.pkg.entity.persistence.transcript import Transcript

    execute = ap.persistence_mgr.execute_async
    scope = f"{bot}:group_{group}"
    for kb in bases:
        await ap.rag_runtime_service.vector_delete(context, kb, filters={"user_key": scope})
        remaining, total = await ap.rag_runtime_service.vector_list(context, kb, filters={"user_key": scope}, limit=1)
        # Chroma returns -1 when a filtered total is unavailable. An empty
        # first page still proves that this group's filter matches no vectors;
        # requiring total == 0 would leave every Chroma reset fenced forever.
        if remaining or type(total) is not int or total not in (0, -1):
            raise RuntimeError("Memory vector deletion could not be verified")
    await execute(sa.delete(BinaryStorage).where(memory_rows(BinaryStorage, context.workspace_uuid, bot, group)))
    # Monitoring links the real platform group session to member runner IDs.
    # Logs stay available for diagnosis, but their model transcripts are erased.
    conversations = group_conversations(context.workspace_uuid, bot, group)
    known = {f"group_{group}", f"GROUP_{group}"}
    for session in getattr(getattr(ap, "sess_mgr", None), "session_list", []):
        key = getattr(session, "_langbot_session_key", ())
        if (len(key) == 6 and key[:4] == (context.instance_uuid, context.workspace_uuid, context.placement_generation, bot)
                and key[4] == "group" and str(session.launcher_id) == group):
            conversation = getattr(session, "using_conversation", None)
            value = getattr(conversation, "_qunling_conversation_id", None)
            if value:
                known.add(value)
    for table in (Transcript, RunnerState):
        await execute(sa.delete(table).where(
            table.workspace_id == context.workspace_uuid, table.bot_id == bot,
            sa.or_(table.conversation_id.in_(conversations), table.conversation_id.in_(known))))


def purge_cached(ap, runtime, bridge, group):
    from qunling.group_knowledge import clear_group
    from qunling.group_batch import clear_group as clear_batches

    context, bot = runtime.execution_context, str(runtime.bot_entity.uuid)
    clear_group(workspace_id=group_reset.scope(context, bot, group)[0], bot_id=bot, group_id=group)
    clear_batches(group_reset.scope(context, bot, group)[0], bot, group)
    from qunling.qq_operation_store import OperationStore, scope_key
    OperationStore().clear_events(scope_key(context.instance_uuid, context.workspace_uuid, bot), f"group:{group}")
    bridge.engine.reset_group_context(group)
    for mapping in (bridge._request_contexts, bridge._received_at, bridge._sent_receipts, bridge._continuation_ingress):
        for key in list(mapping):
            if key[0] == group:
                mapping.pop(key, None)
    dispatch = getattr(ap, "_qq_dispatch", None)
    if dispatch:
        for key, record in list(dispatch.continuations.items()):
            if _matches(record.owner, context, bot, group):
                dispatch.continuations.pop(key, None)
    manager = getattr(ap, "sess_mgr", None)
    if manager:
        for session in list(manager.session_list):
            key = getattr(session, "_langbot_session_key", ())
            if (len(key) == 6 and key[:4] == (context.instance_uuid, context.workspace_uuid, context.placement_generation, bot)
                    and key[4] == "group" and str(session.launcher_id) == group):
                manager._remove_session(session)


async def reset_memory(ap, context, bot_uuid, values):
    from langbot.pkg.core.task_boundary import create_detached_task, run_in_workspace_uow
    from qunling.proactive.host import _authorized_bot, _bridge, list_groups

    group, request_id = validate(values)
    runtime = await _authorized_bot(ap, context, bot_uuid)
    groups = await list_groups(ap, context, bot_uuid)
    if group not in {row["group_id"] for row in groups["groups"]}:
        raise MemoryResetError("invalid_memory_reset", "只能清空当前 QQ 已加入的群。", 400)
    bridge = _bridge(runtime, create=True)
    execution = runtime.execution_context
    key = group_reset.scope(execution, bot_uuid, group)
    locks = getattr(ap, "_group_memory_reset_locks", None)
    if locks is None:
        ap._group_memory_reset_locks = locks = {}
    lock = locks.setdefault(key, asyncio.Lock())
    if lock.locked():
        raise MemoryResetError("memory_reset_busy", "这个群正在清空记忆，请稍候。")
    async with lock:
        previous = group_reset.completed(key, request_id)
        if previous is not None:
            return {"cleared": True, "group_id": group, "cleared_at": previous}
        if "resume_generation" in values:
            boundary = group_reset.boundary(key)
            if (not boundary or not boundary['pending']
                    or boundary['generation'] != values['resume_generation']):
                raise MemoryResetError('memory_reset_changed', '原清空操作已结束或发生变化，未再次清空记忆。')
        bases = await run_in_workspace_uow(ap, context.workspace_uuid, lambda: preflight(ap, execution, bot_uuid, group))
        assert_idle(ap, runtime, bridge, group)
        group_reset.begin(key)
        try:
            bridge.engine.reset_group_context(group)
        except Exception as exc:
            log_failure(ap, 'prepare_context', exc)
            raise MemoryResetError("memory_reset_failed", "记忆未完全清空，该群已暂停接话。请再次点击清空重试。", 503) from None

        stage = 'persistent_memory'
        async def perform():
            nonlocal stage
            await run_in_workspace_uow(ap, context.workspace_uuid,
                                       lambda: purge_persistent(ap, execution, bot_uuid, group, bases))
            stage = 'cached_context'
            purge_cached(ap, runtime, bridge, group)
            stage = 'completion_receipt'
            return group_reset.finish(key, request_id)

        # An HTTP disconnect must not abandon an in-flight purge. A crash or
        # actual storage failure leaves the durable fence closed for a retry.
        # HTTP handlers carry a task-owned persistence boundary even in OSS
        # mode. Copying it into this shielded task makes every database access
        # fail ownership checks. Detached work opens its own explicit scope.
        task = create_detached_task(perform(), workspace_uuid=context.workspace_uuid)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        try:
            cutoff = task.result()
        except Exception as exc:
            log_failure(ap, stage, exc)
            raise MemoryResetError("memory_reset_failed", "记忆未完全清空，该群已暂停接话。请再次点击清空重试。", 503) from None
        if cancelled:
            raise asyncio.CancelledError
        return {"cleared": True, "group_id": group, "cleared_at": cutoff}
