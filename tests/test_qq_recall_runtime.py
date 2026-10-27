import asyncio
from contextlib import suppress
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import aiocqhttp
import pytest

from test_qq_recall import adapter, notice, query
from test_qq_dispatch import app, managed, put, until
from test_proactive_engine import Harness
from qunling import qq_recall as recall, qq_dispatch as dispatch
from langbot.pkg.platform.adapters.aiocqhttp.transport import OneBotConnection
from langbot.pkg.platform.adapters.aiocqhttp.adapter import AiocqhttpAdapter
from langbot.pkg.platform.adapters.aiocqhttp.message_converter import AiocqhttpMessageConverter
from langbot.pkg.pipeline.aggregator import MessageAggregator, PendingMessage, SessionBuffer
from langbot.pkg.api.http.context import ExecutionContext
from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain
from langbot_plugin.api.entities.builtin.provider.session import LauncherTypes


@pytest.mark.asyncio
async def test_socket_receives_recall_while_ordinary_event_consumer_is_blocked(adapter):
    import json
    frames = asyncio.Queue()
    processing, release = asyncio.Event(), asyncio.Event()
    async def consume(data):
        processing.set()
        await release.wait()
    connection = OneBotConnection(AsyncMock(), frames.get, AsyncMock(), consume, AsyncMock(),
        self_id=adapter.bot_account_id, on_received=lambda value: recall.observe_native(adapter, value))
    task = asyncio.create_task(connection.run())
    try:
        await frames.put(json.dumps({'post_type': 'message', 'self_id': adapter.bot_account_id}))
        await processing.wait()
        await frames.put(json.dumps(notice(adapter)))
        await until(lambda: recall.query_recalled(query(adapter)))
        assert not release.is_set()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_actual_adapter_wires_fast_recall_before_event_conversion(adapter):
    import json
    from langbot.pkg.platform.adapters.aiocqhttp.transport import OneBotClient
    frames = asyncio.Queue()
    entered = asyncio.Event()
    real = AiocqhttpAdapter.model_construct(bot=OneBotClient(), bot_account_id='10000', config={'self_id': '10000'})
    runtime = adapter._runtime
    runtime.adapter, runtime.workspace_uuid = real, 'workspace'
    object.__setattr__(real, '_runtime', runtime)
    object.__setattr__(real, '_validate_connection', AsyncMock())
    async def process(native):
        entered.set()
        await asyncio.Event().wait()
    object.__setattr__(real, '_handle_native_event', process)
    sent = AsyncMock()
    task = asyncio.create_task(real.serve_connection(sent, frames.get, AsyncMock(), self_id='10000'))
    try:
        await frames.put(json.dumps({'post_type': 'message', 'message_type': 'group', 'self_id': 10000,
            'message_id': -123, 'group_id': 30000, 'user_id': 20000}))
        await entered.wait()
        await frames.put(json.dumps(notice(real)))
        await until(lambda: recall.query_recalled(query(real)))
        sent.assert_not_awaited()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_final_socket_gate_rechecks_after_send_lock_wait(adapter):
    sent = AsyncMock()
    locked, resume = asyncio.Event(), asyncio.Event()
    connection = OneBotConnection(sent, AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(),
        before_action=lambda action, params: recall.before_action(adapter, action, params))
    await connection.send_lock.acquire()
    async def sender():
        q = query(adapter)
        with recall.protect(adapter, recall.query_keys(q)):
            locked.set()
            # A child task inherits reply provenance even if its parent swallows cancellation.
            child = asyncio.create_task(connection.call_action('send_group_msg', group_id=30000, message='old draft'))
            try:
                await resume.wait()
            except asyncio.CancelledError:
                await resume.wait()
            with pytest.raises(recall.MessageRecalled):
                await child
    task = asyncio.create_task(sender())
    await locked.wait()
    await until(lambda: bool(connection.pending))
    recall.observe_native(adapter, notice(adapter))
    await asyncio.sleep(0)
    connection.send_lock.release()
    resume.set()
    with pytest.raises(recall.MessageRecalled):
        await task
    sent.assert_not_awaited()
    assert not connection.pending


@pytest.mark.asyncio
@pytest.mark.parametrize('quote', [False, True])
async def test_real_adapter_cancels_during_message_conversion(adapter, monkeypatch, quote):
    real = AiocqhttpAdapter.model_construct(bot=NS(send=AsyncMock()), bot_account_id='10000', config={})
    runtime = adapter._runtime
    runtime.adapter = real
    object.__setattr__(real, '_runtime', runtime)
    entered = asyncio.Event()
    async def convert(message):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(AiocqhttpMessageConverter, 'yiri2target', convert)
    native = aiocqhttp.Event.from_payload({'post_type': 'message', 'message_type': 'group',
        'self_id': 10000, 'user_id': 20000, 'group_id': 30000, 'message_id': -123})
    source = NS(message_id=-123, source_platform_object=native, sender=NS(id='20000'), group=NS(id='30000'))
    task = asyncio.create_task(real.reply_message(source, MessageChain([Plain(text='old draft')]), quote_origin=quote))
    await entered.wait()
    recall.observe_native(real, notice(real))
    with pytest.raises(asyncio.CancelledError):
        await task
    real.bot.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_request_is_removed_silently_and_next_message_is_admitted(managed):
    q = await put(managed, '20000', 'same question')
    object.__setattr__(q.message_event, 'message_id', '-123')
    recall.observe_native(managed.adapter, notice(managed.adapter))
    await until(lambda: q._qq_task.done.is_set() and q.query_uuid not in managed.dispatch.tasks)
    assert q._qq_task.terminal == 'message_recalled'
    store, request_id = q._request_journal
    row = store.listing(managed.scope.workspace_uuid, 'bot', request_id=request_id)['requests'][0]
    assert row['status'] == 'cancelled' and row['reason'] == 'message_recalled'
    assert not await dispatch.delivery_allowed(q)
    managed.adapter.reply_message.assert_not_awaited()
    fresh = await put(managed, '20000', 'same question')
    assert fresh is not None and not fresh._qq_task.done.is_set()


@pytest.mark.asyncio
async def test_running_pipeline_recall_does_not_emit_error_or_cancel_notice(managed, monkeypatch):
    from qunling import runtime_health
    monkeypatch.setattr(runtime_health, 'wait_available', AsyncMock())
    started = asyncio.Event()
    async def generate(q):
        started.set()
        await asyncio.Event().wait()
    q = await put(managed, '20000', 'reply after model returns')
    object.__setattr__(q.message_event, 'message_id', '-123')
    task = asyncio.create_task(dispatch.run_pipeline(managed, q, NS(run=generate)))
    await started.wait()
    recall.observe_native(managed.adapter, notice(managed.adapter))
    await task
    await until(lambda: not recall._state(managed.adapter).cleanup)
    assert q._qq_task.done.is_set()
    assert q.query_uuid not in managed.dispatch.tasks
    managed.adapter.reply_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_free_reply_drops_recalled_target_and_keeps_other_members():
    h = Harness()
    entered, resume = asyncio.Event(), asyncio.Event()
    async def generate(ctx, decision):
        entered.set()
        await resume.wait()
        return 'draft'
    h.engine._generate = generate
    h.engine.on_message(h.message('old'))
    await entered.wait()
    h.engine.retract('100', 'old')
    resume.set()
    await h.engine.drain()
    assert not h.sent
    assert not h.engine.snapshot('100')
    h.engine.on_message(h.message('new', sender_id='another'))
    await h.engine.drain()
    assert h.sent[-1][-1] == 'new'
    await h.engine.close()


@pytest.mark.asyncio
async def test_shared_draft_drops_a_recalled_secondary_source():
    from test_proactive_topics import discussion
    h = Harness()
    entered, resume = asyncio.Event(), asyncio.Event()
    async def judge(ctx):
        return {'action': 'reply', 'target_message_id': 'detail', 'covered_message_ids': ['idea', 'detail']}
    async def generate(ctx, decision):
        entered.set()
        await resume.wait()
        return 'draft using both messages'
    h.engine._judge, h.engine._generate = judge, generate
    for message in discussion(h)[:2]:
        h.engine.on_message(message)
    await entered.wait()
    h.engine.retract('100', 'idea')
    resume.set()
    await h.engine.drain()
    assert not h.sent
    assert 'idea' not in {message.message_id for message in h.engine.snapshot('100')}
    await h.engine.close()


@pytest.mark.asyncio
async def test_aggregation_filters_withdrawn_segment_and_retains_live_siblings(adapter):
    scope = ExecutionContext(instance_uuid='instance', workspace_uuid='workspace', placement_generation=1, bot_uuid='bot')
    ap = NS(instance_config=NS(data={}), workspace_service=NS(get_execution_binding=AsyncMock(return_value=scope)),
            query_pool=NS(add_query=AsyncMock()))
    aggregator = MessageAggregator(ap)
    def pending(mid):
        return PendingMessage(scope, 'bot', LauncherTypes.GROUP, '30000', '20000',
            NS(message_id=mid, group=NS(id='30000')), MessageChain([Plain(text=mid)]), adapter, 'pipeline')
    messages = [pending('first'), pending('second')]
    merged = aggregator._merge_messages(messages)
    assert merged.variables[recall.SOURCE_IDS] == ['first', 'second']
    key = aggregator._get_aggregation_key(scope, 'bot', LauncherTypes.GROUP, '30000', 'pipeline')
    aggregator.buffers[key] = SessionBuffer(key, scope, messages)
    recall.observe_native(adapter, notice(adapter, mid='first'))
    await aggregator._flush_buffer(key, scope)
    admitted = ap.query_pool.add_query.await_args.kwargs
    assert admitted['message_event'].message_id == 'second'
    assert str(admitted['message_chain']) == str(messages[1].message_chain)
