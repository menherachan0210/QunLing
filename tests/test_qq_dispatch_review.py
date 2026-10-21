"""Independent dispatch regressions using real host Query/pool fixtures."""
import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from test_qq_sessions import app
from test_qq_dispatch import managed, put, until
from langbot.pkg.pipeline.controller import Controller
from langbot.pkg.plugin.handler import _resolve_action_query
from langbot.pkg.provider.tools.toolmgr import ToolManager
from langbot_plugin.entities.io.context import ActionContext
from qunling import qq_dispatch as dispatch


@pytest.mark.asyncio
async def test_review_missing_pipeline_immediately_finishes_registered_task(managed):
    app = managed
    query = await put(app, "a", "request")
    app.pipeline_mgr.get_pipeline_by_uuid.return_value = None
    controller = Controller(app)
    consumer = asyncio.create_task(controller.consumer())
    try:
        await until(lambda: bool(app.workers) and all(task.done() for task in app.workers))
        assert query._qq_task.done.is_set()
        assert query.query_uuid not in app.dispatch.tasks
        assert controller.semaphore._value == 2
    finally:
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_review_cancelled_trusted_query_cannot_execute_late_authorized_rpc(managed):
    app = managed
    query = await put(app, "a", "request")
    await app.dispatch.stop(query._qq_task, "cancelled", notify=False)
    tool = AsyncMock(return_value="external side effect")
    with pytest.raises(RuntimeError, match="active|cancel|finished|complete|closed"):
        await dispatch.invoke_tool(app, query, tool, "send_attachment")
    tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_cancelled_uuid_cannot_execute_from_reconstructed_trusted_view(managed):
    app = managed
    query = await put(app, "a", "request")
    # The SDK serializes query_uuid but does not serialize the host-only Task.
    view = type(query).model_validate(query.model_dump())
    object.__setattr__(view, "_execution_context", query._execution_context)
    assert view.query_uuid == query.query_uuid and not dispatch.protected(view)
    await app.dispatch.stop(query._qq_task, "cancelled", notify=False)
    tool = AsyncMock(return_value="external side effect")
    with pytest.raises(RuntimeError, match="active|cancel|finished|complete|closed"):
        await dispatch.invoke_tool(app, view, tool, "send_attachment")
    tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_cancel_while_tool_waits_to_resume_restores_capacity(managed):
    app = managed
    query = await put(app, "a", "request")
    controller = Controller(app)
    assert await dispatch.acquire_slot(controller, query)
    tool_entered, finish_tool = asyncio.Event(), asyncio.Event()

    async def tool():
        tool_entered.set()
        await finish_tool.wait()
        return "complete"

    invocation = asyncio.create_task(dispatch.invoke_tool(app, query, tool, "search_web"))
    await tool_entered.wait()
    # Both foreground slots now belong to unrelated short requests.
    await controller.semaphore.acquire()
    await controller.semaphore.acquire()
    finish_tool.set()
    await until(lambda: query._qq_task.phase == "resuming")
    await app.dispatch.stop(query._qq_task, "cancelled", notify=False)
    result = await asyncio.gather(invocation, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert app.dispatch.parked == 0
    assert app.dispatch.tools._value == 1
    assert not query._qq_task.owns_slot
    assert controller.semaphore._value == 0
    controller.semaphore.release()
    controller.semaphore.release()
    dispatch.release_slot(controller, query)
    assert controller.semaphore._value == 2


@pytest.mark.asyncio
async def test_review_sdk_tool_resolution_uses_same_registered_query_and_parks_its_lease(managed):
    app = managed
    query = await put(app, "alice", "tool request")
    other = await put(app, "bob", "other request")
    context = ActionContext.model_construct(instance_uuid=app.scope.instance_uuid,
        workspace_uuid=app.scope.workspace_uuid, placement_generation=app.scope.placement_generation)
    # Runner host session wins over the client payload's unrelated query_id.
    resolved = _resolve_action_query({"query_id": other.query_id},
                                    {"execution_query": query, "query_id": query.query_id}, app, context)
    assert resolved is query
    assert resolved.query_uuid == query.query_uuid
    assert dispatch._scope(resolved) == query._qq_task.owner
    controller = Controller(app)
    assert await dispatch.acquire_slot(controller, query)
    manager = object.__new__(ToolManager)
    manager.ap = app
    manager._record_tool_call = AsyncMock()
    manager._trace_tool_call = Mock()

    async def actual_tool():
        assert query._qq_task.phase == "tool"
        assert not query._qq_task.owns_slot
        assert controller.semaphore._value == 2
        return "result"

    result = await manager._invoke_tool_with_monitoring(source="plugin", name="read_text_attachment",
        parameters={}, query=resolved, invoke=actual_tool)
    assert result == "result"
    assert query._qq_task.owns_slot and controller.semaphore._value == 1
    assert app.dispatch.parked == 0 and app.dispatch.tools._value == 1
    assert manager._record_tool_call.call_args.kwargs["query"] is query
    dispatch.release_slot(controller, query)
    assert controller.semaphore._value == 2


@pytest.mark.asyncio
async def test_review_cancel_while_tool_waits_for_global_tool_slot_restores_only_own_lease(managed):
    app = managed
    query = await put(app, "a", "request")
    controller = Controller(app)
    assert await dispatch.acquire_slot(controller, query)
    await app.dispatch.tools.acquire()  # held by a different request
    tool = AsyncMock()
    invocation = asyncio.create_task(dispatch.invoke_tool(app, query, tool, "search_web"))
    await until(lambda: app.dispatch.parked == 1)
    assert controller.semaphore._value == 2
    await app.dispatch.stop(query._qq_task, "cancelled", notify=False)
    result = await asyncio.gather(invocation, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    tool.assert_not_awaited()
    assert app.dispatch.parked == 0 and app.dispatch.tools._value == 0
    app.dispatch.tools.release()
    dispatch.release_slot(controller, query)
    assert controller.semaphore._value == 2


@pytest.mark.asyncio
async def test_review_slow_session_lookup_does_not_hold_admission_or_cancellation_mutex(managed):
    app = managed
    query = await put(app, "a", "first")
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.sess_mgr.get_session

    async def delayed_lookup(current):
        if current is query:
            entered.set()
            await release.wait()
        return await original(current)

    app.sess_mgr.get_session = delayed_lookup
    app.pipeline.run = AsyncMock()
    controller = Controller(app)
    consumer = asyncio.create_task(controller.consumer())
    try:
        await entered.wait()
        other = await asyncio.wait_for(put(app, "b", "second"), .5)
        assert other.query_uuid in app.dispatch.tasks
        await asyncio.wait_for(app.dispatch.stop(query._qq_task, "cancelled", notify=False), .5)
        assert query._qq_task.done.is_set() and query not in app.query_pool.queries
        release.set()
        await until(lambda: other._qq_task.done.is_set())
        assert all(call.args[0] is not query for call in app.pipeline.run.call_args_list)
    finally:
        release.set()
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer
