"""Exercise the actual console script against a deterministic browser/HTTP boundary."""

from pathlib import Path
import shutil
import subprocess

import pytest


def run_browser(scenario: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for console interaction regressions")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = new Map();
const allNodes = [];
function makeElement(id = '') {
  const value = {
    id, disabled:false, hidden:false, textContent:'', value:'', checked:false,
    elements:{}, dataset:{}, attributes:{}, children:[], className:'', focus(){},
    classList:{add(){},remove(){},toggle(){}},
    addEventListener(name,fn){this[name]=fn;},
    setAttribute(name,val){this.attributes[name]=val;},
    getAttribute(name){return this.attributes[name]??null;},
    removeAttribute(name){delete this.attributes[name];},
    closest(){return element(this.id+'-card');},
    append(...items){this.children.push(...items);},
    replaceChildren(...items){this.children=[...items];}
  };
  Object.defineProperty(value,'innerHTML',{set(){throw Error('Unsafe HTML insertion');}});
  allNodes.push(value);
  return value;
}
function element(id) {
  if(!nodes.has(id))nodes.set(id,makeElement(id));
  return nodes.get(id);
}
const edits=['proactive-enabled','proactive-mode','proactive-display-name','proactive-persona','proactive-default-style','proactive-reset-style','proactive-quiet-enabled','proactive-quiet-start','proactive-quiet-end'];
const options=['off','quiet','natural','active'].map(mode=>({...makeElement(),value:mode}));
const aiFields={base_url:'base-url',api_key:'api-key',chat_model:'chat-model',vision_model:'vision-model',image_model:'image-model',search_model:'search-model',image_size:'image-size',image_quality:'image-quality',image_timeout_seconds:'image-timeout',search_provider:'search-provider',search_base_url:'search-base-url',search_api_key:'search-api-key',search_timeout_seconds:'search-timeout',group_context_hours:'context-hours',group_context_max_messages:'context-messages'};
for(const role of ['vision','image','search','decision','reconcile']){
  aiFields[role+'_base_url']=role+'-base-url';aiFields[role+'_api_key']=role+'-api-key';
}
aiFields.decision_model='decision-model';aiFields.reconcile_model='reconcile-model';
for(const [key,id] of Object.entries(aiFields))element('settings').elements[key]=element(id);
const featureFields=['memory','files','vision','image','search','comic'].map(name=>Object.assign(makeElement(),{value:name,type:'checkbox'}));
element('settings').querySelectorAll=selector=>selector==='input[name="features"]'?featureFields:[];
const pages=['overview','models','connection','proactive','requests','knowledge','maintenance','advanced'].map(id=>{const page=element(id);page.dataset.page=id==='proactive'?'groups':id;return page;});
const tabs=['persona','activity','memory','defaults'].map(name=>{const tab=element('tab-'+name);tab.dataset.groupTab=name;return tab;});
const panels=['persona','activity','memory','defaults'].map(name=>{const panel=element('panel-'+name);panel.dataset.groupPanel=name;return panel;});
const windowEvents={};
const stored=new Map([['qqbot-setup','test-token']]);
let config={version:1,revision:'r0',enabled:false,groups:{}};
let builtinStyle='自然交流，有主见，闲聊简短。';
let groups=[{group_id:'123',name:'测试群'},{group_id:'456',name:'<img src=x onerror=alert(1)>'}];
let setup={plugins_ready:true,plugin_runtime_healthy:true,stage:'ready',configured:true,qq_connected:true,qq_logged_in:true,settings:{}};
let update={available:true,busy:false,phase:'idle'};
let status={available:true};
let groupsFailure=false,configFailure=false,conflict=false,unauthorized=false,holdPost=false,releasePost=null;
let holdGroups=false,releaseGroups=null,groupsReads=0,aiPosts=[],aiPostFailure=false;
let posts=[];
let memoryPosts=[], resetConfirmation=null, memoryFailure=false;
let requestsData={available:true,sampled_at:1700000000,requests:[],next_before:null};
let requestFailure=false,holdRequest=false,releaseRequest=null,requestPaths=[];
let consoleMemory={items:[],total:0,next_offset:null},consoleMemoryPosts=[],consoleMemoryFailure=false,holdMemory=false,releaseMemory=null;
let tasksData={tasks:[],counts:{},sampled_at:1700000000},taskPaths=[],holdTasks=false,releaseTasks=null;
const clone=value=>JSON.parse(JSON.stringify(value));
const context=vm.createContext({
  document:{
    getElementById:element,createElement:()=>makeElement(),
    querySelectorAll(selector){
      if(selector==='[data-proactive-edit], .proactive-group')return [...edits.map(element),...allNodes.filter(e=>e.className==='proactive-group')];
      if(selector==='#proactive-mode option')return options;
      if(selector==='[data-page]')return pages;
      if(selector==='[data-group-tab]')return tabs;
      if(selector==='[data-group-panel]')return panels;
      if(selector==='#settings input, #settings select')return [...Object.values(element('settings').elements),...featureFields];
      return [];
    }
  },
  location:{hash:'',pathname:'/',search:''},history:{replaceState(){}},
  sessionStorage:{getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},
  window:{confirm:()=>true,prompt:()=>resetConfirmation,crypto:require('node:crypto').webcrypto,addEventListener:(name,fn)=>windowEvents[name]=fn},URLSearchParams,AbortController,setTimeout,clearTimeout,setInterval(){},
  FormData:class{*[Symbol.iterator](){}},
  fetch:async(path,opts)=>{
    if(unauthorized)return {ok:false,status:401,json:async()=>({error:'令牌无效'})};
    if(path==='/api/status')return {ok:true,json:async()=>clone(setup)};
    if(path==='/api/update')return {ok:true,json:async()=>clone(update)};
    if(path==='/api/memory'){
      const body=JSON.parse(opts.body);consoleMemoryPosts.push(body);
      if(holdMemory)await new Promise(resolve=>releaseMemory=resolve);
      if(consoleMemoryFailure)return {ok:false,status:409,json:async()=>({error:'这条记忆已变化'})};
      return {ok:true,json:async()=>clone({...consoleMemory,group_id:body.group_id,saved:body.action==='save',deleted:body.action==='delete'})};
    }
    if(path.startsWith('/api/tasks?')){
      taskPaths.push(path);if(holdTasks)await new Promise(resolve=>releaseTasks=resolve);
      return {ok:true,json:async()=>clone(tasksData)};
    }
    if(path.startsWith('/api/requests?')){
      requestPaths.push(path);
      if(holdRequest)await new Promise(resolve=>releaseRequest=resolve);
      if(requestFailure)throw Error('请求记录暂不可用');
      return {ok:true,json:async()=>clone(requestsData)};
    }
    if(path==='/api/proactive/groups'){
      groupsReads++;
      if(holdGroups)await new Promise(resolve=>releaseGroups=resolve);
      if(groupsFailure)throw Error('群列表加载失败');
      return {ok:true,json:async()=>({groups:clone(groups)})};
    }
    if(path==='/api/configure'){
      aiPosts.push(JSON.parse(opts.body));
      if(aiPostFailure)return {ok:false,status:409,json:async()=>({error:'设置已发生变化'})};
      setup.stage='configuring';
      return {ok:true,json:async()=>({status:'accepted'})};
    }
    if(path==='/api/proactive/memory/reset'){
      const body=JSON.parse(opts.body);
      memoryPosts.push(body);
      if(holdPost)await new Promise(resolve=>releasePost=resolve);
      if(memoryFailure)throw Error('连接中断');
      return {ok:true,json:async()=>({cleared:true,group_id:body.group_id,cleared_at:1700000000})};
    }
    if(path==='/api/proactive'){
      if(opts.method==='POST'){
        posts.push(JSON.parse(opts.body));
        if(holdPost)await new Promise(resolve=>releasePost=resolve);
        if(conflict)return {ok:false,status:409,json:async()=>({error:'版本冲突'})};
        config={...JSON.parse(opts.body),revision:'r'+posts.length};
      }
      if(configFailure)throw Error('设置读取失败');
      return {ok:true,json:async()=>({config:clone(config),status:clone(status),builtin_style:builtinStyle})};
    }
    throw Error('unexpected endpoint '+path);
  }
});
const evaluate=source=>vm.runInContext(source,context);
async function settle(){for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));}
async function refresh(){await evaluate('pollProactive(authEpoch, true)');}
function rows(){return element('proactive-group-list').children;}
function select(id){rows().find(row=>row.children[0].children[1].textContent.startsWith(id)).click();}
function change(id,value){const field=element(id);if(typeof value==='boolean')field.checked=value;else field.value=value;field.change();}
function input(id,value){element(id).value=value;element(id).input();}
async function save(){await element('proactive-form').submit({preventDefault(){}});}
(async()=>{
  evaluate(fs.readFileSync(process.argv[1],'utf8'));
  await settle();
""" + scenario + r"""
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    source = Path(__file__).resolve().parents[1] / "bootstrap/static/app.js"
    result = subprocess.run(
        [node, "-e", script, str(source)], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_multiturn_samples_edit_switch_save_reset_and_preview_the_selected_source():
    run_browser(r"""
  select('123');
  element('proactive-add-example').click();
  const samples=()=>element('proactive-examples').children;
  const field=(turn)=>samples()[0].children[turn+1].children[0];
  field(0).value='甲：真的假的';field(0).input();
  field(1).value='你猜';field(1).input();
  samples()[0].children.at(-1).children[0].click();
  field(2).value='乙：认真问';field(2).input();
  field(3).value='那我认真说';field(3).input();
  const stale=field(0);
  select('456');
  stale.value='不能串到另一个群';stale.input();
  assert.equal(samples().length,0);
  select('123');
  assert.equal(field(0).value,'甲：真的假的');
  input('proactive-persona','只用群内设置');
  assert.equal(field(3).value,'那我认真说');
  assert.ok(!element('persona-effective').textContent.includes('那我认真说'));
  await save();
  assert.deepEqual(posts[0].groups['123'].examples,[['甲：真的假的','你猜','乙：认真问','那我认真说']]);
  assert.ok(element('persona-effective').textContent.includes('那我认真说'));
  assert.ok(!element('persona-effective').textContent.includes(builtinStyle));
  element('proactive-reset-group').click();
  assert.equal(samples().length,0);
  await save();
  assert.ok(!posts[1].groups['123']);
  assert.ok(element('persona-effective').textContent.includes(builtinStyle));
""")


def test_default_samples_empty_validation_failed_save_and_restore():
    run_browser(r"""
  select('123');
  element('proactive-add-default-example').click();
  const fields=()=>element('proactive-default-examples').children[0].children;
  await save();
  assert.equal(posts.length,0);
  assert.ok(element('proactive-message').textContent.includes('补全或删除空示例'));
  fields()[1].children[0].value='默认群友';fields()[1].children[0].input();
  fields()[2].children[0].value='默认回应';fields()[2].children[0].input();
  conflict=true;await save();
  assert.equal(fields()[2].children[0].value,'默认回应');
  assert.deepEqual(posts[0].default_examples,[['默认群友','默认回应']]);
  assert.ok(!element('persona-effective').textContent.includes('默认回应'));
""")


def test_memory_reset_confirmation_and_group_isolation():
    run_browser(r"""
  select('123');
  const before=JSON.stringify(config);
  assert.equal(element('proactive-clear-memory').disabled,false);
  await element('proactive-clear-memory').click();
  assert.equal(memoryPosts.length,0);
  resetConfirmation='456';
  await element('proactive-clear-memory').click();
  assert.equal(memoryPosts.length,0);
  assert.match(element('proactive-memory-message').textContent,/群号不一致/);
  resetConfirmation='123';
  holdPost=true;
  const sending=element('proactive-clear-memory').click();
  await settle();
  assert.equal(element('proactive-clear-memory').disabled,true);
  assert.equal(element('proactive-refresh').disabled,true);
  assert.equal(element('save').disabled,true);
  releasePost(); await sending;
  assert.deepEqual(memoryPosts.map(p=>[p.group_id,p.confirmation]),[['123','123']]);
  assert.match(memoryPosts[0].request_id,/^[a-f0-9]{32}$/);
  assert.equal(JSON.stringify(config),before);
  assert.match(element('proactive-memory-message').textContent,/记忆已清空/);
  select('456');
  assert.equal(element('proactive-memory-message').textContent,'');
""")


def test_background_refresh_keeps_editing_enabled_and_preserves_new_input():
    run_browser(r"""
  select('123');
  holdGroups=true;
  const pending=refresh(); await settle();
  assert.equal(element('proactive-persona').disabled,false);
  assert.equal(element('proactive-group-settings').disabled,false);
  input('proactive-persona','刷新期间继续输入');
  releaseGroups(); await pending;
  assert.equal(element('proactive-persona').value,'刷新期间继续输入');
  assert.equal(element('proactive-save').disabled,false);
  const before=groupsReads;
  evaluate('proactiveLastPoll=0');
  await evaluate('pollProactive(authEpoch)');
  assert.equal(groupsReads,before);
  assert.equal(element('proactive-persona').value,'刷新期间继续输入');
""")


def test_page_and_group_tabs_preserve_drafts_and_show_actual_persona_source():
    run_browser(r"""
  evaluate("selectNav('groups')");
  assert.equal(element('models').hidden,true);
  assert.equal(element('proactive').hidden,false);
  select('123');
  assert.equal(element('persona-source').textContent,'继承默认语气');
  input('proactive-persona','本群性格');
  assert.equal(element('persona-effective').textContent,builtinStyle);
  element('tab-activity').click();
  assert.equal(element('panel-persona').hidden,true);
  assert.equal(element('panel-activity').hidden,false);
  evaluate("selectNav('models'); selectNav('groups')");
  element('tab-persona').click();
  assert.equal(element('proactive-persona').value,'本群性格');
  await save();
  assert.equal(element('persona-source').textContent,'本群独立性格');
  assert.equal(element('persona-effective').textContent,'本群性格');
  element('tab-defaults').click();
  assert.equal(element('panel-defaults').hidden,false);
  assert.equal(element('proactive-group-settings').hidden,true);
""")


def test_tab_draft_recovery_detects_conflict_and_beforeunload_protects_changes():
    run_browser(r"""
  select('123'); input('proactive-persona','需要恢复的草稿');
  const persisted=stored.get('qunling-console-draft:groups');
  assert.equal(JSON.parse(persisted).config.groups['123'].persona,'需要恢复的草稿');
  let prevented=false;
  const event={preventDefault(){prevented=true;}};
  windowEvents.beforeunload(event); assert.equal(prevented,true);
  evaluate('proactiveDraft=null; proactiveDirty=false; groupDraftRestored=false');
  config.revision='changed-elsewhere';
  await refresh();
  assert.equal(element('proactive-persona').value,'需要恢复的草稿');
  assert.equal(element('proactive-persona').disabled,false);
  assert.equal(element('proactive-save').disabled,true);
  await element('proactive-reload').click();
  assert.equal(stored.has('qunling-console-draft:groups'),false);
""")


def test_ai_settings_keep_secrets_out_of_drafts_and_wait_for_application_result():
    run_browser(r"""
  setup.settings_revision='ai-v1';
  setup.settings={base_url:'https://ai.example/v1',chat_model:'chat',image_model:'image',search_provider:'shared',api_key_saved:true};
  await evaluate('poll()');
  element('image-quality').value='high';
  element('api-key').value='new-private-key';
  element('search-api-key').value='new-search-private-key';
  element('settings').input();
  const storedDraft=stored.get('qunling-console-draft:ai');
  assert.ok(storedDraft.includes('high'));
  assert.ok(!storedDraft.includes('private-key'));
  await element('settings').submit({preventDefault(){},currentTarget:element('settings')});
  assert.equal(aiPosts[0].image_quality,'high');
  assert.equal(aiPosts[0].api_key,'new-private-key');
  assert.equal(aiPosts[0].settings_revision,'ai-v1');
  assert.equal(element('save').disabled,true);
  assert.match(element('form-message').textContent,/正在应用/);
  setup.stage='ready';setup.settings_revision='ai-v2';setup.settings.image_quality='high';
  await evaluate('poll()');
  assert.match(element('form-message').textContent,/已应用/);
  assert.equal(element('api-key').value,'');
  assert.equal(element('search-api-key').value,'');
  assert.equal(stored.has('qunling-console-draft:ai'),false);
""")


def test_ai_failure_and_external_revision_preserve_unsaved_fields():
    run_browser(r"""
  setup.settings_revision='ai-v1'; setup.settings={base_url:'https://ai.example/v1'};
  await evaluate('poll()');
  element('image-quality').value='high';element('settings').input();
  await element('settings').submit({preventDefault(){},currentTarget:element('settings')});
  setup.stage='error';setup.message='应用失败';
  await evaluate('poll()');
  assert.equal(element('image-quality').value,'high');
  assert.equal(element('form-message').textContent,'应用失败');
  assert.equal(element('save').disabled,false);
  setup.stage='ready';setup.settings_revision='other-page';
  await evaluate('poll()');
  assert.equal(element('save').disabled,true);
  assert.equal(element('image-quality').value,'high');
  assert.equal(element('settings-reload').hidden,false);
""")


def test_memory_reset_pending_survives_reload_and_keeps_retry_available():
    run_browser(r"""
  select('123');
  status.memory_reset_pending=['123']; await refresh();
  assert.equal(element('proactive-runtime-state').textContent,'记忆清除未完成');
  assert.equal(element('proactive-clear-memory').disabled,false);
  select('456');
  assert.notEqual(element('proactive-runtime-state').textContent,'记忆清除未完成');
""")


def test_memory_reset_reuses_request_id_after_uncertain_response():
    run_browser(r"""
  select('123'); resetConfirmation='123'; memoryFailure=true;
  await element('proactive-clear-memory').click();
  assert.doesNotMatch(element('proactive-memory-message').textContent,/记忆已清空/);
  memoryFailure=false;
  await element('proactive-clear-memory').click();
  assert.equal(memoryPosts[1].request_id,memoryPosts[0].request_id);
  await element('proactive-clear-memory').click();
  assert.notEqual(memoryPosts[2].request_id,memoryPosts[1].request_id);
""")


def test_new_groups_are_off_and_master_switch_never_enables_groups():
    run_browser(r"""
  assert.equal(element('proactive-enabled').checked,false);
  assert.equal(element('proactive-save').disabled,true);
  assert.equal(rows().length,2);
  assert.equal(rows()[1].children[0].children[0].textContent,groups[1].name);
  assert.ok(rows().every(row=>row.children[1].textContent==='仅 @ 回复'));
  change('proactive-enabled',true);
  await save();
  assert.deepEqual(posts[0],{version:1,revision:'r0',enabled:true,groups:{}});
  groups.push({group_id:'789',name:'新加入的群'});
  await refresh();
  assert.equal(rows().find(row=>row.children[0].children[1].textContent==='789').children[1].textContent,'仅 @ 回复');
  assert.deepEqual(config.groups,{});
""")


def test_persona_paste_preserves_long_text_and_blocks_oversize_across_groups():
    from html.parser import HTMLParser

    class PersonaField(HTMLParser):
        attrs = None

        def handle_starttag(self, tag, attrs):
            if tag == "textarea" and dict(attrs).get("id") == "proactive-persona":
                self.attrs = dict(attrs)

    field = PersonaField()
    field.feed((Path(__file__).resolve().parents[1] / "bootstrap/static/index.html").read_text(encoding="utf-8"))
    assert field.attrs is not None and "maxlength" not in field.attrs
    run_browser(r"""
  select('123');
  const persona='性格正文'.repeat(300)+'🙂'.repeat(4800);
  input('proactive-persona',persona);
  assert.equal(element('proactive-persona-count').textContent,'6000 / 6000 字');
  await save();
  assert.equal(config.groups['123'].persona,persona);
  await refresh();
  assert.equal(element('proactive-persona').value,persona);
  input('proactive-persona',persona+'多');
  assert.equal(element('proactive-persona').getAttribute('aria-invalid'),'true');
  select('456');
  await save();
  assert.equal(posts.length,1);
  select('123');
  assert.equal(element('proactive-persona').value,persona+'多');
  input('proactive-persona',persona);
  await save();
  assert.equal(posts.length,2);
""")


def test_default_style_can_be_viewed_saved_reloaded_and_reset_without_changing_group_persona():
    run_browser(r"""
  assert.equal(element('proactive-default-style').value,builtinStyle);
  assert.equal(element('proactive-default-style').disabled,false);
  select('123'); input('proactive-persona','本群独立：自信俏皮');
  input('proactive-default-style','所有未设置性格的群：正式严谨');
  await refresh();
  assert.equal(element('proactive-default-style').value,'所有未设置性格的群：正式严谨');
  await save();
  assert.equal(config.default_style,'所有未设置性格的群：正式严谨');
  assert.equal(config.groups['123'].persona,'本群独立：自信俏皮');
  assert.equal(config.groups['123'].mode,'off');
  await evaluate('pollProactive(authEpoch, true, true)');
  assert.equal(element('proactive-default-style').value,config.default_style);
  element('proactive-reset-style').click();
  assert.equal(element('proactive-default-style').value,builtinStyle);
  assert.notEqual(config.default_style,''); // Reset remains a draft until saved.
  await save();
  assert.equal(config.default_style,'');
  assert.equal(config.groups['123'].persona,'本群独立：自信俏皮');
""")


def test_default_style_conflict_and_auth_locks_preserve_unsaved_changes():
    run_browser(r"""
  input('proactive-default-style','未保存的默认语气');
  config.revision='other-tab';
  await refresh();
  assert.equal(element('proactive-default-style').value,'未保存的默认语气');
  assert.equal(element('proactive-default-style').disabled,false);
  assert.equal(element('proactive-reset-style').disabled,false);
  assert.equal(element('proactive-save').disabled,true);
  await element('proactive-reload').click();
  assert.equal(element('proactive-default-style').value,builtinStyle);
  unauthorized=true; await refresh();
  assert.equal(element('proactive-default-style').disabled,true);
  assert.equal(element('proactive-default-style').value,'');
""")


def test_only_explicit_group_is_saved_and_poll_preserves_unsaved_persona():
    run_browser(r"""
  select('123');
  change('proactive-mode','natural');
  input('proactive-persona','有自己的看法，偶尔接梗。');
  input('proactive-display-name','小灵');
  change('proactive-quiet-enabled',true);
  change('proactive-quiet-start','22:30');
  change('proactive-quiet-end','08:00');
  await refresh();
  assert.equal(element('proactive-persona').value,'有自己的看法，偶尔接梗。');
  assert.equal(element('proactive-save').disabled,false);
  await save();
  assert.deepEqual(Object.keys(posts[0].groups),['123']);
  assert.equal(posts[0].groups['123'].mode,'natural');
  assert.equal(posts[0].groups['123'].quiet_hours.timezone,'Asia/Shanghai');
  assert.equal(posts[0].groups['123'].display_name,'小灵');
  assert.equal(posts[0].enabled,false);
  assert.equal(element('proactive-save').disabled,true);
""")


def test_group_list_failure_cannot_clear_saved_policy_or_send_configuration():
    run_browser(r"""
  select('123');change('proactive-mode','quiet');await save();
  change('proactive-enabled',true);
  groupsFailure=true;
  await refresh();
  assert.equal(element('proactive-save').disabled,true);
  assert.match(element('proactive-groups-message').textContent,/加载失败/);
  await save();
  assert.equal(posts.length,1);
  assert.equal(config.groups['123'].mode,'quiet');
  groupsFailure=false;
  await refresh();
  assert.equal(element('proactive-save').disabled,false);
  await save();
  assert.equal(posts[1].groups['123'].mode,'quiet');
""")


def test_revision_conflicts_preserve_edits_until_explicit_reload():
    run_browser(r"""
  select('123');change('proactive-mode','natural');
  input('proactive-persona','未保存的人设');
  config.revision='external';
  await refresh();
  assert.equal(element('proactive-save').disabled,true);
  assert.equal(element('proactive-persona').value,'未保存的人设');
  assert.equal(element('proactive-reload').hidden,false);
  await element('proactive-reload').click();
  assert.equal(element('proactive-mode').value,'off');
  assert.equal(element('proactive-persona').value,'');
  change('proactive-mode','quiet');
  conflict=true;
  await save();
  assert.equal(posts[0].revision,'external');
  assert.equal(element('proactive-save').disabled,true);
  assert.match(element('proactive-message').textContent,/重新载入/);
  assert.equal(element('proactive-mode').value,'quiet');
""")


def test_update_login_and_authentication_locks_prevent_saves():
    run_browser(r"""
  select('123');change('proactive-mode','natural');
  update.busy=true;await evaluate('poll()');
  assert.equal(element('proactive-save').disabled,true);
  await save();assert.equal(posts.length,0);
  update.busy=false;setup.qq_logged_in=false;await evaluate('poll()');
  assert.equal(element('proactive-save').disabled,true);
  setup.qq_logged_in=true;await evaluate('poll()');
  assert.equal(element('proactive-save').disabled,false);
  unauthorized=true;await refresh();
  assert.equal(element('proactive-save').disabled,true);
  assert.equal(element('console-content').hidden,true);
  await save();assert.equal(posts.length,0);
""")


def test_save_serializes_with_update_and_ai_configuration():
    run_browser(r"""
  select('123');change('proactive-mode','natural');
  holdPost=true;
  const pending=save();await settle();
  assert.equal(element('save').disabled,true);
  assert.equal(element('apply-update').disabled,true);
  assert.equal(element('proactive-save').disabled,true);
  change('proactive-mode','active');
  releasePost();await pending;
  assert.equal(config.groups['123'].mode,'natural');
  assert.equal(element('proactive-mode').value,'natural');
""")


def test_quiet_hours_validation_and_search_do_not_change_authorization():
    run_browser(r"""
  input('proactive-search','456');assert.equal(rows().length,1);
  assert.equal(rows()[0].children[0].children[0].textContent,groups[1].name);
  select('456');change('proactive-mode','natural');
  change('proactive-quiet-enabled',true);
  change('proactive-quiet-start','08:00');change('proactive-quiet-end','08:00');
  await save();assert.equal(posts.length,0);
  assert.match(element('proactive-message').textContent,/不能相同/);
  change('proactive-quiet-end','09:00');await save();
  assert.deepEqual(Object.keys(posts[0].groups),['456']);
""")


def test_reset_removes_only_selected_policy_and_returns_to_default_off():
    run_browser(r"""
  select('123');change('proactive-mode','natural');
  select('456');change('proactive-mode','quiet');
  await save();
  assert.equal(element('proactive-reset-group').disabled,false);
  element('proactive-reset-group').click();
  assert.equal(element('proactive-mode').value,'off');
  await save();
  assert.deepEqual(Object.keys(posts[1].groups),['123']);
  assert.equal(posts[1].groups['123'].mode,'natural');
""")


def test_runtime_uses_saved_authorization_and_never_presents_draft_as_active():
    run_browser(r"""
  select('123');
  assert.equal(element('proactive-runtime-state').textContent,'未开启');
  change('proactive-enabled',true);change('proactive-mode','active');
  assert.equal(element('proactive-runtime-state').textContent,'未开启');
  assert.match(element('proactive-runtime-note').textContent,/尚未生效/);
  await save();
  status={available:true,enabled:true,generated_at:1000,groups:{'123':{status_code:'ready'}}};
  await refresh();
  assert.equal(element('proactive-runtime-state').textContent,'已就绪');
  change('proactive-mode','off');
  assert.equal(element('proactive-runtime-state').textContent,'已就绪');
  assert.match(element('proactive-runtime-note').textContent,/已保存/);
  await save();
  assert.equal(element('proactive-runtime-state').textContent,'未开启');
""")


def test_runtime_displays_budgets_and_cooldown_using_server_clock():
    run_browser(r"""
  select('123');change('proactive-enabled',true);change('proactive-mode','active');await save();
  status={available:true,enabled:true,generated_at:1000,judgments_day:45,judgments_day_limit:4800,groups:{'123':{
    status_code:'cooldown',cooldown_until:1090,
    judgments_hour:8,judgments_hour_limit:240,judgments_day:20,judgments_day_limit:2400,
    replies_hour:2,replies_hour_limit:60,replies_day:9,replies_day_limit:600,
    last_event:{code:'stale',at:990},last_sent_at:980
  }}};
  await refresh();
  assert.equal(element('proactive-runtime-state').textContent,'控制发言频率');
  assert.match(element('proactive-runtime-detail').textContent,/普通插话还需约 2 分钟/);
  assert.match(element('proactive-runtime-detail').textContent,/点名或引用/);
  assert.equal(element('proactive-judgments-hour').textContent,'8 / 240');
  assert.equal(element('proactive-judgments-day').textContent,'20 / 2400');
  assert.equal(element('proactive-replies-hour').textContent,'2 / 60');
  assert.equal(element('proactive-replies-day').textContent,'9 / 600');
  assert.match(element('proactive-global-budget').textContent,/45 \/ 4800/);
  assert.match(element('proactive-last-event').textContent,/本次回复已过期/);
  assert.match(element('proactive-last-sent').textContent,/1970/);
""")


def test_runtime_explains_queued_messages_and_non_interrupting_media_events():
    run_browser(r"""
  select('123');change('proactive-enabled',true);change('proactive-mode','active');await save();
  for(const [code,label] of [
    ['queued','后续消息已合并等待'],
    ['media_observed','已收到非文字消息，不打断当前回复'],
    ['cancelled','任务已取消'],
    ['batch_expired','本批次已过期，未继续回复']
  ]){
    status={available:true,enabled:true,groups:{'123':{status_code:'pending',last_event:{code,at:1000}}}};
    await refresh();
    assert.ok(element('proactive-last-event').textContent.startsWith(label));
    assert.match(element('proactive-runtime-detail').textContent,/不打断当前回复/);
  }
""")


def test_runtime_updates_during_edit_without_overwriting_persona():
    run_browser(r"""
  select('123');change('proactive-enabled',true);change('proactive-mode','natural');await save();
  input('proactive-persona','尚未保存的人设');
  for(const [code,label] of [['ready','已就绪'],['explicit_busy','有明确任务进行中'],['quiet_hours','安静时段'],['budget_exhausted','已达参与限额'],['pending','正在接话']]){
    status={available:true,enabled:true,groups:{'123':{status_code:code}}};
    await refresh();
    assert.equal(element('proactive-runtime-state').textContent,label);
    assert.equal(element('proactive-persona').value,'尚未保存的人设');
    assert.match(element('proactive-runtime-note').textContent,/尚未生效/);
  }
""")


def test_runtime_failure_is_visible_and_cached_values_are_marked_stale():
    run_browser(r"""
  select('123');change('proactive-enabled',true);change('proactive-mode','natural');await save();
  status={available:true,enabled:true,generated_at:1000,groups:{'123':{status_code:'ready'}}};
  await refresh();
  configFailure=true;await refresh();
  assert.equal(element('proactive-runtime-state').textContent,'等待服务');
  assert.match(element('proactive-runtime-updated').textContent,/可能已过期/);
  configFailure=false;await refresh();
  assert.equal(element('proactive-runtime-state').textContent,'已就绪');
  status.storage_healthy=false;status.available=false;await refresh();
  assert.equal(element('proactive-runtime-state').textContent,'已暂停');
  status.storage_healthy=true;status.available=true;setup.qq_connected=false;
  await evaluate('poll()');await refresh();
  assert.equal(element('proactive-runtime-state').textContent,'QQ 未连接');
""")


def test_diagnostics_only_display_known_codes_and_valid_counters():
    run_browser(r"""
  select('123');change('proactive-enabled',true);change('proactive-mode','natural');await save();
  const secret='<img src=x onerror=alert(1)> private-chat-and-api-key';
  status={available:true,enabled:true,generated_at:secret,judgments_day:secret,judgments_day_limit:-1,
    groups:{'123':{status_code:'__proto__',judgments_hour:secret,judgments_hour_limit:9999999999,
      last_event:{code:secret,at:secret},last_sent_at:secret,error:secret,reason:secret}}};
  await refresh();
  assert.equal(element('proactive-runtime-state').textContent,'状态待确认');
  assert.equal(element('proactive-judgments-hour').textContent,'— / —');
  assert.equal(element('proactive-last-sent').textContent,'暂无记录');
  assert.match(element('proactive-last-event').textContent,/暂无可识别/);
  for(const node of allNodes)assert.ok(!String(node.textContent).includes('private-chat-and-api-key'));
""")
