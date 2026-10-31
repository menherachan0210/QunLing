"""QQ member conversation lanes; platform routing and attachment scopes stay intact.

Only trusted host Query identity is used. The internal lane key is never sent as
a QQ group ID, and runner variables cannot select another member's history.
"""

from __future__ import annotations

import hashlib
import json
import uuid


def _identity(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ValueError(f"QQ {name} is required for member session isolation")
    return str(value)


def member_key(query) -> str | None:
    """Recognize the host's OneBot adapter, never user-supplied query variables."""
    launcher_type = getattr(query, "launcher_type", None)
    if getattr(launcher_type, "value", launcher_type) != "group":
        return None
    adapter = getattr(query, "adapter", None)
    runtime = getattr(adapter, "_runtime", None)
    entity = getattr(runtime, "bot_entity", None)
    adapter_name = getattr(entity, "adapter", None)
    cls = type(adapter)
    canonical = adapter_name == "onebot-v11"
    legacy = cls.__name__ == "AiocqhttpAdapter" and cls.__module__ in (
        "langbot.pkg.platform.adapters.aiocqhttp.adapter",
        "langbot.pkg.platform.sources.aiocqhttp",
    )
    if not canonical and not legacy:
        return None
    values = [
        "qq-member", _identity(query.launcher_id, "group"),
        _identity(query.sender_id, "sender"),
    ]
    from qunling.group_reset import for_context
    state = for_context(getattr(runtime, "execution_context", None),
                        str(getattr(query, "bot_uuid", "")), str(query.launcher_id))
    if state:
        if state["pending"]:
            raise RuntimeError("Group memory reset is pending")
        values.append(state["generation"])
    return json.dumps(values, ensure_ascii=True, separators=(",", ":"))


def session_key_id(query):
    """Keep the upstream six-part cache index; change only its internal final key."""
    key = member_key(query)
    if key is not None:
        task = _task(query)
        if task is not None:
            if task.session_key is not None:
                return task.session_key
            service = query.adapter._runtime.ap._qq_dispatch
            return service.session_routes.get((task.owner, query.pipeline_uuid, key), key)
    return key or query.launcher_id


def _task(query):
    """Only the registered Host task may choose a conversation branch."""
    from qunling.qq_dispatch import Task, _scope
    task = getattr(query, '_qq_task', None)
    if (isinstance(task, Task) and task.query is query and task.owner == _scope(query)):
        return task
    return None


def remember_route(service, query, session):
    task = _task(query)
    if task is None:
        return
    task.session, task.session_key = session, session._langbot_session_key[5]
    route = (task.owner, query.pipeline_uuid, member_key(query))
    task.session_route = route
    service.session_routes[route] = task.session_key
    service.session_routes.move_to_end(route)
    while len(service.session_routes) > 1024:
        service.session_routes.popitem(last=False)


def release_session(ap, query, session):
    """Discard superseded idle sessions without altering the current branch."""
    task = _task(query)
    service = getattr(ap, '_qq_dispatch', None)
    if (task is not None and task.session is session and service is not None
            and not session._semaphore.locked()
            and service.session_routes.get(task.session_route) != task.session_key):
        ap.sess_mgr._remove_session(session)


async def fork_session(ap, query, blocked):
    """Allocate a separate runner identity, never share mutable session state."""
    task = _task(query)
    assert task is not None and blocked.owner == task.owner
    previous_key = task.session_key
    task.session_key = json.dumps([*json.loads(member_key(query)), 'task', uuid.uuid4().hex],
                                  separators=(',', ':'))
    try:
        session = await ap.sess_mgr.get_session(query)
    except BaseException:
        task.session_key = previous_key
        raise
    source = getattr(blocked.session, 'using_conversation', None)
    if (source is not None and source.pipeline_uuid == query.pipeline_uuid
            and source.bot_uuid == query.bot_uuid):
        # Only history before the running task started is eligible. Its partial
        # output, tools, summary and pending goals must never enter a new turn.
        object.__setattr__(session, '_qunling_fork_history', (
            task.owner, query.pipeline_uuid, history_conversation_id(source),
            getattr(source, 'thread_id', None), blocked.started_wall))
    return session


async def prepare_fork_history(ap, query, session, conversation):
    """Copy a bounded text history once; runner state and attachments stay scoped.

    Use the transcript database, so both the native runner history API and the
    preprocessing view see the same seed. No mutable state, tool results, image
    bytes or external-provider conversation ID are inherited.
    """
    seed = getattr(session, '_qunling_fork_history', None)
    if seed is None:
        return
    task = _task(query)
    if task is None or task.session is not session or seed[:2] != (task.owner, query.pipeline_uuid):
        raise ValueError('Conversation history does not belong to this task')
    _validate_session(query, session)
    import datetime
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import AsyncSession
    from langbot.pkg.entity.persistence.transcript import Transcript
    from langbot.pkg.agent.runner.context_reset import active_history_condition
    target = history_conversation_id(conversation)
    if target == seed[2]:
        raise ValueError('Concurrent tasks cannot share a runner conversation')
    cutoff = datetime.datetime.fromtimestamp(seed[4], datetime.timezone.utc).replace(tzinfo=None)
    async with AsyncSession(ap.persistence_mgr.get_db_engine()) as db, db.begin():
        rows = (await db.execute(sa.select(
            Transcript.event_id, Transcript.role, Transcript.content, Transcript.created_at,
        ).where(
            Transcript.workspace_id == task.owner[1], Transcript.bot_id == query.bot_uuid,
            Transcript.conversation_id == seed[2], Transcript.thread_id == seed[3],
            Transcript.created_at < cutoff, Transcript.item_type == 'message',
            Transcript.role.in_(('user', 'assistant')), active_history_condition(Transcript),
        ).order_by(Transcript.seq.desc()).limit(24))).all()
        for row in reversed(rows):
            if not row.content:
                continue
            item = Transcript(transcript_id=str(uuid.uuid4()), event_id=row.event_id,
                workspace_id=task.owner[1], bot_id=query.bot_uuid, conversation_id=target,
                thread_id=None, role=row.role, item_type='message', content=row.content[:4000],
                seq=0, created_at=row.created_at)
            db.add(item)
            await db.flush()
            item.seq = item.id
    object.__delattr__(session, '_qunling_fork_history')


def _validate_session(query, session):
    task = _task(query)
    if (task is not None and task.session_key is not None
            and getattr(session, '_langbot_session_key', (None,) * 6)[5] != task.session_key):
        raise ValueError("QQ runner session does not belong to this task")
    if (str(session.launcher_id), str(session.sender_id)) != (str(query.launcher_id), str(query.sender_id)):
        raise ValueError("QQ runner session does not belong to this member")
    if getattr(session, "bot_uuid", None) != getattr(query, "bot_uuid", None):
        raise ValueError("QQ runner session does not belong to this bot")
    scope = getattr(query, "_execution_context", None)
    session_scope = getattr(session, "_execution_context", None)
    for field in ("instance_uuid", "workspace_uuid", "placement_generation"):
        if getattr(scope, field, None) != getattr(session_scope, field, None):
            raise ValueError("QQ runner session does not belong to this execution scope")


def bind_conversation(query, session, conversation, *, renew=False):
    key = member_key(query)
    if key is None:
        return
    _validate_session(query, session)
    previous = getattr(conversation, "_qunling_member_key", None)
    if previous is not None and previous != key:
        raise ValueError("QQ conversation member identity changed")
    # Conversation.uuid belongs to external runners such as Dify. Maintain a
    # separate host ID for transcripts/state instead of overwriting that value.
    if renew or not getattr(conversation, "_qunling_conversation_id", None):
        object.__setattr__(conversation, "_qunling_conversation_id", str(uuid.uuid4()))
        object.__setattr__(conversation, "_qunling_member_key", key)


def history_conversation_id(conversation):
    host_id = getattr(conversation, "_qunling_conversation_id", None)
    return host_id if isinstance(host_id, str) and host_id else getattr(conversation, "uuid", None)


def assert_group_reset_idle(ap, workspace, bot, conversation):
    """The management reset targets a whole group, including every member lane."""
    manager = getattr(ap, "sess_mgr", None)
    if not conversation.startswith("group_") or manager is None:
        return
    group = conversation.removeprefix("group_")
    for session in manager.session_list:
        key = getattr(session, "_langbot_session_key", ())
        if (isinstance(key, tuple) and len(key) == 6 and isinstance(key[5], str) and key[5].startswith('["qq-member",')
                and key[1] == workspace and key[3] == bot
                and str(session.launcher_id) == group and key[4] == "group"
                and session._semaphore.locked()):
            raise RuntimeError("Session is processing a task; wait until it finishes")


def runner_conversation_id(query) -> str | None:
    """Resolve member history even before preprocessing, without a group fallback."""
    key = member_key(query)
    if key is None:
        return None
    session = getattr(query, "session", None)
    if session is not None:
        _validate_session(query, session)
        conversation = getattr(session, "using_conversation", None)
        if conversation is not None:
            bind_conversation(query, session, conversation)
            return history_conversation_id(conversation)
    scope = getattr(query, "_execution_context", None)
    identity = [
        _identity(getattr(scope, "instance_uuid", None), "instance"),
        _identity(getattr(scope, "workspace_uuid", None), "workspace"),
        getattr(scope, "placement_generation", None),
        _identity(getattr(query, "bot_uuid", None), "bot"),
        key, getattr(query, "pipeline_uuid", None),
    ]
    return "qq-member:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
