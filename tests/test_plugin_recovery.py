"""Transport fault injection; no model calls or QQ messages."""
import asyncio
import ast
import importlib.util
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from qunling import runtime_health

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def patched(tmp_path):
    import langbot_plugin
    sdk = Path(langbot_plugin.__file__).parent.parent
    patcher = load("recovery_patcher", ROOT / "scripts/Apply-RuntimePatches.py")
    name = "sdk-transport-recovery.patch"
    source = (ROOT / "patches" / name).read_text(encoding="utf-8")
    _, paths = patcher.runtime_payload(source, "sdk")
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sdk / path, target)
    plan = patcher.build_plan(name, tmp_path, source, "sdk")
    patcher.execute_plans([plan], apply=True)
    assert patcher.build_plan(name, tmp_path, source, "sdk").state == "already-applied"
    return NS(stdio=load("recovery_stdio", tmp_path / "langbot_plugin/runtime/io/connections/stdio.py"),
              handler=load("recovery_handler", tmp_path / "langbot_plugin/runtime/io/handler.py"),
              client=load("recovery_client", tmp_path / "langbot_plugin/runtime/io/controllers/stdio/client.py"))


def writer():
    return NS(write=Mock(), drain=AsyncMock(), close=Mock(),
              wait_closed=AsyncMock(), transport=NS(abort=Mock()))


@pytest.mark.asyncio
async def test_cancel_partial_frame_aborts_instead_of_poisoning_next_call(patched):
    output = writer()
    started = asyncio.Event()
    async def blocked():
        started.set()
        await asyncio.Event().wait()
    output.drain.side_effect = blocked
    conn = patched.stdio.StdioConnection(asyncio.StreamReader(), output, chunk_size=20,
                                         reserve_first_message_send=True)
    task = asyncio.create_task(conn.send('x' * 100))
    await started.wait()
    assert b'chunk_start' in output.write.call_args.args[0]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    output.transport.abort.assert_called_once()
    with pytest.raises(patched.stdio.ConnectionClosedError):
        await conn.send('{"action":"__ping"}')


@pytest.mark.asyncio
async def test_cancel_waiting_for_send_lock_does_not_abort_another_sender(patched):
    output = writer()
    conn = patched.stdio.StdioConnection(asyncio.StreamReader(), output)
    async with conn._send_lock:
        task = asyncio.create_task(conn.send('hello'))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    output.transport.abort.assert_not_called()


@pytest.mark.asyncio
async def test_unfinished_chunk_cannot_swallow_heartbeat(patched):
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"type":"chunk_start","total_size":100}\n{"action":"__ping","seq_id":1}\n')
    conn = patched.stdio.StdioConnection(reader, writer())
    with pytest.raises(patched.stdio.ConnectionClosedError):
        await asyncio.wait_for(conn.receive(), .5)


@pytest.mark.asyncio
async def test_blocked_pipe_close_is_bounded_and_aborted(patched):
    output = writer()
    output.wait_closed.side_effect = asyncio.Event().wait
    conn = patched.stdio.StdioConnection(asyncio.StreamReader(), output)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(conn.close(), 3)
    output.transport.abort.assert_called_once()


@pytest.mark.asyncio
async def test_failed_pipe_close_still_reaps_owned_process(patched, monkeypatch):
    client = patched.client.StdioClientController(command='unused', args=[], env={})
    client.connection = NS(close=AsyncMock(side_effect=TimeoutError))
    process = NS(returncode=None)
    client.process = process
    stop = AsyncMock(return_value=True)
    monkeypatch.setattr(patched.client, 'stop_process', stop)
    await client.close()
    stop.assert_awaited_once_with(process)
    assert client.process is None and client.connection is None


@pytest.mark.asyncio
async def test_ping_deadline_includes_blocked_send_and_health_expires(monkeypatch):
    monkeypatch.setattr(runtime_health, 'PING_TIMEOUT', .01)
    handler = NS(ping=AsyncMock(side_effect=asyncio.Event().wait))
    connected = asyncio.Event()
    connected.set()
    connector = NS(handler=handler, _runtime_handler=lambda: handler,
                   _connected=connected, is_enable_plugin=True)
    with pytest.raises(TimeoutError):
        await runtime_health.ping(connector)
    assert runtime_health.snapshot(connector) == {'state':'recovering','healthy':False}
    handler.ping = AsyncMock(return_value={})
    await runtime_health.ping(connector)
    assert runtime_health.snapshot(connector)['healthy'] is True
    connector.handler = object()
    assert runtime_health.snapshot(connector)['healthy'] is False
    connector.handler = handler
    monkeypatch.setattr(runtime_health, 'HEALTH_TTL', -1)
    assert runtime_health.snapshot(connector)['state'] == 'unknown'


@pytest.mark.asyncio
async def test_action_deadline_includes_unsent_request(patched, monkeypatch):
    from langbot_plugin.entities.io.actions.enums import CommonAction
    from langbot_plugin.api.proxies import invocation
    monkeypatch.setattr(invocation, 'invocation_capability', lambda _: None)
    host = NS(seq_id_index=0, requests_total=0, _closed=False, resp_waiters={}, resp_queues={},
              resolve_outbound_action_context=lambda _: None,
              _send_message=AsyncMock(side_effect=lambda *a, **kw: None))
    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()
    host._send_message = blocked
    with pytest.raises(patched.handler.ActionCallTimeoutError):
        await asyncio.wait_for(patched.handler.Handler.call_action(host, CommonAction.PING, {}, timeout=.01), .5)
    assert not host.resp_waiters and not host.resp_queues


def test_wizard_does_not_call_installed_plugins_healthy(tmp_path):
    import threading
    import time
    from bootstrap.server import SetupManager
    manager = object.__new__(SetupManager)
    manager.lock = threading.RLock()
    manager.qq_sampled_at = time.monotonic()
    manager.plugin_sampled_at = time.monotonic()
    manager.status = dict(stage='ready',configured=True,plugins_ready=True,
                          plugin_runtime_state='recovering',qq_connected=True)
    value = manager.snapshot()
    assert value['stage'] == 'recovering'
    assert value['qq_connected'] is True
    assert value['plugin_runtime_healthy'] is False
    manager.status['plugin_runtime_state'] = 'ready'
    assert manager.snapshot()['stage'] == 'ready'
    manager.plugin_sampled_at -= 100
    assert manager.snapshot()['stage'] == 'recovering'


@pytest.mark.asyncio
async def test_normal_multiframe_messages_remain_readable(patched):
    reader = asyncio.StreamReader()
    output = writer()
    output.write.side_effect = reader.feed_data
    sender = patched.stdio.StdioConnection(asyncio.StreamReader(), output, chunk_size=32,
                                          reserve_first_message_send=True)
    receiver = patched.stdio.StdioConnection(reader, writer(), reserve_first_message_decode=True)
    import json
    content = json.dumps({'text':'测试中文多帧消息' * 25}, ensure_ascii=False)
    await sender.send(content)
    assert await asyncio.wait_for(receiver.receive(), 1) == content
    await sender.send('{"action":"__ping"}')
    assert await asyncio.wait_for(receiver.receive(), 1) == '{"action":"__ping"}'


@pytest.mark.asyncio
async def test_cancelled_controller_shutdown_still_reaps(patched, monkeypatch):
    client = patched.client.StdioClientController(command='unused', args=[], env={})
    client.connection = NS(close=AsyncMock(side_effect=asyncio.CancelledError))
    client.process = NS(returncode=None)
    stop = AsyncMock(return_value=True)
    monkeypatch.setattr(patched.client, 'stop_process', stop)
    with pytest.raises(asyncio.CancelledError):
        await client.close()
    stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_send_cannot_prevent_heartbeat_reconnect(monkeypatch):
    # Exercise the actual pinned Host loop, including its three-failure policy.
    host_root = ROOT / 'langbot/src/langbot'
    if not host_root.exists():
        host_root = Path(importlib.util.find_spec('langbot').origin).parent
    tree = ast.parse((host_root / 'pkg/plugin/connector.py').read_text(encoding='utf-8'))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'heartbeat_loop')
    namespace = dict(asyncio=asyncio, _HEARTBEAT_INTERVAL_SEC=.001, _HEARTBEAT_FAILURE_THRESHOLD=3)
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<host-heartbeat>', 'exec'), namespace)
    monkeypatch.setattr(runtime_health, 'PING_TIMEOUT', .01)
    broken = NS(ping=AsyncMock(side_effect=asyncio.Event().wait))
    connector = NS(handler=broken, _closing=False, _connected=asyncio.Event(),
                   ap=NS(logger=Mock()), is_enable_plugin=True)
    connector._connected.set()
    connector._runtime_handler = lambda: connector.handler
    connector.ping_plugin_runtime = lambda: runtime_health.ping(connector)
    reconnected = []
    async def healthy():
        connector._closing = True
        return {}
    def reconnect():
        assert not connector._connected.is_set()
        reconnected.append(True)
        connector.handler = NS(ping=healthy)
        connector._connected.set()
    connector.schedule_reconnect = reconnect
    await asyncio.wait_for(namespace['heartbeat_loop'](connector), .5)
    assert broken.ping.await_count == 3
    assert reconnected == [True]
    assert runtime_health.snapshot(connector)['healthy'] is True


def test_recovery_card_does_not_claim_ready_with_online_qq():
    from test_proactive_frontend import run_browser
    run_browser(r"""
      setup={...setup,qq_state:'online',qq_status_fresh:true,qq_checked_at:1000,plugin_runtime_healthy:false,stage:'recovering'};
      await evaluate('poll()');
      assert.equal(element('summary-qq').textContent,'已连接');
      assert.equal(element('summary-plugins').textContent,'恢复中');
      assert.equal(element('ready').hidden,true);
      assert.equal(element('global-state').textContent,'组件恢复中');
      setup={...setup,plugin_runtime_healthy:true,stage:'ready'};
      await evaluate('poll()');
      assert.equal(element('ready').hidden,false);
      assert.equal(element('summary-plugins').textContent,'已就绪');
    """)
