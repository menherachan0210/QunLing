from test_proactive_frontend import run_browser


def test_diagnostics_are_lazy_text_only_and_support_filters_pages_details():
    run_browser(r"""
  assert.equal(requestPaths.length,0);
  requestsData.requests=[{request_id:'id1',group_id:'<img src=x onerror=1>',sender:'20000',origin:'mention',created:1700000000,elapsed_seconds:12,status:'failed',phase:'ended',reason:'tool_timeout'}];
  requestsData.next_before='1700000000:id1';
  await evaluate('loadRequests()');
  assert.equal(element('requests-list').children.length,1);
  assert.ok(element('requests-list').children[0].children[0].textContent.includes('<img src=x onerror=1>'));
  assert.equal(element('requests-older').hidden,false);
  await element('requests-older').click();
  assert.ok(requestPaths.at(-1).includes('before=1700000000%3Aid1'));
  element('requests-group').value='30000';element('requests-status').value='failed';
  await evaluate('loadRequests()');
  assert.ok(requestPaths.at(-1).includes('group=30000') && requestPaths.at(-1).includes('status=failed'));
  assert.ok(!requestPaths.at(-1).includes('before='));
  requestsData.requests[0].events=[{at:1700000000,phase:'tool',tool:'read_recent_group_context',attempt:2,reason:'tool_timeout'}];
  await evaluate('showRequest("id1")');
  assert.ok(requestPaths.at(-1).includes('request_id=id1'));
  assert.ok(element('request-detail').children[1].children[0].textContent.includes('工具执行超时'));
  requestFailure=true;await evaluate('loadRequests()');
  assert.equal(element('requests-list').children.length,0);
  assert.equal(element('request-detail').hidden,true);
  assert.equal(element('requests-older').hidden,true);
  assert.ok(element('requests-note').textContent.includes('暂不可用'));
""")


def test_late_diagnostic_response_cannot_reveal_records_after_lock():
    run_browser(r"""
  requestsData.requests=[{request_id:'private',group_id:'30000',sender:'20000'}];
  holdRequest=true;
  const pending=evaluate('loadRequests()');await settle();
  evaluate('locked("expired")');
  releaseRequest();await pending;
  assert.equal(element('requests-list').children.length,0);
  assert.equal(element('request-detail').hidden,true);
  assert.equal(element('console-content').hidden,true);
""")
