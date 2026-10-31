from copy import deepcopy
from types import SimpleNamespace as NS
import threading
import pytest
from bootstrap import runtime
from bootstrap.api import SetupError, ProactiveAPIError
from bootstrap.configure import RUNNER
from bootstrap.state import State

@pytest.fixture
def manager(tmp_path):
    state=State(tmp_path)
    state.write({'pipeline_uuid':'pipeline'})
    pipeline={'config':{'ai':{'runner':{'id':RUNNER},'qunling_decision_model':'fast',
              'runner_config':{RUNNER:{'model':{'primary':'chat','fallbacks':['backup'],'reasoning':{'other':'high'}},
                                      'max-tool-iterations':24,'timeout':600,'remove-think':True,'prompt':[{'content':'private-prompt'}]}}},
                         'other':{'key':'private-key'}},'extensions_preferences':{'plugins':[{'name':'custom'}]}}
    writes=[]
    models=[{'uuid':name,'name':'gpt-6.1-sol' if name=='chat' else 'decision-model',
             'abilities':['reasoning'],'extra_args':{'private-header':'private-key'},
             'reasoning_capabilities':{'supported':True,'levels':['provider_default','low','medium','high','xhigh','max']}}
            for name in ('chat','fast')]
    def call(method,path,data=None):
        if path=='/api/v1/provider/models/llm': return {'models':deepcopy(models)}
        if method=='PUT':
            writes.append(deepcopy(data));pipeline.update(data)
        return {'pipeline':deepcopy(pipeline)}
    return NS(state=state,lock=threading.RLock(),api=NS(call=call),pending=None,update_inflight=False,
              update_status=lambda:{'busy':False},pipeline=pipeline,writes=writes,models=models)

def test_runtime_save_preserves_unmanaged_fields_and_checks_revision(manager):
    before=runtime.settings(manager)
    assert 'private' not in str(before)
    edit=deepcopy(before['config']);edit['tool_iterations']=18
    edit['reasoning'].update(chat='low',reconcile='low')
    result=runtime.save(manager,edit)
    assert result['config']['tool_iterations']==18
    runner=manager.pipeline['config']['ai']['runner_config'][RUNNER]
    assert runner['model']['fallbacks']==['backup'] and runner['model']['reasoning']['other']=='high'
    assert runner['prompt']==[{'content':'private-prompt'}] and manager.pipeline['config']['other']['key']=='private-key'
    assert runner['remove-think'] is True
    assert manager.pipeline['extensions_preferences']=={'plugins':[{'name':'custom'}]}
    with pytest.raises(ProactiveAPIError): runtime.save(manager,edit)
    assert len(manager.writes)==1

def test_runtime_rejects_ambiguous_shared_model_and_busy_or_invalid_writes(manager):
    edit=runtime.settings(manager)['config']
    edit['reasoning']['chat']='high'
    with pytest.raises(SetupError): runtime.save(manager,edit)
    edit['reasoning']['reconcile']='high'
    manager.pending={'new':'config'}
    with pytest.raises(SetupError): runtime.save(manager,edit)
    manager.pending=None;edit['tool_iterations']=True
    with pytest.raises(SetupError): runtime.save(manager,edit)
    assert not manager.writes


def test_reasoning_options_are_scoped_to_each_saved_model_and_never_expose_raw_args(manager):
    manager.models[1]['reasoning_capabilities']['levels']=['provider_default','disabled','enabled','invalid',{}]
    result=runtime.settings(manager)
    assert 'private' not in str(result)
    assert result['reasoning_models'][0]['roles']==['chat','reconcile']
    assert result['reasoning_models'][1]['roles']==['decision']
    assert result['reasoning_models'][1]['levels']==['provider_default','disabled','enabled']
    assert 'disabled' not in result['reasoning_models'][0]['levels']


@pytest.mark.parametrize('problem',['unsupported','ability','raw_args','extra_body'])
def test_reasoning_rejects_unusable_level_before_writing(manager,problem):
    model=manager.models[0]
    if problem=='unsupported': model['reasoning_capabilities']['levels']=['provider_default','low']
    elif problem=='ability': model['abilities']=[]
    elif problem=='raw_args': model['extra_args']['reasoning_effort']='low'
    else: model['extra_args']['extra_body']={'thinking':{'type':'enabled'}}
    edit=runtime.settings(manager)['config']
    edit['reasoning'].update(chat='high',reconcile='high')
    with pytest.raises(SetupError,match='不支持'): runtime.save(manager,edit)
    assert not manager.writes


def test_unavailable_legacy_level_can_be_preserved_or_reset_without_changing_other_controls(manager):
    runner=manager.pipeline['config']['ai']['runner_config'][RUNNER]
    runner['model']['reasoning']['chat']='enabled'
    manager.models[0]['reasoning_capabilities']['levels']=['provider_default','low']
    edit=runtime.settings(manager)['config'];edit['tool_iterations']=17
    saved=runtime.save(manager,edit)
    assert saved['config']['reasoning']['chat']=='enabled'
    assert 'fast' not in runner['model']['reasoning']
    edit=saved['config'];edit['reasoning'].update(chat='provider_default',reconcile='provider_default')
    saved=runtime.save(manager,edit)
    assert saved['config']['tool_iterations']==17 and saved['config']['request_timeout']==600
    assert saved['config']['reasoning']['chat']=='provider_default'


@pytest.mark.asyncio
@pytest.mark.parametrize('level',['provider_default','low','medium','high','xhigh','max'])
@pytest.mark.parametrize('stream',[False,True])
async def test_saved_level_reaches_real_host_request_arguments(manager,level,stream):
    from unittest.mock import Mock
    from langbot.pkg.provider.modelmgr.reasoning import model_with_reasoning_level
    from langbot.pkg.provider.modelmgr.requesters.litellmchat import LiteLLMRequester

    instance=LiteLLMRequester(ap=NS(logger=Mock()),config={'custom_llm_provider':'openai','requester_name':'openai-chat-completions'})
    entity=NS(name='gpt-6.1-sol',abilities=['reasoning'],extra_args={},reasoning_config={'level':'provider_default'})
    model=NS(model_entity=entity,provider=NS(token_mgr=NS(get_token=lambda:'test-key'),
             provider_entity=NS(requester='openai-chat-completions')))
    capabilities=instance.get_reasoning_capabilities(model)
    manager.models[0]['reasoning_capabilities']=capabilities
    edit=runtime.settings(manager)['config'];edit['reasoning'].update(chat=level,reconcile=level)
    if level not in capabilities['levels']:
        with pytest.raises(SetupError,match='不支持'): runtime.save(manager,edit)
        assert not manager.writes
        return
    saved=runtime.save(manager,edit)
    scoped=model_with_reasoning_level(model,saved['config']['reasoning']['chat'])
    args=await instance._build_completion_args(scoped,[],[],stream=stream)
    if level=='provider_default': assert 'reasoning_effort' not in args
    else:
        assert args['reasoning_effort']==level
        assert 'reasoning_effort' in args['allowed_openai_params']
    assert not hasattr(model,'reasoning_config_override')
    assert manager.pipeline['config']['ai']['runner_config'][RUNNER]['remove-think'] is True
