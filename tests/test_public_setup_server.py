"""Exercise the actual onboarding HTTP boundary without any live service."""

from copy import deepcopy
import threading

import pytest
import requests

from bootstrap.api import SetupError
from bootstrap.server import SetupHTTPServer, SetupManager
from bootstrap.state import State, prepare


class Manager:
    secrets = {"setup_token": "offline-test-token"}

    def __init__(self, directory):
        self.state = State(directory)
        self.state.write(
            {"user": "offline@example.invalid", "password": "offline-password"},
            "langbot-admin.json",
        )
        self.submissions = []
        self.refreshes = 0

    def snapshot(self):
        return {"configured": False, "settings": {"api_key_saved": True}}

    def submit(self, value):
        if not isinstance(value, dict):
            raise SetupError("Invalid settings")
        self.submissions.append(deepcopy(value))

    def refresh_qr(self):
        self.refreshes += 1


@pytest.fixture
def endpoint(tmp_path):
    manager = Manager(tmp_path)
    server = SetupHTTPServer(("127.0.0.1", 0), manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    http = requests.Session()
    http.trust_env = False
    yield http, url, manager
    http.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def headers(url):
    return {"Authorization": "Bearer offline-test-token", "Origin": url}


def test_page_is_public_but_state_and_admin_require_token(endpoint):
    http, url, _ = endpoint
    page = http.get(url, timeout=5)
    assert page.status_code == 200
    assert "offline-password" not in page.text
    assert page.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    for path in ("/api/status", "/api/admin"):
        assert http.get(url + path, timeout=5).status_code == 401
    response = http.get(url + "/api/status", headers=headers(url), timeout=5)
    assert response.status_code == 200
    assert "offline-password" not in response.text
    assert "offline-test-token" not in response.text


@pytest.mark.parametrize(
    "host", ["evil.example", "localhost.evil.example", "127.0.0.1.evil.example"]
)
def test_untrusted_host_cannot_read_state_even_with_token(endpoint, host):
    http, url, _ = endpoint
    assert (
        http.get(
            url + "/api/status", headers={**headers(url), "Host": host}, timeout=5
        ).status_code
        == 403
    )


@pytest.mark.parametrize("origin", [None, "https://evil.example", "null"])
def test_cross_site_post_does_not_mutate(endpoint, origin):
    http, url, manager = endpoint
    values = headers(url)
    if origin is None:
        values.pop("Origin")
    else:
        values["Origin"] = origin
    assert (
        http.post(
            url + "/api/configure", json={}, headers=values, timeout=5
        ).status_code
        == 403
    )
    assert not manager.submissions


def test_authorized_json_is_submitted_without_echoing_key(endpoint):
    http, url, manager = endpoint
    response = http.post(
        url + "/api/configure",
        json={"api_key": "private-fixture"},
        headers=headers(url),
        timeout=5,
    )
    assert response.status_code == 202
    assert manager.submissions == [{"api_key": "private-fixture"}]
    assert "private-fixture" not in response.text


def test_large_and_invalid_payloads_are_rejected(endpoint):
    http, url, manager = endpoint
    assert (
        http.post(
            url + "/api/configure",
            json={"x": "a" * 17000},
            headers=headers(url),
            timeout=5,
        ).status_code
        == 413
    )
    assert (
        http.post(
            url + "/api/configure", json=["unexpected"], headers=headers(url), timeout=5
        ).status_code
        == 400
    )
    assert (
        http.post(
            url + "/api/configure", data="x", headers=headers(url), timeout=5
        ).status_code
        == 415
    )
    assert not manager.submissions


def test_admin_and_qr_are_available_only_to_authenticated_operator(endpoint):
    http, url, manager = endpoint
    assert (
        http.get(url + "/api/admin", headers=headers(url), timeout=5).json()["user"]
        == "offline@example.invalid"
    )
    assert (
        http.post(
            url + "/api/qr/refresh", json={}, headers=headers(url), timeout=5
        ).status_code
        == 202
    )
    assert manager.refreshes == 1


@pytest.mark.parametrize("stage", ["configuring", "error"])
def test_qq_heartbeat_cannot_hide_configuration_progress_or_error(
    tmp_path, monkeypatch, stage
):
    from types import SimpleNamespace
    import bootstrap.server as module

    prepare(tmp_path / "private", tmp_path / "napcat")
    manager = SetupManager(
        State(tmp_path / "private"),
        "http://app:5300",
        "http://napcat:6099",
        tmp_path,
        "7zz",
    )
    manager.napcat = SimpleNamespace(call=lambda *_: {"isLogin": True})
    manager.api = SimpleNamespace(
        call=lambda *_: {
            "bot": {"adapter_runtime_values": {"bot_account_id": "synthetic"}}
        }
    )
    monkeypatch.setattr(module, "link_napcat", lambda *_: False)
    manager.update(
        stage=stage, configured=True, message="configuration-specific-status"
    )
    manager._qq_status(
        {"configured": True, "onebot_url": "ws://app/bot", "bot_uuid": "fixture"}
    )
    assert manager.snapshot()["qq_connected"] is True
    assert manager.snapshot()["stage"] == stage
    assert manager.snapshot()["message"] == "configuration-specific-status"
