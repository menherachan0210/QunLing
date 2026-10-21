"""QQ badges must describe a recent health check, never a cached account ID."""

from test_proactive_frontend import run_browser


def test_only_confirmed_online_health_can_render_connected():
    run_browser(r"""
  assert.equal(element('summary-qq').textContent,'状态待确认');
  assert.equal(element('summary-qq-card').dataset.tone,'warning');
  assert.equal(element('ready').hidden,true);
  setup={...setup,qq_state:'online',qq_status_fresh:true,qq_checked_at:1000};
  await evaluate('poll()');
  assert.equal(element('summary-qq').textContent,'已连接');
  assert.equal(element('summary-qq-card').dataset.tone,'success');
  assert.equal(element('connection-card').dataset.state,'connected');
  assert.equal(element('ready').hidden,false);
  assert.equal(element('refresh').disabled,true);
  assert.match(element('last-sync').textContent,/1970/);
  const actualCheck=element('last-sync').textContent;
  await evaluate('poll()');
  assert.equal(element('last-sync').textContent,actualCheck);
  setup.qq_connected=false;
  await evaluate('poll()');
  assert.equal(element('summary-qq').textContent,'状态待确认');
  assert.equal(element('ready').hidden,true);
""")


def test_offline_disconnected_and_waiting_login_are_distinct():
    run_browser(r"""
  setup={...setup,qq_status_fresh:true,qq_checked_at:1000,qr_image:'data:image/png;base64,test'};
  for(const [code,label] of [['offline','QQ 已离线'],['disconnected','消息通道断开'],['connecting','连接中'],['waiting_login','等待登录']]){
    setup.qq_state=code;
    setup.qq_logged_in=['disconnected','connecting'].includes(code);
    await evaluate('poll()');
    assert.equal(element('summary-qq').textContent,label);
    assert.equal(element('summary-qq-card').dataset.tone,'warning');
    assert.equal(element('ready').hidden,true);
    assert.notEqual(element('connection-card').dataset.state,'connected');
    assert.equal(element('refresh').disabled,setup.qq_logged_in);
    assert.equal(element('qr').hidden,setup.qq_logged_in);
    assert.ok(!element('status').textContent.includes('机器人已连接'));
  }
""")


def test_stale_or_invalid_health_cannot_keep_badges_qr_or_controls_logged_in():
    run_browser(r"""
  setup={...setup,qq_state:'online',qq_status_fresh:true,qq_checked_at:1000,qr_image:'old-login-code'};
  await evaluate('poll()');
  for(const invalid of [
    {qq_status_fresh:false}, {qq_state:'unknown'}, {qq_state:'__proto__'},
    {qq_checked_at:null}, {qq_checked_at:'1000'}, {qq_checked_at:-1}
  ]){
    setup={...setup,qq_state:'online',qq_status_fresh:true,qq_checked_at:1000,...invalid};
    await evaluate('poll()');
    assert.equal(element('summary-qq').textContent,'状态待确认');
    assert.equal(element('summary-qq-card').dataset.tone,'warning');
    assert.equal(element('global-state').dataset.tone,'warning');
    assert.equal(element('ready').hidden,true);
    assert.equal(element('refresh').disabled,false);
    assert.equal(element('qr').hidden,true);
    assert.notEqual(element('connection-card').dataset.state,'connected');
    assert.match(element('qq-status').textContent,/无法确认/);
  }
""")


def test_network_failure_clears_connected_and_retains_actual_check_time():
    run_browser(r"""
  setup={...setup,qq_state:'online',qq_status_fresh:true,qq_checked_at:1000};
  await evaluate('poll()');
  evaluate("const healthyFetch=fetch; fetch=async(path,...args)=>{if(path==='/api/status')throw new Error('网络中断'); return healthyFetch(path,...args);}");
  await evaluate('poll()');
  assert.equal(element('summary-qq').textContent,'状态待确认');
  assert.equal(element('summary-qq-card').dataset.tone,'warning');
  assert.equal(element('ready').hidden,true);
  assert.equal(element('connection-card').dataset.state,'waiting');
  assert.match(element('last-sync').textContent,/1970.*本次读取失败/);
  evaluate('fetch=healthyFetch');
  await evaluate('poll()');
  assert.equal(element('summary-qq').textContent,'已连接');
""")


def test_browser_expires_old_health_while_waiting_for_further_requests():
    run_browser(r"""
  setup={...setup,qq_state:'online',qq_status_fresh:true,qq_checked_at:1000};
  await evaluate('poll()');
  evaluate('expireQQStatus(qqStatusReceivedAt + 29999)');
  assert.equal(element('summary-qq').textContent,'已连接');
  evaluate('expireQQStatus(qqStatusReceivedAt + 30000)');
  assert.equal(element('summary-qq').textContent,'状态待确认');
  assert.equal(element('ready').hidden,true);
  assert.equal(element('refresh').disabled,false);
  assert.match(element('last-sync').textContent,/1970.*状态已过期/);
  await evaluate('poll()');
  assert.equal(element('summary-qq').textContent,'已连接');
""")
