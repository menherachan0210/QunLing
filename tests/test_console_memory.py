import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from qunling import console_memory as memory, group_reset
from qunling.group_reset_host import MemoryResetError
from qunling import reply_preferences
import time


@pytest.mark.parametrize('value', [None, {}, {'group_id':'../123','action':'list'},
    {'group_id':'123','action':'list','scope_key':'other'}, {'group_id':'123','action':'list','offset':-1},
    {'group_id':'123','action':'delete','kind':'episode','revision':'a'*64,'id':'../id','collection_id':'kb'},
    {'group_id':'123','action':'save','kind':'profile','revision':'a'*64,'sender_id':'../user'}])
def test_invalid_memory_operations(value):
    with pytest.raises(ValueError):
        memory.validate(value)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    import qunling.proactive.host as host
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    context = NS(instance_uuid='instance', workspace_uuid='workspace')
    runtime = NS(execution_context=context)
    monkeypatch.setattr(host, '_authorized_bot', AsyncMock(return_value=runtime))
    monkeypatch.setattr(host, 'list_groups', AsyncMock(return_value={'groups':[{'group_id':'123'}]}))
    connector = NS(require_workspace_context=AsyncMock(), handle_page_api=AsyncMock(return_value={'data':{'items':[]}}))
    return NS(plugin_connector=connector), context


@pytest.mark.asyncio
async def test_only_joined_group_can_be_forwarded_with_host_scope(environment):
    ap, context = environment
    assert (await memory.manage(ap, context, 'bot', {'group_id':'123','action':'list'}))['group_id'] == '123'
    args = ap.plugin_connector.handle_page_api.call_args.args
    assert args[-1]['scope_key'] == 'bot:group_123'
    ap.plugin_connector.handle_page_api.reset_mock()
    with pytest.raises(MemoryResetError):
        await memory.manage(ap, context, 'bot', {'group_id':'456','action':'list'})
    ap.plugin_connector.handle_page_api.assert_not_called()


@pytest.mark.asyncio
async def test_interaction_preferences_visible_editable_exported_and_deleted(environment):
    ap, context = environment
    key = reply_preferences.key(context, 'bot', '123')
    row = reply_preferences.save(key, '456', mode='mention_only', rule='没@不接话', expected_revision='0',
        source_message_id='message', source_time=time.time() - 10)
    result = await memory.manage(ap, context, 'bot', {'group_id':'123', 'action':'list', 'kind':'interaction'})
    assert result['total'] == 1 and result['items'][0]['value']['mode'] == 'mention_only'
    ap.plugin_connector.handle_page_api.assert_not_called()
    result = await memory.manage(ap, context, 'bot', {'group_id':'123', 'action':'export'})
    assert result['reply_preferences'][0]['sender_id'] == '456'
    body = {'group_id':'123', 'action':'save', 'kind':'interaction', 'sender_id':'456', 'revision':row['revision'],
            'value':{'mode':'conditional', 'rule':'只有我在提问时才参与'}}
    assert (await memory.manage(ap, context, 'bot', body))['saved']
    with pytest.raises(MemoryResetError):
        await memory.manage(ap, context, 'bot', body)
    assert (await memory.manage(ap, context, 'bot', {**body, 'action':'delete',
        'revision':reply_preferences.get(key, '456')['revision']}))['deleted']
    assert reply_preferences.list_group(key) == []


@pytest.mark.asyncio
async def test_combined_memory_paging_keeps_both_sources_without_duplicates(environment):
    ap, context = environment
    key = reply_preferences.key(context, 'bot', '123')
    for member in range(100, 135):
        reply_preferences.save(key, str(member), mode='mention_only', rule='仅@', expected_revision='0',
            source_message_id='message', source_time=time.time())
    async def plugin_page(*args):
        offset = args[-1].get('offset', 0)
        return {'data':{'items':[{'id':str(i), 'kind':'episode'} for i in range(offset, min(offset+30, 45))], 'total':45}}
    ap.plugin_connector.handle_page_api.side_effect = plugin_page
    items = []
    for offset in (0, 30, 60):
        result = await memory.manage(ap, context, 'bot', {'group_id':'123', 'action':'list', 'offset':offset})
        assert result['total'] == 80
        items.extend(result['items'])
    assert len(items) == 80
    assert len({(i['kind'], i.get('id', i.get('sender_id'))) for i in items}) == 80


@pytest.mark.asyncio
async def test_pending_reset_or_reset_lock_cannot_resurrect_memory(environment):
    ap, context = environment
    key = group_reset.scope(context, 'bot', '123')
    lock = asyncio.Lock()
    ap._group_memory_reset_locks = {key:lock}
    await lock.acquire()
    with pytest.raises(MemoryResetError):
        await memory.manage(ap, context, 'bot', {'group_id':'123','action':'list'})
    lock.release()
    group_reset.begin(key)
    with pytest.raises(MemoryResetError):
        await memory.manage(ap, context, 'bot', {'group_id':'123','action':'list'})
    ap.plugin_connector.handle_page_api.assert_not_called()


@pytest.mark.asyncio
async def test_conflict_is_public_but_plugin_exception_is_not(environment):
    ap, context = environment
    ap.plugin_connector.handle_page_api.return_value = {'data':{'error':'conflict'}}
    with pytest.raises(MemoryResetError, match='已变化') as error:
        await memory.manage(ap, context, 'bot', {'group_id':'123','action':'list'})
    assert error.value.status == 409
    ap.plugin_connector.handle_page_api.return_value = {'error':'secret traceback /private/path'}
    with pytest.raises(MemoryResetError, match='暂时不可用') as error:
        await memory.manage(ap, context, 'bot', {'group_id':'123','action':'list'})
    assert 'secret' not in str(error.value)
