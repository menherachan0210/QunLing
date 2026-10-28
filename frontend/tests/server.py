"""Offline browser fixture. Production HTTP/auth/static handling, synthetic business data."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from copy import deepcopy
import json
import tempfile
import threading
import time
from urllib.parse import urlsplit

from bootstrap import server
from bootstrap.api import ProactiveAPIError
from bootstrap.state import State
from qunling.proactive.config import validate
from qunling.knowledge.config import defaults

class Manager:
    secrets = {'setup_token': 'offline-console-test'}
    def __init__(self, root):
        self.state = State(root)
        self.lock = threading.RLock()
        self.pending = None
        self.update_inflight = False
        self.revision = 0
        self.settings = dict(base_url='https://example.invalid/v1',chat_model='chat-example',api_key_saved=True,
            features=['memory','files','vision','image','search'],image_model='image-example',group_context_hours=24,
            group_context_max_messages=1000,image_size='1024x1024',image_quality='auto',image_timeout_seconds=300,search_timeout_seconds=65)
        self.config = {'version':1,'revision':'0','enabled':True,'groups':{
            '30001':{'mode':'natural','display_name':'群灵','persona':'说话随意，熟悉以后会接梗，也会认真听。',
                'quiet_hours':{'enabled':False,'start':'23:00','end':'08:00','timezone':'Asia/Shanghai'},'examples':[]},
            '30002':{'mode':'quiet','display_name':'小灵','persona':'简洁，平和，不主动插话。',
                'quiet_hours':{'enabled':True,'start':'23:00','end':'08:00','timezone':'Asia/Shanghai'},'examples':[]}}}
        self.knowledge={'config':defaults(),'status':{'counters':{'hit':12,'miss':3,'collected':4},'sources':{}}}
        self.runtime={'config':{'revision':'a'*64,'tool_iterations':24,'request_timeout':600,
                               'reasoning':{k:'provider_default' for k in ('chat','decision','reconcile')}},'shared_roles':[['chat'],['decision','reconcile']]}
        self.capability_checks=type('Checks',(),{'snapshot':lambda _: {'job':None}})()
        self.jobs={}
        self.memories=[{'kind':'profile','sender_id':'10001','title':'阿夏 · 成员资料','value':{'name':'阿夏','traits':['喜欢简洁的回答'],'preferences':['少用表情'],'notes':''},'revision':'b'*64,'updated_at':time.time()}]
    def snapshot(self):
        return dict(configured=True,stage='ready',qq_connected=True,qq_logged_in=True,qq_checked_at=time.time(),
                    qq_status_fresh=True,plugin_runtime_healthy=True,plugins_ready=True,message='QQ 消息通道已连接。',
                    settings=self.settings,settings_revision=str(self.revision))
    def submit(self, value):
        if value.get('settings_revision')!=str(self.revision): raise ProactiveAPIError('设置已变化',409)
        self.settings.update({k:v for k,v in value.items() if not k.endswith('api_key') and k!='settings_revision'})
        self.revision+=1
        return str(self.revision)
    def update_status(self):
        return dict(available=True,busy=False,phase='idle',current_revision='a'*40,latest_revision='a'*40,update_available=False,
                    message='当前运行版本已确认。',repository='QunLing',branch='main')
    def proactive_settings(self): return {'config':deepcopy(self.config),'status':{'groups':{}},'builtin_style':'自然交流'}
    def proactive_groups(self): return {'groups':[{'group_id':'30001','name':'周五放映室'},{'group_id':'30002','name':'小院闲聊'}]}
    def save_proactive(self, values):
        values=validate(values)
        if values['revision']!=self.config['revision']: raise ProactiveAPIError('设置已在其他页面变化。',409)
        self.config={**values,'revision':f'{int(self.config["revision"],16)+1:032x}'}
        return self.proactive_settings()
    def request_diagnostics(self, filters):
        row={'request_id':'request-001','group_id':'30001','sender':'10001','status':'completed','phase':'delivered',
             'created':time.time()-42,'updated':time.time()-30,'elapsed_seconds':12,'reason':'','attempts':1}
        if filters.get('request_id'): row['events']=[{'phase':'received','at':row['created']},{'phase':'model','at':row['created']+1},{'phase':'delivered','at':row['updated']}]
        return {'available':True,'requests':[row],'next_before':None}
    def console_tasks(self, filters):
        return {'tasks':[{'id':'a'*32,'status':'completed','conversation':'group:30001','actor':'10001','delivery':'sent',
                          'created_at':time.time()-60,'attempts':1,'result_count':2,'elapsed_seconds':8}], 'next_before':None}
    def console_memory(self, values):
        group=values['group_id']
        if values['action']=='list': return {'group_id':group,'items':deepcopy(self.memories) if group=='30001' else [],'total':len(self.memories),'next_offset':None}
        if values['action']=='export': return {'group_id':group,'items':deepcopy(self.memories)}
        row=next(x for x in self.memories if x['sender_id']==values['sender_id'])
        if row['revision']!=values['revision']: raise ProactiveAPIError('这条记忆已变化',409)
        if values['action']=='save': row.update(value=values['value'],revision='c'*64)
        else: self.memories.remove(row)
        return {'group_id':group,'saved':values['action']=='save','deleted':values['action']=='delete'}
    def clear_group_memory(self, values):
        return {'cleared':True,'group_id':values['group_id']}

def preview(manager, values):
    from qunling.console_preview import validate
    values=validate(values)
    key=values['request_id']
    if values['action']=='start':
        manager.jobs[key]={'request_id':key,'group_id':values['group_id'],'status':'running','start':time.monotonic(),
                           'reply':'忙完了？那今晚可以安心摸鱼了。','persona':values['config']['groups'][values['group_id']]['persona'],
                           'memory':{},'stages':[{'stage':'generate','seconds':.3}],'outcome':'reply'}
    row=manager.jobs[key]
    if values['action']=='cancel': row['status']='cancelled'
    elif time.monotonic()-row['start']>.3: row['status']='completed'
    return {k:v for k,v in row.items() if k!='start'}

def runtime_save(manager, values):
    if values['revision']!=manager.runtime['config']['revision']: raise ProactiveAPIError('高级参数已变化',409)
    manager.runtime['config']={**values,'revision':'b'*64}
    return manager.runtime

def knowledge_save(manager, values):
    manager.knowledge['config']={**values,'revision':values['revision']+1}
    return manager.knowledge

server.console.preview=preview
server.knowledge.settings=lambda manager: manager.knowledge
server.knowledge.save=knowledge_save
server.runtime.settings=lambda manager: manager.runtime
server.runtime.save=runtime_save
server.runtime.health=lambda manager: {'storage':{'total':40*1024**3,'used':18*1024**3,'free':22*1024**3,'sampled_at':time.time()},
    'plugins':[{'id':'local/QQGroupContext','version':'1.0','enabled':True,'initialized':True,'managed':True}]}

if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='qunling-console-test-') as root:
        server.SetupHTTPServer(('127.0.0.1',15318),Manager(root)).serve_forever()
