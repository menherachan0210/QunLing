"""Exercise the real provider bridge and authorization without sending to QQ."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock
import pytest
from langbot.pkg.api.http.authz import PermissionDeniedError
from langbot_plugin.api.entities.builtin.provider.message import Message as ModelMessage
from qunling import console_preview, group_memory
from qunling.group_reset_host import MemoryResetError
from test_proactive_host import bridge, admin_context
from test_console_preview import trial_values


@pytest.mark.asyncio
async def test_trial_uses_real_role_routing_draft_persona_and_review_without_memory_writes_or_sends(bridge,monkeypatch,tmp_path):
    monkeypatch.delenv('QUNLING_JEV_REVIEW_ENABLED',raising=False)
    provider=bridge.bot.ap.model_mgr.get_model_by_uuid.return_value.provider
    pipeline=bridge.bot.ap.pipeline_mgr.get_pipeline_by_uuid.return_value.pipeline_entity.config
    pipeline['ai'].update(qunling_decision_model='fast',qunling_reconcile_model='review')
    provider.invoke_llm.side_effect=[ModelMessage(role='assistant',content=json.dumps(v)) for v in (
        {'action':'reply','target_message_id':'trial-0'},
        {'action':'reply','text':'那就歇会儿。'},
        {'action':'keep','reason':'符合当前表达'})]
    recall=AsyncMock();monkeypatch.setattr(group_memory,'recall_group_memory',recall)
    before={p.relative_to(tmp_path):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    values=trial_values();values['config']['groups']['30000']['persona']='简洁平和，不刻意开玩笑。'
    result=await console_preview.run_trial(bridge.bot,values)
    assert result['reply']=='那就歇会儿。'
    assert values['config']['groups']['30000']['persona'] in result['persona']
    assert [c.args[1] for c in bridge.bot.ap.model_mgr.get_model_by_uuid.await_args_list]==['fast','chat-model','review']
    assert all(c.kwargs['funcs']==[] and c.kwargs['extra_args']['tools'] is None for c in provider.invoke_llm.await_args_list)
    recall.assert_not_awaited();bridge.bot.adapter.send_message.assert_not_awaited()
    assert before=={p.relative_to(tmp_path):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.asyncio
async def test_trial_admission_is_idempotent_bounded_and_immediately_cancellable(bridge,monkeypatch):
    pending=AsyncMock(side_effect=lambda *args: None)
    async def never(*args): await asyncio.Event().wait()
    monkeypatch.setattr(console_preview,'run_trial',never)
    values=trial_values();ap=bridge.bot.ap;context=admin_context()
    first=await console_preview.manage(ap,context,'bot',values)
    duplicate=await console_preview.manage(ap,context,'bot',values)
    assert first==duplicate and len(ap._qunling_console_trials)==1
    with pytest.raises(MemoryResetError): await console_preview.manage(ap,context,'bot',{**values,'request_id':'b'*32})
    changed=deepcopy(values);changed['messages'][0]['text']='different'
    with pytest.raises(ValueError): await console_preview.manage(ap,context,'bot',changed)
    result=await console_preview.manage(ap,context,'bot',{'action':'cancel','request_id':values['request_id'],'group_id':'30000'})
    assert result['status']=='cancelled'
    await asyncio.sleep(0);await asyncio.sleep(0)
    bridge.bot.adapter.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_trial_rejects_other_workspace_unjoined_group_and_read_only_user(bridge,monkeypatch):
    run=AsyncMock();monkeypatch.setattr(console_preview,'run_trial',run)
    values=trial_values();ap=bridge.bot.ap
    with pytest.raises(PermissionDeniedError): await console_preview.manage(ap,admin_context('viewer'),'bot',values)
    with pytest.raises(Exception): await console_preview.manage(ap,admin_context(workspace='foreign'),'bot',values)
    values['group_id']='30001';values['config']['groups']['30001']=deepcopy(values['config']['groups']['30000'])
    with pytest.raises(ValueError): await console_preview.manage(ap,admin_context(),'bot',values)
    run.assert_not_awaited()
