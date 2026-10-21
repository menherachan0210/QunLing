"""Transactional Compose updates using named volumes, also on Docker Desktop.

The public web page cannot choose commands, paths or a repository. A separate
job container survives replacement of the app, wizard and updater services.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from urllib.request import Request, urlopen

SERVICES = ("init", "app", "napcat", "wizard", "updater")
DATA_VOLUMES = ("langbot-data", "setup-state", "napcat-qq", "napcat-config", "napcat-plugins")
SHA = re.compile(r"^[0-9a-f]{40}$")
TERMINAL = {"idle", "available", "completed", "rolled_back", "failed"}


class UpdateError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path, default=None):
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else default


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Runner:
    def run(self, args, *, timeout=120, env=None, log=None):
        process_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
        # Never invoke a shell or return process output to the browser. Long build
        # logs go to the private update volume, not to memory or the HTTP response.
        if log:
            with Path(log).open("ab") as stream:
                result = subprocess.run(args, env=process_env, stdout=stream,
                                        stderr=subprocess.STDOUT, timeout=timeout)
            output = ""
        else:
            result = subprocess.run(args, env=process_env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=timeout)
            output = result.stdout.decode("utf-8", "replace")
        if result.returncode:
            raise UpdateError("部署命令执行失败，详细日志保存在更新数据卷。")
        return output


class UpdateEngine:
    def __init__(self, directory=None, runner=None, project=None, repository=None, branch=None):
        self.root = Path(directory or os.environ.get("QQBOT_UPDATE_DIR", "/var/lib/qqbot-updater"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.runner = runner or Runner()
        self.project = project or os.environ.get("QQBOT_PROJECT", "qq-ai-bot")
        self.repository = repository or os.environ.get("QQBOT_UPDATE_REPOSITORY", "menherachan0210/QunLing")
        self.branch = branch or os.environ.get("QQBOT_UPDATE_BRANCH", "main")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", self.project):
            raise UpdateError("部署项目名称无效。")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise UpdateError("更新仓库必须是 GitHub 的 owner/repository。")
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", self.branch)
                or ".." in self.branch or self.branch.endswith(("/", ".lock"))):
            raise UpdateError("更新分支名称无效。")
        self.remote = "https://github.com/" + self.repository + ".git"
        self.state_path = self.root / "status.json"
        self.active_path = self.root / "active-compose.json"
        self.lock = threading.Lock()
        self.cleanup_path = self.root / "cleanup.json"
        self._cleanup_checked = 0.0
        self._cleanup_generation = None
        self.retention_valid = True
        try:
            self.keep_backups = int(os.environ.get("QQBOT_UPDATE_KEEP_BACKUPS", "2"))
            if not 1 <= self.keep_backups <= 10:
                raise ValueError()
        except ValueError:
            # A mistyped setting must not silently choose a smaller retention.
            self.keep_backups = 2
            self.retention_valid = False

    def run(self, *args, **kwargs):
        return self.runner.run(list(args), **kwargs)

    def inspect_service(self, service):
        ids = self.run("docker", "ps", "-aq", "--filter",
                       "label=com.docker.compose.project=" + self.project,
                       "--filter", "label=com.docker.compose.service=" + service,
                       "--filter", "label=com.docker.compose.oneoff=False").split()
        if len(ids) != 1:
            raise UpdateError("当前部署服务不完整或存在重复实例，请先检查容器状态。")
        return json.loads(self.run("docker", "inspect", ids[0]))[0]

    def current_revision(self):
        info = self.inspect_service("app")
        image = json.loads(self.run("docker", "image", "inspect", info["Image"]))[0]
        revision = (image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision", "")
        return revision if SHA.fullmatch(revision) else ""

    def snapshot(self):
        value = read(self.state_path, {})
        current = value.get("current_revision", "")
        if not current:
            try:
                current = self.current_revision()
            except Exception:
                pass
        phase = value.get("phase", "idle")
        return {
            "available": True, "repository": self.repository, "branch": self.branch,
            "current_revision": current, "latest_revision": value.get("latest_revision", ""),
            "update_available": bool(value.get("latest_revision") and value["latest_revision"] != current),
            "phase": phase, "busy": phase not in TERMINAL,
            "message": value.get("message", "点击检查 GitHub 更新。"),
            "checked_at": value.get("checked_at", ""),
            "cleanup": self.cleanup_snapshot(),
        }

    def cleanup_snapshot(self):
        try:
            report = read(self.cleanup_path, {})
            # Reports contain only counts, timestamps and fixed warning codes;
            # never surface private command output or a backup's contents.
            return {key: report[key] for key in (
                "checked_at", "status", "keep_backups", "reclaimed_backup_bytes", "warnings"
            ) if key in report} | {
                "removed_jobs": len(report.get("removed_jobs", [])),
                "removed_images": len(report.get("removed_images", [])),
                "removed_workers": len(report.get("removed_workers", [])),
            } if report else {}
        except Exception:
            return {}

    def cleanup_history(self, force=False):
        """Best-effort maintenance never changes the update transaction state."""
        from deploy.updater.locking import worker_lock
        from deploy.updater.retention import cleanup

        if not self.lock.acquire(blocking=False):
            return None
        try:
            # Maintenance runs outside this API process. Re-read transaction
            # state only after taking the lock shared with those workers.
            with worker_lock(self.root) as acquired:
                if not acquired:
                    return None
                return self._cleanup_locked(cleanup, force)
        except Exception:
            return None
        finally:
            self.lock.release()

    def _cleanup_locked(self, cleanup, force):
        try:
            state = read(self.state_path, {})
            if state.get("phase", "idle") not in TERMINAL:
                return None
            generation = (state.get("job_id"), state.get("phase"),
                          state.get("proactive_completed"), state.get("maintenance_completed"))
            current = time.monotonic()
            if (not force and self._cleanup_checked and current - self._cleanup_checked < 3600
                    and generation == self._cleanup_generation):
                return None
            self._cleanup_checked = current
            self._cleanup_generation = generation
            try:
                if self.retention_valid:
                    report = cleanup(self, keep_backups=self.keep_backups)
                else:
                    report = {"checked_at": now(), "status": "failed", "keep_backups": self.keep_backups,
                              "warnings": ["invalid_retention_setting_cleanup_disabled"]}
                write(self.cleanup_path, report)
                return report
            except Exception:
                # Disk/Docker failures during maintenance must never trigger a
                # rollback of an otherwise successful update.
                return None
        except Exception:
            return None

    def publish(self, **values):
        state = read(self.state_path, {})
        state.update(values)
        write(self.state_path, state)
        return state

    def remote_revision(self):
        ref = "refs/heads/" + self.branch
        output = self.run("git", "ls-remote", "--exit-code", self.remote, ref, timeout=45)
        rows = [line.split() for line in output.splitlines()]
        if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != ref or not SHA.fullmatch(rows[0][0]):
            raise UpdateError("GitHub 没有返回有效的分支版本。")
        return rows[0][0]

    def check(self):
        with self.lock:
            if self.snapshot()["busy"]:
                raise UpdateError("正在执行更新任务，请稍候。")
            self.publish(phase="checking", message="正在检查 GitHub 更新。")
        try:
            latest, current = self.remote_revision(), self.current_revision()
            return self.publish(phase="available" if latest != current else "idle",
                                current_revision=current, latest_revision=latest, checked_at=now(),
                                message="发现新版本，可点击更新。" if latest != current else "当前已是最新版本。")
        except Exception as exc:
            self.publish(phase="failed", message="检查更新失败，当前服务未受影响，请稍后重试。")
            raise UpdateError("检查更新失败，请检查到 GitHub 的网络连接。") from exc

    def setup_token(self):
        secret = read(Path(os.environ.get("QQBOT_STATE_DIR", "/var/lib/qqbot-setup")) / "secrets.json", {})
        return secret.get("setup_token", "")

    def wizard_status(self):
        request = Request("http://wizard:5310/api/status",
                          headers={"Authorization": "Bearer " + self.setup_token(), "Host": "localhost"})
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def start(self, revision):
        if not isinstance(revision, str) or not SHA.fullmatch(revision):
            raise UpdateError("更新版本格式无效，请重新检查更新。")
        with self.lock:
            status = self.snapshot()
            if status["busy"]:
                raise UpdateError("已有更新任务正在进行。")
            if revision != status["latest_revision"] or not status["update_available"]:
                raise UpdateError("只能安装刚刚检查到的版本，请重新检查更新。")
            try:
                wizard = self.wizard_status()
            except Exception as exc:
                raise UpdateError("设置服务尚未就绪，暂不能更新。") from exc
            if not wizard.get("configured") or wizard.get("stage") not in {"ready", "waiting_qq"}:
                raise UpdateError("初始化或 AI 配置正在进行，请完成后再更新。")
            job_id = secrets.token_hex(12)
            job = {"id": job_id, "revision": revision, "phase": "queued",
                   "mutation_started": False, "backup_complete": False, "created_at": now()}
            write(self.root / "jobs" / job_id / "job.json", job)
            self.publish(phase="queued", job_id=job_id, message="更新任务已排队，开始准备新版本。")
            try:
                self.launch_job(job_id)
            except Exception as exc:
                running = self.run("docker", "ps", "-q", "--filter",
                                   "label=io.qunling.update-job=" + job_id).strip()
                if running:
                    return self.snapshot()
                job.update(finished=True, terminal_phase="failed",
                           terminal_message="无法启动更新任务，当前服务未改动。")
                write(self.root / "jobs" / job_id / "job.json", job)
                self.publish(phase="failed", message="无法启动更新任务，当前服务未改动。")
                raise UpdateError("无法启动更新任务，请检查 Docker。") from exc
        return self.snapshot()

    def launch_job(self, job_id, recovery=False):
        if not re.fullmatch(r"[0-9a-f]{24}", job_id):
            raise UpdateError("无效的更新任务。")
        info = self.inspect_service("updater")
        mounts = {m["Destination"]: m for m in info["Mounts"]}
        for destination in (str(self.root), "/var/lib/qqbot-setup"):
            if mounts.get(destination, {}).get("Type") != "volume":
                raise UpdateError("更新器必须使用命名数据卷。")
        name = self.project + "-update-" + job_id + ("-recovery-" + secrets.token_hex(3) if recovery else "")
        self.run(
            "docker", "run", "-d", "--name", name,
            "--label", "io.qunling.update-project=" + self.project,
            "--label", "io.qunling.update-job=" + job_id,
            "--network", self.project + "_default",
            "--mount", "type=volume,src=" + mounts[str(self.root)]["Name"] + ",dst=" + str(self.root),
            "--mount", "type=volume,src=" + mounts["/var/lib/qqbot-setup"]["Name"] + ",dst=/var/lib/qqbot-setup,readonly",
            "--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "-e", "QQBOT_PROJECT=" + self.project,
            "-e", "QQBOT_UPDATE_REPOSITORY=" + self.repository,
            "-e", "QQBOT_UPDATE_BRANCH=" + self.branch,
            "-e", "QQBOT_UPDATE_KEEP_BACKUPS=" + (str(self.keep_backups) if self.retention_valid else "disabled"),
            info["Image"], "worker", job_id,
        )

    def recover_orphan(self):
        status = read(self.state_path, {})
        if status.get("phase") == "checking":
            self.publish(phase="failed", message="上次检查被中断，可重新检查更新。")
            return
        job_id = status.get("job_id")
        if status.get("phase", "idle") in TERMINAL or not job_id:
            return
        running = self.run("docker", "ps", "-q", "--filter",
                           "label=io.qunling.update-project=" + self.project,
                           "--filter", "label=io.qunling.update-job=" + job_id).strip()
        if not running:
            self.launch_job(job_id, recovery=True)

    def monitor_once(self):
        """Recover a killed worker even while the API container stays alive."""
        with self.lock:
            phase = read(self.state_path, {}).get("phase", "idle")
            # A live check belongs to this process. Recovery failures need an
            # operator to fix the cause; do not repeatedly overwrite data.
            if phase not in TERMINAL | {"checking", "recovery_required"}:
                self.recover_orphan()

    def compose(self, path, *args, **kwargs):
        return self.run("docker", "compose", "--project-name", self.project, "-f", str(path), *args, **kwargs)

    def resolve(self, source, app_image=None, updater_image=None):
        env = {"COMPOSE_PROJECT_NAME": self.project}
        if app_image:
            env["QQBOT_APP_IMAGE"] = app_image
        if updater_image:
            env["QQBOT_UPDATER_IMAGE"] = updater_image
        value = json.loads(self.compose(source, "config", "--format", "json", env=env))
        if set(value.get("services", {})) != set(SERVICES):
            raise UpdateError("新版部署结构需要手动迁移，当前版本不会自动替换。")
        for service in value["services"].values():
            service.pop("build", None)
            # Compose-generated host paths must never become bind mounts in the
            # app. Only the dedicated updater may access the Docker socket.
        for name in SERVICES:
            for mount in value["services"][name].get("volumes", []):
                if mount["type"] != "volume" and not (
                    name == "updater" and mount.get("source") == "/var/run/docker.sock"
                    and mount.get("target") == "/var/run/docker.sock"
                ):
                    raise UpdateError("新版包含不支持的主机挂载，需要手动升级。")
        return value

    def capture_current(self, job_dir, source=None):
        source = source or (self.active_path if self.active_path.exists() else Path("/opt/qqbot/deploy/compose/compose.yaml"))
        value = self.resolve(source)
        volumes = {}
        for service in SERVICES:
            info = self.inspect_service(service)
            value["services"][service]["image"] = info["Image"]
            # Capture exact mounted volume names instead of reconstructing paths
            # from an unrelated checkout or assuming the default project name.
            actual = {m["Destination"]: m for m in info["Mounts"]}
            for mount in value["services"][service].get("volumes", []):
                if mount["type"] == "volume":
                    observed = actual.get(mount["target"], {})
                    if observed.get("Type") != "volume":
                        raise UpdateError("运行中的数据卷与部署配置不一致。")
                    logical = mount["source"]
                    if logical in volumes and volumes[logical] != observed["Name"]:
                        raise UpdateError("多个服务的数据卷配置不一致。")
                    volumes[logical] = observed["Name"]
        if not set(DATA_VOLUMES).issubset(volumes) or "update-data" not in volumes:
            raise UpdateError("数据卷不完整，已取消更新。")
        for logical, actual in volumes.items():
            metadata = json.loads(self.run("docker", "volume", "inspect", actual))[0]
            labels = metadata.get("Labels") or {}
            if labels.get("com.docker.compose.project") != self.project:
                raise UpdateError("数据卷不属于当前项目，已取消更新。")
            value["volumes"][logical] = {"name": actual, "external": True}
        path = job_dir / "old-compose.json"
        write(path, value)
        return value, volumes

    def checkout(self, revision, directory):
        if self.remote_revision() != revision:
            raise UpdateError("GitHub 分支已有新的提交，请重新检查后更新。")
        self.run("git", "init", str(directory))
        self.run("git", "-C", str(directory), "remote", "add", "origin", self.remote)
        self.run("git", "-C", str(directory), "fetch", "--depth=1", "origin", "refs/heads/" + self.branch,
                 timeout=300)
        actual = self.run("git", "-C", str(directory), "rev-parse", "FETCH_HEAD").strip()
        if actual != revision:
            raise UpdateError("下载期间分支已变化，请重新检查更新。")
        self.run("git", "-C", str(directory), "checkout", "--detach", revision)
        if read(directory / "deploy/updater/protocol.json", {}).get("version") != 1:
            raise UpdateError("该版本不支持当前自动更新协议，请使用启动脚本手动升级。")

    def backup(self, volume, backup, image, update_volume, job_id, restore=False):
        # The helper mounts named volumes only; paths inside this updater are
        # never passed as daemon-host paths (important on Docker Desktop).
        operation = "restore" if restore else "backup"
        self.run("docker", "run", "--rm", "--network", "none",
                 "--mount", "type=volume,src=" + volume + ",dst=/data" + ("" if restore else ",readonly"),
                 "--mount", "type=volume,src=" + update_volume + ",dst=/updates",
                 image, "volume", operation, "/updates/jobs/" + job_id + "/" + backup,
                 timeout=1800, log=self.root / "jobs" / job_id / "private.log")

    def stop(self, path):
        self.compose(path, "stop", "--timeout", "60", "wizard", "app", "napcat", timeout=240)

    def start_compose(self, path, log):
        self.compose(path, "up", "-d", "--no-build", "--wait", "--wait-timeout", "600",
                     *SERVICES, timeout=900, log=log)

    def wait_ready(self, timeout=900):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = self.wizard_status()
                if status.get("stage") == "error":
                    raise UpdateError("新版插件或配置初始化失败。")
                if status.get("plugins_ready") and status.get("configured"):
                    # QQ may require a new-device confirmation; successful setup
                    # recovery is distinct from QQ account login availability.
                    return
            except UpdateError:
                raise
            except Exception:
                pass
            time.sleep(5)
        raise UpdateError("等待新版服务恢复超时。")

    def execute(self, job_id):
        if not re.fullmatch(r"[0-9a-f]{24}", job_id):
            raise UpdateError("无效的更新任务。")
        directory = self.root / "jobs" / job_id
        job = read(directory / "job.json")
        if not job or not SHA.fullmatch(job.get("revision", "")):
            raise UpdateError("更新任务记录无效。")
        if job.get("finished"):
            if job.get("terminal_phase"):
                self.publish(phase=job["terminal_phase"], current_revision=job.get("terminal_revision", ""),
                             message=job.get("terminal_message", "更新任务已结束。"))
            return
        log = directory / "private.log"
        old_path, new_path = directory / "old-compose.json", directory / "new-compose.json"

        def phase(value, message, **extra):
            job.update(phase=value, **extra)
            write(directory / "job.json", job)
            self.publish(phase=value, message=message)

        try:
            if job.get("phase") != "queued":
                raise UpdateError("更新任务被中断，正在恢复上次部署。")
            phase("building", "正在下载并构建新版本，机器人暂时继续运行。")
            source = directory / "source"
            self.checkout(job["revision"], source)
            old, volumes = self.capture_current(directory)
            worker_image = old["services"]["updater"]["image"]
            job.update(volumes=volumes, worker_image=worker_image,
                       old_revision=self.current_revision())
            write(directory / "job.json", job)
            app_image = self.project + "-app:" + job["revision"]
            updater_image = self.project + "-updater:" + job["revision"]
            for dockerfile, image in (("deploy/compose/Dockerfile", app_image),
                                      ("deploy/updater/Dockerfile", updater_image)):
                self.run("docker", "build", "--label",
                         "org.opencontainers.image.revision=" + job["revision"],
                         "--label", "io.qunling.update-project=" + self.project,
                         "--build-arg", "QQBOT_REVISION=" + job["revision"],
                         "-t", image, "-f", str(source / dockerfile), str(source),
                         timeout=7200, log=log)
            new = self.resolve(source / "deploy/compose/compose.yaml", app_image, updater_image)
            if set(new["volumes"]) != set(old["volumes"]):
                raise UpdateError("新版更改了数据卷结构，需要手动迁移。")
            for service in SERVICES:
                def volume_layout(config):
                    return {(m["source"], m["target"], bool(m.get("read_only")))
                            for m in config["services"][service].get("volumes", [])
                            if m["type"] == "volume"}
                if volume_layout(new) != volume_layout(old):
                    raise UpdateError("新版更改了服务的数据卷挂载，需要手动迁移。")
            if new.get("networks") != old.get("networks"):
                raise UpdateError("新版更改了容器网络，需要手动迁移。")
            for name, actual in volumes.items():
                new["volumes"][name] = {"name": actual, "external": True}
            # Preserve configured update source and project, even when the
            # repository's default values differ in a future version.
            new["services"]["updater"].setdefault("environment", {}).update(
                QQBOT_PROJECT=self.project, QQBOT_UPDATE_REPOSITORY=self.repository,
                QQBOT_UPDATE_BRANCH=self.branch,
                QQBOT_UPDATE_KEEP_BACKUPS=str(self.keep_backups) if self.retention_valid else "disabled")
            write(new_path, new)
            # Pull a changed QQ image before interrupting the running deployment.
            self.run("docker", "image", "inspect", new["services"]["napcat"]["image"]) \
                if new["services"]["napcat"]["image"] == old["services"]["napcat"]["image"] \
                else self.run("docker", "pull", new["services"]["napcat"]["image"], timeout=1200, log=log)
            phase("backing_up", "正在暂停机器人并备份 QQ、配置和记忆。", services_stopped=True)
            self.stop(old_path)
            for name in DATA_VOLUMES:
                self.backup(volumes[name], name + ".tar", worker_image, volumes["update-data"], job_id)
            phase("restarting", "备份完成，正在切换到新版本。", backup_complete=True, mutation_started=True)
            self.start_compose(new_path, log)
            phase("verifying", "正在检查新版插件和配置恢复。")
            self.wait_ready()
            write(self.active_path, new)
            job.update(finished=True, terminal_phase="completed", terminal_revision=job["revision"], completed_at=now(),
                       terminal_message="更新完成。QQ 如需重新确认登录，请在上方扫码。")
            write(directory / "job.json", job)
            self.publish(phase="completed", current_revision=job["revision"],
                         message="更新完成。QQ 如需重新确认登录，请在上方扫码。",
                         latest_revision=job["revision"], completed_at=now())
        except Exception as exc:
            try:
                if job.get("services_stopped"):
                    phase("rolling_back", "更新未完成，正在恢复旧版本和原有数据。")
                    self.stop(new_path if new_path.exists() else old_path)
                    if job.get("mutation_started"):
                        if not job.get("backup_complete"):
                            raise UpdateError("没有完整备份，停止自动恢复以保护现有数据。")
                        for name in DATA_VOLUMES:
                            self.run("docker", "run", "--rm", "--network", "none",
                                     "--mount", "type=volume,src=" + job["volumes"][name] + ",dst=/data,readonly",
                                     "--mount", "type=volume,src=" + job["volumes"]["update-data"] + ",dst=/updates",
                                     job["worker_image"], "volume", "verify",
                                     "/updates/jobs/" + job_id + "/" + name + ".tar",
                                     timeout=1800, log=log)
                        for name in DATA_VOLUMES:
                            self.backup(job["volumes"][name], name + ".tar", job["worker_image"],
                                        job["volumes"]["update-data"], job_id, restore=True)
                    self.start_compose(old_path, log)
                    self.wait_ready()
                    write(self.active_path, read(old_path))
                    job.update(finished=True, terminal_phase="rolled_back",
                               terminal_revision=job.get("old_revision", ""),
                               terminal_message="更新失败，已恢复旧版本和原有数据。可检查日志后重试。")
                    write(directory / "job.json", job)
                    self.publish(phase="rolled_back", current_revision=job.get("old_revision", ""),
                                 message="更新失败，已恢复旧版本和原有数据。可检查日志后重试。")
                else:
                    job.update(finished=True, terminal_phase="failed",
                               terminal_revision=job.get("old_revision", ""),
                               terminal_message=str(exc) if isinstance(exc, UpdateError)
                               else "更新准备失败，原服务和数据未改动。")
                    write(directory / "job.json", job)
                    self.publish(phase="failed", message=str(exc) if isinstance(exc, UpdateError)
                                 else "更新准备失败，原服务和数据未改动。")
            except Exception:
                self.publish(phase="recovery_required",
                             message="自动恢复未完成，备份已保留。请按运维文档恢复，勿删除数据卷。")
