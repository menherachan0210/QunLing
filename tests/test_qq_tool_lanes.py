"""Real dispatch integration: slow work cannot hold up unrelated short reads."""
import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest

from test_qq_sessions import app
from test_qq_dispatch import managed, put, until
from langbot.pkg.pipeline.controller import Controller
from qunling import qq_dispatch as dispatch


@pytest.mark.asyncio
async def test_full_long_job_lane_keeps_network_and_context_queries_moving(managed):
    gate = asyncio.Event()
    completed = []
    entered = []

    async def slow():
        entered.append('image')
        await gate.wait()

    async def run(query):
        label = str(query.message_chain)
        name = 'generate_image' if 'long' in label else 'search_web' if 'search' in label else 'read_recent_group_context'
        await dispatch.invoke_tool(managed, query, slow if 'long' in label else AsyncMock(return_value='read'),
                                   name, source='plugin')
        completed.append(label)

    managed.pipeline.run = run
    controller = Controller(managed)
    consumer = asyncio.create_task(controller.consumer())
    try:
        await put(managed, 'a', 'long1')
        await put(managed, 'b', 'long2')
        await until(lambda: managed.dispatch.parked_by_lane['general'] == 2)
        await put(managed, 'c', 'search')
        await put(managed, 'd', 'context')
        await until(lambda: any('context' in value for value in completed) and any('search' in value for value in completed))
        assert len(entered) == 1  # Paid/long work still serial, not globally unbounded.
        assert not any('long' in value for value in completed)
        gate.set()
        await until(lambda: len(completed) == 4)
        await asyncio.gather(*managed.workers)
        assert managed.dispatch.parked == 0
        assert set(managed.dispatch.parked_by_lane.values()) == {0}
        assert controller.semaphore._value == 2
    finally:
        gate.set()
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
@pytest.mark.parametrize('name,params,lane', [
    ('read_recent_group_context', {}, 'quick'),
    ('search_web', {}, 'network'),
    ('generate_image', {}, 'general'),
])
async def test_cancel_queued_call_returns_only_its_own_lane_capacity(managed, name, params, lane):
    query = await put(managed, 'a', 'request')
    controller = Controller(managed)
    assert await dispatch.acquire_slot(controller, query)
    capacity = {'quick': managed.dispatch.quick_tools, 'network': managed.dispatch.network_tools,
                'general': managed.dispatch.tools}[lane]
    slots = 2 if lane == 'quick' else 1
    for _ in range(slots):
        await capacity.acquire()
    invoke = AsyncMock()
    pending = asyncio.create_task(dispatch.invoke_tool(managed, query, invoke, name, source='plugin', parameters=params))
    await until(lambda: managed.dispatch.parked_by_lane[lane] == 1)
    await managed.dispatch.stop(query._qq_task, 'cancelled', notify=False)
    result = await asyncio.gather(pending, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    invoke.assert_not_awaited()
    assert capacity._value == 0
    assert managed.dispatch.parked == 0 and set(managed.dispatch.parked_by_lane.values()) == {0}
    for _ in range(slots):
        capacity.release()
    dispatch.release_slot(controller, query)
    assert controller.semaphore._value == 2


@pytest.mark.asyncio
async def test_quick_operation_cannot_become_a_download_while_queued(managed):
    query = await put(managed, 'a', 'request')
    controller = Controller(managed)
    assert await dispatch.acquire_slot(controller, query)
    for _ in range(2):
        await managed.dispatch.quick_tools.acquire()
    params = {'action': 'list'}
    invoke = AsyncMock()
    pending = asyncio.create_task(dispatch.invoke_tool(managed, query, invoke, 'browse_group_files', source='plugin', parameters=params))
    await until(lambda: managed.dispatch.parked == 1)
    params['action'] = 'import'
    managed.dispatch.quick_tools.release()
    with pytest.raises(RuntimeError, match='operation changed'):
        await pending
    invoke.assert_not_awaited()
    assert managed.dispatch.parked == 0 and managed.dispatch.quick_tools._value == 1
    managed.dispatch.quick_tools.release()
    dispatch.release_slot(controller, query)
    assert controller.semaphore._value == 2


@pytest.mark.parametrize('name,source,params,expected', [
    ('read_web_page', 'mcp', {}, 'general'),
    ('unknown', 'plugin', {}, 'general'),
    ('browse_group_files', 'plugin', {'action': 'import'}, 'general'),
    ('file_task', 'plugin', {'action': 'status'}, 'general'),
    ('file_task', 'plugin', {'action': 'status', 'wait_seconds': 0}, 'quick'),
    ('file_task', 'plugin', {'action': 'list'}, 'quick'),
    ('file_task', 'plugin', {'action': 'cancel'}, 'general'),
])
def test_mixed_action_tools_keep_waits_and_mutations_out_of_quick_lane(name, source, params, expected):
    assert dispatch._tool_lane(name, source, params) == expected
