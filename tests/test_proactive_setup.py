"""Operator APIs: no QQ account or paid model calls are used."""
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest
import requests

from bootstrap.api import LangBotAPI, ProactiveAPIError, SetupError
from bootstrap.proactive import settings, groups, save, clear_memory
from bootstrap.server import SetupHTTPServer
from qunling.proactive.config import defaults


def manager():
    state = {"configured": True, "plugins_ready": True, "stage": "ready",
             "qq_logged_in": True, "qq_connected": True}
    return SimpleNamespace(lock=threading.RLock(), update_inflight=False, pending=None,
        update_status=lambda: {"busy": False}, snapshot=lambda: state,
        state=SimpleNamespace(read=lambda **_: {"bot_uuid": "trusted-bot"}),
        api=SimpleNamespace(call=Mock(return_value={"config": defaults(), "status": {"available": True}})))


def test_proxy_uses_trusted_bot_and_does_not_accept_browser_identity():
    m = manager()
    settings(m)
    groups(m)
    save(m, defaults())
    calls = m.api.call.call_args_list
    assert calls[0].args == ("GET", "/api/v1/platform/bots/trusted-bot/proactive")
    assert calls[1].args == ("GET", "/api/v1/platform/bots/trusted-bot/proactive/groups")
    with pytest.raises(SetupError):
        save(m, {**defaults(), "bot_uuid": "evil"})
    assert m.api.call.call_count == 3


@pytest.mark.parametrize("field,value", [("configured", False), ("plugins_ready", False),
    ("stage", "configuring"), ("qq_logged_in", False), ("qq_connected", False)])
def test_unready_bot_cannot_save(field, value):
    m = manager()
    m.snapshot()[field] = value
    with pytest.raises(SetupError):
        save(m, defaults())
    m.api.call.assert_not_called()


def test_update_and_configuration_block_mutation():
    for field in ("update_inflight", "pending"):
        m = manager()
        setattr(m, field, True)
        with pytest.raises(SetupError):
            save(m, defaults())
        m.api.call.assert_not_called()
    m = manager()
    m.update_status = lambda: {"busy": True}
    with pytest.raises(SetupError):
        save(m, defaults())


def test_initial_install_has_disabled_configuration_without_host_calls():
    m = manager()
    m.snapshot()["configured"] = False
    assert settings(m) == {"config": defaults(), "status": {"available": False,
                         "message": "请先完成 AI 配置并连接 QQ。"}}
    m.api.call.assert_not_called()


@pytest.mark.parametrize("reason,status", [("revision_conflict", 409), ("group_not_joined", 400),
    ("groups_unavailable", 503), ("secret-raw-provider-error", 503)])
def test_api_exposes_only_fixed_safe_errors(reason, status):
    client = LangBotAPI("http://localhost:5300", None)
    response = Mock(status_code=status, ok=False)
    response.json.return_value = {"error_code": reason, "msg": "secret-provider-key"}
    client.http = SimpleNamespace(request=Mock(return_value=response))
    with pytest.raises(ProactiveAPIError) as info:
        client.call("POST", "/api/v1/platform/bots/trusted/proactive", defaults())
    assert info.value.status == status
    assert "secret" not in str(info.value)


@pytest.fixture
def http_endpoint():
    m = manager()
    m.secrets = {"setup_token": "fixture-token"}
    m.proactive_settings = lambda: settings(m)
    m.proactive_groups = lambda: {"groups": [{"group_id": "123456", "name": "群名称"}]}
    m.save_proactive = lambda data: save(m, data)
    server = SetupHTTPServer(("127.0.0.1", 0), m)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = requests.Session()
    session.trust_env = False
    url = f"http://127.0.0.1:{server.server_port}"
    yield session, url, m, {"Authorization": "Bearer fixture-token", "Origin": url}
    session.close()
    server.shutdown()
    server.server_close()
    thread.join(2)


def test_http_authentication_and_save_contract(http_endpoint):
    http, url, m, headers = http_endpoint
    for path in ("/api/proactive", "/api/proactive/groups"):
        assert http.get(url + path, timeout=5).status_code == 401
        assert http.get(url + path, headers=headers, timeout=5).status_code == 200
    assert http.post(url + "/api/proactive", json=defaults(), timeout=5).status_code == 403
    bad = {**headers, "Origin": "https://evil.example"}
    assert http.post(url + "/api/proactive", json=defaults(), headers=bad, timeout=5).status_code == 403
    result = http.post(url + "/api/proactive", json=defaults(), headers=headers, timeout=5)
    assert result.status_code == 200 and result.json()["config"] == defaults()


def test_http_conflict_remains_conflict(http_endpoint):
    http, url, m, headers = http_endpoint
    def conflict(_):
        raise ProactiveAPIError("设置已变化", 409)
    m.save_proactive = conflict
    result = http.post(url + "/api/proactive", json=defaults(), headers=headers, timeout=5)
    assert result.status_code == 409


def test_memory_reset_requires_auth_origin_and_typed_group(http_endpoint):
    http, url, m, headers = http_endpoint
    m.clear_group_memory = lambda values: clear_memory(m, values)
    body = {"group_id": "123", "confirmation": "123", "request_id": "a" * 32}
    path = url + "/api/proactive/memory/reset"
    assert http.post(path, json=body, timeout=5).status_code == 403
    assert http.post(path, json=body, headers={**headers, "Origin": "https://evil.example"}, timeout=5).status_code == 403
    assert http.post(path, json={**body, "confirmation": "456"}, headers=headers, timeout=5).status_code == 400
    assert http.post(path, json={**body, "bot_uuid": "other"}, headers=headers, timeout=5).status_code == 400
    m.api.call.assert_not_called()
    assert http.post(path, json=body, headers=headers, timeout=5).status_code == 200
    m.api.call.assert_called_once_with("POST", "/api/v1/platform/bots/trusted-bot/proactive/memory/reset", body)


def test_memory_reset_is_blocked_during_update_or_setup():
    m = manager()
    body = {"group_id": "123", "confirmation": "123", "request_id": "a" * 32}
    m.update_inflight = True
    with pytest.raises(SetupError):
        clear_memory(m, body)
    m.update_inflight = False
    m.snapshot()["plugins_ready"] = False
    with pytest.raises(SetupError):
        clear_memory(m, body)
    m.api.call.assert_not_called()
