"""Natural requests cross real SDK events, admission, and the Host bridge."""
import asyncio
import copy
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import aiocqhttp

from test_qq_sessions import app
from test_qq_dispatch import managed
from qunling.proactive import host
from qunling import qq_dispatch
from qunling.proactive.config import ConfigStore, DEFAULT_GROUP
from langbot_plugin.api.entities.builtin.platform import events, message as pm, entities as pe
from langbot_plugin.api.entities.builtin.provider.session import LauncherTypes
from langbot.pkg.platform.adapters.aiocqhttp.event_converter import AiocqhttpEventConverter
from langbot.pkg.pipeline.controller import Controller


@pytest.mark.asyncio
@pytest.mark.parametrize('pipeline_uuid', ['pipeline', None])
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('action', ['task_request', 'research'])
async def test_natural_generation_from_native_event_reaches_real_task_queue(managed, tmp_path, monkeypatch, pipeline_uuid, native, action):
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    runtime = managed.adapter._runtime
    runtime.bot_entity.workspace_uuid = 'workspace'
    runtime.assert_execution_active = AsyncMock()
    managed.workspace_service.instance_uuid = 'instance'
    object.__setattr__(managed.adapter, 'bot', NS(connection=NS(closed=False)))
    store = ConfigStore('bot', tmp_path / 'proactive')
    raw = store.read()
    raw.update(enabled=True, groups={'30000': {**copy.deepcopy(DEFAULT_GROUP), 'mode': 'active'}})
    store.save(raw, known_group_ids={'30000'})
    bridge = host.HostBridge(runtime, store=store)
    runtime._qunling_proactive = bridge
    bridge.engine._debounce_scale = 0
    bridge.engine._judge = AsyncMock(return_value={'action': action, 'target_message_id': '123'})
    bridge.engine._generate = AsyncMock()
    chain = pm.MessageChain([pm.Source(id=123, time=int(time.time())), pm.Plain(text='那你重新生成一张')])
    event = events.MessageReceivedEvent(
        message_id='123', message_chain=chain, sender=pe.User(id='20000', nickname='Member'),
        group=pe.UserGroup(id='30000', name='Group'), chat_type=pe.ChatType.GROUP,
        chat_id='30000', timestamp=time.time(), bot_uuid='bot', adapter_name='onebot-v11')
    if native:
        raw_event = aiocqhttp.Event({'post_type':'message', 'message_type':'group', 'sub_type':'normal',
            'time':int(time.time()), 'self_id':10000, 'message_id':123, 'group_id':30000,
            'user_id':20000, 'message':[{'type':'text','data':{'text':'那你重新生成一张'}}],
            'sender':{'user_id':20000, 'nickname':'Member', 'card':'Member', 'role':'member'}})
        event = await AiocqhttpEventConverter.message_to_eba(raw_event)
    host.observe(runtime, event, managed.adapter)
    legacy = event.to_legacy_event()
    try:
        assert await managed.query_pool.add_query(bot_uuid='bot', launcher_type=LauncherTypes.GROUP,
            launcher_id='30000', sender_id='20000', message_event=legacy,
            message_chain=legacy.message_chain, adapter=managed.adapter,
            pipeline_uuid=pipeline_uuid, execution_context=managed.scope) is None
        await asyncio.wait_for(bridge.engine.drain(), 8)
        state = bridge.engine._groups['30000']
        assert len(managed.query_pool.queries) == 1, (state.last_event, bridge.last_error,
            list(bridge._continuation_ingress), managed.dispatch.observation_failed)
        query = managed.query_pool.queries[0]
        assert query._qq_continuation.allow_actions is (action == 'task_request')
        assert query.message_event is legacy
        assert not any(isinstance(part, pm.At) for part in query.message_chain)
        assert query.sender_id == '20000' and query.launcher_id == '30000'
        assert await qq_dispatch.acquire_slot(Controller(managed), query)
        tool = AsyncMock(return_value='result')
        assert await qq_dispatch.invoke_tool(managed, query, tool,
            'generate_image' if action == 'task_request' else 'read_group_context_images',
            source='plugin', parameters={}) == 'result'
        tool.assert_awaited_once()
        bridge.engine._generate.assert_not_awaited()
    finally:
        await bridge.engine.close()
