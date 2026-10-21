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
    elements:{}, dataset:{}, attributes:{}, children:[], className:'',
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
const edits=['proactive-enabled','proactive-mode','proactive-display-name','proactive-persona','proactive-quiet-enabled','proactive-quiet-start','proactive-quiet-end'];
const options=['off','quiet','natural','active'].map(mode=>({...makeElement(),value:mode}));
element('settings').elements.api_key={value:'',type:'password'};
const stored=new Map([['qqbot-setup','test-token']]);
let config={version:1,revision:'r0',enabled:false,groups:{}};
let groups=[{group_id:'123',name:'测试群'},{group_id:'456',name:'<img src=x onerror=alert(1)>'}];
let setup={plugins_ready:true,stage:'ready',configured:true,qq_connected:true,qq_logged_in:true,settings:{}};
let update={available:true,busy:false,phase:'idle'};
let status={available:true};
let groupsFailure=false,configFailure=false,conflict=false,unauthorized=false,holdPost=false,releasePost=null;
let posts=[];
const clone=value=>JSON.parse(JSON.stringify(value));
const context=vm.createContext({
  document:{
    getElementById:element,createElement:()=>makeElement(),
    querySelectorAll(selector){
      if(selector==='[data-proactive-edit], .proactive-group')return [...edits.map(element),...allNodes.filter(e=>e.className==='proactive-group')];
      if(selector==='#proactive-mode option')return options;
      return [];
    }
  },
  location:{hash:'',pathname:'/',search:''},history:{replaceState(){}},
  sessionStorage:{getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},
  window:{confirm:()=>true},URLSearchParams,AbortController,setTimeout,clearTimeout,setInterval(){},
  FormData:class{*[Symbol.iterator](){}},
  fetch:async(path,opts)=>{
    if(unauthorized)return {ok:false,status:401,json:async()=>({error:'令牌无效'})};
    if(path==='/api/status')return {ok:true,json:async()=>clone(setup)};
    if(path==='/api/update')return {ok:true,json:async()=>clone(update)};
    if(path==='/api/proactive/groups'){
      if(groupsFailure)throw Error('群列表加载失败');
      return {ok:true,json:async()=>({groups:clone(groups)})};
    }
    if(path==='/api/proactive'){
      if(opts.method==='POST'){
        posts.push(JSON.parse(opts.body));
        if(holdPost)await new Promise(resolve=>releasePost=resolve);
        if(conflict)return {ok:false,status:409,json:async()=>({error:'版本冲突'})};
        config={...JSON.parse(opts.body),revision:'r'+posts.length};
      }
      if(configFailure)throw Error('设置读取失败');
      return {ok:true,json:async()=>({config:clone(config),status:clone(status)})};
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
