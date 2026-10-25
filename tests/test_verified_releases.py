import pytest
from deploy.updater.releases import verified_revision
from deploy.updater.engine import UpdateEngine, UpdateError
from deploy.updater import engine as module


def test_stable_channel_ignores_skipped_linux_and_pull_request_runs():
    runs = [{"id": i, "head_sha": str(i)*40, "head_branch": "main", "conclusion": "success",
             "event": event, "head_repository": {"full_name": "owner/repo"}}
            for i, event in [(1,"pull_request"), (2,"push"), (3,"push")]]
    def fetch(url):
        if "workflows" in url:
            return {"workflow_runs": runs}
        return {"jobs": [{"name": "Public source and setup tests", "conclusion": "success"},
                         {"name": "Linux image and fresh initialization",
                          "conclusion": "skipped" if "/2/" in url else "success"}]}
    assert verified_revision("owner/repo", "main", fetch) == "3"*40
    with pytest.raises(RuntimeError):
        verified_revision("owner/repo", "main", lambda _: {"workflow_runs": []})


def test_update_requires_two_advancing_healthy_probes(monkeypatch, tmp_path):
    clock = [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    engine = UpdateEngine(tmp_path)
    values = iter([
        {"stage": "recovering", "plugin_runtime_healthy": False},
        {"stage": "ready", "plugin_runtime_healthy": True, "plugin_runtime_probe": "1"},
        {"stage": "ready", "plugin_runtime_healthy": True, "plugin_runtime_probe": "1"},
        {"stage": "ready", "plugin_runtime_healthy": True, "plugin_runtime_probe": "2"},
    ])
    engine.wizard_status = lambda: {"configured": True, "plugins_ready": True, **next(values)}
    engine.wait_ready(timeout=20)
    assert clock[0] == 15
    engine.wizard_status = lambda: {"configured": True, "plugins_ready": True, "stage": "recovering"}
    with pytest.raises(UpdateError, match="超时"):
        engine.wait_ready(timeout=10)
