"""A fast external judgment must never remove the original fallback or guards."""
import asyncio
import copy
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from aiohttp import web
import pytest
import pytest_asyncio

from qunling import jev, dialogue_review
from qunling.performance import ModelTimings


def answer():
    return {'model':jev.MODEL, 'answers':{
        'verdict':{'type':'choice','choice':'keep','confidence':.925,
                   'probabilities':{'keep':.95,'review':.04,'uncertain':.01}},
        **{k:{'type':'noul','noul':.1} for k in jev.QUESTIONS if k!='verdict'}},
        'usage':{'input_tokens':100,'output_tokens':50}}


@pytest.fixture
def configured(monkeypatch, tmp_path):
    secret=tmp_path/'key'
    secret.write_text('test-secret-not-a-real-key',encoding='utf-8')
    monkeypatch.setenv('QUNLING_JEV_REVIEW_ENABLED','1')
    monkeypatch.setenv('QUNLING_JEV_API_KEY_FILE',str(secret))
    monkeypatch.setenv('QUNLING_DATA_DIR',str(tmp_path))
    return NS(performance=ModelTimings(), decision_timeout=25,
              _complete=AsyncMock(return_value='{"action":"replace","reason":"重复认错","text":"周六六点。"}'))


def frame():
    return {'group_id':'123','target':{'sender_id':'456','text':'好的，周六六点。'},
            'messages':[], 'understanding':{'response_mode':'social'}}


@pytest_asyncio.fixture
async def server(monkeypatch):
    state={'response':answer(),'calls':0,'redirect_hits':0,'bodies':[]}
    async def handler(request):
        state['calls']+=1
        assert request.headers['Authorization']=='Bearer test-secret-not-a-real-key'
        state['bodies'].append(await request.json())
        if state.get('redirect'):
            raise web.HTTPFound('/redirect')
        if state.get('large'):
            return web.Response(body=b'x'*(jev.MAX_RESPONSE+1))
        return web.json_response(state['response'],status=state.get('status',200))
    async def redirected(request):
        state['redirect_hits']+=1
        return web.json_response(answer())
    app=web.Application()
    app.router.add_post('/decision',handler)
    app.router.add_route('*','/redirect',redirected)
    runner=web.AppRunner(app)
    await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0)
    await site.start()
    port=site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(jev,'ENDPOINT',f'http://127.0.0.1:{port}/decision')
    yield state
    await runner.cleanup()


@pytest.mark.asyncio
async def test_approved_draft_is_unchanged_and_metadata_contains_no_content(configured,server):
    original=frame()
    before=copy.deepcopy(original)
    text=await dialogue_review.review_text(configured,style='熟人接话',frame=original,draft='周六六点。')
    assert text=='周六六点。' and original==before
    configured._complete.assert_not_awaited()
    sent=server['bodies'][0]
    assert sent['model']==jev.MODEL
    assert sent['state']['review']['dialogue']['target']==original['target']
    assert '熟人接话' in sent['state']['review_policy']
    snapshot=configured.performance.snapshot()
    assert snapshot['stages']['review_gate']['count']==1
    assert snapshot['stages']['review_gate']['failed']==0
    assert snapshot['usage']['stages']['review_gate']['input_tokens']==100
    assert '周六' not in json.dumps(snapshot,ensure_ascii=False)
    assert 'test-secret' not in json.dumps(snapshot)


@pytest.mark.asyncio
async def test_one_issue_or_uncertainty_keeps_original_reviewer(configured,server):
    server['response']['answers']['stale_or_self_commentary']['noul']=.7
    draft='周六六点，刚才我说错星期了。'
    result=await dialogue_review.review_text(configured,style='自然',frame=frame(),draft=draft)
    assert result=='周六六点。'
    configured._complete.assert_awaited_once()
    assert configured._complete.await_args.args[1]['draft']==draft
    assert configured._complete.await_args.kwargs=={'stage':'review'}
    assert not hasattr(configured,'_jev_retry_at')  # A cautious judgment is not a service outage.


@pytest.mark.parametrize('damage',[
    lambda a:a.update(model='jev-latest'),
    lambda a:a['answers'].pop('grounding_error'),
    lambda a:a['answers'].update(extra={'type':'noul','noul':0}),
    lambda a:a['answers']['voice_error'].update(noul=float('nan')),
    lambda a:a['answers']['voice_error'].update(noul=True),
    lambda a:a['answers']['voice_error'].update(noul=-1),
    lambda a:a['answers']['voice_error'].update(type='choice'),
    lambda a:a['answers']['verdict'].update(confidence='high'),
    lambda a:a['answers']['verdict']['probabilities'].update(keep=1),
])
def test_invalid_or_drifted_response_cannot_approve(damage):
    value=answer()
    damage(value)
    with pytest.raises(ValueError):jev.approved(value)


@pytest.mark.parametrize('keep,issue,choice',[(.84,.1,'keep'),(.95,.26,'keep'),(.95,.1,'review')])
def test_all_thresholds_and_verdict_must_agree(keep,issue,choice):
    value=answer()
    value['answers']['verdict'].update(choice=choice,probabilities={'keep':keep,'review':1-keep,'uncertain':0})
    value['answers']['intent_error']['noul']=issue
    assert jev.approved(value) is False


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['status','redirect','large','invalid'])
async def test_transport_failures_fall_back_and_do_not_retry_or_follow_redirects(configured,server,failure):
    if failure=='status':server['status']=429
    elif failure=='invalid':server['response']={'answers':{}}
    else:server[failure]=True
    for _ in range(2):
        assert await dialogue_review.review_text(configured,style='自然',frame=frame(),draft='原稿')=='周六六点。'
    assert configured._complete.await_count==2
    assert server['calls']==1 and server['redirect_hits']==0
    assert configured.performance.snapshot()['stages']['review_gate']['failed']==1


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_cancellation_propagates(configured,monkeypatch):
    entered=asyncio.Event()
    async def stalled(*args):
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr(jev,'_request',stalled)
    monkeypatch.setattr(jev,'TIMEOUT',.04)
    started=time.monotonic()
    assert await jev.review_pass(configured,'style',{'dialogue':frame()}) is False
    assert time.monotonic()-started<.5
    configured._jev_retry_at=0
    entered.clear()
    task=asyncio.create_task(jev.review_pass(configured,'style',{'dialogue':frame()}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert configured._jev_retry_at==0


@pytest.mark.asyncio
async def test_disabled_missing_key_and_short_budget_never_call_service(configured,monkeypatch):
    request=AsyncMock(return_value=answer())
    monkeypatch.setattr(jev,'_request',request)
    monkeypatch.setenv('QUNLING_JEV_REVIEW_ENABLED','0')
    await dialogue_review.review_text(configured,style='',frame=frame(),draft='原稿')
    monkeypatch.setenv('QUNLING_JEV_REVIEW_ENABLED','1')
    await dialogue_review.review_text(configured,style='',frame=frame(),draft='原稿',timeout=2)
    monkeypatch.setenv('QUNLING_JEV_API_KEY_FILE','does-not-exist')
    await dialogue_review.review_text(configured,style='',frame=frame(),draft='原稿')
    request.assert_not_awaited()
    assert configured._complete.await_count==3


@pytest.mark.asyncio
async def test_unexpected_gate_failure_still_runs_original_reviewer(configured,monkeypatch):
    monkeypatch.setattr(jev,'review_pass',AsyncMock(side_effect=RuntimeError('synthetic telemetry failure')))
    assert await dialogue_review.review_text(configured,style='',frame=frame(),draft='原稿')=='周六六点。'
    configured._complete.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('resists_cancellation',[False,True])
async def test_recall_during_jev_request_still_prevents_delivery(configured,monkeypatch,resists_cancellation):
    from qunling import qq_recall as recall
    entered=asyncio.Event()
    adapter=NS(bot_account_id='10000')
    adapter._runtime=NS(adapter=adapter,bot_entity=NS(adapter='onebot-v11',uuid='bot'),
        execution_context=NS(instance_uuid='i',workspace_uuid='w',placement_generation=1),ap=NS())
    async def blocked(*args):
        entered.set()
        try:await asyncio.Future()
        except asyncio.CancelledError:
            if not resists_cancellation:raise
            return answer()
    monkeypatch.setattr(jev,'_request',blocked)
    delivered=[]
    async def reply():
        with recall.protect(adapter,[('group','123','-123')]):
            text=await dialogue_review.review_text(configured,style='',frame=frame(),draft='原稿')
            recall.before_action(adapter,'send_group_msg',{})
            delivered.append(text)
    task=asyncio.create_task(reply())
    await entered.wait()
    recall.observe_native(adapter,{'post_type':'notice','notice_type':'group_recall','self_id':'10000',
        'group_id':'123','message_id':'-123','user_id':'456','operator_id':'456'})
    with pytest.raises(asyncio.CancelledError):await task
    configured._complete.assert_not_awaited()
    assert delivered==[]
