from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from deploy.updater.engine import read, write
from deploy.updater.locking import worker_lock
from deploy.updater.watchdog import poll


@pytest.fixture
def service(tmp_path):
    calls, states = [], {}
    engine = SimpleNamespace(root=tmp_path, project="test", state_path=tmp_path / "status.json",
                             active_path=tmp_path / "active.json")
    for name in ("app", "wizard", "file-worker", "napcat"):
        states[name] = {"Id": "container-" + name, "Config": {"Labels": {
            "com.docker.compose.project": "test", "com.docker.compose.service": name}},
            "State": {"Status": "running", "Health": {"Status": "healthy"},
                      "StartedAt": datetime.fromtimestamp(100, timezone.utc).isoformat()}}
    engine.inspect_service = lambda name: states[name]
    engine.run = lambda *args, **kwargs: calls.append(args)
    write(engine.active_path, {"services": {name: {} for name in states}})
    write(engine.state_path, {"phase": "completed"})
    return engine, states, calls


def test_transient_failure_recovers_without_restart(service):
    engine, states, calls = service
    states["app"]["State"]["Health"]["Status"] = "unhealthy"
    poll(engine, clock=lambda: 1000)
    states["app"]["State"]["Health"]["Status"] = "healthy"
    poll(engine, clock=lambda: 1050)
    assert not calls
    assert read(engine.root / "recovery.json")["app"]["bad_since"] is None


def test_restart_smallest_component_and_never_touch_qq(service):
    engine, states, calls = service
    for info in states.values():
        info["State"]["Health"]["Status"] = "unhealthy"
    poll(engine, clock=lambda: 1000)
    poll(engine, clock=lambda: 1091)
    assert calls == [("docker", "restart", "--time", "30", "container-file-worker")]
    for _ in range(10):
        poll(engine, clock=lambda: 1100)
    assert all(call[-1] != "container-napcat" for call in calls)
    assert sum(call[-1] == "container-file-worker" for call in calls) == 1


def test_cooldown_persists_across_monitor_instances(service):
    engine, states, calls = service
    states["app"]["State"]["Health"]["Status"] = "unhealthy"
    for timestamp in (1000, 1091, 1100, 1191, 1500, 1690):
        poll(engine, clock=lambda: timestamp)
    assert len(calls) == 1
    poll(engine, clock=lambda: 1692)
    assert len(calls) == 2
    poll(engine, clock=lambda: 1700)
    poll(engine, clock=lambda: 2800)
    assert len(calls) == 2  # Second failure doubles the cooldown.


@pytest.mark.parametrize("phase", ["building", "verifying", "rolling_back", "recovery_required", "checking"])
def test_no_recovery_during_updates_or_manual_repair(service, phase):
    engine, states, calls = service
    states["app"]["State"]["Health"]["Status"] = "unhealthy"
    write(engine.state_path, {"phase": phase})
    poll(engine, clock=lambda: 1000)
    poll(engine, clock=lambda: 2000)
    assert not calls


def test_deployment_lock_prevents_recovery(service):
    engine, states, calls = service
    states["app"]["State"]["Health"]["Status"] = "unhealthy"
    with worker_lock(engine.root) as acquired:
        assert acquired
        poll(engine, clock=lambda: 1000)
        poll(engine, clock=lambda: 2000)
    assert not calls


def test_manual_stop_and_pause_are_respected(service):
    engine, states, calls = service
    states["file-worker"]["State"].update(Status="exited", Health={"Status": "unhealthy"})
    states["app"]["State"].update(Paused=True, Health={"Status": "unhealthy"})
    poll(engine, clock=lambda: 1000)
    poll(engine, clock=lambda: 2000)
    assert not calls
