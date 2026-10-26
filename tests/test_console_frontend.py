from test_proactive_frontend import run_browser


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
