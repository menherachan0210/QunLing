"""Run real, isolated Docker update/rollback transactions without QQ or AI calls.

Usage (Docker CLI + Compose required; helper image must already exist):
    python -m deploy.updater.smoke run --helper-image qunling-updater:test

Only checkout and wizard readiness are synthetic. Image builds, Compose service
replacement, standalone workers, five named-volume backups and restores use the
production engine. All created resources carry a unique test project label and
are removed in ``finally``; existing deployments are never selected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import time

from deploy.updater.engine import DATA_VOLUMES, SERVICES, UpdateEngine, read, write

PROJECT_PATTERN = re.compile(r"qunling-update-smoke-[0-9a-f]{12}\Z")
OLD = "1" * 40
GOOD = "2" * 40
BAD = "3" * 40
ROOT = Path("/var/lib/qqbot-updater")
SMOKE_LABEL = "io.qunling.update-smoke"


def project_name(value):
    if not isinstance(value, str) or not PROJECT_PATTERN.fullmatch(value):
        raise RuntimeError("Refusing a project without the unique smoke-test prefix")
    return value


def fixture_compose(project, napcat_image):
    """No published ports, host paths other than Docker, or production names."""
    project_name(project)
    app = "${QQBOT_APP_IMAGE:-" + project + "-fixture:base}"
    updater = "${QQBOT_UPDATER_IMAGE:-" + project + "-fixture:base}"
    shared = {
        "restart": "no",
        "depends_on": {"init": {"condition": "service_completed_successfully"}},
    }
    def mounts(names):
        return [{"type": "volume", "source": name, "target": "/data/" + name} for name in names]
    return {
        "name": project,
        "services": {
            "init": {"restart": "no", "image": app, "command": ["noop"]},
            "app": {**shared, "image": app, "command": ["idle"],
                    "volumes": mounts(["langbot-data"])},
            "napcat": {**shared, "image": napcat_image, "command": ["idle"],
                       "volumes": mounts(["napcat-qq", "napcat-config", "napcat-plugins"])},
            "wizard": {**shared, "image": app, "command": ["idle"], "volumes": [
                {"type": "volume", "source": "setup-state", "target": "/var/lib/qqbot-setup"}
            ]},
            "updater": {**shared, "image": updater, "command": ["idle"],
                        "environment": {"QQBOT_PROJECT": project}, "volumes": [
                {"type": "volume", "source": "setup-state",
                 "target": "/var/lib/qqbot-setup", "read_only": True},
                {"type": "volume", "source": "update-data", "target": str(ROOT)},
                {"type": "bind", "source": "/var/run/docker.sock",
                 "target": "/var/run/docker.sock"},
            ]},
        },
        "volumes": {name: {} for name in (*DATA_VOLUMES, "update-data")},
    }


class SmokeEngine(UpdateEngine):
    def __init__(self):
        super().__init__(
            directory=ROOT, project=project_name(os.environ.get("QQBOT_PROJECT")),
            repository="qunling-smoke/offline-fixture", branch="main",
        )

    def checkout(self, revision, directory):
        if revision not in {GOOD, BAD}:
            raise RuntimeError("Only smoke-test revisions are allowed")
        directory.mkdir(parents=True)
        napcat = self.inspect_service("napcat")["Image"]
        write(directory / "deploy/compose/compose.yaml", fixture_compose(self.project, napcat))
        # Both derived images inherit the real update/volume code and this
        # harness. Docker performs actual builds, without downloading layers.
        dockerfile = (
            f"FROM {self.project}-fixture:base\n"
            f"LABEL org.opencontainers.image.revision={revision}\n"
            f"LABEL {SMOKE_LABEL}={self.project}\n"
        )
        for relative in ("deploy/compose/Dockerfile", "deploy/updater/Dockerfile"):
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(dockerfile, encoding="utf-8")

    def wizard_status(self):
        state = read(self.state_path, {})
        job_id = state.get("job_id")
        job = read(self.root / "jobs" / job_id / "job.json", {}) if job_id else {}
        if (
            job.get("revision") == BAD
            and job.get("phase") == "verifying"
            and self.current_revision() == BAD
        ):
            # Simulate a new plugin/schema writing data before readiness fails.
            # All writes occur through real mounts, so rollback must remove new
            # files as well as restoring overwritten content in all five volumes.
            for logical in DATA_VOLUMES:
                volume = job["volumes"][logical]
                self.run(
                    "docker", "run", "--rm", "--network", "none",
                    "--mount", f"type=volume,src={volume},dst=/data",
                    self.project + "-fixture:base", "mutate",
                )
            return {"stage": "error", "configured": False, "plugins_ready": False}
        return {"stage": "ready", "configured": True, "plugins_ready": True}


def run_command(*args, timeout=300, env=None):
    result = subprocess.run(
        list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    if result.returncode:
        raise RuntimeError(
            "Smoke command failed: " + " ".join(map(str, args[:5]))
            + "\n" + result.stdout[-6000:] + result.stderr[-6000:]
        )
    return result.stdout.strip()


def inspect(resource, identifier):
    args = ["docker", resource, "inspect", identifier] if resource else ["docker", "inspect", identifier]
    return json.loads(run_command(*args))[0]


def service_container(project, service):
    project_name(project)
    ids = run_command(
        "docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project,
        "--filter", "label=com.docker.compose.service=" + service,
        "--filter", "label=com.docker.compose.oneoff=False",
    ).split()
    if len(ids) != 1:
        raise RuntimeError("Expected exactly one smoke service: " + service)
    info = inspect("", ids[0])
    if (info.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project") != project:
        raise RuntimeError("Container belongs to another project")
    return info


def image_ids(project):
    return {name: service_container(project, name)["Image"] for name in SERVICES}


def helper(project, *args, volume=None, control=False):
    project_name(project)
    command = [
        "docker", "run", "--rm", "--label", SMOKE_LABEL + "=" + project,
        "--network", project + "_default" if control else "none",
    ]
    if volume:
        if volume not in DATA_VOLUMES:
            raise RuntimeError("Unexpected test data volume")
        command.extend(["--mount", f"type=volume,src={project}_{volume},dst=/data"])
    if control:
        command.extend([
            "--mount", f"type=volume,src={project}_update-data,dst={ROOT}",
            "--mount", f"type=volume,src={project}_setup-state,dst=/var/lib/qqbot-setup,readonly",
            "--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "-e", "QQBOT_PROJECT=" + project,
        ])
    command.extend([project + "-fixture:base", *args])
    return run_command(*command)


def assert_data(project):
    result = {}
    for logical in DATA_VOLUMES:
        actual = json.loads(helper(project, "read-data", volume=logical))
        expected = {"sentinel.txt": project + "/" + logical, "nested/original.bin": "000102ff"}
        if actual != expected:
            raise AssertionError(f"Volume data changed or new files survived: {logical}: {actual}")
        result[logical] = hashlib.sha256(
            json.dumps(actual, sort_keys=True).encode()
        ).hexdigest()
    return result


def transaction(project, revision):
    before_updater = service_container(project, "updater")["Id"]
    queued = json.loads(helper(project, "controller", revision, control=True))
    job_id = queued["job_id"]
    if not re.fullmatch(r"[0-9a-f]{24}", job_id):
        raise RuntimeError("Invalid smoke job id")
    worker_name = project + "-update-" + job_id
    before_worker = inspect("", worker_name)
    if before_worker["Config"]["Labels"].get("io.qunling.update-project") != project:
        raise RuntimeError("Worker belongs to another project")
    exit_code = run_command("docker", "wait", worker_name, timeout=600)
    after_worker = inspect("", worker_name)
    outcome = json.loads(helper(project, "report", job_id, control=True))
    if (
        exit_code != "0"
        or before_worker["Id"] != after_worker["Id"]
        or after_worker.get("RestartCount") != 0
        or not outcome["worker_survived"]
    ):
        logs = run_command("docker", "logs", worker_name)
        raise AssertionError("Standalone worker did not survive replacement:\n" + logs[-6000:])
    expected = "completed" if revision == GOOD else "rolled_back"
    if outcome["status"]["phase"] != expected:
        raise AssertionError("Unexpected update outcome: " + json.dumps(outcome, ensure_ascii=False))
    if outcome["actual_revision"] != GOOD or outcome["status"]["current_revision"] != GOOD:
        raise AssertionError("Current image revision disagrees with recorded revision")
    if service_container(project, "updater")["Id"] == before_updater:
        raise AssertionError("Updater was not replaced; worker survival was not exercised")
    if revision == GOOD and after_worker["Image"] == service_container(project, "updater")["Image"]:
        raise AssertionError("Independent worker did not keep the previous image")
    outcome["worker_id"] = after_worker["Id"]
    return outcome


def cleanup(project):
    """Enumerate only our exact labels; recheck ownership before every removal."""
    project_name(project)
    errors = []

    def attempt(action):
        try:
            action()
        except Exception as exc:
            errors.append(str(exc))

    containers = set()
    for label in ("com.docker.compose.project=", "io.qunling.update-project=", SMOKE_LABEL + "="):
        containers.update(run_command("docker", "ps", "-aq", "--filter", "label=" + label + project).split())
    for identifier in containers:
        def remove_container(identifier=identifier):
            value = inspect("", identifier)
            labels = value.get("Config", {}).get("Labels") or {}
            if project not in (
                labels.get("com.docker.compose.project"), labels.get("io.qunling.update-project"),
                labels.get(SMOKE_LABEL),
            ):
                raise RuntimeError("Refusing foreign container cleanup")
            run_command("docker", "rm", "-f", identifier)
        attempt(remove_container)
    for kind in ("network", "volume"):
        identifiers = run_command(
            "docker", kind, "ls", "-q", "--filter", "label=com.docker.compose.project=" + project
        ).split()
        for identifier in identifiers:
            def remove_resource(identifier=identifier, kind=kind):
                value = inspect(kind, identifier)
                if (
                    value.get("Labels", {}).get("com.docker.compose.project") != project
                    or not value.get("Name", "").startswith(project + "_")
                ):
                    raise RuntimeError("Refusing foreign " + kind + " cleanup")
                run_command("docker", kind, "rm", identifier)
            attempt(remove_resource)
    image_values = run_command(
        "docker", "image", "ls", "-q", "--filter", "label=" + SMOKE_LABEL + "=" + project
    ).split()
    # Children before their fixture base also works with the classic builder.
    owned_images = [inspect("image", identifier) for identifier in set(image_values)]
    for value in sorted(owned_images, key=lambda image: image.get("Created", ""), reverse=True):
        def remove_image(value=value):
            if (value.get("Config", {}).get("Labels") or {}).get(SMOKE_LABEL) != project:
                raise RuntimeError("Refusing foreign image cleanup")
            run_command("docker", "image", "rm", "-f", value["Id"])
        attempt(remove_image)
    if errors:
        raise RuntimeError("Smoke resource cleanup was incomplete:\n" + "\n".join(errors))


def run_smoke(helper_image):
    run_command("docker", "version", "--format", "{{.Server.Version}}")
    run_command("docker", "compose", "version")
    # Inspect first; never download an unexpected helper image.
    source_image = inspect("image", helper_image)
    project = "qunling-update-smoke-" + secrets.token_hex(6)
    project_name(project)
    report = {"project": project, "production_services_used": False}
    try:
        with tempfile.TemporaryDirectory(prefix=project + "-") as temporary:
            directory = Path(temporary)
            shutil.copyfile(__file__, directory / "smoke.py")
            (directory / "Dockerfile").write_text(
                "ARG BASE\nFROM ${BASE}\n"
                "COPY smoke.py /opt/qqbot/deploy/updater/smoke.py\n"
                'ENTRYPOINT ["python", "-m", "deploy.updater.smoke"]\n'
                'CMD ["idle"]\n'
                f"LABEL org.opencontainers.image.revision={OLD}\n"
                f"LABEL {SMOKE_LABEL}={project}\n",
                encoding="utf-8",
            )
            base = project + "-fixture:base"
            run_command(
                "docker", "build", "--pull=false", "--network=none",
                "--build-arg", "BASE=" + helper_image, "-t", base, str(directory), timeout=600,
            )
            image = inspect("image", base)
            if image["Id"] == source_image["Id"]:
                raise AssertionError("Fixture image must be distinct from the supplied helper")
            compose = directory / "compose.json"
            write(compose, fixture_compose(project, image["Id"]))
            run_command(
                "docker", "compose", "--project-name", project, "-f", str(compose),
                "up", "-d", "--no-build", "--wait", "--wait-timeout", "60",
                timeout=120, env={"QQBOT_APP_IMAGE": base, "QQBOT_UPDATER_IMAGE": base},
            )
            for logical in (*DATA_VOLUMES, "update-data"):
                info = inspect("volume", project + "_" + logical)
                if info.get("Labels", {}).get("com.docker.compose.project") != project:
                    raise RuntimeError("Fixture volume is not owned by smoke project")
            updater = service_container(project, "updater")
            run_command("docker", "cp", str(compose), updater["Id"] + ":" + str(ROOT / "active-compose.json"))
            for logical in DATA_VOLUMES:
                helper(project, "seed", project + "/" + logical, volume=logical)
            original_data = assert_data(project)
            original_images = image_ids(project)
            success = transaction(project, GOOD)
            good_images = image_ids(project)
            if any(good_images[name] == original_images[name] for name in ("init", "app", "wizard", "updater")):
                raise AssertionError("Successful update did not replace all managed images")
            if good_images["napcat"] != original_images["napcat"]:
                raise AssertionError("QQ fixture image unexpectedly changed")
            if assert_data(project) != original_data:
                raise AssertionError("Successful update lost volume data")
            rollback = transaction(project, BAD)
            if image_ids(project) != good_images:
                raise AssertionError("Rollback did not restore exact previous image IDs")
            if assert_data(project) != original_data:
                raise AssertionError("Rollback did not restore all five original volumes")
            report.update(
                success_phase=success["status"]["phase"],
                rollback_phase=rollback["status"]["phase"],
                current_revision=rollback["actual_revision"],
                verified_volumes=list(DATA_VOLUMES), exact_images_restored=True,
                workers_survived=True, new_files_removed=True,
                success_worker=success["worker_id"], rollback_worker=rollback["worker_id"],
            )
    finally:
        cleanup(project)
    report["resources_cleaned"] = True
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


def container_role(role, arguments):
    if role == "volume":
        from deploy.updater.server import main as volume_main
        volume_main()
    elif role == "idle":
        import signal
        import sys
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        while True:
            time.sleep(3600)
    elif role == "noop":
        return
    elif role == "seed":
        if len(arguments) != 1 or not Path("/data").is_mount():
            raise RuntimeError("Invalid seed mount")
        Path("/data/sentinel.txt").write_text(arguments[0], encoding="utf-8")
        Path("/data/nested").mkdir(exist_ok=True)
        Path("/data/nested/original.bin").write_bytes(b"\x00\x01\x02\xff")
    elif role == "mutate":
        if not Path("/data").is_mount():
            raise RuntimeError("Invalid mutation mount")
        Path("/data/sentinel.txt").write_text("overwritten-by-failed-version", encoding="utf-8")
        Path("/data/new-file-from-failed-version.txt").write_text("remove-on-rollback", encoding="utf-8")
    elif role == "read-data":
        if not Path("/data").is_mount():
            raise RuntimeError("Invalid data read mount")
        values = {}
        for path in sorted(Path("/data").rglob("*")):
            if path.is_file():
                relative = path.relative_to("/data").as_posix()
                values[relative] = path.read_bytes().hex() if path.suffix == ".bin" else path.read_text(encoding="utf-8")
        print(json.dumps(values), flush=True)
    elif role == "controller":
        engine = SmokeEngine()
        engine.publish(
            phase="available", latest_revision=arguments[0],
            current_revision=engine.current_revision(), message="Smoke update ready",
        )
        engine.start(arguments[0])
        print(json.dumps(read(engine.state_path)), flush=True)
    elif role == "worker":
        import fcntl
        engine = SmokeEngine()
        with (engine.root / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            engine.execute(arguments[0])
            write(engine.root / "jobs" / arguments[0] / "worker-survived.json", {"completed": True})
    elif role == "report":
        engine = SmokeEngine()
        job_dir = engine.root / "jobs" / arguments[0]
        print(json.dumps({
            "status": engine.snapshot(), "job": read(job_dir / "job.json", {}),
            "actual_revision": engine.current_revision(),
            "worker_survived": bool(read(job_dir / "worker-survived.json", {}).get("completed")),
        }), flush=True)
    else:
        raise RuntimeError("Unknown smoke role")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=[
        "run", "idle", "noop", "worker", "controller", "report",
        "volume", "seed", "mutate", "read-data",
    ])
    parser.add_argument("arguments", nargs="*")
    parser.add_argument("--helper-image")
    options = parser.parse_args()
    if options.role == "run":
        if not options.helper_image or options.arguments:
            parser.error("run requires --helper-image and no positional arguments")
        run_smoke(options.helper_image)
    else:
        container_role(options.role, options.arguments)


if __name__ == "__main__":
    main()
