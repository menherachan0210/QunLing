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
                                      'max-tool-iterations':24,'timeout':600,'prompt':[{'content':'private-prompt'}]}}},
                         'other':{'key':'private-key'}},'extensions_preferences':{'plugins':[{'name':'custom'}]}}
    writes=[]
    def call(method,path,data=None):
        if method=='PUT':
            writes.append(deepcopy(data));pipeline.update(data)
        return {'pipeline':deepcopy(pipeline)}
    return NS(state=state,lock=threading.RLock(),api=NS(call=call),pending=None,update_inflight=False,
              update_status=lambda:{'busy':False},pipeline=pipeline,writes=writes)

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
