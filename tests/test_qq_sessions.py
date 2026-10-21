"""Real scheduler/session/transcript integration, with synthetic QQ and model I/O."""
from __future__ import annotations

import asyncio
import contextlib
import datetime
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / "langbot/src").is_dir():
    sys.path.insert(0, str(ROOT / "langbot/src"))

from qunling.qq_sessions import member_key, runner_conversation_id, history_conversation_id, bind_conversation
from langbot.pkg.provider.session.sessionmgr import SessionManager
from langbot.pkg.api.http.context import ExecutionContext
from langbot.pkg.pipeline.controller import Controller
from langbot.pkg.pipeline.pool import QueryPool, ExecutionContextMismatchError
from langbot.pkg.pipeline.preproc.preproc import PreProcessor
from langbot.pkg.pipeline.resprule.resprule import GroupRespondRuleCheckStage
from langbot.pkg.pipeline.resprule.rules.atbot import AtBotRule
from langbot.pkg.pipeline.entities import ResultType
from langbot.pkg.agent.runner.query_entry_adapter import QueryEntryAdapter
from langbot.pkg.agent.runner.orchestrator import AgentRunOrchestrator
from langbot.pkg.agent.runner.transcript_store import TranscriptStore
from langbot.pkg.agent.runner.persistent_state_store import PersistentStateStore
from langbot.pkg.agent.runner.host_models import StatePolicy
from langbot.pkg.api.http.service.monitoring import MonitoringService
from langbot.pkg.entity.persistence.base import Base
from langbot.pkg.entity.persistence.monitoring import MonitoringSession
from langbot_plugin.api.entities.builtin.platform import message as pm, entities as pe, events as events
from langbot_plugin.api.entities.builtin.provider.session import LauncherTypes
from langbot_plugin.api.entities.builtin.provider.message import Message


class Persistence:
    mode = NS(value="oss")

    def __init__(self, engine):
        self.engine = engine

    def get_db_engine(self):
        return self.engine

    @contextlib.asynccontextmanager
    async def tenant_uow(self, workspace):
        async with self.engine.begin() as conn:
            self.conn = conn
            yield

    async def execute_async(self, statement):
        return await self.conn.execute(statement)


@pytest_asyncio.fixture
async def app(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    scope = ExecutionContext(instance_uuid="instance", workspace_uuid="workspace", placement_generation=1)
    value = NS(
        instance_config=NS(data={"concurrency": {"pipeline": 2, "session": 8}}),
        query_pool=QueryPool(max_queries=64, max_queries_per_workspace=32),
        persistence_mgr=Persistence(engine), logger=Mock(),
        workspace_service=NS(get_execution_binding=AsyncMock(return_value=scope)),
    )
    value.sess_mgr = SessionManager(value)
    value.scope = scope
    value.adapter = NS(bot_account_id="bot-account", _runtime=NS(bot_entity=NS(adapter="onebot-v11")))
    value.agent_run_orchestrator = object.__new__(AgentRunOrchestrator)
    value.agent_run_orchestrator.query_bridge = Mock()
    value.workers = []

    def create_task(coro, **kwargs):
        task = asyncio.create_task(coro)
        value.workers.append(task)
        return task

    value.task_mgr = NS(create_task=create_task)
    yield value
    for task in value.workers:
        if not task.done():
            task.cancel()
    await asyncio.gather(*value.workers, return_exceptions=True)
    await engine.dispose()


async def enqueue(app, sender, text="question", *, group="30000", addressed=True, bot="bot", scope=None):
    chain = pm.MessageChain(([pm.At(target="bot-account")] if addressed else []) + [pm.Plain(text=text)])
    grp = pe.Group(id=group, name="Synthetic", permission=pe.Permission.Member)
    event = events.GroupMessage(
        type="GroupMessage", sender=pe.GroupMember(id=sender, member_name=sender,
        permission=pe.Permission.Member, group=grp), message_chain=chain, time=1609459200,
    )
    query = await app.query_pool.add_query(
        bot_uuid=bot, launcher_type=LauncherTypes.GROUP, launcher_id=group, sender_id=sender,
        message_event=event, message_chain=chain, adapter=None, pipeline_uuid="pipeline",
        execution_context=scope or app.scope,
    )
    query.adapter = app.adapter
    return query


async def conversation(app, query):
    query.session = await app.sess_mgr.get_session(query)
    return await app.sess_mgr.get_conversation(query, query.session, [], query.pipeline_uuid, query.bot_uuid)


@pytest.mark.asyncio
async def test_real_scheduler_runs_other_members_while_translation_waits(app):
    """A1 is held in a tool wait: B/C finish, A2 waits, and histories stay separate."""
    gate, started, finished_other, done = (asyncio.Event() for _ in range(4))
    pipeline = NS(pipeline_entity=NS(config={"trigger": {"group-respond-rules": {"at": True}}}),
                  bound_plugins=[], bound_mcp_servers=[])
    app.pipeline_mgr = NS(get_pipeline_by_uuid=AsyncMock(return_value=pipeline))
    rules = GroupRespondRuleCheckStage(app)
    rules.rule_matchers = [AtBotRule(app)]
    preproc = PreProcessor(app)
    store = TranscriptStore(app.persistence_mgr.engine)
    histories, ids, completed, active, peak = {}, {}, [], set(), 0

    async def run(query):
        nonlocal peak
        query.pipeline_config = pipeline.pipeline_entity.config
        result = await rules.process(query, "trigger")
        if result.result_type != ResultType.CONTINUE:
            return
        label = str(query.message_chain)
        conv = await conversation(app, query)
        ctx = QueryEntryAdapter._build_conversation_context(query)
        ids[label] = ctx.conversation_id
        assert ctx.launcher_id == query.session.launcher_id == "30000"
        assert ctx.sender_id == query.sender_id
        assert QueryEntryAdapter._build_delivery_context(query).reply_target["target_id"] == "30000"
        histories[label] = [m.content for m in await preproc._resolve_history_messages(
            "local-agent", conv, bot_id="bot", workspace_id="workspace")]
        active.add(label)
        peak = max(peak, len(active))
        if label == "A1":
            started.set()
            await gate.wait()  # fake slow image-translation/model tool, not a network call
        await store.append_transcript(None, query.query_uuid, ctx.conversation_id, "user",
                                      bot_id="bot", workspace_id="workspace", content=label)
        conv.messages.append(Message(role="user", content=label))
        active.remove(label)
        completed.append(label)
        if {"B1", "C1"}.issubset(completed):
            finished_other.set()
        if len(completed) == 4:
            done.set()

    pipeline.run = run
    a1 = await enqueue(app, "20001", "A1")
    controller = Controller(app)
    consumer = asyncio.create_task(controller.consumer())
    try:
        await asyncio.wait_for(started.wait(), 3)
        a2 = await enqueue(app, "20001", "A2")
        b1 = await enqueue(app, "20002", "B1")
        await enqueue(app, "20003", "ordinary chatter", addressed=False)
        await enqueue(app, "20003", "C1")
        await asyncio.wait_for(finished_other.wait(), 3)
        assert "A2" not in ids and "A1" not in completed
        assert a1.session is await app.sess_mgr.get_session(a2)
        assert a1.session is not b1.session
        assert a1.session._langbot_session_concurrency == 1
        app.agent_run_orchestrator.query_bridge.build_plan.assert_not_called()
        gate.set()
        await asyncio.wait_for(done.wait(), 3)
        await asyncio.gather(*app.workers)
        assert peak == 2
        assert histories == {"A1": [], "B1": [], "C1": [], "A2": ["A1"]}
        assert ids["A1"] == ids["A2"] and len({ids["A1"], ids["B1"], ids["C1"]}) == 3
        assert [m.content for m in b1.session.using_conversation.messages] == ["B1"]
        assert app.query_pool.active_query_count_by_workspace == {}
        assert controller.semaphore._value == 2
    finally:
        gate.set()
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_fallback_does_not_trust_variables_and_preserves_delivery(app):
    a, b = await enqueue(app, "a"), await enqueue(app, "b")
    for query in (a, b):
        query.variables["conversation_id"] = "someone-elses-conversation"
    a_id = QueryEntryAdapter._build_conversation_context(a).conversation_id
    b_id = QueryEntryAdapter._build_conversation_context(b).conversation_id
    assert a_id.startswith("qq-member:") and a_id != b_id
    assert a_id == runner_conversation_id(await enqueue(app, "a"))
    other_bot = await enqueue(app, "a", bot="other-bot")
    assert runner_conversation_id(other_bot) != a_id
    other_scope = ExecutionContext(instance_uuid="instance", workspace_uuid="other", placement_generation=1)
    other_workspace = await enqueue(app, "a", scope=other_scope)
    assert runner_conversation_id(other_workspace) != a_id
    conv = await conversation(app, a)
    conv.uuid = "external-provider-session"
    assert runner_conversation_id(a) == history_conversation_id(conv)
    assert runner_conversation_id(a) != conv.uuid
    b.session = a.session
    with pytest.raises(ValueError, match="member"):
        runner_conversation_id(b)
    with pytest.raises(ExecutionContextMismatchError):
        await app.sess_mgr.get_conversation(b, a.session, [], "pipeline", "bot")
    other_workspace.session = a.session
    with pytest.raises(ValueError, match="scope"):
        runner_conversation_id(other_workspace)


@pytest.mark.asyncio
async def test_member_reset_and_expiry_leave_other_members_untouched(app):
    a, b = await enqueue(app, "a"), await enqueue(app, "b")
    ca, cb = await conversation(app, a), await conversation(app, b)
    a_id, b_id = history_conversation_id(ca), history_conversation_id(cb)
    # SDK CREATE_NEW_CONVERSATION clears only the current query's Session.
    a.session.using_conversation = None
    new_a = await conversation(app, a)
    assert history_conversation_id(new_a) != a_id
    assert history_conversation_id(await conversation(app, b)) == b_id
    # External conversation expiry renews the host transcript/state identity.
    renewed_id = history_conversation_id(new_a)
    bind_conversation(a, a.session, new_a, renew=True)
    assert history_conversation_id(new_a) != renewed_id
    assert history_conversation_id(cb) == b_id


@pytest.mark.asyncio
async def test_persisted_runner_conversation_state_never_crosses_members(app):
    a, b = await enqueue(app, "a"), await enqueue(app, "b")
    await conversation(app, a)
    await conversation(app, b)

    def envelope(query):
        ctx = QueryEntryAdapter._build_conversation_context(query)
        return NS(conversation_id=ctx.conversation_id, bot_id=ctx.bot_id, workspace_id="workspace",
                  thread_id=None, actor=NS(actor_type="user", actor_id=ctx.sender_id), subject=None)

    store = PersistentStateStore(app.persistence_mgr.engine)
    binding = NS(binding_id="binding", state_policy=StatePolicy())
    descriptor = NS(id="local-agent")
    assert await store.apply_update_from_event(envelope(a), binding, descriptor, "conversation",
                                              "task.progress", {"owner": "a"}) == (True, None)
    assert (await store.build_snapshot_from_event(envelope(a), binding, descriptor))["conversation"]["task.progress"] == {"owner": "a"}
    assert "task.progress" not in (await store.build_snapshot_from_event(envelope(b), binding, descriptor))["conversation"]
    a.session.using_conversation = None
    await conversation(app, a)
    assert "task.progress" not in (await store.build_snapshot_from_event(envelope(a), binding, descriptor))["conversation"]


@pytest.mark.asyncio
async def test_management_group_reset_invalidates_all_members_only_in_scope(app):
    queries = [await enqueue(app, "a"), await enqueue(app, "b"),
               await enqueue(app, "a", group="other"), await enqueue(app, "a", bot="other")]
    for query in queries:
        await conversation(app, query)
    before = [history_conversation_id(q.session.using_conversation) for q in queries]
    async with app.persistence_mgr.engine.begin() as conn:
        await conn.execute(sa.insert(MonitoringSession).values(
            workspace_uuid="workspace", bot_id="bot", session_id="group_30000", bot_name="Bot",
            pipeline_id="pipeline", pipeline_name="Pipeline", start_time=datetime.datetime.utcnow(),
            last_activity=datetime.datetime.utcnow()))
    service = MonitoringService(app)
    await queries[0].session._semaphore.acquire()
    with pytest.raises(RuntimeError, match="processing"):
        await service.reset_session_context("workspace", "bot", "group_30000")
    queries[0].session._semaphore.release()
    await service.reset_session_context("workspace", "bot", "group_30000")
    after = [history_conversation_id(await conversation(app, q)) for q in queries]
    assert after[0] != before[0] and after[1] != before[1]
    assert after[2:] == before[2:]


@pytest.mark.asyncio
async def test_drop_one_selected_query_does_not_release_or_remove_another_lane(app):
    a, b = await enqueue(app, "a"), await enqueue(app, "b")
    sa_, sb = await app.sess_mgr.get_session(a), await app.sess_mgr.get_session(b)
    await sa_._semaphore.acquire()
    await sb._semaphore.acquire()
    async with app.query_pool:
        app.query_pool.mark_query_running_locked(a)
    await Controller(app)._drop_selected_query(a, sa_)
    assert sa_._semaphore._value == 1 and sb._semaphore.locked()
    assert await app.query_pool.get_query("workspace", a.query_uuid) is None
    assert await app.query_pool.get_query("workspace", b.query_uuid) is b
    sb._semaphore.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, adapter", [(LauncherTypes.PERSON, "onebot-v11"), (LauncherTypes.GROUP, "telegram")])
async def test_other_platforms_and_private_sessions_are_unchanged(app, kind, adapter):
    a, b = await enqueue(app, "a"), await enqueue(app, "b")
    for query in (a, b):
        query.launcher_type = kind
        query.adapter = NS(_runtime=NS(bot_entity=NS(adapter=adapter)))
    assert member_key(a) is None and runner_conversation_id(a) is None
    sa_, sb = await app.sess_mgr.get_session(a), await app.sess_mgr.get_session(b)
    assert sa_ is sb and sa_._langbot_session_concurrency == 8
    ca = await conversation(app, a)
    assert getattr(ca, "_qunling_conversation_id", None) is None


@pytest.mark.asyncio
async def test_session_cache_roundtrip_and_removal_keep_sibling_lane(app):
    a, b = await enqueue(app, "a"), await enqueue(app, "b")
    sa_, sb = await app.sess_mgr.get_session(a), await app.sess_mgr.get_session(b)
    app.sess_mgr.session_list = app.sess_mgr.session_list
    assert await app.sess_mgr.get_session(a) is sa_
    app.sess_mgr._remove_session(sa_)
    assert await app.sess_mgr.get_session(b) is sb
    assert await app.sess_mgr.get_session(a) is not sa_
