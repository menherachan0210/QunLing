"""Offline update transactions against the real engine; no Docker or network."""

from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tarfile
import threading

import pytest
import requests

from deploy.updater import engine as module
from deploy.updater import volume


OLD = "1" * 40
NEW = "2" * 40
OTHER = "3" * 40
JOB = "a" * 24
PROJECT = "custom-deployment"


def compose_document():
    destinations = {
        "init": [
            ("setup-state", "/var/lib/qqbot-setup"),
            ("napcat-config", "/var/lib/napcat-config"),
        ],
        "app": [("langbot-data", "/opt/qq-ai-bot/langbot/data")],
        "napcat": [
            ("napcat-qq", "/app/.config/QQ"),
            ("napcat-config", "/app/napcat/config"),
            ("napcat-plugins", "/app/napcat/plugins"),
        ],
        "wizard": [("setup-state", "/var/lib/qqbot-setup")],
        "updater": [
            ("update-data", "/var/lib/qqbot-updater"),
            ("setup-state", "/var/lib/qqbot-setup"),
        ],
    }
    services = {}
    for name in module.SERVICES:
        services[name] = {
            "image": "initial-" + name,
            "volumes": [
                {"type": "volume", "source": logical, "target": target}
                for logical, target in destinations[name]
            ],
        }
    services["updater"]["volumes"].append(
        {
            "type": "bind",
            "source": "/var/run/docker.sock",
            "target": "/var/run/docker.sock",
        }
    )
    return {
        "name": PROJECT,
        "services": services,
        "volumes": {name: {} for name in (*module.DATA_VOLUMES, "update-data")},
        "networks": {"default": {"name": PROJECT + "_default"}},
    }


class DockerModel:
    """Record commands and model persisted data independently of runtime code."""

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.events = []
        self.current = compose_document()
        self.candidate = compose_document()
        for document in (self.current, self.candidate):
            for mount in document["services"]["updater"]["volumes"]:
                if mount["source"] == "update-data":
                    mount["target"] = str(root)
        self.real_volumes = {
            name: "persistent_" + name.replace("-", "_")
            for name in self.current["volumes"]
        }
        self.images = {
            name: "sha256:" + format(index + 10, "064x")
            for index, name in enumerate(module.SERVICES)
        }
        self.data = {name: b"original-" + name.encode() for name in module.DATA_VOLUMES}
        self.backups = {}
        self.remote_output = NEW + "\trefs/heads/main\n"
        self.fetched = NEW
        self.protocol = 1
        self.fail = None
        self.foreign_volume = None
        self.active = "old"
        self.builds = 0
        self.backup_count = 0
        self.restore_count = 0
        self.verify_count = 0
        self.revision = OLD
        self.running_jobs = {}

    def inspect(self, service):
        mounts = []
        for mount in self.current["services"][service]["volumes"]:
            value = {"Type": mount["type"], "Destination": mount["target"]}
            if mount["type"] == "volume":
                value["Name"] = self.real_volumes[mount["source"]]
                if service == "updater" and mount["source"] == "update-data":
                    value["Destination"] = str(self.root)
            else:
                value["Source"] = mount["source"]
            mounts.append(value)
        return {
            "Image": self.images[service],
            "Mounts": mounts,
            "Config": {
                "Labels": {
                    "com.docker.compose.project": PROJECT,
                    "com.docker.compose.service": service,
                }
            },
            "NetworkSettings": {"Networks": {PROJECT + "_default": {}}},
        }

    def run(self, args, **kwargs):
        args = list(args)
        self.calls.append((args, deepcopy(kwargs)))
        if args[:2] == ["git", "ls-remote"]:
            return self.remote_output
        if args[:2] == ["git", "init"]:
            Path(args[2]).mkdir(parents=True, exist_ok=True)
            return ""
        if args[:2] == ["git", "-C"]:
            if "rev-parse" in args:
                return self.fetched + "\n"
            if "checkout" in args:
                module.write(
                    Path(args[2]) / "deploy/updater/protocol.json",
                    {"version": self.protocol},
                )
            return ""
        if args[:2] == ["docker", "ps"]:
            service = next(
                (
                    part.split("=", 2)[-1]
                    for part in args
                    if part.startswith("label=com.docker.compose.service=")
                ),
                None,
            )
            if service:
                return "container-" + service
            labels = dict(
                part.removeprefix("label=").split("=", 1)
                for part in args
                if part.startswith("label=")
            )
            return self.running_jobs.get(
                (
                    labels.get("io.qunling.update-project"),
                    labels.get("io.qunling.update-job"),
                ),
                "",
            )
        if args[:2] == ["docker", "inspect"]:
            return json.dumps([self.inspect(args[2].removeprefix("container-"))])
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps(
                [
                    {
                        "Config": {
                            "Labels": {
                                "org.opencontainers.image.revision": self.revision,
                            }
                        }
                    }
                ]
            )
        if args[:3] == ["docker", "volume", "inspect"]:
            owner = "another-project" if args[3] == self.foreign_volume else PROJECT
            return json.dumps([{"Labels": {"com.docker.compose.project": owner}}])
        if args[:2] == ["docker", "build"]:
            self.builds += 1
            self.events.append(("build", args[args.index("-t") + 1]))
            if self.fail == "build":
                raise module.UpdateError("synthetic build failure")
            return ""
        if args[:2] == ["docker", "pull"]:
            self.events.append(("pull", args[2]))
            return ""
        if args[:2] == ["docker", "compose"]:
            assert args[2:4] == ["--project-name", PROJECT]
            path = Path(args[args.index("-f") + 1])
            command = args[args.index("-f") + 2 :]
            if command[:1] == ["config"]:
                value = deepcopy(
                    self.candidate if "source" in path.parts else self.current
                )
                env = kwargs.get("env", {})
                for name in ("init", "app", "wizard"):
                    value["services"][name]["image"] = env.get(
                        "QQBOT_APP_IMAGE", value["services"][name]["image"]
                    )
                value["services"]["updater"]["image"] = env.get(
                    "QQBOT_UPDATER_IMAGE", value["services"]["updater"]["image"]
                )
                return json.dumps(value)
            stack = "new" if path.name == "new-compose.json" else "old"
            if command[:1] == ["stop"]:
                self.events.append(("stop", stack))
                return ""
            assert command[:1] == ["up"], command
            self.events.append(("start", stack))
            self.active = stack
            if stack == "new":
                self.data = {
                    name: b"new-version-" + name.encode() for name in self.data
                }
            if self.fail == stack + "_start":
                raise module.UpdateError("synthetic start failure")
            return ""
        if args[:2] == ["docker", "run"]:
            if "volume" not in args:
                name = args[args.index("--name") + 1]
                self.events.append(("launch", name))
                labels = dict(
                    args[index + 1].split("=", 1)
                    for index, part in enumerate(args)
                    if part == "--label"
                )
                self.running_jobs[
                    (
                        labels["io.qunling.update-project"],
                        labels["io.qunling.update-job"],
                    )
                ] = name
                return "synthetic-job-container"
            operation = args[args.index("volume") + 1]
            mounted = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
            source = next(value for value in mounted if ",dst=/data" in value)
            actual = source.split("src=", 1)[1].split(",", 1)[0]
            logical = next(
                name for name, value in self.real_volumes.items() if value == actual
            )
            self.events.append((operation, logical))
            if operation == "backup":
                self.backup_count += 1
                if self.fail == "backup" and self.backup_count == 2:
                    raise module.UpdateError("synthetic backup failure")
                self.backups[logical] = self.data[logical]
            elif operation == "restore":
                self.restore_count += 1
                if self.fail == "restore" and self.restore_count == 2:
                    raise module.UpdateError("synthetic restore failure")
                self.data[logical] = self.backups[logical]
            else:
                assert operation == "verify", operation
                self.verify_count += 1
                if self.fail == "verify" and self.verify_count == 2:
                    raise module.UpdateError("synthetic archive validation failure")
            return ""
        raise AssertionError(("unexpected external command", args))


@pytest.fixture
def transaction(tmp_path, monkeypatch):
    monkeypatch.setenv("QQBOT_IMAGE_MODE", "build")
    monkeypatch.setenv("QQBOT_UPDATE_CHANNEL", "development")
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    driver = DockerModel(tmp_path)
    engine = module.UpdateEngine(
        tmp_path,
        runner=driver,
        project=PROJECT,
        repository="menherachan0210/QunLing",
        branch="main",
    )

    def wizard():
        if driver.active == "new" and driver.fail in {"new_ready", "restore", "verify"}:
            return {"stage": "error", "configured": False, "plugins_ready": False}
        return {"stage": "ready", "configured": True, "plugins_ready": True,
                "plugin_runtime_healthy": True}

    monkeypatch.setattr(engine, "wizard_status", wizard)
    job = {
        "id": JOB,
        "revision": NEW,
        "phase": "queued",
        "mutation_started": False,
        "backup_complete": False,
    }
    module.write(tmp_path / "jobs" / JOB / "job.json", job)
    engine.publish(
        phase="queued", job_id=JOB, latest_revision=NEW, current_revision=OLD
    )
    return engine, driver


def state(engine):
    return module.read(engine.state_path)


def test_success_builds_before_downtime_and_reuses_exact_project_volumes(transaction):
    engine, docker = transaction
    engine.execute(JOB)
    assert state(engine)["phase"] == "completed"
    assert state(engine)["current_revision"] == NEW
    assert docker.events[:2] == [
        ("build", PROJECT + "-app:" + NEW),
        ("build", PROJECT + "-updater:" + NEW),
    ]
    first_stop = docker.events.index(("stop", "old"))
    assert all(event[0] in {"build", "pull"} for event in docker.events[:first_stop])
    backups = [name for operation, name in docker.events if operation == "backup"]
    assert backups == []
    assert not any(operation == "restore" for operation, _ in docker.events)
    old = module.read(engine.root / "jobs" / JOB / "old-compose.json")
    active = module.read(engine.active_path)
    for service, image_id in docker.images.items():
        assert old["services"][service]["image"] == image_id
    for logical, actual in docker.real_volumes.items():
        assert (
            old["volumes"][logical]
            == active["volumes"][logical]
            == {
                "name": actual,
                "external": True,
            }
        )
    assert active["services"]["app"]["image"] == PROJECT + "-app:" + NEW
    assert active["services"]["updater"]["image"] == PROJECT + "-updater:" + NEW


def test_failed_build_never_stops_or_backs_up_running_services(transaction):
    engine, docker = transaction
    before = deepcopy(docker.data)
    docker.fail = "build"
    engine.execute(JOB)
    assert state(engine)["phase"] == "failed"
    assert docker.data == before
    assert [operation for operation, _ in docker.events] == ["build"]


@pytest.mark.parametrize(
    "change",
    [
        "missing_declaration",
        "missing_mount",
        "swapped_mount",
        "moved_target",
        "read_only",
        "network",
    ],
)
def test_candidate_data_volume_changes_are_rejected_before_downtime(
    transaction, change
):
    engine, docker = transaction
    if change == "missing_declaration":
        docker.candidate["volumes"].pop("langbot-data")
    elif change == "missing_mount":
        docker.candidate["services"]["app"]["volumes"] = []
    elif change == "swapped_mount":
        docker.candidate["services"]["app"]["volumes"][0]["source"] = "setup-state"
    elif change == "moved_target":
        docker.candidate["services"]["app"]["volumes"][0]["target"] = "/empty-data"
    elif change == "read_only":
        docker.candidate["services"]["app"]["volumes"][0]["read_only"] = True
    else:
        docker.candidate["networks"]["default"]["name"] = "another-network"
    engine.execute(JOB)
    assert state(engine)["phase"] == "failed"
    assert not any(
        operation in {"stop", "backup", "start"} for operation, _ in docker.events
    )


def test_new_update_never_calls_backup_even_when_backup_helper_is_broken(
    transaction,
):
    engine, docker = transaction
    docker.fail = "backup"
    engine.execute(JOB)
    assert state(engine)["phase"] == "completed"
    assert docker.backup_count == 0
    assert ("start", "new") in docker.events
    assert not any(operation == "restore" for operation, _ in docker.events)
    assert not module.read(engine.root / "jobs" / JOB / "job.json")["backup_complete"]


@pytest.mark.parametrize("failure", ["new_start", "new_ready"])
def test_failed_version_restores_images_and_preserves_current_data(
    transaction, failure
):
    engine, docker = transaction
    docker.fail = failure
    engine.execute(JOB)
    assert state(engine)["phase"] == "rolled_back"
    assert state(engine)["current_revision"] == OLD
    assert all(value.startswith(b"new-version-") for value in docker.data.values())
    restores = [name for operation, name in docker.events if operation == "restore"]
    assert restores == []
    verified = [name for operation, name in docker.events if operation == "verify"]
    assert verified == []
    assert docker.backup_count == 0
    assert ("start", "old") in docker.events
    active = module.read(engine.active_path)
    assert active["services"]["app"]["image"] == docker.images["app"]


def legacy_interrupted(engine, docker):
    directory = engine.root / "jobs" / JOB
    old, volumes = engine.capture_current(directory)
    module.write(directory / "new-compose.json", old)
    job = module.read(directory / "job.json")
    job.update(phase="verifying", services_stopped=True, mutation_started=True,
               backup_complete=True, volumes=volumes, worker_image=old["services"]["updater"]["image"],
               old_revision=OLD)
    module.write(directory / "job.json", job)
    docker.backups = deepcopy(docker.data)
    docker.data = {name: b"new-version-" + name.encode() for name in module.DATA_VOLUMES}
    docker.active = "new"


def test_any_bad_archive_prevents_all_volume_restores_and_old_service_restart(
    transaction,
):
    engine, docker = transaction
    docker.fail = "verify"
    legacy_interrupted(engine, docker)
    engine.execute(JOB)
    assert state(engine)["phase"] == "recovery_required"
    assert engine.snapshot()["busy"]
    assert [name for operation, name in docker.events if operation == "verify"] == list(
        module.DATA_VOLUMES[:2]
    )
    assert not any(operation == "restore" for operation, _ in docker.events)
    assert ("start", "old") not in docker.events
    assert all(value.startswith(b"new-version-") for value in docker.data.values())
    assert all(value.startswith(b"original-") for value in docker.backups.values())
    with pytest.raises(module.UpdateError):
        engine.check()
    with pytest.raises(module.UpdateError):
        engine.start(NEW)


def test_restore_failure_keeps_business_stopped_and_disallows_another_update(
    transaction,
):
    engine, docker = transaction
    docker.fail = "restore"
    legacy_interrupted(engine, docker)
    engine.execute(JOB)
    assert ("start", "old") not in docker.events
    assert docker.events[-1] == ("restore", module.DATA_VOLUMES[1])
    job = module.read(engine.root / "jobs" / JOB / "job.json")
    assert (
        job["backup_complete"] and job["mutation_started"] and not job.get("finished")
    )
    assert engine.snapshot()["busy"], "Partial restore must not authorize a new update"
    with pytest.raises(module.UpdateError):
        engine.start(NEW)


def test_interrupted_partial_restore_can_recover_without_new_build_or_new_backup(
    transaction,
):
    engine, docker = transaction
    original = deepcopy(docker.data)
    docker.fail = "restore"
    legacy_interrupted(engine, docker)
    engine.execute(JOB)
    assert engine.snapshot()["busy"]
    docker.fail = None
    docker.events.clear()
    engine.execute(JOB)
    assert state(engine)["phase"] == "rolled_back"
    assert docker.data == original
    assert not any(operation in {"build", "backup"} for operation, _ in docker.events)
    assert [
        name for operation, name in docker.events if operation == "restore"
    ] == list(module.DATA_VOLUMES)
    assert docker.events[-1] == ("start", "old")


def test_reexecuting_completed_job_never_stops_services_again(transaction):
    engine, docker = transaction
    engine.execute(JOB)
    first = deepcopy(docker.events)
    engine.execute(JOB)
    assert state(engine)["phase"] == "completed"
    assert docker.events == first


@pytest.mark.parametrize("foreign", ["langbot-data", "setup-state"])
def test_foreign_data_volume_is_rejected_before_build_or_downtime(transaction, foreign):
    engine, docker = transaction
    docker.foreign_volume = docker.real_volumes[foreign]
    engine.execute(JOB)
    assert state(engine)["phase"] == "failed"
    assert docker.events == []


@pytest.mark.parametrize(
    "service,source,target",
    [
        ("app", "/var/run/docker.sock", "/var/run/docker.sock"),
        ("wizard", "/etc", "/host"),
        ("napcat", r"C:\Users\operator\data", "/data"),
        ("updater", "/etc", "/var/run/docker.sock"),
        ("updater", "/var/run/docker.sock", "/another.sock"),
    ],
)
def test_candidate_host_mounts_are_rejected(transaction, service, source, target):
    engine, docker = transaction
    docker.candidate["services"][service]["volumes"].append(
        {
            "type": "bind",
            "source": source,
            "target": target,
        }
    )
    with pytest.raises(module.UpdateError, match="挂载"):
        engine.resolve(engine.root / "source/deploy/compose/compose.yaml")


def test_volume_helper_uses_named_volumes_and_never_host_checkout_paths(transaction):
    engine, docker = transaction
    legacy_interrupted(engine, docker)
    engine.execute(JOB)
    helpers = [
        args
        for args, _ in docker.calls
        if args[:2] == ["docker", "run"] and "volume" in args
    ]
    assert len(helpers) == 10
    for args in helpers:
        assert args[args.index("--network") + 1] == "none"
        mounts = [args[i + 1] for i, value in enumerate(args) if value == "--mount"]
        assert len(mounts) == 2 and all(
            m.startswith("type=volume,src=persistent_") for m in mounts
        )
        assert not any(str(engine.root) in value for value in mounts)
        assert args[args.index("volume") - 1] == docker.images["updater"]


@pytest.mark.parametrize(
    "output",
    [
        "",
        NEW + "\trefs/heads/other\n",
        "bad\trefs/heads/main\n",
        NEW + "\trefs/heads/main\n" + OTHER + "\trefs/heads/main\n",
    ],
)
def test_remote_revision_rejects_nonexact_branch_response(transaction, output):
    engine, docker = transaction
    docker.remote_output = output
    with pytest.raises(module.UpdateError):
        engine.remote_revision()


@pytest.mark.parametrize("when", ["before_fetch", "after_fetch"])
def test_checkout_rejects_main_movement_without_checking_out_another_commit(
    transaction, when
):
    engine, docker = transaction
    if when == "before_fetch":
        docker.remote_output = OTHER + "\trefs/heads/main\n"
    else:
        docker.fetched = OTHER
    with pytest.raises(module.UpdateError, match="提交|变化"):
        engine.checkout(NEW, engine.root / "checkout")
    assert not any("checkout" in args for args, _ in docker.calls)


def test_checkout_detaches_only_the_previously_checked_sha(transaction):
    engine, docker = transaction
    directory = engine.root / "checkout"
    engine.checkout(NEW, directory)
    commands = [args for args, _ in docker.calls]
    assert ["git", "-C", str(directory), "checkout", "--detach", NEW] in commands
    assert [
        "git",
        "-C",
        str(directory),
        "fetch",
        "--depth=1",
        "origin",
        NEW,
    ] in commands
    assert [
        "git",
        "-C",
        str(directory),
        "remote",
        "add",
        "origin",
        "https://github.com/menherachan0210/QunLing.git",
    ] in commands


def test_unknown_initial_image_revision_is_not_reported_as_the_remote_commit(
    transaction,
):
    engine, docker = transaction
    docker.revision = "unknown"
    engine.publish(phase="idle", current_revision="")
    assert engine.snapshot()["current_revision"] == ""
    engine.check()
    assert state(engine)["current_revision"] == ""
    assert engine.snapshot()["update_available"] is True


@pytest.mark.parametrize("revision", [OTHER, "../main", "main", NEW + ";command", None])
def test_start_rejects_unchecked_or_invalid_revision_before_launch(
    transaction, revision
):
    engine, docker = transaction
    engine.publish(phase="available")
    with pytest.raises(module.UpdateError):
        engine.start(revision)
    assert not any(operation == "launch" for operation, _ in docker.events)


def test_job_launch_uses_exact_updater_image_and_only_named_data_volumes(transaction):
    engine, docker = transaction
    engine.publish(phase="available")
    engine.start(NEW)
    args = next(args for args, _ in docker.calls if args[:3] == ["docker", "run", "-d"])
    assert args[args.index("worker") - 1] == docker.images["updater"]
    assert not any("com.docker.compose.service" in part for part in args)
    mounts = [args[i + 1] for i, value in enumerate(args) if value == "--mount"]
    assert len(mounts) == 3
    assert any("type=volume,src=persistent_update_data," in mount for mount in mounts)
    assert any(
        "type=volume,src=persistent_setup_state," in mount
        and mount.endswith(",readonly")
        for mount in mounts
    )
    assert [mount for mount in mounts if mount.startswith("type=bind")] == [
        "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
    ]


def test_finished_job_recovery_reconstructs_terminal_status_after_publish_interruption(
    transaction,
):
    engine, docker = transaction
    directory = engine.root / "jobs" / JOB
    job = module.read(directory / "job.json")
    job.update(
        phase="verifying",
        finished=True,
        revision=NEW,
        terminal_phase="completed",
        terminal_revision=NEW,
        terminal_message="completed",
    )
    module.write(directory / "job.json", job)
    engine.publish(phase="verifying")
    engine.execute(JOB)
    assert engine.snapshot()["phase"] == "completed"
    assert engine.snapshot()["current_revision"] == NEW
    assert docker.events == []


@pytest.mark.parametrize(
    "phase",
    ["queued", "building", "backing_up", "restarting", "verifying", "rolling_back"],
)
def test_watchdog_recovers_busy_orphan_once_without_replacing_its_transaction(
    transaction, phase
):
    engine, docker = transaction
    engine.publish(phase=phase)
    before = state(engine)
    job_path = engine.root / "jobs" / JOB / "job.json"
    original_job = job_path.read_bytes()

    engine.monitor_once()
    assert len(docker.events) == 1
    operation, name = docker.events[0]
    assert operation == "launch"
    assert name.startswith(PROJECT + "-update-" + JOB + "-recovery-")
    command = next(
        args for args, _ in docker.calls if args[:3] == ["docker", "run", "-d"]
    )
    assert command[-3:] == [docker.images["updater"], "worker", JOB]
    assert state(engine) == before
    assert job_path.read_bytes() == original_job

    # The recovery worker is now visible to Docker. A later watchdog tick must
    # not start a second one while the first still owns the transaction.
    engine.monitor_once()
    assert docker.events == [("launch", name)]
    assert state(engine) == before


def test_watchdog_does_not_duplicate_an_existing_live_worker(transaction):
    engine, docker = transaction
    engine.publish(phase="verifying")
    before = state(engine)
    docker.running_jobs[PROJECT, JOB] = "already-running-worker"
    engine.monitor_once()
    assert docker.events == []
    assert state(engine) == before
    assert len(docker.calls) == 1
    query, _ = docker.calls[0]
    assert query[:3] == ["docker", "ps", "-q"]
    assert "label=io.qunling.update-project=" + PROJECT in query
    assert "label=io.qunling.update-job=" + JOB in query


@pytest.mark.parametrize(
    "phase",
    [
        "checking",
        "recovery_required",
        "idle",
        "available",
        "completed",
        "rolled_back",
        "failed",
    ],
)
def test_watchdog_leaves_checks_manual_recovery_and_terminal_states_untouched(
    transaction, phase
):
    engine, docker = transaction
    engine.publish(phase=phase)
    before = state(engine)
    engine.monitor_once()
    assert state(engine) == before
    assert docker.calls == []
    assert docker.events == []


def test_watchdog_docker_outage_keeps_busy_state_for_a_later_retry(
    transaction, monkeypatch
):
    engine, docker = transaction
    engine.publish(phase="backing_up")
    before = state(engine)
    run = docker.run

    def disconnected(args, **kwargs):
        if args[:3] == ["docker", "ps", "-q"]:
            raise module.UpdateError("synthetic Docker outage")
        return run(args, **kwargs)

    monkeypatch.setattr(docker, "run", disconnected)
    with pytest.raises(module.UpdateError, match="synthetic Docker outage"):
        engine.monitor_once()
    assert state(engine) == before and engine.snapshot()["busy"]
    assert docker.events == []
    monkeypatch.setattr(docker, "run", run)
    engine.monitor_once()
    assert len(docker.events) == 1 and docker.events[0][0] == "launch"


def volume_directory(tmp_path):
    destination = (tmp_path / "data").resolve()
    assert destination.parent == tmp_path.resolve() and destination.name == "data"
    destination.mkdir()
    return destination


def make_archive(path, entries):
    with tarfile.open(path, "w:") as archive:
        for name, kind, value in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            if kind == tarfile.REGTYPE:
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
            else:
                member.linkname = value
                archive.addfile(member)
    module.write(
        str(path) + ".json",
        {"size": path.stat().st_size, "sha256": volume.digest(path)},
    )


def test_real_volume_backup_restore_preserves_old_data_and_removes_new_files(tmp_path):
    data = volume_directory(tmp_path)
    (data / "nested").mkdir()
    (data / "nested/登录状态.txt").write_text("original session", encoding="utf-8")
    (data / "memory.bin").write_bytes(b"\x00original-memory\xff")
    backup = tmp_path / "backup.tar"
    volume.backup(data, backup)
    volume.verify(backup)
    (data / "memory.bin").write_bytes(b"new schema")
    (data / "nested/登录状态.txt").unlink()
    (data / "new-version-only.db").write_bytes(b"must disappear on rollback")
    volume.restore(data, backup)
    assert (data / "nested/登录状态.txt").read_text(
        encoding="utf-8"
    ) == "original session"
    assert (data / "memory.bin").read_bytes() == b"\x00original-memory\xff"
    assert not (data / "new-version-only.db").exists()


@pytest.mark.parametrize("corruption", ["bytes", "size", "missing_manifest"])
def test_bad_backup_cannot_clear_existing_volume(tmp_path, corruption):
    data = volume_directory(tmp_path)
    (data / "keep.db").write_bytes(b"existing database")
    backup = tmp_path / "backup.tar"
    volume.backup(data, backup)
    if corruption == "bytes":
        body = bytearray(backup.read_bytes())
        body[100] ^= 1
        backup.write_bytes(body)
    elif corruption == "size":
        backup.write_bytes(backup.read_bytes() + b"changed")
    else:
        Path(str(backup) + ".json").unlink()
    with pytest.raises(module.UpdateError, match="校验"):
        volume.restore(data, backup)
    assert (data / "keep.db").read_bytes() == b"existing database"


@pytest.mark.parametrize(
    "entries",
    [
        [("../escape", tarfile.REGTYPE, b"bad")],
        [("/escape", tarfile.REGTYPE, b"bad")],
        [("device", tarfile.CHRTYPE, "")],
        [("fifo", tarfile.FIFOTYPE, "")],
        [("bad-link", tarfile.LNKTYPE, "missing")],
        [("jump", tarfile.SYMTYPE, "/outside"), ("jump/file", tarfile.REGTYPE, b"bad")],
        [
            ("./jump", tarfile.SYMTYPE, "/outside"),
            ("jump/file", tarfile.REGTYPE, b"bad"),
        ],
        [
            ("normal", tarfile.REGTYPE, b"first"),
            ("./normal", tarfile.REGTYPE, b"second"),
        ],
    ],
)
def test_illegal_tar_structure_is_rejected_before_any_restore_write(tmp_path, entries):
    backup = tmp_path / "crafted.tar"
    make_archive(backup, entries)
    # Validation must reject links/path aliases before restore deletes data.
    with pytest.raises(module.UpdateError):
        volume.verify(backup)


def test_failed_backup_validation_does_not_replace_a_previous_verified_archive(
    tmp_path, monkeypatch
):
    data = volume_directory(tmp_path)
    (data / "keep.db").write_bytes(b"old backup")
    backup = tmp_path / "backup.tar"
    volume.backup(data, backup)
    previous = backup.read_bytes(), Path(str(backup) + ".json").read_bytes()
    (data / "keep.db").write_bytes(b"new incomplete backup")

    def reject(_):
        raise module.UpdateError("synthetic validation failure")

    monkeypatch.setattr(volume, "validate_archive", reject)
    with pytest.raises(module.UpdateError):
        volume.backup(data, backup)
    assert (backup.read_bytes(), Path(str(backup) + ".json").read_bytes()) == previous


@pytest.fixture
def updater_http(transaction, monkeypatch):
    # The updater always runs in a Linux container, including on Docker Desktop.
    # These real HTTP tests run in Linux CI; the engine/backup tests run everywhere.
    if sys.platform == "win32":
        pytest.skip("Updater API module uses Linux fcntl; exercised in Linux CI")
    from deploy.updater.server import Server

    engine, driver = transaction
    engine.publish(phase="available")
    monkeypatch.setattr(engine, "setup_token", lambda: "offline-updater-token")
    server = Server(("127.0.0.1", 0), engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    http = requests.Session()
    http.trust_env = False
    try:
        yield http, f"http://127.0.0.1:{server.server_port}", engine, driver
    finally:
        http.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("authorization", [None, "Bearer wrong"])
def test_update_status_requires_setup_token(updater_http, authorization):
    http, url, _, driver = updater_http
    headers = {} if authorization is None else {"Authorization": authorization}
    assert http.get(url + "/status", headers=headers, timeout=5).status_code == 401
    assert driver.events == []


@pytest.mark.parametrize("path", ["/check", "/apply"])
def test_unauthorized_update_requests_cannot_change_transaction_state(
    updater_http, path
):
    http, url, engine, driver = updater_http
    before = state(engine)
    response = http.post(
        url + path,
        json={} if path == "/check" else {"revision": NEW},
        headers={"Authorization": "Bearer wrong"},
        timeout=5,
    )
    assert response.status_code == 401
    assert state(engine) == before
    assert driver.events == []


@pytest.mark.parametrize(
    "body,content_type",
    [
        ("{", "application/json"),
        ("[]", "application/json"),
        ('{"revision":"' + NEW + '"}', "text/plain"),
        (" " * 2049, "application/json"),
    ],
)
def test_bad_update_request_body_cannot_launch_a_job(updater_http, body, content_type):
    http, url, engine, driver = updater_http
    before = state(engine)
    response = http.post(
        url + "/apply",
        data=body,
        headers={
            "Authorization": "Bearer offline-updater-token",
            "Content-Type": content_type,
        },
        timeout=5,
    )
    assert response.status_code == 400
    assert state(engine) == before
    assert driver.events == []


def test_missing_setup_secret_never_authorizes_an_empty_bearer(
    updater_http, monkeypatch
):
    http, url, engine, driver = updater_http
    monkeypatch.setattr(engine, "setup_token", lambda: "")
    response = http.get(
        url + "/status", headers={"Authorization": "Bearer "}, timeout=5
    )
    assert response.status_code == 401
    assert driver.events == []


def test_check_reserves_the_transaction_before_background_work(
    updater_http, monkeypatch
):
    http, url, engine, driver = updater_http
    entered, release, finished = (threading.Event() for _ in range(3))
    publish = engine.publish

    def remote():
        entered.set()
        if not release.wait(timeout=5):
            raise RuntimeError("test did not release the synthetic remote")
        return NEW

    def record(**values):
        result = publish(**values)
        if values.get("phase") in {"available", "failed"}:
            finished.set()
        return result

    monkeypatch.setattr(engine, "remote_revision", remote)
    monkeypatch.setattr(engine, "publish", record)
    headers = {"Authorization": "Bearer offline-updater-token"}
    try:
        first = http.post(url + "/check", json={}, headers=headers, timeout=5)
        assert first.status_code == 202 and entered.wait(timeout=2)
        assert first.json()["busy"] is True
        second = http.post(url + "/check", json={}, headers=headers, timeout=5)
        assert second.status_code == 409
        apply = http.post(
            url + "/apply", json={"revision": NEW}, headers=headers, timeout=5
        )
        assert apply.status_code == 409
        assert driver.events == []
    finally:
        release.set()
        assert finished.wait(timeout=5)
    assert engine.snapshot()["phase"] == "available"


@pytest.mark.parametrize(
    "body",
    [
        {"revision": NEW, "repository": "attacker/repository"},
        {"revision": NEW, "command": "delete"},
        {"revision": "../main"},
    ],
)
def test_update_api_cannot_accept_repository_commands_or_unchecked_refs(
    updater_http, body
):
    http, url, _, driver = updater_http
    response = http.post(
        url + "/apply",
        json=body,
        headers={"Authorization": "Bearer offline-updater-token"},
        timeout=5,
    )
    assert response.status_code in {400, 409}
    assert driver.events == []


def test_update_api_launches_checked_commit_without_exposing_token(updater_http):
    http, url, engine, driver = updater_http
    headers = {"Authorization": "Bearer offline-updater-token"}
    response = http.post(
        url + "/apply", json={"revision": NEW}, headers=headers, timeout=5
    )
    assert response.status_code == 202
    assert response.json()["busy"] is True
    assert "offline-updater-token" not in response.text
    assert sum(operation == "launch" for operation, _ in driver.events) == 1
    again = http.post(
        url + "/apply", json={"revision": NEW}, headers=headers, timeout=5
    )
    assert again.status_code == 409
    assert sum(operation == "launch" for operation, _ in driver.events) == 1
    assert state(engine)["phase"] == "queued"


@pytest.fixture
def updater_cli(transaction, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("Updater CLI uses Linux fcntl; exercised in Linux CI")
    from deploy.updater import server

    engine, driver = transaction
    monkeypatch.setattr(server, "UpdateEngine", lambda: engine)
    monkeypatch.setattr(server.os, "umask", lambda _: None)
    return server, engine, driver


def test_cli_guard_blocks_stopping_a_busy_deployment(updater_cli, monkeypatch):
    server, engine, driver = updater_cli
    engine.publish(phase="recovery_required")
    monkeypatch.setattr(sys, "argv", ["updater", "guard"])
    with pytest.raises(SystemExit, match="Update in progress"):
        server.main()
    assert driver.events == []


def test_cli_resume_uses_saved_active_images_without_rebuilding(
    updater_cli, monkeypatch
):
    server, engine, driver = updater_cli
    document = deepcopy(driver.current)
    document["services"]["app"]["image"] = "sha256:" + "f" * 64
    module.write(engine.active_path, document)
    engine.publish(phase="completed")
    monkeypatch.setattr(sys, "argv", ["updater", "resume"])
    server.main()
    assert driver.events == [("start", "old")]
    commands = [args for args, _ in driver.calls if args[:2] == ["docker", "compose"]]
    assert len(commands) == 1
    assert commands[0][commands[0].index("-f") + 1] == str(engine.active_path)
    assert "--no-build" in commands[0]
    assert module.read(engine.active_path) == document


def test_cli_resume_without_a_snapshot_reports_missing_instead_of_downgrading(
    updater_cli, monkeypatch
):
    server, engine, driver = updater_cli
    engine.publish(phase="idle")
    monkeypatch.setattr(sys, "argv", ["updater", "resume"])
    with pytest.raises(SystemExit) as error:
        server.main()
    assert error.value.code == 3
    assert driver.events == []


def test_cli_adopt_records_running_image_ids_and_actual_named_volumes(
    updater_cli, monkeypatch
):
    server, engine, driver = updater_cli
    engine.publish(phase="idle")
    monkeypatch.setattr(sys, "argv", ["updater", "adopt"])
    server.main()
    captured = module.read(engine.active_path)
    assert engine.snapshot()["current_revision"] == OLD
    for service, image_id in driver.images.items():
        assert captured["services"][service]["image"] == image_id
    for logical, actual in driver.real_volumes.items():
        assert captured["volumes"][logical] == {"name": actual, "external": True}
    assert driver.events == []
