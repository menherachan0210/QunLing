"""Mixed foreground work and free conversation must not appear group-paused."""
from test_proactive_frontend import run_browser


def test_parallel_activity_is_visible_without_blocking_the_group():
    run_browser(r"""
  select('123');change('proactive-enabled',true);change('proactive-mode','active');await save();
  status={available:true,enabled:true,groups:{'123':{
    status_code:'pending',active_conversations:2,queued_conversations:1,explicit_requests:1
  }}};
  await refresh();
  assert.equal(element('proactive-runtime-state').textContent,'正在接话');
  assert.match(element('proactive-runtime-detail').textContent,/2 路接话处理中、1 路等待、1 个明确任务/);
  status.groups['123'].status_code='explicit_busy';
  await refresh();
  assert.match(element('proactive-runtime-detail').textContent,/其他人的对话可以继续/);
  for(const invalid of [-1, 'private-content', NaN, 9999999999]){
    status.groups['123'].active_conversations=invalid;
    await refresh();
    assert.ok(!element('proactive-runtime-detail').textContent.includes('路接话处理中'));
    assert.ok(!element('proactive-runtime-detail').textContent.includes('private-content'));
  }
""")
