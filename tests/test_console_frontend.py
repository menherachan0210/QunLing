from test_proactive_frontend import run_browser


def test_member_reply_agreement_edit_uses_bound_member_and_tracks_dirty_state():
    run_browser(r"""
  select('123');
  consoleMemory={items:[{kind:'interaction',sender_id:'456',title:'成员 456',revision:'a'.repeat(64),
    value:{mode:'mention_only',rule:'没@不接话'},status:'active'}],total:1,next_offset:null};
  await evaluate('loadMemory()');element('memory-list').children[0].children[0].click();
  assert.equal(element('memory-interaction-fields').hidden,false);
  assert.equal(element('memory-profile-fields').hidden,true);
  element('memory-reply-mode').value='conditional';element('memory-reply-mode').input();
  element('memory-reply-rule').value='只在我明确提问时参与';element('memory-reply-rule').input();
  assert.equal(evaluate('memoryDirty'),true);
  await element('memory-save').click();
  const write=consoleMemoryPosts.find(row=>row.action==='save');
  assert.equal(write.sender_id,'456');assert.equal(write.group_id,'123');assert.equal(write.kind,'interaction');
  assert.equal(write.value.mode,'conditional');assert.equal(write.value.rule,'只在我明确提问时参与');
  assert.equal(evaluate('memoryDirty'),false);
""")


def test_optional_features_and_independent_providers_survive_console_submission():
    run_browser(r"""
  setup.settings_revision='providers-v1';
  setup.settings={base_url:'https://chat.example/v1',chat_model:'chat',features:[],api_key_saved:true};
  await evaluate('poll()');
  featureFields.find(field=>field.value==='search').checked=true;
  for(const role of ['vision','image','search','decision','reconcile']){
    element(role+'-base-url').value='https://'+role+'.example/v1';
    element(role+'-api-key').value=role+'-private-secret';
  }
  element('decision-model').value='fast-judge';element('reconcile-model').value='reviewer';
  element('settings').input();
  const draft=JSON.parse(stored.get('qunling-console-draft:ai'));
  assert.deepEqual(draft.values.features,['search']);
  assert.equal(draft.values.decision_model,'fast-judge');
  assert.ok(!JSON.stringify(draft).includes('private-secret'));
  await element('settings').submit({preventDefault(){},currentTarget:element('settings')});
  assert.deepEqual(aiPosts[0].features,['search']);
  for(const role of ['vision','image','search','decision','reconcile']){
    assert.equal(aiPosts[0][role+'_base_url'],'https://'+role+'.example/v1');
    assert.equal(aiPosts[0][role+'_api_key'],role+'-private-secret');
  }
  assert.equal(aiPosts[0].reconcile_model,'reviewer');
  setup.settings={...setup.settings,...aiPosts[0]};setup.settings_revision='providers-v2';setup.stage='ready';
  await evaluate('poll()');
  for(const role of ['vision','image','search','decision','reconcile'])assert.equal(element(role+'-api-key').value,'');
  assert.equal(stored.has('qunling-console-draft:ai'),false);
""")


def test_provider_draft_recovery_requires_new_endpoint_secret_and_keeps_navigation():
    run_browser(r"""
  setup.settings_revision='providers-v1';
  setup.settings={base_url:'https://chat.example/v1',chat_model:'chat',features:[],
    decision_base_url:'https://old.example/v1',decision_api_key_saved:true};
  await evaluate('poll()');
  assert.equal(element('decision-api-key').required,false);
  stored.set('qunling-console-draft:ai',JSON.stringify({revision:'providers-v1',values:{
    features:['image'],image_model:'image',decision_base_url:'https://new.example/v1',decision_model:'fast'
  }}));
  evaluate('settingsDraftRestored=false');await evaluate('poll()');
  assert.equal(element('decision-base-url').value,'https://new.example/v1');
  assert.equal(element('decision-api-key').required,true);
  assert.equal(element('image-model').required,true);
  context.location.hash='#knowledge';windowEvents.hashchange();
  assert.equal(element('knowledge').hidden,false);
  assert.equal(element('models').hidden,true);
  assert.equal(element('decision-model').value,'fast');
  assert.equal(evaluate('settingsDirty'),true);
""")


def test_missing_optional_workers_do_not_block_disabling_features():
    run_browser(r"""
  setup.plugins_ready=false;setup.plugin_runtime_healthy=false;setup.stage='error';
  setup.settings_revision='optional-v1';
  setup.settings={base_url:'https://chat.example/v1',chat_model:'chat',features:['image'],image_model:'image',api_key_saved:true};
  await evaluate('poll()');
  for(const field of featureFields)field.checked=false;
  element('settings').input();
  assert.equal(element('save').disabled,false);
  assert.equal(element('image-model').required,false);
  await element('settings').submit({preventDefault(){},currentTarget:element('settings')});
  assert.deepEqual(aiPosts[0].features,[]);
""")


def test_memory_scoping_filter_pagination_and_late_response():
    run_browser(r"""
  select('123');
  consoleMemory={items:[{kind:'episode',title:'<img src=x>',value:'记忆正文',id:'id',revision:'a'.repeat(64),collection_id:'kb'}],total:31,next_offset:30};
  await evaluate('loadMemory()');
  assert.equal(consoleMemoryPosts.at(-1).group_id,'123');
  assert.equal(element('memory-list').children[0].children[0].textContent,'事件 · <img src=x>');
  await element('memory-next').click();assert.equal(consoleMemoryPosts.at(-1).offset,30);
  holdMemory=true;const pending=evaluate('loadMemory()');await settle();
  select('456');releaseMemory();await pending;
  assert.equal(element('memory-list').children.length,0);
""")


def test_memory_edit_conflict_keeps_draft_and_blocks_navigation_while_saving():
    run_browser(r"""
  select('123');
  consoleMemory.items=[{kind:'episode',title:'事件',value:'旧记忆',id:'id',revision:'a'.repeat(64),collection_id:'kb'}];
  await evaluate('loadMemory()');element('memory-list').children[0].children[0].click();
  input('memory-content','新记忆');consoleMemoryFailure=true;
  await element('memory-save').click();
  assert.equal(element('memory-content').value,'新记忆');
  assert.equal(evaluate('memoryDirty'),true);
  assert.ok(element('memory-editor-message').textContent.includes('已变化'));
  const sent=consoleMemoryPosts.at(-1);assert.equal(sent.group_id,'123');assert.equal(sent.value,'新记忆');
  assert.equal(sent.revision,'a'.repeat(64));
  let blocked=false;windowEvents.beforeunload({preventDefault(){blocked=true;}});assert.equal(blocked,true);
  consoleMemoryFailure=false;holdMemory=true;
  const pending=element('memory-save').click();await settle();select('456');
  assert.equal(evaluate('proactiveSelected'),'123');
  holdMemory=false;releaseMemory();await pending;
  assert.equal(evaluate('memoryDirty'),false);assert.equal(element('memory-editor').hidden,true);
""")


def test_lock_clears_memory_content_and_invalidates_inflight_reads():
    run_browser(r"""
  select('123');consoleMemory.items=[{kind:'episode',title:'私密',value:'私密内容'}];
  await evaluate('loadMemory()');element('memory-list').children[0].children[0].click();
  holdMemory=true;const pending=evaluate('loadMemory()');await settle();
  evaluate('locked("expired")');releaseMemory();await pending;
  assert.equal(element('memory-list').children.length,0);assert.equal(element('memory-content').value,'');
""")


def test_tasks_distinguish_processed_and_delivered_and_pause_old_pages():
    run_browser(r"""
  tasksData={tasks:[{id:'a'.repeat(32),conversation:'group:123',actor:'456',status:'completed',delivery:'unconfirmed',attempts:1,result_count:1,elapsed_seconds:2,created_at:1700000000}],counts:{completed:1},next_before:'1700000000:abc',sampled_at:1700000000};
  await evaluate('loadTasks()');
  assert.ok(element('tasks-list').children[0].children[0].textContent.includes('处理完成'));
  assert.equal(element('tasks-list').children[0].children[1].textContent,'QQ 发送未确认');
  await element('tasks-older').click();const count=taskPaths.length;
  await evaluate('loadTasks(false,true)');assert.equal(taskPaths.length,count);
  holdTasks=true;const pending=evaluate('loadTasks()');await settle();
  evaluate('locked("expired")');releaseTasks();await pending;
  assert.equal(element('tasks-list').children.length,0);
""")
