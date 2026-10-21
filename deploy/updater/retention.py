"""Conservative retention for this deployment's completed update artifacts.

This deliberately does not call Docker prune: other projects, live volumes,
unresolved updates and snapshots required for rollback are outside its scope.
"""
from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile


JOB_ID = re.compile(r"[0-9a-f]{24}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
DATA_VOLUMES = ("langbot-data", "setup-state", "napcat-qq", "napcat-config", "napcat-plugins")
SERVICES = {"init", "app", "napcat", "wizard", "updater"}
IDLE_PHASES = {"idle", "available", "completed", "rolled_back", "failed"}
MAINTENANCE_VOLUMES = ("langbot-data", "setup-state")
MAINTENANCE_JOURNALS = {"proactive-job.json", "password-files-job.json",
                        "file-workspace-job.json", "recent-history-job.json"}


class UnsafeInventory(Exception):
    """An incomplete inventory cannot authorize deletion."""


def _json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise UnsafeInventory()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise UnsafeInventory()
    return value


def _timestamp(value):
    if not isinstance(value, str):
        raise UnsafeInventory()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise UnsafeInventory()
    return parsed.timestamp()


def _tree_size(directory, parent):
    """Verify the exact deletion boundary and every entry without following links."""
    directory, parent = Path(directory), Path(parent)
    if (directory.parent != parent or directory.is_symlink() or not directory.is_dir()
            or directory.resolve() != directory.absolute() or os.path.ismount(directory)):
        raise UnsafeInventory()
    size, device = 0, directory.stat().st_dev
    for current, dirs, files in os.walk(directory, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(current) / name
            info = path.lstat()
            if (stat.S_ISLNK(info.st_mode) or info.st_dev != device
                    or getattr(info, "st_file_attributes", 0) & 0x400
                    or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                    or path.resolve() != path.absolute() or os.path.ismount(path)):
                raise UnsafeInventory()
            if stat.S_ISREG(info.st_mode):
                size += info.st_size
    return size


def _compose(path, project, update_volume):
    value = _json(path)
    if value.get("name") != project or set(value.get("services", {})) != SERVICES:
        raise UnsafeInventory()
    if value.get("volumes", {}).get("update-data", {}).get("name") != update_volume:
        raise UnsafeInventory()
    refs, owned_refs = set(), set()
    for name, service in value["services"].items():
        image = service.get("image")
        if not isinstance(image, str) or not image or any(c.isspace() for c in image):
            raise UnsafeInventory()
        refs.add(image)
        if name != "napcat":
            owned_refs.add(image)
    return refs, owned_refs


def _complete_job(directory, project, update_volume):
    from deploy.updater.volume import verify

    job = _json(directory / "job.json")
    if (job.get("id") != directory.name or not JOB_ID.fullmatch(directory.name)
            or job.get("finished") is not True or job.get("terminal_phase") != "completed"
            or job.get("backup_complete") is not True
            or not isinstance(job.get("revision"), str) or not REVISION.fullmatch(job["revision"])):
        raise UnsafeInventory()
    completed = _timestamp(job.get("completed_at", job.get("created_at")))
    refs, owned_refs = set(), set()
    for name in ("old-compose.json", "new-compose.json"):
        document_refs, own = _compose(directory / name, project, update_volume)
        refs.update(document_refs)
        owned_refs.update(own)
    worker = job.get("worker_image")
    if not isinstance(worker, str) or not worker or any(c.isspace() for c in worker):
        raise UnsafeInventory()
    refs.add(worker)
    owned_refs.add(worker)
    allowed = {"job.json", "old-compose.json", "new-compose.json", "private.log", "source"}
    for name in DATA_VOLUMES:
        archive = directory / (name + ".tar")
        manifest = _json(directory / (name + ".tar.json"))
        if (archive.is_symlink() or not archive.is_file()
                or archive.stat().st_size != manifest.get("size")
                or not isinstance(manifest.get("sha256"), str)
                or not HASH.fullmatch(manifest["sha256"])):
            raise UnsafeInventory()
        allowed.update((archive.name, archive.name + ".json"))
        # An intact old backup must not be removed in favor of a recent archive
        # that has since been truncated or corrupted, even at the same size.
        verify(archive)
    if any(path.name not in allowed for path in directory.iterdir()):
        raise UnsafeInventory()
    size = _tree_size(directory, directory.parent)
    return {"id": directory.name, "path": directory, "time": completed,
            "refs": refs, "owned_refs": owned_refs, "size": size,
            "coverage": tuple(sorted(DATA_VOLUMES)), "maintenance": False}


def _maintenance_snapshot(directory, project, update_volume, journal_name):
    """Authenticate a known two-volume rollout without trusting its filename."""
    from deploy.updater.volume import verify

    directory = Path(directory).absolute()
    if (directory.parent.name != "jobs" or not JOB_ID.fullmatch(directory.name)
            or not isinstance(journal_name, str) or journal_name not in MAINTENANCE_JOURNALS):
        raise UnsafeInventory()
    _tree_size(directory, directory.parent)  # Reject links before reading metadata.
    metadata = {journal_name, "old-compose.json", "new-compose.json", "captured.json",
                "previous-status.json", "retention-pin.json", "adopted/old-compose.json",
                *(name + ".tar.json" for name in MAINTENANCE_VOLUMES)}
    archives = {name + ".tar" for name in MAINTENANCE_VOLUMES}
    allowed_files = metadata | archives | {"maintenance.json"}
    files = set()
    for path in directory.rglob("*"):
        name = path.relative_to(directory).as_posix()
        if path.is_dir():
            if name != "adopted":
                raise UnsafeInventory()
        elif path.is_file() and name in allowed_files:
            files.add(name)
        else:
            raise UnsafeInventory()
    job = _json(directory / journal_name)
    if (job.get("id") != directory.name or job.get("phase") != "completed"
            or job.get("terminal_phase") != "completed"
            or any(job.get(flag) is not True for flag in
                   ("backup_complete", "mutation_started", "services_stopped"))):
        raise UnsafeInventory()
    old, new = (_json(directory / name) for name in ("old-compose.json", "new-compose.json"))
    refs = set()
    for name in ("old-compose.json", "new-compose.json"):
        refs.update(_compose(directory / name, project, update_volume)[0])
    if any(not IMAGE_ID.fullmatch(ref) for ref in refs):
        raise UnsafeInventory()
    candidate, helper = job.get("candidate_image"), job.get("helper_image")
    if (not isinstance(candidate, str) or not IMAGE_ID.fullmatch(candidate)
            or helper != old["services"]["updater"]["image"]
            or candidate == old["services"]["napcat"]["image"]):
        raise UnsafeInventory()
    expected = deepcopy(old)
    for service in ("app", "wizard"):
        expected["services"][service]["image"] = candidate
    if new != expected:
        raise UnsafeInventory()
    volumes = job.get("volumes")
    if (not isinstance(volumes, dict) or set(volumes) != {*DATA_VOLUMES, "update-data"}
            or volumes.get("update-data") != update_volume
            or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", v)
                   for v in volumes.values()) or len(set(volumes.values())) != len(volumes)):
        raise UnsafeInventory()
    for document in (old, new):
        if set(document.get("volumes", {})) != set(volumes):
            raise UnsafeInventory()
        if any(document["volumes"][name].get("name") != actual for name, actual in volumes.items()):
            raise UnsafeInventory()
    before = job.get("before")
    if (not isinstance(before, dict) or set(before) != SERVICES
            or any(not isinstance(before[name], dict)
                   or before[name].get("image") != old["services"][name]["image"] for name in SERVICES)):
        raise UnsafeInventory()
    captured = _json(directory / "captured.json")
    if captured != {"volumes": volumes, "services": before}:
        raise UnsafeInventory()
    # A successful rollout captures the actual running deployment before it
    # marks the journal complete; do not certify a mere intended Compose edit.
    if _json(directory / "adopted/old-compose.json") != new:
        raise UnsafeInventory()
    if "retention-pin.json" in files:
        pin = _json(directory / "retention-pin.json")
        if (pin.get("version") != 1 or pin.get("project") != project
                or not isinstance(pin.get("images"), list)
                or any(not isinstance(ref, str) for ref in pin["images"])
                or set(pin["images"]) != refs):
            raise UnsafeInventory()
    archive_records = {}
    for name in MAINTENANCE_VOLUMES:
        archive = directory / (name + ".tar")
        manifest = _json(directory / (name + ".tar.json"))
        if (type(manifest.get("size")) is not int or manifest["size"] < 0
                or not isinstance(manifest.get("sha256"), str)
                or not HASH.fullmatch(manifest["sha256"])):
            raise UnsafeInventory()
        verify(archive)
        archive_records[name] = {"size": manifest["size"], "sha256": manifest["sha256"]}
    metadata_hashes = {}
    for name in sorted(files & metadata):
        _json(directory / name)
        metadata_hashes[name] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
    return {"metadata_sha256": metadata_hashes, "archives": archive_records,
            "images": sorted(refs), "owned_images": [candidate]}


def register_completed_maintenance(directory, project, update_volume, completed_at,
                                   journal_name="proactive-job.json"):
    """Explicit operator opt-in; call under worker.lock with updates idle.

    Legacy journal mtime may be supplied as an aware ISO completion timestamp,
    but only the completed journal, actual deployment snapshot and verified
    backups authorize registration. No Docker image or live data is modified.
    """
    directory = Path(directory).absolute()
    _timestamp(completed_at)
    snapshot = _maintenance_snapshot(directory, project, update_volume, journal_name)
    record = {"version": 1, "kind": "qunling-maintenance", "project": project,
              "id": directory.name, "update_volume": update_volume,
              "completed_at": completed_at, "completed_at_source": "operator_supplied",
              "journal": journal_name, "backup_volumes": list(MAINTENANCE_VOLUMES), **snapshot}
    destination = directory / "maintenance.json"
    if destination.exists() or destination.is_symlink():
        if _json(destination) != record:
            raise UnsafeInventory()
        return record
    descriptor, temporary = tempfile.mkstemp(prefix=".maintenance-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(record, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return record


def _complete_maintenance(directory, project, update_volume):
    record = _json(directory / "maintenance.json")
    if (record.get("version") != 1 or record.get("kind") != "qunling-maintenance"
            or record.get("project") != project or record.get("id") != directory.name
            or record.get("update_volume") != update_volume
            or record.get("completed_at_source") != "operator_supplied"
            or record.get("backup_volumes") != list(MAINTENANCE_VOLUMES)):
        raise UnsafeInventory()
    completed = _timestamp(record.get("completed_at"))
    snapshot = _maintenance_snapshot(directory, project, update_volume, record.get("journal"))
    if any(record.get(name) != value for name, value in snapshot.items()):
        raise UnsafeInventory()
    return {"id": directory.name, "path": directory, "time": completed,
            "refs": set(snapshot["images"]), "owned_refs": set(snapshot["owned_images"]),
            "size": _tree_size(directory, directory.parent),
            "coverage": tuple(sorted(MAINTENANCE_VOLUMES)), "maintenance": True}


def _docker_list(engine, command, inspect):
    ids = list(dict.fromkeys(engine.run(*command).split()))
    if not ids:
        return []
    output = []
    for offset in range(0, len(ids), 100):
        chunk = ids[offset:offset + 100]
        values = json.loads(engine.run(*inspect, *chunk))
        if not isinstance(values, list) or len(values) != len(chunk):
            raise UnsafeInventory()
        output.extend(values)
    return output


def cleanup(engine, keep_backups=2):
    """Called while the API's engine lock is held and no update is in flight."""
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "status": "completed",
              "keep_backups": keep_backups, "removed_jobs": [], "removed_images": [],
              "removed_workers": [], "reclaimed_backup_bytes": 0, "warnings": []}

    def warn(code):
        if code not in report["warnings"]:
            report["warnings"].append(code)
        report["status"] = "partial"

    try:
        if isinstance(keep_backups, bool) or not isinstance(keep_backups, int) or not 1 <= keep_backups <= 10:
            raise UnsafeInventory()
        root = Path(engine.root).absolute()
        if root.is_symlink() or root.resolve() != root:
            raise UnsafeInventory()
        status = _json(engine.state_path) if Path(engine.state_path).exists() else {}
        if status.get("phase", "idle") not in IDLE_PHASES:
            report["status"] = "skipped"
            return report
        updater = engine.inspect_service("updater")
        mounts = [m for m in updater["Mounts"] if m.get("Destination") == str(engine.root)]
        if len(mounts) != 1 or mounts[0].get("Type") != "volume":
            raise UnsafeInventory()
        update_volume = mounts[0]["Name"]
        volumes = json.loads(engine.run("docker", "volume", "inspect", update_volume))
        if (not isinstance(volumes, list) or len(volumes) != 1
                or (volumes[0].get("Labels") or {}).get("com.docker.compose.project") != engine.project):
            raise UnsafeInventory()
        protected_refs, _ = _compose(engine.active_path, engine.project, update_volume)
        containers = _docker_list(engine, ("docker", "ps", "-aq", "--no-trunc"), ("docker", "inspect"))
        images = _docker_list(engine, ("docker", "image", "ls", "-aq", "--no-trunc"),
                              ("docker", "image", "inspect"))
        refs_to_ids, image_by_id = {}, {}
        for image in images:
            identity = image.get("Id", "")
            if not isinstance(identity, str) or not IMAGE_ID.fullmatch(identity):
                raise UnsafeInventory()
            image_by_id[identity] = image
            for ref in [identity, *(image.get("RepoTags") or []), *(image.get("RepoDigests") or [])]:
                if not isinstance(ref, str) or (ref in refs_to_ids and refs_to_ids[ref] != identity):
                    raise UnsafeInventory()
                refs_to_ids[ref] = identity
        protected_jobs = {status.get("job_id"), *(value for key, value in status.items()
                          if key.endswith("_owner") and isinstance(value, str))}
        for container in containers:
            identity, image = container.get("Id"), container.get("Image")
            if not identity or not isinstance(image, str) or not IMAGE_ID.fullmatch(image):
                raise UnsafeInventory()
            labels = container.get("Config", {}).get("Labels") or {}
            state = container.get("State", {})
            if labels.get("io.qunling.update-project") == engine.project and state.get("Status") not in {"exited", "dead"}:
                protected_jobs.add(labels.get("io.qunling.update-job"))
            helper_job = labels.get("io.qunling.proactive-helper")
            if (isinstance(helper_job, str) and JOB_ID.fullmatch(helper_job)
                    and state.get("Status") not in {"exited", "dead"}):
                protected_jobs.add(helper_job)
        jobs_root = root / "jobs"
        if jobs_root.is_symlink() or (jobs_root.exists() and (not jobs_root.is_dir() or jobs_root.resolve() != jobs_root)):
            raise UnsafeInventory()
        valid, image_inventory_safe = [], True
        all_owned_refs, maintenance_owned_ids = set(), set()
        for directory in sorted(jobs_root.iterdir()) if jobs_root.exists() else []:
            try:
                # A registered maintenance record has its own narrow protocol.
                # Never infer permission from a private journal or a pin alone.
                candidate = (_complete_maintenance(directory, engine.project, update_volume)
                             if (directory / "maintenance.json").exists()
                             else _complete_job(directory, engine.project, update_volume))
                valid.append(candidate)
                all_owned_refs.update(candidate["owned_refs"])
                if candidate["maintenance"]:
                    maintenance_owned_ids.update(candidate["owned_refs"])
            except Exception:
                # Incomplete/private/manual/malformed jobs are never deleted.
                # Their snapshots still protect images when their provenance is readable.
                warn("unmanaged_or_unresolved_history_preserved")
                try:
                    if directory.is_symlink() or not directory.is_dir():
                        raise UnsafeInventory()
                    found = False
                    for name in ("old-compose.json", "new-compose.json"):
                        if (directory / name).exists():
                            protected_refs.update(_compose(directory / name, engine.project, update_volume)[0])
                            found = True
                    job = _json(directory / "job.json")
                    worker = job.get("worker_image")
                    if worker:
                        protected_refs.add(worker)
                    if not found:
                        raise UnsafeInventory()
                except Exception:
                    try:
                        # An operator may pin manually deployed snapshots. This
                        # never makes their directories eligible for deletion.
                        if directory.is_symlink() or not directory.is_dir():
                            raise UnsafeInventory()
                        pin = _json(directory / "retention-pin.json")
                        refs = pin.get("images")
                        if (pin.get("version") != 1 or pin.get("project") != engine.project
                                or not isinstance(refs, list) or not 1 <= len(refs) <= 100
                                or any(not isinstance(ref, str) or not ref or any(c.isspace() for c in ref)
                                       for ref in refs)):
                            raise UnsafeInventory()
                        protected_refs.update(refs)
                    except Exception:
                        image_inventory_safe = False
        ordered = sorted(valid, key=lambda job: (job["time"], job["id"]), reverse=True)
        # A recent app-only snapshot cannot replace the last complete QQ backup.
        keep, coverage_counts = set(protected_jobs), {}
        for job in ordered:
            coverage = job["coverage"]
            coverage_counts[coverage] = coverage_counts.get(coverage, 0) + 1
            if coverage_counts[coverage] <= keep_backups:
                keep.add(job["id"])
        for job in valid:
            if job["id"] in keep:
                protected_refs.update(job["refs"])
        # Resolve every retained reference before authorizing image removal.
        for ref in protected_refs:
            if ref in refs_to_ids:
                continue
            # Compose can retain repo:tag@digest while Docker indexes RepoDigests
            # as repo@digest. An exact digest alias denotes the same local image.
            repository, separator, digest = ref.partition("@")
            if separator and IMAGE_ID.fullmatch(digest):
                if repository.rfind(":") > repository.rfind("/"):
                    repository = repository.rsplit(":", 1)[0]
                canonical = repository + "@" + digest
                if canonical in refs_to_ids:
                    refs_to_ids[ref] = refs_to_ids[canonical]
                    continue
            try:
                found = json.loads(engine.run("docker", "image", "inspect", ref))
                if not isinstance(found, list) or len(found) != 1 or found[0].get("Id") not in image_by_id:
                    raise UnsafeInventory()
                refs_to_ids[ref] = found[0]["Id"]
            except Exception:
                image_inventory_safe = False
        protected_ids = {refs_to_ids[ref] for ref in protected_refs if ref in refs_to_ids}
        valid_ids = {job["id"] for job in valid}
        worker_pattern = re.compile(re.escape(engine.project) + r"-update-([0-9a-f]{24})(?:-recovery-[0-9a-f]{6})?\Z")
        for container in containers:
            labels = container.get("Config", {}).get("Labels") or {}
            state = container.get("State", {})
            job_id = labels.get("io.qunling.update-job")
            matched = worker_pattern.fullmatch(container.get("Name", "").removeprefix("/"))
            if (job_id in valid_ids and labels.get("io.qunling.update-project") == engine.project
                    and matched and matched[1] == job_id and state.get("Status") in {"exited", "dead"}
                    and state.get("Running") is False):
                try:
                    engine.run("docker", "container", "rm", container["Id"])
                    report["removed_workers"].append(container["Id"])
                    continue
                except Exception:
                    warn("worker_removal_failed")
            protected_ids.add(container["Image"])
        for job in ordered:
            if job["id"] not in keep:
                try:
                    _tree_size(job["path"], jobs_root)
                    shutil.rmtree(job["path"])
                    report["removed_jobs"].append(job["id"])
                    report["reclaimed_backup_bytes"] += job["size"]
                except Exception:
                    protected_refs.update(job["refs"])
                    protected_ids.update(refs_to_ids[ref] for ref in job["refs"] if ref in refs_to_ids)
                    warn("backup_removal_failed")
        own_tag = re.compile(re.escape(engine.project) + r"-(?:app|updater):[0-9a-f]{40}\Z")
        maintenance_tag = re.compile(re.escape(engine.project)
                                     + r"-[a-z0-9]+(?:-[a-z0-9]+)*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
        recorded_ids = {refs_to_ids[ref] for ref in all_owned_refs if ref in refs_to_ids}
        if not image_inventory_safe:
            warn("image_cleanup_skipped_incomplete_history")
        else:
            for identity, image in image_by_id.items():
                tags = [tag for tag in (image.get("RepoTags") or []) if tag != "<none>:<none>"]
                labels = image.get("Config", {}).get("Labels") or {}
                owner = labels.get("io.qunling.update-project")
                owned = ((tags and all(own_tag.fullmatch(tag) for tag in tags) and owner in {None, engine.project})
                         or (not tags and owner == engine.project and identity in recorded_ids)
                         or (identity in maintenance_owned_ids and owner in {None, engine.project}
                             and all(maintenance_tag.fullmatch(tag) for tag in tags)))
                if identity in protected_ids or not owned or image.get("RepoDigests"):
                    continue
                try:
                    # No --force: a container created concurrently by an operator
                    # remains protected by Docker's own reference checks.
                    if len(tags) > 1:
                        # Docker refuses multi-tag IDs without force; preserve them.
                        continue
                    engine.run("docker", "image", "rm", identity, timeout=120)
                    report["removed_images"].append(identity)
                except Exception:
                    warn("image_removal_failed")
    except Exception:
        report["status"] = "failed"
        report["warnings"] = [*report["warnings"], "cleanup_inventory_failed"]
    return report
