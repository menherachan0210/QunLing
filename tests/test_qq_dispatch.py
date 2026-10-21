"""QQ foreground integration with real pool/controller/session/trigger stages."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest
import pytest_asyncio

from test_qq_sessions import app  # real SQLite + host fixture
from langbot_plugin.api.entities.builtin.platform import message as pm, entities as pe, events
from langbot_plugin.api.entities.builtin.provider.session import LauncherTypes
from langbot.pkg.pipeline.controller import Controller
from langbot.pkg.pipeline.resprule.resprule import GroupRespondRuleCheckStage
from langbot.pkg.pipeline.resprule.rules.atbot import AtBotRule
from langbot.pkg.pipeline.resprule.rules.prefix import PrefixRule
from langbot.pkg.pipeline.entities import ResultType
from langbot.pkg.platform.adapters.aiocqhttp.adapter import AiocqhttpAdapter
from qunling import qq_dispatch as dispatch


@pytest_asyncio.fixture
async def managed(app, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", "synthetic")
    app.adapter = AiocqhttpAdapter.model_construct(bot=None, bot_account_id="bot-account", config={})
    runtime = NS(ap=app, bot_entity=NS(adapter="onebot-v11", uuid="bot", enable=True),
                 enable=True, execution_context=app.scope, adapter=app.adapter)
    object.__setattr__(app.adapter, "_runtime", runtime)
    app.bot_service = NS(get_bot=AsyncMock(return_value={"enable": True, "adapter": "onebot-v11"}))
    app.platform_mgr = NS(get_bot_by_uuid=AsyncMock(return_value=runtime))
    object.__setattr__(app.adapter, "reply_message", AsyncMock())
    stage = GroupRespondRuleCheckStage(app)
    stage.rule_matchers = [AtBotRule(app), PrefixRule(app)]
    pipeline = NS(pipeline_entity=NS(config={"trigger": {"group-respond-rules": {"at": True, "prefix": ["/ai"]}}}),
                  bound_plugins=[], bound_mcp_servers=[], stage_containers=[NS(inst=stage)],
                  _assert_execution_active=AsyncMock())
    app.pipeline_mgr = NS(get_pipeline_by_uuid=AsyncMock(return_value=pipeline))
    app.plugin_connector = NS(emit_event=AsyncMock())
    app.pipeline = pipeline
    app.trigger = stage
    app.dispatch = dispatch._service(app)
    yield app
    await app.dispatch.close()


async def put(app, sender, text, *, addressed=True, group="30000"):
    chain = pm.MessageChain(([pm.At(target="bot-account")] if addressed else []) + [pm.Plain(text=text)])
    grp = pe.Group(id=group, name="Synthetic", permission=pe.Permission.Member)
    event = events.GroupMessage(type="GroupMessage", sender=pe.GroupMember(
        id=sender, member_name=sender, permission=pe.Permission.Member, group=grp),
        message_chain=chain, time=1609459200)
    return await app.query_pool.add_query(
        bot_uuid="bot", launcher_type=LauncherTypes.GROUP, launcher_id=group,
        sender_id=sender, message_event=event, message_chain=chain,
        adapter=app.adapter, pipeline_uuid="pipeline", execution_context=app.scope)


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


@pytest.mark.asyncio
async def test_ordinary_flood_never_displaces_explicit_and_observer_is_bounded(managed):
    app = managed
    app.query_pool.max_queries = 4
    app.query_pool.max_queries_per_workspace = 4
    gate = asyncio.Event()
    app.plugin_connector.emit_event.side_effect = lambda *a: None

    async def observe(*args):
        await gate.wait()
    app.plugin_connector.emit_event.side_effect = observe
    explicit = await put(app, "a", "hello")
    for n in range(80):
        await put(app, "b", f"ordinary {n}", addressed=False)
    assert explicit in app.query_pool.queries
    assert len(app.query_pool.queries) == 1
    assert len(app.dispatch.observations) <= app.dispatch.observation_limit
    assert app.dispatch.observation_dropped > 0
    assert app.query_pool.get_dropped_query_count(app.scope) == 0
    assert app.adapter.reply_message.await_count == 0
    # A full foreground queue rejects the newcomer with an honest response.
    for sender in ("c", "d", "e"):
        await put(app, sender, "request")
    assert await put(app, "f", "request") is None
    assert explicit in app.query_pool.queries
    assert "排满" in str(app.adapter.reply_message.call_args.kwargs["message"])
    gate.set()


@pytest.mark.asyncio
async def test_two_real_tool_waits_allow_short_request_and_same_sender_stays_ordered(managed):
    app = managed
    gate = asyncio.Event()
    completed, started = [], []

    async def slow():
        await gate.wait()
        return "done"

    async def run(query):
        query.pipeline_config = app.pipeline.pipeline_entity.config
        assert (await app.trigger.process(query, "trigger")).result_type == ResultType.CONTINUE
        label = str(query.message_chain)
        started.append(label)
        if label in {"A1", "B1"}:
            await dispatch.invoke_tool(app, query, slow, "search_web")
        completed.append(label)

    app.pipeline.run = run
    controller = Controller(app)
    consumer = asyncio.create_task(controller.consumer())
    try:
        a = await put(app, "a", "A1")
        b = await put(app, "b", "B1")
        await until(lambda: app.dispatch.parked == 2)
        await put(app, "a", "A2")
        await put(app, "c", "C1")
        await until(lambda: "C1" in completed)
        assert "A2" not in started and "A1" not in completed
        await until(lambda: controller.semaphore._value == 2)
        assert controller.semaphore._value == 2
        assert a._qq_task.owner != b._qq_task.owner
        gate.set()
        await until(lambda: len(completed) == 4)
        await asyncio.gather(*app.workers)
        assert completed.index("A1") < completed.index("A2")
        assert app.dispatch.parked == 0 and controller.semaphore._value == 2
        assert app.query_pool.active_query_count_by_workspace == {}
    finally:
        gate.set()
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_status_cancel_bypass_busy_member_and_never_cancel_other_member(managed):
    app = managed
    gate = asyncio.Event()
    async def run(query):
        await gate.wait()
    app.pipeline.run = run
    controller = Controller(app)
    consumer = asyncio.create_task(controller.consumer())
    try:
        a, b = await put(app, "a", "long"), await put(app, "b", "other")
        await until(lambda: a._qq_task.runner and b._qq_task.runner)
        assert await put(app, "a", "任务状态") is None
        assert "处理中" in str(app.adapter.reply_message.call_args.kwargs["message"])
        await put(app, "a", "取消任务")
        await until(lambda: a._qq_task.runner.done())
        assert not b._qq_task.done.is_set()
        assert a._qq_task.terminal == "cancelled"
        assert controller.semaphore._value == 1
        gate.set()
        await until(lambda: b._qq_task.done.is_set())
        await asyncio.gather(*app.workers)
        assert controller.semaphore._value == 2
    finally:
        gate.set()
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_queue_timeout_not_silent_and_never_executes_later(managed):
    app = managed
    app.dispatch.queue_timeout = .04
    app.dispatch.feedback_after = .01
    query = await put(app, "a", "pending")
    await until(lambda: query._qq_task.done.is_set())
    await until(lambda: "terminal" in query._qq_task.notices)
    assert query not in app.query_pool.queries
    assert query._qq_task.runner is None
    assert "排队超时" in str(app.adapter.reply_message.call_args.kwargs["message"])


@pytest.mark.asyncio
async def test_execution_timeout_releases_slot_and_reports_once(managed):
    app = managed
    app.dispatch.active_timeout = .04
    app.dispatch.feedback_after = .01
    app.pipeline.run = AsyncMock(side_effect=lambda query: None)
    async def run(query):
        await asyncio.Event().wait()
    app.pipeline.run = run
    controller = Controller(app)
    consumer = asyncio.create_task(controller.consumer())
    try:
        query = await put(app, "a", "slow")
        await until(lambda: query._qq_task.done.is_set())
        await until(lambda: "terminal" in query._qq_task.notices)
        await asyncio.gather(*app.workers)
        assert controller.semaphore._value == 2
        assert query._qq_task.terminal == "execution_timeout"
        assert app.adapter.reply_message.await_count == 2  # one wait + one terminal
    finally:
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_observation_order_preserves_old_attachment_and_defers_newer(managed):
    app = managed
    observed, captured = [], []
    block = asyncio.Event()
    async def observe(event, plugins):
        observed.append(str(event.message_chain))
    app.plugin_connector.emit_event.side_effect = observe
    async def run(query):
        captured.extend(observed)
        await block.wait()
    app.pipeline.run = run
    await put(app, "a", "old attachment", addressed=False)
    task = await put(app, "a", "translate")
    await put(app, "a", "new attachment", addressed=False)
    assert task._qq_task.owner == dispatch._scope(app.dispatch.observations[-1][0])
    assert task._qq_task.sequence < app.dispatch.observations[-1][0]._qq_receive_sequence
    controller = Controller(app)
    consumer = asyncio.create_task(controller.consumer())
    try:
        await until(lambda: bool(captured))
        assert captured == ["old attachment"]
        assert "new attachment" not in observed
        block.set()
        await until(lambda: task._qq_task.done.is_set() and len(observed) == 2)
        assert observed == ["old attachment", "new attachment"]
    finally:
        block.set()
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_prefix_and_random_policy_are_evaluated_once(managed, monkeypatch):
    app = managed
    query = await put(app, "a", "/ai test", addressed=False)
    query.pipeline_config = app.pipeline.pipeline_entity.config
    app.trigger.rule_matchers = []  # a second evaluation would now fail
    assert (await app.trigger.process(query, "trigger")).result_type == ResultType.CONTINUE
    assert str(query.message_chain).strip() == "test"
    import importlib
    random_rule = importlib.import_module("langbot.pkg.pipeline.resprule.rules.random")
    draw = Mock(return_value=.1)
    monkeypatch.setattr(random_rule.random, "random", draw)
    app.trigger.rule_matchers = [random_rule.RandomRespRule(app)]
    app.pipeline.pipeline_entity.config["trigger"]["group-respond-rules"]["random"] = .5
    query = await put(app, "b", "random", addressed=False)
    query.pipeline_config = app.pipeline.pipeline_entity.config
    await app.trigger.process(query, "trigger")
    assert draw.call_count == 1


@pytest.mark.asyncio
async def test_config_failure_does_not_spam_ordinary_group_traffic(managed):
    app = managed
    app.pipeline_mgr.get_pipeline_by_uuid.side_effect = RuntimeError("synthetic unavailable")
    for n in range(8):
        await put(app, "a", str(n), addressed=False)
    assert app.adapter.reply_message.await_count == 0
    assert app.dispatch.admission_failed == 8
    await put(app, "a", "explicit")
    assert app.adapter.reply_message.await_count == 1


@pytest.mark.asyncio
async def test_disabled_or_revoked_bot_cannot_send_task_notices(managed):
    app = managed
    query = await put(app, "a", "hello")
    app.adapter._runtime.enable = False
    await app.dispatch.notice(query, "not delivered")
    assert app.adapter.reply_message.await_count == 0
    app.adapter._runtime.enable = True
    app.bot_service.get_bot.return_value["enable"] = False
    await app.dispatch.notice(query, "not delivered")
    assert app.adapter.reply_message.await_count == 0


@pytest.mark.asyncio
async def test_cancelled_rpc_and_rebuilt_query_never_execute_tool(managed):
    app = managed
    query = await put(app, "a", "hello")
    await app.dispatch.stop(query._qq_task, "cancelled")
    invoke = AsyncMock()
    with pytest.raises(RuntimeError, match="finished|cancelled"):
        await dispatch.invoke_tool(app, query, invoke)
    rebuilt = query.model_copy()
    object.__delattr__(rebuilt, "_qq_task")
    with pytest.raises(RuntimeError, match="finished|cancelled"):
        await dispatch.invoke_tool(app, rebuilt, invoke)
    invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_parked_rpc_restores_all_capacity(managed):
    app = managed
    started = asyncio.Event()
    child = None
    async def slow():
        started.set()
        await asyncio.Event().wait()
    async def run(query):
        nonlocal child
        child = asyncio.create_task(dispatch.invoke_tool(app, query, slow))
        await child
    app.pipeline.run = run
    controller = Controller(app)
    consumer = asyncio.create_task(controller.consumer())
    try:
        query = await put(app, "a", "long")
        await asyncio.wait_for(started.wait(), 2)
        await put(app, "a", "取消任务")
        await until(lambda: query._qq_task.runner.done() and child.done())
        assert controller.semaphore._value == 2
        assert app.dispatch.parked == 0 and app.dispatch.tools._value == 1
        assert not app.dispatch.tasks
    finally:
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_early_controller_failure_settles_task_without_later_timeout(managed):
    app = managed
    query = await put(app, "a", "hello")
    app.pipeline_mgr.get_pipeline_by_uuid.return_value = None
    controller = Controller(app)
    session = await app.sess_mgr.get_session(query)
    await session._semaphore.acquire()
    await dispatch.acquire_slot(controller, query)
    await controller._process_query(query, selected_session=session, global_slot_reserved=True)
    assert query._qq_task.done.is_set() and query._qq_task.terminal == "failed"
    assert not app.dispatch.tasks
    assert controller.semaphore._value == 2
    assert app.adapter.reply_message.await_count == 1


@pytest.mark.asyncio
async def test_slow_session_lookup_does_not_hold_admission_or_cancel_mutex(managed):
    app = managed
    query = await put(app, "a", "slow db")
    entered = asyncio.Event()
    async def lookup(query):
        entered.set()
        await asyncio.Event().wait()
    app.sess_mgr.get_session = lookup
    controller = Controller(app)
    selection = asyncio.create_task(dispatch.next_query(controller))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # Neither admission nor cancelling needs to wait for that database call.
        async with asyncio.timeout(.3):
            assert await put(app, "b", "another") is not None
            await put(app, "a", "取消任务")
        assert query._qq_task.done.is_set()
        assert not app.query_pool.pool_lock.locked()
    finally:
        selection.cancel()
        with suppress(asyncio.CancelledError):
            await selection


@pytest.mark.asyncio
async def test_slow_trigger_preparation_cannot_let_later_attachment_overtake(managed):
    app = managed
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def pipeline_lookup(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return app.pipeline
    app.pipeline_mgr.get_pipeline_by_uuid.side_effect = pipeline_lookup
    preparing = asyncio.create_task(put(app, "a", "use previous attachment"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await put(app, "a", "new attachment", addressed=False)
        await asyncio.sleep(.02)
        app.plugin_connector.emit_event.assert_not_awaited()
        release.set()
        query = await preparing
        await asyncio.sleep(.02)
        app.plugin_connector.emit_event.assert_not_awaited()
        await app.dispatch.stop(query._qq_task, "cancelled")
        await until(lambda: app.plugin_connector.emit_event.await_count == 1)
        assert not app.dispatch.preparing
    finally:
        release.set()
        await preparing
