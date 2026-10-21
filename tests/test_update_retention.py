"""Exercise updater retention against real files and a stateful Docker model.

No daemon/network access: deleted backups must disappear on disk, while Docker
objects are independently modelled so shared tags and stopped containers matter.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from deploy.updater import engine as engine_module
from deploy.updater import volume


PROJECT = "retention-bot"
REPOSITORY = "menherachan0210/QunLing"


def image_id(number):
    return "sha256:" + format(number, "064x")


def compose(images):
    return {
        "name": PROJECT,
        "services": {
            name: {"image": images[name]}
            for name in engine_module.SERVICES
        },
        "volumes": {
            name: {"name": PROJECT + "_" + name, "external": True}
            for name in (*engine_module.DATA_VOLUMES, "update-data")
        },
        "networks": {"default": {"name": PROJECT + "_default"}},
    }


class DockerInventory:
    """Reject global pruning, forced removals and accidental volume mutation."""

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.images = {}
        self.containers = {}
        self.removed_images = []
        self.removed_workers = []
        self.foreign_update_volume = False
        self.fail = None
        self.live_images = {}
        for index, service in enumerate(engine_module.SERVICES, 1):
            image = self.add_image(index, tags=[PROJECT + "-" + service + ":live"])
            self.live_images[service] = image
            self.add_container("live-" + service, image, service=service)

    def add_image(self, number, *, tags=None, labels=None):
        identity = image_id(number)
        self.images[identity] = {
            "Id": identity,
            "RepoTags": tags or [],
            "RepoDigests": [],
            "Config": {"Labels": labels or {}},
            "Created": "2026-10-01T00:00:00Z",
            "Size": 1024,
        }
        return identity

    def add_container(self, name, image, *, service=None, labels=None, running=True):
        metadata = dict(labels or {})
        if service:
            metadata.update({
                "com.docker.compose.project": PROJECT,
                "com.docker.compose.service": service,
                "com.docker.compose.oneoff": "False",
            })
        mounts = []
        if service == "updater":
            mounts.append({
                "Type": "volume", "Name": PROJECT + "_update-data",
                "Destination": str(self.root),
            })
        self.containers[name] = {
            "Id": name, "Name": "/" + name, "Image": image,
            "State": {"Running": running, "Status": "running" if running else "exited"},
            "Config": {"Labels": metadata}, "Mounts": mounts,
        }

    def resolve(self, reference):
        if reference in self.images:
            return self.images[reference]
        matches = [v for v in self.images.values() if reference in v["RepoTags"]]
        if len(matches) != 1:
            raise engine_module.UpdateError("missing image")
        return matches[0]

    def run(self, args, **kwargs):
        args = list(args)
        self.calls.append(args)
        assert "prune" not in args, "automatic cleanup must not prune the host daemon"
        assert "--force" not in args and "-f" not in args, "never force deletion"
        if self.fail and tuple(args[:len(self.fail)]) == self.fail:
            raise engine_module.UpdateError("injected Docker failure")
        if args[:2] == ["docker", "ps"]:
            rows = list(self.containers.values())
            for index, arg in enumerate(args):
                if arg == "--filter":
                    key, _, value = args[index + 1].partition("=")
                    if key == "label":
                        label, _, expected = value.partition("=")
                        rows = [row for row in rows if row["Config"]["Labels"].get(label) == expected]
            if not any("a" in arg for arg in args[2:] if arg.startswith("-") and not arg.startswith("--")):
                rows = [row for row in rows if row["State"]["Running"]]
            return "\n".join(row["Id"] for row in rows)
        if args[:2] == ["docker", "inspect"]:
            return json.dumps([deepcopy(self.containers[name]) for name in args[2:]])
        if args[:3] == ["docker", "container", "inspect"]:
            return json.dumps([deepcopy(self.containers[name]) for name in args[3:]])
        if args[:3] == ["docker", "volume", "inspect"]:
            return json.dumps([{"Name": args[3], "Labels": {
                "com.docker.compose.project": "foreign" if self.foreign_update_volume else PROJECT,
            }}])
        if args[:3] in (["docker", "image", "ls"], ["docker", "image", "list"]):
            return "\n".join(self.images)
        if args[:2] == ["docker", "images"]:
            return "\n".join(self.images)
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([deepcopy(self.resolve(name)) for name in args[3:]])
        if args[:3] in (["docker", "image", "rm"], ["docker", "image", "remove"]):
            for reference in args[3:]:
                image = self.resolve(reference)
                if any(c["Image"] == image["Id"] for c in self.containers.values()):
                    raise engine_module.UpdateError("image is used by a container")
                self.removed_images.append(image["Id"])
                del self.images[image["Id"]]
            return ""
        if args[:2] == ["docker", "rmi"]:
            return self.run(["docker", "image", "rm", *args[2:]])
        if args[:2] == ["docker", "rm"] or args[:3] == ["docker", "container", "rm"]:
            for name in args[2 if args[1] == "rm" else 3:]:
                assert not self.containers[name]["State"]["Running"]
                self.removed_workers.append(name)
                del self.containers[name]
            return ""
        raise AssertionError(f"Unexpected Docker operation: {args}")


@pytest.fixture
def deployed(tmp_path):
    root = tmp_path / "updates"
    root.mkdir()
    docker = DockerInventory(root)
    engine = engine_module.UpdateEngine(root, runner=docker, project=PROJECT, repository=REPOSITORY)
    engine_module.write(engine.active_path, compose(docker.live_images))
    engine.publish(phase="idle")
    return engine, docker


def completed_job(engine, docker, number, *, phase="completed", complete=True):
    job_id = format(number, "024x")
    directory = engine.root / "jobs" / job_id
    timestamp = (datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(days=number)).isoformat()
    revision = format(number + 100, "040x")
    old_images = dict(docker.live_images)
    new_images = dict(docker.live_images)
    old_images["app"] = docker.add_image(1000 + number, tags=[PROJECT + "-app:" + format(number + 200, "040x")])
    new_images["app"] = docker.add_image(2000 + number, tags=[PROJECT + "-app:" + revision])
    metadata = {
        "id": job_id, "revision": revision, "finished": True,
        "terminal_phase": phase, "backup_complete": complete,
        "phase": "verifying", "created_at": timestamp,
        "completed_at": timestamp, "finished_at": timestamp,
        "worker_image": docker.live_images["updater"],
    }
    engine_module.write(directory / "job.json", metadata)
    engine_module.write(directory / "old-compose.json", compose(old_images))
    engine_module.write(directory / "new-compose.json", compose(new_images))
    data = engine.root / ("fixture-volume-" + job_id)
    data.mkdir()
    (data / "payload.db").write_bytes(b"recoverable old database")
    for logical in engine_module.DATA_VOLUMES:
        volume.backup(data, directory / (logical + ".tar"))
    (directory / "source").mkdir()
    (directory / "source" / "README.md").write_text("old source", encoding="utf-8")
    return directory, old_images["app"], new_images["app"]


def cleanup(engine, **kwargs):
    from deploy.updater.retention import cleanup as run_cleanup

    return run_cleanup(engine, **kwargs)


def test_deletes_only_old_successful_backups_and_keeps_two_recovery_snapshots(deployed):
    engine, docker = deployed
    oldest, old_image, obsolete_image = completed_job(engine, docker, 1)
    middle, rollback_image, _ = completed_job(engine, docker, 2)
    newest, _, _ = completed_job(engine, docker, 3)

    report = cleanup(engine)

    assert report["status"] == "completed"
    assert report["removed_jobs"] == [oldest.name]
    assert not oldest.exists()
    assert middle.is_dir() and newest.is_dir()
    for directory in (middle, newest):
        for name in engine_module.DATA_VOLUMES:
            volume.verify(directory / (name + ".tar"))
    assert rollback_image in docker.images
    assert set(docker.live_images.values()).issubset(docker.images)
    assert old_image in docker.removed_images and obsolete_image in docker.removed_images
    assert report["reclaimed_backup_bytes"] > 0


def test_current_status_job_is_retained_in_addition_to_newest_backup_quota(deployed):
    engine, docker = deployed
    oldest, old_image, _ = completed_job(engine, docker, 1)
    second, _, _ = completed_job(engine, docker, 2)
    third, _, _ = completed_job(engine, docker, 3)
    fourth, _, _ = completed_job(engine, docker, 4)
    engine.publish(phase="completed", job_id=oldest.name)

    cleanup(engine, keep_backups=2)

    assert oldest.exists() and old_image in docker.images
    assert not second.exists()
    assert third.exists() and fourth.exists()


@pytest.mark.parametrize("phase", ["failed", "rolled_back", "recovery_required"])
def test_failed_and_rolled_back_jobs_keep_data_and_referenced_images(deployed, phase):
    engine, docker = deployed
    failed, old_image, candidate_image = completed_job(engine, docker, 1, phase=phase)
    for number in (2, 3, 4):
        completed_job(engine, docker, number)

    report = cleanup(engine)

    assert failed.exists()
    assert old_image in docker.images and candidate_image in docker.images
    assert failed.name not in report["removed_jobs"]


def test_stopped_container_from_another_project_protects_an_old_image(deployed):
    engine, docker = deployed
    _, old_image, _ = completed_job(engine, docker, 1)
    completed_job(engine, docker, 2)
    completed_job(engine, docker, 3)
    docker.add_container("other-project-stopped", old_image, running=False, labels={
        "com.docker.compose.project": "another-app",
    })

    cleanup(engine)

    assert old_image in docker.images
    assert "other-project-stopped" in docker.containers


def test_foreign_alias_on_an_old_image_protects_all_its_tags(deployed):
    engine, docker = deployed
    _, old_image, _ = completed_job(engine, docker, 1)
    completed_job(engine, docker, 2)
    completed_job(engine, docker, 3)
    docker.images[old_image]["RepoTags"].append("other-app:stable")
    unrelated = docker.add_image(9000, tags=["another-project-app:old"])
    third_party = docker.add_image(9001, tags=["ghcr.io/napnex/napcat-docker:latest"])

    cleanup(engine)

    assert {old_image, unrelated, third_party}.issubset(docker.images)


def test_exited_owned_worker_can_release_image_but_running_worker_is_preserved(deployed):
    engine, docker = deployed
    directory, _, candidate_image = completed_job(engine, docker, 1)
    completed_job(engine, docker, 2)
    completed_job(engine, docker, 3)
    worker = PROJECT + "-update-" + directory.name
    docker.add_container(worker, candidate_image, running=False, labels={
        "io.qunling.update-project": PROJECT, "io.qunling.update-job": directory.name,
    })

    cleanup(engine)

    assert worker in docker.removed_workers
    assert candidate_image in docker.removed_images


@pytest.mark.parametrize("foreign", ["name", "label"])
def test_worker_removal_requires_both_expected_name_and_project_labels(deployed, foreign):
    engine, docker = deployed
    directory, _, candidate_image = completed_job(engine, docker, 1)
    completed_job(engine, docker, 2)
    completed_job(engine, docker, 3)
    worker = ("another-app" if foreign == "name" else PROJECT) + "-update-" + directory.name
    docker.add_container(worker, candidate_image, running=False, labels={
        "io.qunling.update-project": "foreign" if foreign == "label" else PROJECT,
        "io.qunling.update-job": directory.name,
    })

    cleanup(engine)

    assert worker in docker.containers
    assert candidate_image in docker.images


@pytest.mark.parametrize("unsafe", ["invalid_json", "wrong_id", "unfinished", "partial_backup", "foreign_compose", "unknown_file"])
def test_ambiguous_or_incomplete_jobs_are_never_recursively_removed(deployed, unsafe):
    engine, docker = deployed
    directory, old_image, candidate_image = completed_job(engine, docker, 1)
    completed_job(engine, docker, 2)
    completed_job(engine, docker, 3)
    if unsafe == "invalid_json":
        (directory / "job.json").write_text("{invalid", encoding="utf-8")
    elif unsafe in {"wrong_id", "unfinished"}:
        job = engine_module.read(directory / "job.json")
        job["id" if unsafe == "wrong_id" else "finished"] = "f" * 24 if unsafe == "wrong_id" else False
        engine_module.write(directory / "job.json", job)
    elif unsafe == "partial_backup":
        (directory / "langbot-data.tar.json").unlink()
    elif unsafe == "foreign_compose":
        document = engine_module.read(directory / "old-compose.json")
        document["name"] = "another-project"
        engine_module.write(directory / "old-compose.json", document)
    elif unsafe == "unknown_file":
        (directory / "operator-important-notes.txt").write_text("keep this", encoding="utf-8")

    cleanup(engine)

    assert directory.exists()
    assert old_image in docker.images and candidate_image in docker.images


@pytest.mark.parametrize("bad_path", ["job_directory", "metadata", "backup", "source_directory"])
def test_symlinks_never_delete_or_read_outside_updater_owned_history(deployed, tmp_path, bad_path):
    engine, docker = deployed
    directory, _, _ = completed_job(engine, docker, 1)
    completed_job(engine, docker, 2)
    completed_job(engine, docker, 3)
    external = tmp_path / "private-outside-updater"
    external.mkdir()
    sentinel = external / "important.txt"
    sentinel.write_text("must survive", encoding="utf-8")
    if bad_path == "job_directory":
        link = engine.root / "jobs" / ("e" * 24)
        target = external
    elif bad_path == "metadata":
        link = directory / "job.json"
        target = external / "job.json"
        target.write_bytes(link.read_bytes())
        link.unlink()
    elif bad_path == "backup":
        link = directory / "langbot-data.tar"
        target = external / "langbot-data.tar"
        target.write_bytes(link.read_bytes())
        link.unlink()
    else:
        link = directory / "source" / "linked_directory"
        target = external
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("creating symlinks requires developer mode on Windows")

    cleanup(engine)

    assert sentinel.read_text(encoding="utf-8") == "must survive"
    assert link.is_symlink()
    if bad_path != "job_directory":
        assert directory.exists()


@pytest.mark.parametrize("bad_source", ["active_missing", "active_corrupt", "foreign_volume", "docker_unavailable"])
def test_untrustworthy_deployment_inventory_aborts_without_deleting_backups(deployed, bad_source):
    engine, docker = deployed
    directories = [completed_job(engine, docker, n)[0] for n in (1, 2, 3)]
    if bad_source == "active_missing":
        engine.active_path.unlink()
    elif bad_source == "active_corrupt":
        engine.active_path.write_text("not-json", encoding="utf-8")
    elif bad_source == "foreign_volume":
        docker.foreign_update_volume = True
    else:
        docker.fail = ("docker", "image", "inspect")

    report = cleanup(engine)

    assert report["status"] in {"skipped", "failed", "partial"}
    assert all(path.exists() for path in directories)
    assert not docker.removed_images and not docker.removed_workers


def test_docker_image_delete_failure_does_not_undo_successful_update_state(deployed):
    engine, docker = deployed
    for number in (1, 2, 3):
        completed_job(engine, docker, number)
    engine.publish(phase="completed", current_revision="a" * 40, message="update succeeded")
    before = engine_module.read(engine.state_path)
    docker.fail = ("docker", "image", "rm")

    report = cleanup(engine)

    assert report["status"] == "partial"
    assert report["warnings"]
    assert engine_module.read(engine.state_path) == before
    assert not docker.removed_images


@pytest.mark.parametrize("phase", ["checking", "queued", "building", "backing_up", "verifying", "rolling_back", "recovery_required", "maintenance"])
def test_cleanup_entrypoint_never_runs_during_an_update_or_manual_recovery(deployed, phase):
    engine, docker = deployed
    engine.publish(phase=phase)
    before = engine_module.read(engine.state_path)

    engine.cleanup_history(force=True)

    assert not docker.calls
    assert engine_module.read(engine.state_path) == before


def test_cleanup_entrypoint_does_not_compete_with_an_in_process_update_lock(deployed):
    engine, docker = deployed
    with engine.lock:
        engine.cleanup_history(force=True)

    assert not docker.calls


def test_cleanup_entrypoint_is_throttled_and_exposes_only_its_summary(deployed):
    engine, docker = deployed
    for number in (1, 2, 3):
        completed_job(engine, docker, number)
    report = engine.cleanup_history(force=True)
    assert report["status"] == "completed"
    docker.calls.clear()

    engine.cleanup_history()

    assert not docker.calls
    persisted = engine_module.read(engine.root / "cleanup.json")
    assert persisted["status"] == "completed"
    assert engine.snapshot()["cleanup"]["status"] == "completed"
    assert "private.log" not in json.dumps(engine.snapshot()["cleanup"])
    assert str(engine.root) not in json.dumps(engine.snapshot()["cleanup"])


@pytest.mark.parametrize("value", [0, 11, -1, True, "2", None])
def test_invalid_retention_bounds_cannot_authorize_any_deletion(deployed, value):
    engine, docker = deployed
    directory, _, _ = completed_job(engine, docker, 1)

    report = cleanup(engine, keep_backups=value)

    assert report["status"] == "failed"
    assert directory.exists()
    assert not docker.calls


def test_missing_retained_image_disables_image_cleanup_but_preserves_backups(deployed):
    engine, docker = deployed
    _, old_image, _ = completed_job(engine, docker, 1)
    retained, rollback_image, _ = completed_job(engine, docker, 2)
    completed_job(engine, docker, 3)
    del docker.images[rollback_image]

    report = cleanup(engine)

    assert report["status"] == "partial"
    assert old_image in docker.images
    assert retained.exists()
    assert not docker.removed_images


def test_dangling_image_needs_both_owned_label_and_recorded_service_reference(deployed):
    engine, docker = deployed
    _, old_image, candidate_image = completed_job(engine, docker, 1)
    completed_job(engine, docker, 2)
    completed_job(engine, docker, 3)
    docker.images[old_image]["RepoTags"] = []
    docker.images[old_image]["Config"]["Labels"]["io.qunling.update-project"] = PROJECT
    docker.images[candidate_image]["RepoTags"] = []
    unrecorded = docker.add_image(9200, labels={"io.qunling.update-project": PROJECT})

    cleanup(engine)

    assert old_image in docker.removed_images
    assert candidate_image in docker.images
    assert unrecorded in docker.images


def test_retained_compose_image_tag_protects_its_underlying_image(deployed):
    engine, docker = deployed
    completed_job(engine, docker, 1)
    retained, rollback_image, _ = completed_job(engine, docker, 2)
    completed_job(engine, docker, 3)
    document = engine_module.read(retained / "old-compose.json")
    document["services"]["app"]["image"] = docker.images[rollback_image]["RepoTags"][0]
    engine_module.write(retained / "old-compose.json", document)

    cleanup(engine)

    assert rollback_image in docker.images


def test_backup_removal_error_keeps_its_rollback_images_and_reports_partial(deployed, monkeypatch):
    from deploy.updater import retention

    engine, docker = deployed
    oldest, old_image, candidate_image = completed_job(engine, docker, 1)
    completed_job(engine, docker, 2)
    completed_job(engine, docker, 3)
    original = retention.shutil.rmtree

    def permission_denied(path, *args, **kwargs):
        if Path(path) == oldest:
            raise PermissionError("private filesystem details must not reach API")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(retention.shutil, "rmtree", permission_denied)
    report = cleanup(engine)

    assert report["status"] == "partial"
    assert oldest.exists()
    assert {old_image, candidate_image}.issubset(docker.images)
    assert "private filesystem details" not in json.dumps(report)


def test_running_worker_keeps_job_backups_and_image_even_if_status_is_terminal(deployed):
    engine, docker = deployed
    directory, _, candidate_image = completed_job(engine, docker, 1)
    completed_job(engine, docker, 2)
    completed_job(engine, docker, 3)
    worker = PROJECT + "-update-" + directory.name
    docker.add_container(worker, candidate_image, running=True, labels={
        "io.qunling.update-project": PROJECT, "io.qunling.update-job": directory.name,
    })

    cleanup(engine)

    assert directory.exists() and worker in docker.containers
    assert candidate_image in docker.images


def test_newly_completed_update_triggers_retention_without_waiting_an_hour(deployed):
    engine, docker = deployed
    for number in (1, 2, 3):
        completed_job(engine, docker, number)
    engine.cleanup_history(force=True)
    fourth, _, _ = completed_job(engine, docker, 4)
    engine.publish(phase="completed", job_id=fourth.name)
    docker.calls.clear()

    report = engine.cleanup_history()

    assert report and report["status"] == "completed"
    assert docker.calls
    assert not (engine.root / "jobs" / format(2, "024x")).exists()


@pytest.mark.parametrize("value", ["0", "11", "invalid"])
def test_bad_environment_setting_disables_cleanup_instead_of_removing_too_much(tmp_path, monkeypatch, value):
    monkeypatch.setenv("QQBOT_UPDATE_KEEP_BACKUPS", value)
    docker = DockerInventory(tmp_path)
    engine = engine_module.UpdateEngine(tmp_path, runner=docker, project=PROJECT, repository=REPOSITORY)

    report = engine.cleanup_history(force=True)

    assert report["status"] == "failed"
    assert not docker.calls


def test_same_size_corruption_does_not_displace_older_verified_backups(deployed):
    engine, docker = deployed
    oldest, _, _ = completed_job(engine, docker, 1)
    second, _, _ = completed_job(engine, docker, 2)
    corrupt, _, _ = completed_job(engine, docker, 3)
    archive = corrupt / "langbot-data.tar"
    raw = bytearray(archive.read_bytes())
    raw[len(raw) // 2] ^= 1
    archive.write_bytes(raw)

    report = cleanup(engine)

    assert report["status"] == "partial"
    assert oldest.exists() and second.exists() and corrupt.exists()
    assert not report["removed_jobs"]
    volume.verify(oldest / "langbot-data.tar")
    volume.verify(second / "langbot-data.tar")


def test_manual_pinned_snapshot_keeps_its_images_without_blocking_other_owned_images(deployed):
    engine, docker = deployed
    _, old_image, candidate_image = completed_job(engine, docker, 1)
    completed_job(engine, docker, 2)
    completed_job(engine, docker, 3)
    manual = engine.root / "jobs" / "operator-assisted-deploy"
    engine_module.write(manual / "retention-pin.json", {
        "version": 1, "project": PROJECT, "images": [old_image],
    })
    (manual / "private-backup.tar").write_bytes(b"operator managed backup")

    report = cleanup(engine)

    assert manual.exists() and (manual / "private-backup.tar").exists()
    assert old_image in docker.images
    assert candidate_image in docker.removed_images
    assert "image_cleanup_skipped_incomplete_history" not in report["warnings"]


@pytest.mark.parametrize("bad_pin", ["foreign_project", "empty_images", "unknown_version", "invalid_image_type", "missing_image"])
def test_invalid_manual_pin_keeps_directory_and_blocks_image_cleanup(deployed, bad_pin):
    engine, docker = deployed
    _, old_image, _ = completed_job(engine, docker, 1)
    completed_job(engine, docker, 2)
    completed_job(engine, docker, 3)
    manual = engine.root / "jobs" / "operator-assisted-deploy"
    pin = {"version": 1, "project": PROJECT, "images": [old_image]}
    if bad_pin == "foreign_project":
        pin["project"] = "someone-else"
    elif bad_pin == "empty_images":
        pin["images"] = []
    elif bad_pin == "unknown_version":
        pin["version"] = 99
    elif bad_pin == "invalid_image_type":
        pin["images"] = [{"command": "not an image"}]
    else:
        pin["images"] = [image_id(99999)]
    engine_module.write(manual / "retention-pin.json", pin)

    report = cleanup(engine)

    assert manual.exists()
    assert not docker.removed_images
    assert "image_cleanup_skipped_incomplete_history" in report["warnings"]


def test_digest_pinned_active_image_resolves_tag_alias_without_disabling_cleanup(deployed):
    engine, docker = deployed
    _, old_image, _ = completed_job(engine, docker, 1)
    completed_job(engine, docker, 2)
    completed_job(engine, docker, 3)
    active = engine_module.read(engine.active_path)
    digest = "sha256:" + "d" * 64
    actual_image = docker.live_images["napcat"]
    canonical = "registry.example.org/napcat@" + digest
    docker.images[actual_image]["RepoDigests"] = [canonical]
    active["services"]["napcat"]["image"] = "registry.example.org/napcat:stable@" + digest
    engine_module.write(engine.active_path, active)

    report = cleanup(engine)

    assert report["status"] == "completed"
    assert old_image in docker.removed_images
    assert actual_image in docker.images


def maintenance_job(engine, docker, number, *, register=True, journal_name="proactive-job.json"):
    from deploy.updater.retention import MAINTENANCE_VOLUMES, register_completed_maintenance

    directory = engine.root / "jobs" / format(number + 100, "024x")
    old = compose(docker.live_images)
    candidate = docker.add_image(3000 + number, tags=[PROJECT + "-group-info:test-" + str(number)])
    new = deepcopy(old)
    for name in ("app", "wizard"):
        new["services"][name]["image"] = candidate
    volumes = {name: item["name"] for name, item in old["volumes"].items()}
    before = {name: {"id": "before-" + name, "image": item["image"], "started_at": ""}
              for name, item in old["services"].items()}
    job = {"id": directory.name, "phase": "completed", "terminal_phase": "completed",
           "backup_complete": True, "mutation_started": True, "services_stopped": True,
           "candidate_image": candidate, "helper_image": docker.live_images["updater"],
           "volumes": volumes, "before": before}
    engine_module.write(directory / journal_name, job)
    engine_module.write(directory / "old-compose.json", old)
    engine_module.write(directory / "new-compose.json", new)
    engine_module.write(directory / "adopted/old-compose.json", new)
    engine_module.write(directory / "captured.json", {"volumes": volumes, "services": before})
    engine_module.write(directory / "previous-status.json", {"phase": "idle"})
    engine_module.write(directory / "retention-pin.json", {"version": 1, "project": PROJECT,
                         "images": sorted({candidate, *docker.live_images.values()})})
    data = engine.root / ("manual-fixture-volume-" + directory.name)
    data.mkdir()
    (data / "payload.db").write_bytes(b"verified maintenance database")
    for logical in MAINTENANCE_VOLUMES:
        volume.backup(data, directory / (logical + ".tar"))
    timestamp = (datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(days=number)).isoformat()
    if register:
        register_completed_maintenance(directory, PROJECT, volumes["update-data"], timestamp, journal_name)
    return directory, candidate, timestamp


@pytest.mark.parametrize("journal_name", ["proactive-job.json", "password-files-job.json",
                                          "file-workspace-job.json", "recent-history-job.json"])
def test_maintenance_registration_is_explicit_atomic_and_does_not_change_original_journal(deployed, journal_name):
    from deploy.updater.retention import register_completed_maintenance

    engine, docker = deployed
    directory, candidate, timestamp = maintenance_job(engine, docker, 1, register=False, journal_name=journal_name)
    before = (directory / journal_name).read_bytes()
    record = register_completed_maintenance(directory, PROJECT, PROJECT + "_update-data", timestamp, journal_name)

    assert record == engine_module.read(directory / "maintenance.json")
    assert record["owned_images"] == [candidate]
    assert record["backup_volumes"] == ["langbot-data", "setup-state"]
    assert record["completed_at_source"] == "operator_supplied"
    assert record["completed_at"] == timestamp
    assert (directory / journal_name).read_bytes() == before
    assert not list(directory.glob(".maintenance-*"))
    assert register_completed_maintenance(directory, PROJECT, PROJECT + "_update-data", timestamp, journal_name) == record


def test_registered_maintenance_rotates_only_completed_two_volume_backups(deployed):
    engine, docker = deployed
    oldest, candidate, _ = maintenance_job(engine, docker, 1)
    middle, _, _ = maintenance_job(engine, docker, 2)
    newest, _, _ = maintenance_job(engine, docker, 3)

    report = cleanup(engine)

    assert report["status"] == "completed"
    assert report["removed_jobs"] == [oldest.name]
    assert not oldest.exists() and middle.exists() and newest.exists()
    assert candidate in docker.removed_images
    assert set(docker.live_images.values()).issubset(docker.images)
    # Deleted old image IDs in an expired registration are not interpreted as
    # unresolved retained references on the following cleanup cycle.
    again = cleanup(engine)
    assert again["status"] == "completed"
    assert not again["removed_jobs"] and not again["removed_images"]


def test_two_volume_maintenance_does_not_displace_full_qq_backup_quota(deployed):
    engine, docker = deployed
    full = [completed_job(engine, docker, n)[0] for n in (1, 2, 3)]
    partial = [maintenance_job(engine, docker, n)[0] for n in (4, 5, 6)]

    report = cleanup(engine)

    assert set(report["removed_jobs"]) == {full[0].name, partial[0].name}
    assert all(path.exists() for path in [*full[1:], *partial[1:]])


def test_unregistered_successful_manual_journal_remains_operator_managed(deployed):
    engine, docker = deployed
    manual, candidate, _ = maintenance_job(engine, docker, 1, register=False)
    for number in (2, 3, 4):
        maintenance_job(engine, docker, number)

    report = cleanup(engine)

    assert manual.exists() and candidate in docker.images
    assert manual.name not in report["removed_jobs"]
    assert "unmanaged_or_unresolved_history_preserved" in report["warnings"]


@pytest.mark.parametrize("unsafe", ["unfinished", "rolled_back", "partial_backup", "wrong_id",
                                    "foreign_project", "different_volume", "napcat_changed",
                                    "helper_changed", "candidate_is_napcat", "unresolved_image_tag",
                                    "captured_mismatch", "adopted_missing", "adopted_mismatch",
                                    "unknown_file", "unexpected_directory", "corrupt_archive"])
def test_maintenance_registration_rejects_incomplete_or_unproven_rollouts(deployed, unsafe):
    from deploy.updater.retention import UnsafeInventory, register_completed_maintenance

    engine, docker = deployed
    directory, _, timestamp = maintenance_job(engine, docker, 1, register=False)
    job_path = directory / "proactive-job.json"
    job = engine_module.read(job_path)
    if unsafe in {"unfinished", "rolled_back", "partial_backup", "wrong_id", "helper_changed", "candidate_is_napcat"}:
        if unsafe == "unfinished":
            job["phase"] = "finalizing"
        elif unsafe == "rolled_back":
            job["terminal_phase"] = "rolled_back"
        elif unsafe == "partial_backup":
            job["backup_complete"] = False
        elif unsafe == "wrong_id":
            job["id"] = "e" * 24
        elif unsafe == "helper_changed":
            job["helper_image"] = image_id(12345)
        else:
            job["candidate_image"] = docker.live_images["napcat"]
        engine_module.write(job_path, job)
    elif unsafe in {"foreign_project", "different_volume", "napcat_changed", "unresolved_image_tag"}:
        document = engine_module.read(directory / "new-compose.json")
        if unsafe == "foreign_project":
            document["name"] = "someone-else"
        elif unsafe == "different_volume":
            document["volumes"]["setup-state"]["name"] = "another-volume"
        elif unsafe == "napcat_changed":
            document["services"]["napcat"]["image"] = image_id(22222)
        else:
            document["services"]["app"]["image"] = PROJECT + "-app:latest"
        engine_module.write(directory / "new-compose.json", document)
    elif unsafe == "captured_mismatch":
        engine_module.write(directory / "captured.json", {})
    elif unsafe == "adopted_missing":
        (directory / "adopted/old-compose.json").unlink()
    elif unsafe == "adopted_mismatch":
        engine_module.write(directory / "adopted/old-compose.json", engine_module.read(directory / "old-compose.json"))
    elif unsafe == "unknown_file":
        (directory / "important.txt").write_text("keep")
    elif unsafe == "unexpected_directory":
        (directory / "operator-data").mkdir()
    else:
        path = directory / "langbot-data.tar"
        raw = bytearray(path.read_bytes())
        raw[len(raw) // 2] ^= 1
        path.write_bytes(raw)

    with pytest.raises((UnsafeInventory, engine_module.UpdateError)):
        register_completed_maintenance(directory, PROJECT, PROJECT + "_update-data", timestamp)
    assert not (directory / "maintenance.json").exists()
    assert directory.exists() and not docker.removed_images


@pytest.mark.parametrize("tamper", ["record_owned_ids", "record_volume", "journal", "metadata", "archive"])
def test_registered_maintenance_is_revalidated_before_retention(deployed, tamper):
    engine, docker = deployed
    directory, candidate, _ = maintenance_job(engine, docker, 1)
    for number in (2, 3, 4):
        maintenance_job(engine, docker, number)
    if tamper.startswith("record_"):
        record = engine_module.read(directory / "maintenance.json")
        if tamper == "record_owned_ids":
            record["owned_images"].append(docker.live_images["napcat"])
        else:
            record["backup_volumes"] = ["langbot-data"]
        engine_module.write(directory / "maintenance.json", record)
    elif tamper == "journal":
        job = engine_module.read(directory / "proactive-job.json")
        job["phase"] = "recovery_required"
        engine_module.write(directory / "proactive-job.json", job)
    elif tamper == "metadata":
        engine_module.write(directory / "previous-status.json", {"phase": "failed"})
    else:
        path = directory / "setup-state.tar"
        raw = bytearray(path.read_bytes())
        raw[len(raw) // 2] ^= 1
        path.write_bytes(raw)

    cleanup(engine)

    assert directory.exists() and candidate in docker.images


@pytest.mark.parametrize("protection", ["active", "stopped_container", "owner", "running_helper",
                                       "foreign_tag", "foreign_owner", "digest", "unrecorded_image"])
def test_maintenance_images_and_active_backups_keep_existing_safety_boundaries(deployed, protection):
    engine, docker = deployed
    oldest, candidate, _ = maintenance_job(engine, docker, 1)
    for number in (2, 3):
        maintenance_job(engine, docker, number)
    target = candidate
    if protection == "active":
        active = engine_module.read(engine.active_path)
        active["services"]["app"]["image"] = candidate
        engine_module.write(engine.active_path, active)
    elif protection == "stopped_container":
        docker.add_container("unrelated-stopped", candidate, running=False)
    elif protection == "owner":
        engine.publish(proactive_owner=oldest.name)
    elif protection == "running_helper":
        docker.add_container("manual-helper", docker.live_images["updater"],
                             labels={"io.qunling.proactive-helper": oldest.name})
    elif protection == "foreign_tag":
        docker.images[candidate]["RepoTags"].append("another-app:important")
    elif protection == "foreign_owner":
        docker.images[candidate]["Config"]["Labels"]["io.qunling.update-project"] = "foreign"
    elif protection == "digest":
        docker.images[candidate]["RepoDigests"] = ["registry.example/owned@" + image_id(98765)]
    else:
        target = docker.add_image(99999, tags=[PROJECT + "-group-info:unrecorded"])

    cleanup(engine)

    assert target in docker.images
    if protection in {"owner", "running_helper"}:
        assert oldest.exists()
