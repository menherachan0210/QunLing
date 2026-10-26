import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from qunling import console_memory as memory, group_reset
from qunling.group_reset_host import MemoryResetError


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
