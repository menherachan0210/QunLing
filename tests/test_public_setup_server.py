"""Exercise the actual onboarding HTTP boundary without any live service."""

from copy import deepcopy
import socket
import threading
import time
from urllib.parse import urlsplit

import pytest
import requests

from bootstrap.api import SetupError
from bootstrap.server import SetupHTTPServer, SetupManager
from bootstrap.state import State, prepare
from bootstrap.update_client import UpdateError


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
        self.update_operations = []

    def snapshot(self):
        return {"configured": False, "settings": {"api_key_saved": True}}

    def submit(self, value):
        if not isinstance(value, dict):
            raise SetupError("Invalid settings")
        self.submissions.append(deepcopy(value))

    def refresh_qr(self):
        self.refreshes += 1

    def update_status(self):
        return {"phase": "idle", "busy": False}

    def check_update(self, value):
        self.update_operations.append(("check", deepcopy(value)))
        return {"phase": "available", "busy": False}

    def apply_update(self, value):
        self.update_operations.append(("apply", deepcopy(value)))
        return {"phase": "backing_up", "busy": True}


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
    for path in ("/api/status", "/api/admin", "/api/update"):
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


@pytest.mark.parametrize("path", ["/api/update/check", "/api/update/apply"])
@pytest.mark.parametrize(
    "override", [
        {"Origin": "https://evil.example"},
        {"Authorization": "Bearer wrong"},
        {"Host": "evil.example"},
    ],
)
def test_update_mutations_enforce_origin_token_and_host(endpoint, path, override):
    http, url, manager = endpoint
    response = http.post(
        url + path, json={}, headers={**headers(url), **override}, timeout=5
    )
    assert response.status_code == 403
    assert manager.update_operations == []


def test_update_routes_forward_only_authenticated_operations(endpoint):
    http, url, manager = endpoint
    response = http.get(url + "/api/update", headers=headers(url), timeout=5)
    assert response.status_code == 200
    assert response.json() == {"phase": "idle", "busy": False}
    assert http.post(
        url + "/api/update/check", json={}, headers=headers(url), timeout=5
    ).json()["phase"] == "available"
    response = http.post(
        url + "/api/update/apply",
        json={"revision": "a" * 40},
        headers=headers(url), timeout=5,
    )
    assert response.status_code == 202
    assert manager.update_operations == [("check", {}), ("apply", {"revision": "a" * 40})]


@pytest.mark.parametrize("uncertain, expected", [(True, 503), (False, 409)])
def test_update_response_distinguishes_rejection_from_unknown_outcome(endpoint, uncertain, expected):
    http, url, manager = endpoint

    def apply(_value):
        raise UpdateError("无法确认", uncertain=uncertain)

    manager.apply_update = apply
    response = http.post(
        url + "/api/update/apply", json={"revision": "a" * 40},
        headers=headers(url), timeout=5,
    )
    assert response.status_code == expected


@pytest.mark.parametrize("declared_length", ["4096", "1073741824", "invalid"])
def test_unauthorized_post_body_drain_is_bounded_and_does_not_mutate(endpoint, declared_length):
    _, url, manager = endpoint
    parsed = urlsplit(url)
    # A peer may never finish a small body, claim a huge length, or send invalid
    # framing. Every case must still receive the auth rejection promptly.
    with socket.create_connection((parsed.hostname, parsed.port), timeout=2) as client:
        client.sendall((
            "POST /api/update/apply HTTP/1.1\r\n"
            f"Host: {parsed.netloc}\r\n"
            "Authorization: Bearer wrong\r\n"
            f"Origin: {url}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {declared_length}\r\n\r\n"
        ).encode() + b"{")
        result = b""
        while b"\r\n\r\n" not in result:
            chunk = client.recv(4096)
            assert chunk, "Server closed before returning its authentication result"
            result += chunk
        assert result.startswith(b"HTTP/1.0 403")
    assert manager.update_operations == []
    assert manager.submissions == []


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
            "bot": {"adapter_runtime_values": {"bot_account_id": "synthetic", "qq_status": {
                "state": "online", "online": True, "transport_connected": True,
                "checked_at": time.time(),
            }}}
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
