"""Update proxy boundaries, revision pinning and configuration mutual exclusion."""

from copy import deepcopy
from types import SimpleNamespace
import threading

import pytest
import requests

from bootstrap.api import SetupError
from bootstrap.server import SetupHTTPServer, SetupManager
from bootstrap.state import State, prepare
from bootstrap.update_client import UpdateClient, UpdateError

CURRENT = "a" * 40
LATEST = "b" * 40
CONFIG = {
    "base_url": "https://api.example.invalid/v1", "api_key": "fixture",
    "chat_model": "chat", "image_model": "image",
}


def status(**changes):
    return {
        "available": True, "current_revision": CURRENT, "latest_revision": LATEST,
        "update_available": True, "phase": "available", "message": "发现更新",
        "checked_at": "2026-10-20T00:00:00Z",
        "repository": "owner/repository", "branch": "main", "busy": False,
        **changes,
    }


class HTTP:
    def __init__(self, value=None, code=200, error=None):
        self.value = value or status()
        self.code = code
        self.error = error
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(status_code=self.code, json=lambda: self.value)


@pytest.fixture
def client():
    client = UpdateClient("http://updater:5320", "fixture-private-token")
    client.http = HTTP()
    return client


def test_client_never_follows_redirects_or_uses_environment_proxy():
    client = UpdateClient("http://updater:5320", "fixture-private-token")
    assert client.http.trust_env is False
    assert client.http.headers["Authorization"] == "Bearer fixture-private-token"
    client.http = HTTP(code=302)
    result = client.status()
    assert result["available"] is False
    assert client.http.calls[0][1]["allow_redirects"] is False
    assert "fixture-private-token" not in repr(result)


@pytest.mark.parametrize("url", [
    "file:///tmp/private", "http://user:secret@updater:5320",
    "http://updater:5320/?command=rm", "http://updater:5320/#secret",
    "http://updater:5320/other", "https://",
])
def test_client_rejects_invalid_deployment_url(url):
    with pytest.raises(SetupError):
        UpdateClient(url, "fixture")


def test_no_updater_or_unreachable_updater_is_readable_and_not_busy(client):
    unsupported = UpdateClient("", "fixture").status()
    assert unsupported["phase"] == "unsupported"
    assert unsupported["busy"] is False
    client.http = HTTP(error=requests.ConnectionError("private-internal-address"))
    unavailable = client.status()
    assert unavailable["phase"] == "unavailable"
    assert unavailable["busy"] is False
    assert "private-internal-address" not in repr(unavailable)


def test_client_whitelists_status_and_pins_mutation(client):
    client.http.value = status(config={"api_key": "private"}, command="arbitrary")
    result = client.apply(LATEST.upper())
    assert "config" not in result and "command" not in result
    assert client.http.calls == [
        (("POST", "http://updater:5320/apply"), {
            "json": {"revision": LATEST}, "timeout": (3, 8), "allow_redirects": False
        })
    ]


@pytest.mark.parametrize("revision", ["main", "a" * 39, "a" * 41, "a" * 40 + "\n", None, 1])
def test_client_rejects_unpinned_revision_without_request(client, revision):
    with pytest.raises(UpdateError):
        client.apply(revision)
    assert not client.http.calls


@pytest.mark.parametrize("value", [
    [], status(busy="false"), status(available=None),
    status(latest_revision="main"), status(current_revision={"secret": "x"}),
    status(message={}), status(checked_at=123),
])
def test_malformed_status_cannot_enable_update(client, value):
    client.http.value = value
    result = client.status()
    assert result["available"] is False
    assert result["update_available"] is False


def test_transport_failure_on_apply_is_ambiguous(client):
    client.http = HTTP(error=requests.Timeout("sensitive transport data"))
    with pytest.raises(UpdateError) as captured:
        client.apply(LATEST)
    assert captured.value.uncertain is True
    assert "sensitive" not in str(captured.value)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.delenv("QQBOT_UPDATER_URL", raising=False)
    prepare(tmp_path / "private", tmp_path / "napcat")
    manager = SetupManager(
        State(tmp_path / "private"), "http://app:5300", "http://napcat:6099",
        tmp_path, "7zz",
    )
    manager.update(stage="ready", plugins_ready=True)
    manager.updater = UpdateClient("http://updater:5320", "fixture")
    manager.updater.http = HTTP()
    return manager


@pytest.mark.parametrize("body", [
    {}, [], {"revision": "main"}, {"revision": LATEST, "command": "anything"},
    {"revision": LATEST, "repository": "other/repo"},
])
def test_manager_rejects_arbitrary_update_parameters(manager, body):
    with pytest.raises(SetupError):
        manager.apply_update(body)
    assert manager.updater.http.calls == []


def test_manager_rejects_stale_target_and_configuration_conflict(manager):
    with pytest.raises(SetupError, match="版本已变化"):
        manager.apply_update({"revision": CURRENT})
    manager.update(stage="configuring")
    with pytest.raises(SetupError, match="配置正在进行"):
        manager.apply_update({"revision": LATEST})
    assert all(call[0][0] == "GET" for call in manager.updater.http.calls)


def test_busy_update_blocks_configuration_and_duplicate_updates(manager):
    manager.updater.http.value = status(busy=True, phase="building")
    for action in (
        lambda: manager.submit(CONFIG),
        lambda: manager.apply_update({"revision": LATEST}),
        lambda: manager.check_update({}),
    ):
        with pytest.raises(SetupError, match="更新"):
            action()
    assert manager.pending is None
    assert all(call[0][0] == "GET" for call in manager.updater.http.calls)


def test_unavailable_updater_does_not_block_normal_configuration(manager):
    manager.updater.http = HTTP(error=requests.ConnectionError())
    manager.submit(CONFIG)
    assert manager.pending["chat_model"] == "chat"


def test_ambiguous_apply_keeps_configuration_locked_until_status_recovers(manager):
    responses = [status()]

    def request(method, *_args, **_kwargs):
        if method == "POST" or not responses:
            raise requests.Timeout()
        return SimpleNamespace(status_code=200, json=lambda: responses.pop(0))

    manager.updater.http = SimpleNamespace(request=request)
    with pytest.raises(UpdateError):
        manager.apply_update({"revision": LATEST})
    assert manager.update_pending is True
    with pytest.raises(SetupError, match="更新"):
        manager.submit(CONFIG)
    manager.updater.http = HTTP(status(phase="completed", busy=False))
    assert manager.update_status()["busy"] is False
    manager.submit(CONFIG)
    assert manager.pending is not None


def test_inflight_apply_rejects_configuration_without_holding_snapshot_lock(manager):
    accepted = threading.Event()
    release = threading.Event()
    submit_done = threading.Event()
    errors = []

    def apply(revision):
        accepted.set()
        assert release.wait(3)
        return status(busy=True, phase="backing_up")

    manager.updater.apply = apply
    manager.updater.status = lambda: status(
        busy=manager.update_pending, phase="backing_up" if manager.update_pending else "available"
    )
    updater = threading.Thread(target=lambda: manager.apply_update({"revision": LATEST}))

    def submit():
        try:
            manager.submit(deepcopy(CONFIG))
        except SetupError as exc:
            errors.append(exc)
        finally:
            submit_done.set()

    updater.start()
    assert accepted.wait(3)
    submitter = threading.Thread(target=submit)
    submitter.start()
    assert submit_done.wait(1), "Configuration should be rejected without waiting for the updater"
    assert errors and manager.pending is None
    assert manager.snapshot()["stage"] == "ready"
    release.set()
    updater.join(3)
    submitter.join(3)
    assert errors and manager.pending is None


def test_apply_allows_updater_http_callback_while_rejecting_configuration(manager):
    """The worker rechecks GET /api/status before accepting the apply request."""
    server = SetupHTTPServer(("127.0.0.1", 0), manager)
    service = threading.Thread(target=server.serve_forever, daemon=True)
    service.start()
    url = f"http://127.0.0.1:{server.server_port}"
    headers = {"Authorization": "Bearer " + manager.secrets["setup_token"], "Origin": url}
    callback_received = threading.Event()
    release = threading.Event()
    results = []
    failures = []

    def apply(_revision):
        with requests.Session() as callback:
            callback.trust_env = False
            result = callback.get(url + "/api/status", headers=headers, timeout=2)
            assert result.status_code == 200
            assert result.json()["stage"] == "ready"
        callback_received.set()
        assert release.wait(3)
        return status(busy=True, phase="queued")

    manager.updater.apply = apply

    def post_update():
        try:
            with requests.Session() as request:
                request.trust_env = False
                results.append(request.post(
                    url + "/api/update/apply", headers=headers,
                    json={"revision": LATEST}, timeout=5,
                ))
        except Exception as exc:
            failures.append(exc)

    request = threading.Thread(target=post_update, daemon=True)
    try:
        request.start()
        assert callback_received.wait(2), "Readiness callback deadlocked on the wizard snapshot lock"
        with requests.Session() as other_request:
            other_request.trust_env = False
            response = other_request.post(
                url + "/api/configure", headers=headers, json=CONFIG, timeout=2
            )
            assert response.status_code == 400
            assert "更新" in response.json()["error"]
            manager.updater.http.error = requests.ConnectionError()
            result = other_request.get(url + "/api/update", headers=headers, timeout=2)
            assert result.json()["busy"] is True
            assert result.json()["available"] is False
        assert manager.pending is None
        release.set()
        request.join(3)
        assert not failures
        assert len(results) == 1 and results[0].status_code == 202
        assert results[0].json()["phase"] == "queued"
        assert manager.update_inflight is False
    finally:
        release.set()
        request.join(3)
        server.shutdown()
        server.server_close()
        service.join(2)


def test_check_update_only_accepts_empty_object(manager):
    for value in (None, [], {"repository": "other"}):
        with pytest.raises(SetupError):
            manager.check_update(value)
    assert manager.updater.http.calls == []


def test_browser_waits_for_recovered_target_and_never_calls_disconnect_success():
    """Run the real browser script through disconnect, stale status and recovery."""
    from pathlib import Path
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute the browser interaction regression")
    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const nodes = new Map();
function element(id) {
  if (!nodes.has(id)) nodes.set(id, {
    id, disabled: false, hidden: false, textContent: "", value: "", elements: {},
    addEventListener(name, fn) { this[name] = fn; }
  });
  return nodes.get(id);
}
element("settings").elements.api_key = {value:"", placeholder:""};
const stored = new Map([["qqbot-setup", "fixture"]]);
const old = "a".repeat(40), target = "b".repeat(40);
let remote = {
  available: true, busy: false, update_available: true,
  current_revision: old, latest_revision: target, phase: "available",
  message: "发现更新", repository: "owner/repo", branch: "main"
};
let offline = false, disconnectAfterApply = false;
const context = vm.createContext({
  document: {getElementById: element},
  location: {hash: "", pathname: "/"},
  history: {replaceState() {}},
  sessionStorage: {
    getItem(key) { return stored.get(key); },
    setItem(key, value) { stored.set(key, value); },
    removeItem(key) { stored.delete(key); }
  },
  window: {confirm() { return true; }},
  URLSearchParams, AbortController, setTimeout, clearTimeout,
  setInterval() {}, FormData: class {},
  fetch: async(path, opts) => {
    if (offline) throw new Error("offline");
    if (path === "/api/status") return {ok:true, json:async()=>({
      plugins_ready: true, stage:"ready", configured:true, qq_connected:true,
      qq_logged_in:true, settings:{}, message:"已连接"
    })};
    if (path === "/api/update/apply") {
      assert.deepEqual(JSON.parse(opts.body), {revision:target});
      if (disconnectAfterApply) {
        // An immediate completed POST still must not mark the UI complete
        // before a subsequent status response confirms the running revision.
        offline = true;
        return {ok:true, json:async()=>({...remote, phase:"completed", busy:false})};
      }
      remote = {...remote, phase:"building", busy:true};
    }
    return {ok:true, json:async()=>({...remote})};
  }
});
async function settle() { for(let i=0;i<8;i++) await new Promise(resolve=>setImmediate(resolve)); }
(async()=>{
  vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
  await settle();
  assert.equal(element("apply-update").disabled, false);
  disconnectAfterApply = true;
  await element("apply-update").click();
  assert.equal(element("save").disabled, true);
  assert.notEqual(element("update-phase").textContent, "更新完成");
  assert.match(element("update-message").textContent, /尚未确认/);
  offline = false;
  remote = {...remote, phase:"recovery_required", busy:true};
  await vm.runInContext("poll()", context);
  assert.equal(element("save").disabled, true);
  assert.equal(element("apply-update").disabled, true);
  assert.equal(element("update-phase").textContent, "需要恢复");
  remote = {...remote, phase:"completed", current_revision:old, busy:false};
  await vm.runInContext("poll()", context);
  assert.equal(element("save").disabled, true, "stale completion must not unlock settings");
  assert.equal(element("update-phase").textContent, "等待确认");
  remote = {...remote, current_revision:target, update_available:false};
  await vm.runInContext("poll()", context);
  assert.equal(element("update-phase").textContent, "更新完成");
  assert.equal(element("save").disabled, false);
  assert.equal(stored.has("qunling-update-pending"), false);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    source = Path(__file__).resolve().parents[1] / "bootstrap/static/app.js"
    result = subprocess.run(
        [node, "-e", script, str(source)], text=True, capture_output=True, timeout=15
    )
    assert result.returncode == 0, result.stdout + result.stderr
