"""Concurrent same-member turns through the real scheduler and transcript store."""
import asyncio
from contextlib import suppress
from types import SimpleNamespace as NS

import pytest

from test_qq_sessions import app, conversation
from test_qq_dispatch import managed, put, until
from langbot.pkg.pipeline.controller import Controller
from langbot.pkg.pipeline.preproc.preproc import PreProcessor
from langbot.pkg.agent.runner.transcript_store import TranscriptStore
from langbot.pkg.agent.runner.query_entry_adapter import QueryEntryAdapter
from langbot.pkg.agent.runner.persistent_state_store import PersistentStateStore
from langbot.pkg.agent.runner.host_models import StatePolicy
from qunling import qq_dispatch as dispatch
from qunling.qq_sessions import (
    history_conversation_id, prepare_fork_history, assert_group_reset_idle,
)


async def ready(app, query):
    conv = await conversation(app, query)
    await prepare_fork_history(app, query, query.session, conv)
    dispatch.context_ready(query)
    return conv


@pytest.mark.asyncio
@pytest.mark.parametrize('wait_kind', ['model', 'tool'])
async def test_long_task_does_not_block_same_members_search_or_next_turn(managed, wait_kind):
    app = managed
    store = TranscriptStore(app.persistence_mgr.engine)
    gate, entered = asyncio.Event(), asyncio.Event()
    done, histories, contexts = [], {}, {}
    preproc = PreProcessor(app)
    old = await put(app, 'a', 'long file task')
    base = await conversation(app, old)
    base.uuid = 'external-conversation-must-not-be-reused'
    source = history_conversation_id(base)
    await store.append_transcript(None, 'before', source, 'user', bot_id='bot',
                                  workspace_id='workspace', content='completed background')
    await store.append_transcript(None, 'foreign', source, 'user', bot_id='bot',
                                  workspace_id='another-workspace', content='private foreign text')

    async def append(query, text):
        await store.append_transcript(None, query.query_uuid, contexts[str(query.message_chain)],
            'assistant', bot_id='bot', workspace_id='workspace', content=text)

    async def run(query):
        query.message_chain = query._qq_trigger_replacement
        label = str(query.message_chain)
        conv = await ready(app, query)
        contexts[label] = QueryEntryAdapter._build_conversation_context(query).conversation_id
        histories[label] = [m.content for m in await preproc._resolve_history_messages(
            'local-agent', conv, bot_id='bot', workspace_id='workspace')]
        if query is old:
            await append(query, 'unfinished task must stay here')
            query._qq_task.started -= 170  # reproduce the 21:50 request without sleeping minutes
            entered.set()
            if wait_kind == 'tool':
                await dispatch.invoke_tool(app, query, gate.wait, 'search_web', source='plugin')
            else:
                await gate.wait()
            await append(query, 'old task final')
        else:
            assert conv.uuid != base.uuid
            await append(query, label + ' answer')
        done.append(label)

    app.pipeline.run = run
    controller = Controller(app)
    consumer = asyncio.create_task(controller.consumer())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        search = await put(app, 'a', 'find an image')
        await until(lambda: 'find an image' in done)
        assert not old._qq_task.done.is_set()
        assert search.session is not old.session
        assert search.session._semaphore is not old.session._semaphore
        assert old.session is await app.sess_mgr.get_session(old)
        assert histories['find an image'] == ['completed background']
        assert contexts['find an image'] != contexts['long file task']
        state = PersistentStateStore(app.persistence_mgr.engine)
        binding, descriptor = NS(binding_id='binding', state_policy=StatePolicy()), NS(id='local-agent')
        def envelope(query):
            return NS(conversation_id=history_conversation_id(query.session.using_conversation),
                bot_id='bot', workspace_id='workspace', thread_id=None,
                actor=NS(actor_type='user', actor_id='a'), subject=None)
        assert await state.apply_update_from_event(envelope(old), binding, descriptor,
            'conversation', 'task.progress', {'pending': 'file task'}) == (True, None)
        assert 'task.progress' not in (await state.build_snapshot_from_event(
            envelope(search), binding, descriptor))['conversation']
        correct_session = search.session
        search.session = old.session
        with pytest.raises(ValueError, match='task'):
            QueryEntryAdapter._build_conversation_context(search)
        search.session = correct_session
        assert QueryEntryAdapter._build_delivery_context(search).reply_target['target_id'] == '30000'
        followup = await put(app, 'a', 'about that image')
        await until(lambda: 'about that image' in done)
        assert followup.session is search.session
        assert histories['about that image'] == ['completed background', 'find an image answer']
        with pytest.raises(RuntimeError, match='processing'):
            assert_group_reset_idle(app, 'workspace', 'bot', 'group_30000')
        gate.set()
        await until(lambda: old._qq_task.done.is_set())
        after = await put(app, 'a', 'later chat')
        await until(lambda: 'later chat' in done)
        assert after.session is search.session  # late old completion never restores the old lane
        assert 'old task final' not in histories['later chat']
        assert 'about that image answer' in histories['later chat']
        await asyncio.gather(*app.workers)
        assert controller.semaphore._value == 2
        assert app.query_pool.active_query_count_by_workspace == {}
        assert all(item is not old.session for item in app.sess_mgr.session_list)
        assert any(item is search.session for item in app.sess_mgr.session_list)
        assert not search._qq_task.terminal
    finally:
        gate.set()
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_overlap_keeps_member_limit_and_cancellation_is_per_request(managed):
    app = managed
    gates = {name: asyncio.Event() for name in ('first', 'second')}
    started, done = [], []

    async def run(query):
        query.message_chain = query._qq_trigger_replacement
        label = str(query.message_chain)
        await ready(app, query)
        query._qq_task.started -= 170
        started.append(label)
        if label in gates:
            await gates[label].wait()
        done.append(label)

    app.pipeline.run = run
    controller = Controller(app)
    consumer = asyncio.create_task(controller.consumer())
    try:
        first = await put(app, 'a', 'first')
        await until(lambda: 'first' in started)
        second = await put(app, 'a', 'second')
        await until(lambda: 'second' in started)
        third = await put(app, 'a', 'third')
        await asyncio.sleep(.05)
        assert 'third' not in started
        assert controller.semaphore._value == 0
        await app.dispatch.stop(second._qq_task, 'message_recalled', notify=False)
        await until(lambda: 'third' in done)
        assert not first._qq_task.done.is_set()
        assert 'second' not in done and not third._qq_task.terminal
        gates['first'].set()
        await until(lambda: first._qq_task.done.is_set())
        await asyncio.gather(*app.workers)
        assert controller.semaphore._value == 2
    finally:
        for gate in gates.values():
            gate.set()
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_new_attachment_observation_waits_for_capture_then_unblocks(managed):
    app = managed
    gate, begun = asyncio.Event(), asyncio.Event()
    observed = []

    async def observe(event, plugins):
        observed.append(str(event.message_chain))

    app.plugin_connector.emit_event.side_effect = observe

    async def run(query):
        query._qq_task.started -= 170
        begun.set()
        await gate.wait()

    app.pipeline.run = run
    controller = Controller(app)
    consumer = asyncio.create_task(controller.consumer())
    try:
        first = await put(app, 'a', 'first')
        await asyncio.wait_for(begun.wait(), 2)
        await put(app, 'a', 'new attachment', addressed=False)
        app.dispatch.background(first._qq_task)
        assert not first._qq_task.backgrounded and observed == []
        dispatch.context_ready(first)
        app.dispatch.background(first._qq_task)
        await until(lambda: observed == ['new attachment'])
        assert not first._qq_task.done.is_set()
        gate.set()
        await until(lambda: first._qq_task.done.is_set())
    finally:
        gate.set()
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_background_release_allows_natural_chat_without_completing_task():
    from test_proactive_engine import Harness, eventually
    h = Harness()
    try:
        h.engine.on_message(h.message('long', explicit_request=True))
        h.engine.on_message(h.message('chat', text='哈哈这也太慢了'))
        await asyncio.sleep(0)
        assert h.checked == []
        h.engine.explicit_backgrounded('100', 'long')
        await eventually(lambda: bool(h.sent))
        assert h.sent[0][2] == 'chat'
        assert h.engine.get_observed_message('100', 'long') is not None
        h.engine.explicit_done('100', 'long')
        assert len(h.sent) == 1
    finally:
        await h.engine.close()
