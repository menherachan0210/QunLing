"""Fault injection at request, plugin and operator boundaries; no live QQ I/O."""
import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from test_qq_sessions import app
from test_qq_dispatch import managed, put, until
from langbot.pkg.pipeline.controller import Controller
from langbot.pkg.provider.tools.loaders.plugin import PluginToolLoader
from qunling import request_journal as journal, request_recovery as recovery
from qunling import qq_dispatch as dispatch


def query(key):
    return NS(query_uuid=key, message_event=NS(message_id='10001'), _request_phase='tool')


OWNER = ('instance', 'workspace', 1, 'bot', '30000', '20000')


def test_crash_recovery_preserves_completed_and_never_replays(tmp_path):
    path = tmp_path/'journal.db'
    store = journal.Journal(path, 'instance', boot_id='first')
    for key in ('queued', 'model', 'tool', 'sent'):
        store.start(query(key), OWNER)
    store.record('queued', 'queued')
    store.record('model', 'model')
    store.record('tool', 'tool', uncertain=True)
    store.record('sent', 'ended', status='completed')
    assert sum(r['status']=='active' for r in journal.Journal(path,'instance',boot_id='first').listing('workspace','bot')['requests']) == 3
    restarted = journal.Journal(path, 'instance', boot_id='second')
    rows = {r['request_id']:r for r in restarted.listing('workspace','bot')['requests']}
    assert rows['queued']['reason'] == 'restart_before_execution'
    assert rows['model']['reason'] == 'restart_interrupted'
    assert rows['tool']['status'] == 'unconfirmed'
    assert rows['sent']['status'] == 'completed'
    # Late completions from the old process cannot revive or overwrite a task.
    store.record('tool','ended',status='completed')
    assert restarted.listing('workspace','bot',request_id='tool')['requests'][0]['status']=='unconfirmed'


def test_scope_pagination_retention_and_metadata_only(tmp_path, monkeypatch):
    clock = [1000.0]
    store = journal.Journal(tmp_path/'journal.db','instance',clock=lambda:clock[0])
    for key, owner in [('a',OWNER),('b',OWNER),('c',OWNER),('foreign',('instance','other',1,'bot','30000','20000'))]:
        store.start(query(key),owner)
        store.record(key,'ended',status='completed')
    first = store.listing('workspace','bot',limit=2)
    second = store.listing('workspace','bot',limit=2,before=first['next_before'])
    assert {x['request_id'] for x in first['requests']+second['requests']} == {'a','b','c'}
    assert second['next_before'] is None
    assert store.listing('workspace','foreign')['requests']==[]
    q = query('privacy'); store.start(q,OWNER); q._request_journal=(store,'privacy')
    journal.failure(q, TimeoutError('password=private-key; private-chat'))
    detail=store.listing('workspace','bot',request_id='privacy')
    assert 'private-key' not in json.dumps(detail) and 'private-chat' not in json.dumps(detail)
    assert detail['requests'][0]['events'][-1]['reason']=='tool_timeout'
    for _ in range(110): store.record('privacy','running')
    assert len(store.listing('workspace','bot',request_id='privacy')['requests'][0]['events']) == journal.MAX_EVENTS
    clock[0] += journal.KEEP_SECONDS+1
    assert [r['request_id'] for r in store.listing('workspace','bot')['requests']]==['privacy']
    # Bound retained completions even if nothing is old enough to expire.
    monkeypatch.setattr(journal,'MAX_RECORDS',2)
    for key in ('d','e','f'):
        store.start(query(key),OWNER);store.record(key,'ended',status='completed')
    assert len(store.listing('workspace','bot')['requests'])==3


@pytest.mark.parametrize('filters', [{'group':'1 OR 1=1'}, {'status':'x'}, {'before':'nan:a'}, {'before':'42'}, {'limit':'0'}, {'workspace':'other'}])
def test_invalid_filters_cannot_change_scope(filters):
    with pytest.raises(ValueError): journal.parse_filters(filters)


def test_ingress_failures_before_query_identity_are_individually_recorded(tmp_path):
    store=journal.Journal(tmp_path/'journal.db','instance')
    first=store.start(query(None),OWNER)
    second=store.start(query(None),OWNER)
    assert first!=second
    assert len(store.listing('workspace','bot')['requests'])==2


def test_wrapped_runner_failure_keeps_underlying_classification():
    from langbot.pkg.agent.runner.errors import RunnerExecutionError, RunnerNotAuthorizedError
    error=RunnerExecutionError('runner','private-error',error_code='runner.timeout')
    assert journal.classify(error)=='upstream_timeout'
    assert journal.classify(RunnerNotAuthorizedError('runner',[]))=='permission'
    wrapped=RunnerExecutionError('runner','private-error')
    wrapped.__cause__=ConnectionError('sensitive-url')
    assert journal.classify(wrapped)=='network'
    unknown=RuntimeError('sensitive-key')
    unknown.__cause__=unknown
    assert journal.classify(unknown)=='pipeline_failed'


@pytest.mark.asyncio
async def test_operator_permission_and_bot_scope_checked_before_opening_store(tmp_path,monkeypatch):
    monkeypatch.setenv('QUNLING_DATA_DIR',str(tmp_path))
    ap = NS(bot_service=NS(get_bot=AsyncMock(return_value={'adapter':'onebot-v11'})),
            workspace_service=NS(get_execution_binding=AsyncMock(return_value=NS(instance_uuid='instance'))))
    context=NS(workspace_uuid='workspace',workspace=NS(permissions=frozenset()))
    with pytest.raises(Exception) as denied: await journal.diagnostics(ap,context,'bot',{})
    assert type(denied.value).__name__=='PermissionDeniedError'
    ap.bot_service.get_bot.assert_not_awaited()
    context.workspace.permissions=frozenset({'resource.manage'})
    store=journal.get_journal(ap,'instance')
    store.start(query('own'),OWNER)
    store.start(query('other'),('instance','foreign',1,'bot','30000','20000'))
    result=await journal.diagnostics(ap,context,'bot',{})
    assert [r['request_id'] for r in result['requests']]==['own']
    ap.bot_service.get_bot.assert_awaited_with(context,'bot',include_secret=False)
    ap.bot_service.get_bot.return_value=None
    with pytest.raises(PermissionError): await journal.diagnostics(ap,context,'other',{})


@pytest.fixture
def plugin_boundary(monkeypatch):
    monkeypatch.setattr(recovery,'RETRY_DELAY',.001)
    monkeypatch.setattr(recovery,'ATTEMPT_TIMEOUT',.05)
    auth=AsyncMock()
    monkeypatch.setattr(dispatch,'_authorized_notice',auth)
    ap=NS(plugin_connector=NS(call_tool=AsyncMock()),logger=Mock())
    q=query('retry');q._qq_task=NS(done=asyncio.Event());q.query_id=7;q.session=NS()
    return ap,q,auth,PluginToolLoader(ap)


@pytest.mark.asyncio
async def test_real_loader_retries_exact_read_once_and_checks_permission_each_time(plugin_boundary):
    ap,q,auth,loader=plugin_boundary
    ap.plugin_connector.call_tool.side_effect=[TimeoutError('secret'),{'ok':True}]
    result=await loader.invoke_tool('read_recent_group_context',{},q,source_id='local/QQGroupContext')
    assert result=={'ok':True}
    assert auth.await_count==ap.plugin_connector.call_tool.await_count==2
    assert all(c.kwargs['bound_plugins']==['local/QQGroupContext'] for c in ap.plugin_connector.call_tool.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('source,name', [('local/AIImagePlugin','generate_image'),('other/QQGroupContext','read_recent_group_context'),(None,'read_recent_group_context'),('local/QQWebSearch','search_web')])
async def test_ambiguous_writes_unknown_sources_and_model_search_are_never_replayed(plugin_boundary,source,name):
    ap,q,auth,loader=plugin_boundary
    ap.plugin_connector.call_tool.side_effect=TimeoutError('synthetic')
    result=await loader.invoke_tool(name,{},q,source_id=source)
    assert isinstance(result,str) and 'error occurred' in result
    assert ap.plugin_connector.call_tool.await_count==1


@pytest.mark.asyncio
async def test_retry_stops_after_second_failure_and_revocation(plugin_boundary):
    ap,q,auth,loader=plugin_boundary
    ap.plugin_connector.call_tool.side_effect=TimeoutError()
    await loader.invoke_tool('read_recent_group_context',{},q,source_id='local/QQGroupContext')
    assert ap.plugin_connector.call_tool.await_count==2
    ap.plugin_connector.call_tool.reset_mock();auth.reset_mock()
    auth.side_effect=[None,PermissionError('revoked')]
    await loader.invoke_tool('read_recent_group_context',{},q,source_id='local/QQGroupContext')
    assert ap.plugin_connector.call_tool.await_count==1


@pytest.mark.asyncio
async def test_cancellation_during_read_never_retries(plugin_boundary):
    ap,q,auth,loader=plugin_boundary
    entered=asyncio.Event()
    async def hang(*a,**kw): entered.set();await asyncio.Event().wait()
    ap.plugin_connector.call_tool.side_effect=hang
    running=asyncio.create_task(loader.invoke_tool('read_recent_group_context',{},q,source_id='local/QQGroupContext'))
    await entered.wait();running.cancel()
    with pytest.raises(asyncio.CancelledError): await running
    assert ap.plugin_connector.call_tool.await_count==1


@pytest.mark.asyncio
async def test_journal_tracks_real_queue_timeout_without_running_request(managed,tmp_path,monkeypatch):
    monkeypatch.setenv('QUNLING_DATA_DIR',str(tmp_path))
    managed.dispatch.queue_timeout=.025
    q=await put(managed,'20000','private-content')
    await until(lambda:q._qq_task.done.is_set())
    store=journal.get_journal(managed,managed.scope.instance_uuid)
    row=store.listing(managed.scope.workspace_uuid,'bot',request_id=q.query_uuid)['requests'][0]
    assert row['reason']=='queue_timeout' and row['status']=='failed'
    assert q._qq_task.runner is None
    assert 'private-content' not in json.dumps(row)


@pytest.mark.asyncio
async def test_long_tool_short_reply_failure_and_storage_failure_are_isolated(managed,tmp_path,monkeypatch):
    monkeypatch.setenv('QUNLING_DATA_DIR',str(tmp_path))
    gate=asyncio.Event()
    async def slow(): await gate.wait();return 'done'
    async def run(q):
        label=str(q.message_chain)
        if 'long' in label: await dispatch.invoke_tool(managed,q,slow,'generate_image')
        elif 'fail' in label:
            journal.failure(q,TimeoutError());q.variables['_monitoring_has_error']=True;return
        object.__setattr__(q,'_qq_delivery_confirmed',True)
    managed.pipeline.run=run
    controller=Controller(managed);consumer=asyncio.create_task(controller.consumer())
    try:
        long=await put(managed,'20001','long')
        await until(lambda:managed.dispatch.parked==1)
        short=await put(managed,'20002','short')
        await until(lambda:short._qq_task.done.is_set())
        store=journal.get_journal(managed,managed.scope.instance_uuid)
        row=lambda q:store.listing(managed.scope.workspace_uuid,'bot',request_id=q.query_uuid)['requests'][0]
        assert row(short)['status']=='completed' and row(long)['phase']=='tool'
        failed=await put(managed,'20003','fail')
        await until(lambda:failed._qq_task.done.is_set())
        assert row(failed)['reason']=='upstream_timeout'
        # Diagnostics must not turn an otherwise successful reply into failure.
        monkeypatch.setattr(store,'record',Mock(side_effect=OSError('disk full')))
        other=await put(managed,'20004','short')
        await until(lambda:other._qq_task.done.is_set())
        assert other._qq_delivery_confirmed and store.write_failed
        gate.set();await until(lambda:long._qq_task.done.is_set())
    finally:
        gate.set();consumer.cancel()
        with suppress(asyncio.CancelledError): await consumer
        await asyncio.gather(*managed.workers)
