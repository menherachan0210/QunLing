"""Group reset uses isolated stores and fake QQ; never touches deployed memory."""
import asyncio
from datetime import datetime
import json
import sqlite3
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from qunling import group_reset as reset
from qunling import group_reset_host as host
from qunling.proactive.engine import ProactiveEngine, EngineConfig, GroupPolicy, Message
from qunling.group_knowledge import publish, recall
from qunling.qq_sessions import member_key


@pytest.fixture
def scope(tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    return NS(instance_uuid="instance", workspace_uuid="workspace", placement_generation=1)


def request(group="123", token="a" * 32):
    return dict(group_id=group, confirmation=group, request_id=token)


@pytest.mark.parametrize("bad", [None, {}, {**request(), "bot_id": "other"},
    {**request(), "confirmation": "456"}, {**request(), "group_id": True},
    {**request(), "group_id": "../123"}, {**request(), "request_id": "bad"}])
def test_confirmation_is_server_validated(bad):
    with pytest.raises(host.MemoryResetError):
        host.validate(bad)


@pytest.mark.parametrize('generation', [None, True, '', '../123'])
def test_pending_resume_generation_is_validated(generation):
    with pytest.raises(host.MemoryResetError):
        host.validate({**request(), 'resume_generation': generation})


def test_durable_boundary_rejects_stale_events_and_preserves_other_scopes(scope):
    key = reset.scope(scope, "bot", "123")
    first = reset.begin(key)
    assert not reset.allows(scope, "bot", "123", time.time() + 10)
    assert reset.pending_groups(scope, "bot") == ["123"]
    assert reset.pending_groups(scope, "other-bot") == []
    assert reset.begin(key)["generation"] == first["generation"]  # resume a failed purge
    cutoff = reset.finish(key, "a" * 32)
    assert reset.completed(key, "a" * 32) == cutoff
    assert reset.pending_groups(scope, "bot") == []
    assert not reset.allows(scope, "bot", "123", int(cutoff))
    assert reset.allows(scope, "bot", "123", int(cutoff) + 1)
    assert reset.allows(scope, "bot", "1234", cutoff - 50)
    assert reset.allows(scope, "other-bot", "123", cutoff - 50)
    assert reset.allows(NS(instance_uuid="other", workspace_uuid="workspace"), "bot", "123", cutoff - 50)
    assert reset.boundary(key)["generation"] == first["generation"]


def test_history_does_not_reload_old_images_or_reply_relationships(scope):
    key = reset.scope(scope, "bot", "123")
    runtime = NS(execution_context=scope, bot_entity=NS(uuid="bot"))
    reset.begin(key)
    cutoff = reset.finish(key, "a" * 32)
    history = {"messages": [dict(message_id="old", time=cutoff - 5, images=["old-pixels"]),
                              dict(message_id="new", time=cutoff + 1, reply_message_id="old", images=["new-pixels"])]}
    result = reset.filter_history(runtime, "123", history)
    assert result["messages"] == [dict(message_id="new", time=cutoff + 1, images=["new-pixels"])]
    assert len(history["messages"]) == 2
    assert reset.filter_history(runtime, "456", history) is history


def test_all_member_session_keys_change_without_changing_other_group_or_settings(scope):
    runtime = NS(execution_context=scope, bot_entity=NS(adapter="onebot-v11"))
    def query(sender, group="123"):
        return NS(launcher_type="group", launcher_id=group, sender_id=sender, bot_uuid="bot", adapter=NS(_runtime=runtime))
    before = [member_key(query(x)) for x in ("1", "2")]
    other = member_key(query("1", "456"))
    key = reset.scope(scope, "bot", "123")
    reset.begin(key)
    with pytest.raises(RuntimeError):
        member_key(query("1"))
    reset.finish(key, "a" * 32)
    assert all(member_key(query(x)) != before[i] for i, x in enumerate(("1", "2")))
    assert member_key(query("1", "456")) == other


@pytest.mark.asyncio
async def test_proactive_reset_clears_topics_without_erasing_budget_or_other_group(scope):
    config = EngineConfig(revision="r", enabled=True, groups={"123": GroupPolicy(mode="natural"), "456": GroupPolicy(mode="natural")})
    engine = ProactiveEngine(bot_id="bot", config=lambda: config, judge=AsyncMock(), generate=AsyncMock(), send=AsyncMock())
    engine._state("123").messages.append(Message("bot", "123", "1", "1", "old conversation", time.time()))
    engine._state("123").sends.append(time.time())
    engine._state("456").messages.append(Message("bot", "456", "2", "1", "other conversation", time.time()))
    engine._pending[("123", "1")] = (1, "r")
    engine.reset_group_context("123")
    assert not engine._state("123").messages
    assert len(engine._state("123").sends) == 1
    assert [message.text for message in engine._state("456").messages] == ["other conversation"]
    assert ("123", "1") not in engine._pending
    await engine.close()


def test_background_file_delivery_must_settle_before_reset(scope, tmp_path):
    path = tmp_path / "tasks.sqlite"
    bridge = NS(queued=set(), engine=NS(_groups={}))
    runtime = NS(execution_context=scope, bot_entity=NS(uuid="bot"))
    owner = {"workspace_id": reset.scope(scope, "bot", "123")[0], "bot_id": "bot", "conversation_id": "group:123"}
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE ql_tasks(scope_json TEXT, status TEXT)")
        db.execute("CREATE TABLE ql_file_outbox(scope_json TEXT, state TEXT)")
        db.execute("INSERT INTO ql_tasks VALUES(?, 'running')", (json.dumps({**owner, "conversation_id": "group:456"}),))
        db.execute("INSERT INTO ql_file_outbox VALUES(?, 'queued')", (json.dumps(owner),))
    with pytest.raises(host.MemoryResetError, match="后台工具任务"):
        host.assert_idle(NS(), runtime, bridge, "123")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE ql_file_outbox SET state='sent'")
    host.assert_idle(NS(), runtime, bridge, "123")


@pytest.mark.asyncio
@pytest.mark.parametrize('filtered_total', [0, -1])
async def test_purge_deletes_every_member_profile_candidates_and_vectors_in_group_only(scope, tmp_path, filtered_total):
    from langbot.pkg.entity.persistence.bstorage import BinaryStorage as B
    from langbot.pkg.entity.persistence.base import Base
    from langbot.pkg.entity.persistence.runner_state import RunnerState as R
    from langbot.pkg.entity.persistence.transcript import Transcript as T
    from langbot.pkg.entity.persistence.agent_run import AgentRun as A
    from langbot.pkg.entity.persistence.monitoring import MonitoringMessage as M
    # Load Workspace FK metadata before constructing an isolated database.
    from langbot.pkg.entity.persistence import workspace, user
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}")
    async with engine.begin() as db:
        await db.run_sync(Base.metadata.create_all)
        owned = [f"{p}:bot:group_123" for p in ("ps", "audit", "candidates", "inj")]
        owned += ["pp:bot:group_123:1", "pp:bot:group_123:2"]
        keep = ["ps:bot:group_1234", "pp:bot:group_456:1", "ps:bot:person_123", "ps:other:group_123", "kb_configs"]
        for key in owned + keep:
            await db.execute(sa.insert(B).values(workspace_uuid="workspace", unique_key=key, key=key,
                owner_type="plugin", owner="langbot-team/LongTermMemory", value=b'{}'))
        await db.execute(sa.insert(B).values(workspace_uuid="other", unique_key=owned[0], key=owned[0],
            owner_type="plugin", owner="langbot-team/LongTermMemory", value=b'{}'))
        for identity, workspace_id, bot, group in (("one", "workspace", "bot", "123"),
                ("two", "workspace", "bot", "123"), ("neighbor", "workspace", "bot", "1234"),
                ("other-bot", "workspace", "other", "123"), ("other-workspace", "other", "bot", "123")):
            await db.execute(sa.insert(A).values(run_id=identity, runner_id="local", workspace_id=workspace_id,
                bot_id=bot, conversation_id=identity, status="completed"))
            await db.execute(sa.insert(M).values(id=identity, workspace_uuid=workspace_id, timestamp=datetime.utcnow(),
                bot_id=bot, bot_name="bot", pipeline_id="pipeline", pipeline_name="pipeline", message_content="old",
                session_id=f"group_{group}", status="success", level="info", run_id=identity))
            await db.execute(sa.insert(T).values(transcript_id=identity, event_id=identity, bot_id=bot,
                workspace_id=workspace_id, conversation_id=identity, role="user", item_type="message", content="old", seq=1))
            await db.execute(sa.insert(R).values(runner_id="local", binding_identity="local", scope="conversation",
                scope_key=identity, state_key="history", value_json='"old"', workspace_id=workspace_id,
                bot_id=bot, conversation_id=identity))
        ap = NS(persistence_mgr=NS(execute_async=db.execute), rag_runtime_service=NS(
            vector_delete=AsyncMock(return_value=3), vector_list=AsyncMock(return_value=([], filtered_total))))
        await host.purge_persistent(ap, scope, "bot", "123", ["memory-kb"])
        rows = (await db.execute(sa.select(B.workspace_uuid, B.key))).all()
        assert set(rows) == {("workspace", key) for key in keep} | {("other", owned[0])}
        assert set((await db.execute(sa.select(T.conversation_id))).scalars()) == {"neighbor", "other-bot", "other-workspace"}
        assert set((await db.execute(sa.select(R.conversation_id))).scalars()) == {"neighbor", "other-bot", "other-workspace"}
        assert len((await db.execute(sa.select(M.id))).all()) == 5
        ap.rag_runtime_service.vector_delete.assert_awaited_once_with(scope, "memory-kb", filters={"user_key": "bot:group_123"})
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('remaining,total', [([{'id':'still-present'}], -1), ([], 1), ([], -2), ([], None)])
async def test_purge_keeps_fence_when_vectors_remain_or_verification_is_invalid(scope, remaining, total):
    ap = NS(persistence_mgr=NS(execute_async=AsyncMock()), rag_runtime_service=NS(
        vector_delete=AsyncMock(return_value=1), vector_list=AsyncMock(return_value=(remaining, total))))
    with pytest.raises(RuntimeError, match='could not be verified'):
        await host.purge_persistent(ap, scope, 'bot', '123', ['memory-kb'])
    ap.persistence_mgr.execute_async.assert_not_awaited()


@pytest.fixture
def reset_app(scope, monkeypatch):
    from qunling.proactive import host as bridge_host
    from langbot.pkg.core import task_boundary
    bridge = NS(queued=set(), engine=NS(_groups={}, reset_group_context=Mock()))
    runtime = NS(execution_context=scope, bot_entity=NS(uuid="bot"))
    ap = NS()
    monkeypatch.setattr(bridge_host, "_authorized_bot", AsyncMock(return_value=runtime))
    monkeypatch.setattr(bridge_host, "list_groups", AsyncMock(return_value={"groups": [{"group_id": "123"}]}))
    monkeypatch.setattr(bridge_host, "_bridge", lambda *a, **k: bridge)
    async def uow(ap, workspace, operation):
        return await operation()
    monkeypatch.setattr(task_boundary, "run_in_workspace_uow", uow)
    monkeypatch.setattr(host, "preflight", AsyncMock(return_value=["kb"]))
    monkeypatch.setattr(host, "purge_persistent", AsyncMock())
    monkeypatch.setattr(host, "purge_cached", Mock())
    return ap, scope, bridge


@pytest.mark.asyncio
async def test_lost_response_retry_does_not_clear_new_memories(reset_app):
    ap, scope, bridge = reset_app
    first = await host.reset_memory(ap, scope, "bot", request())
    second = await host.reset_memory(ap, scope, "bot", request())
    assert first == second and first["cleared"]
    host.purge_persistent.assert_awaited_once()
    host.purge_cached.assert_called_once()


@pytest.mark.asyncio
async def test_partial_failure_blocks_old_memory_and_retry_finishes(reset_app):
    ap, scope, bridge = reset_app
    ap.logger = Mock()
    host.purge_persistent.side_effect = RuntimeError("private storage detail")
    with pytest.raises(host.MemoryResetError, match="未完全清空"):
        await host.reset_memory(ap, scope, "bot", request())
    ap.logger.error.assert_called_once_with('Group memory reset failed: persistent_memory (RuntimeError)')
    assert reset.boundary(reset.scope(scope, "bot", "123"))["pending"]
    host.purge_persistent.side_effect = None
    assert (await host.reset_memory(ap, scope, "bot", request()))["cleared"]
    assert not reset.boundary(reset.scope(scope, "bot", "123"))["pending"]


@pytest.mark.asyncio
async def test_pending_only_resume_never_begins_a_new_clear(reset_app):
    ap, scope, bridge = reset_app
    key = reset.scope(scope, 'bot', '123')
    generation = reset.begin(key)['generation']
    values = {**request(), 'resume_generation': generation}
    assert (await host.reset_memory(ap, scope, 'bot', values))['cleared']
    assert (await host.reset_memory(ap, scope, 'bot', values))['cleared']
    host.purge_persistent.assert_awaited_once()
    with pytest.raises(host.MemoryResetError) as error:
        await host.reset_memory(ap, scope, 'bot', {**values, 'request_id':'b'*32})
    assert error.value.code == 'memory_reset_changed'
    reset.begin(key)
    with pytest.raises(host.MemoryResetError):
        await host.reset_memory(ap, scope, 'bot', {**values, 'request_id':'c'*32})
    host.purge_persistent.assert_awaited_once()


@pytest.mark.asyncio
async def test_http_database_scope_is_not_inherited_by_reset_task(reset_app, tmp_path):
    from langbot.pkg.persistence.mgr import PersistenceManager
    ap, scope, bridge = reset_app
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scope.db'}")
    manager = PersistenceManager(ap)
    manager.db = NS(get_engine=lambda: engine)
    ap.persistence_mgr = manager
    observed = []
    async def access_real_database(*args):
        observed.append(manager.current_scope())
        # Use the actual persistence task-owner guard, not a mocked execute.
        result = await manager.execute_async(sa.select(sa.literal(1)))
        assert result.scalar_one() == 1
    host.purge_persistent.side_effect = access_real_database
    try:
        async with manager.tenant_scope(scope.workspace_uuid):
            parent = manager.current_scope()
            result = await host.reset_memory(ap, scope, 'bot', request())
            assert result['cleared']
            assert manager.current_scope() == parent
        assert observed == [None]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_processing_group_is_rejected_before_any_deletion(reset_app):
    ap, scope, bridge = reset_app
    bridge.queued.add(("123", "message"))
    with pytest.raises(host.MemoryResetError, match="正在处理"):
        await host.reset_memory(ap, scope, "bot", request())
    assert reset.boundary(reset.scope(scope, "bot", "123")) is None
    host.purge_persistent.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_cancellation_waits_for_purge_and_duplicate_request_is_rejected(reset_app):
    ap, scope, bridge = reset_app
    entered, release = asyncio.Event(), asyncio.Event()
    async def purge(*args):
        entered.set()
        await release.wait()
    host.purge_persistent.side_effect = purge
    task = asyncio.create_task(host.reset_memory(ap, scope, "bot", request()))
    await entered.wait()
    with pytest.raises(host.MemoryResetError):
        await host.reset_memory(ap, scope, "bot", request())
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reset.completed(reset.scope(scope, "bot", "123"), "a" * 32)


@pytest.mark.asyncio
async def test_foreign_group_never_reaches_storage(reset_app):
    ap, scope, bridge = reset_app
    with pytest.raises(host.MemoryResetError):
        await host.reset_memory(ap, scope, "bot", request("456"))
    host.purge_persistent.assert_not_awaited()
