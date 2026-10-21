"""Private update API. Only the wizard can proxy authenticated operator actions."""
from __future__ import annotations

import argparse
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import time

from deploy.updater.engine import UpdateEngine, UpdateError, TERMINAL, read


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, engine):
        super().__init__(address, Handler)
        self.engine = engine


class Handler(BaseHTTPRequestHandler):
    server_version = "QunLingUpdate/1"

    def log_message(self, *args):
        pass

    def send(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        token = self.server.engine.setup_token()
        return bool(token) and secrets.compare_digest(
            self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode())

    def do_GET(self):
        if self.path == "/healthz":
            return self.send(200, {"status": "ok"})
        if not self.authorized():
            return self.send(401, {"error": "未授权。"})
        if self.path != "/status":
            return self.send(404, {"error": "Not found"})
        return self.send(200, self.server.engine.snapshot())

    def do_POST(self):
        if not self.authorized():
            return self.send(401, {"error": "未授权。"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 2048 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send(400, {"error": "需要有效的 JSON 请求。"})
            self.connection.settimeout(10)
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError()
            engine = self.server.engine
            if self.path == "/check" and not body:
                # Reserve checking synchronously: concurrent requests cannot
                # both be accepted before the background thread starts.
                if not engine.lock.acquire(blocking=False):
                    raise UpdateError("已有更新操作正在进行。")
                try:
                    if engine.snapshot()["busy"]:
                        raise UpdateError("已有更新操作正在进行。")
                    engine.publish(phase="checking", message="正在检查 GitHub 更新。")
                finally:
                    engine.lock.release()

                def check():
                    try:
                        latest, current = engine.remote_revision(), engine.current_revision()
                        from deploy.updater.engine import now
                        engine.publish(phase="available" if latest != current else "idle",
                                       current_revision=current, latest_revision=latest, checked_at=now(),
                                       message="发现新版本，可点击更新。" if latest != current else "当前已是最新版本。")
                    except Exception:
                        engine.publish(phase="failed", message="检查更新失败，当前服务未受影响，请稍后重试。")
                threading.Thread(target=check, daemon=True).start()
            elif self.path == "/apply" and set(body) == {"revision"}:
                engine.start(body["revision"])
            else:
                return self.send(400, {"error": "不支持的更新操作或参数。"})
            return self.send(202, engine.snapshot())
        except (ValueError, UnicodeError):
            return self.send(400, {"error": "请求格式无效。"})
        except UpdateError as exc:
            return self.send(409, {"error": str(exc)})
        except Exception:
            return self.send(503, {"error": "更新服务暂不可用，请查看状态后重试。"})


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["serve", "worker", "volume", "resume", "guard", "adopt"], nargs="?", default="serve")
    parser.add_argument("args", nargs="*")
    options = parser.parse_args()
    if options.role == "volume":
        from deploy.updater.volume import backup, restore, verify
        if len(options.args) != 2 or options.args[0] not in {"backup", "restore", "verify"}:
            raise SystemExit("Invalid volume operation")
        operation, destination = options.args
        resolved = Path(destination).resolve()
        if not resolved.is_relative_to(Path("/updates/jobs")) or not Path("/data").is_mount():
            raise SystemExit("Invalid backup or data mount")
        if operation == "verify":
            verify(resolved)
        else:
            (backup if operation == "backup" else restore)("/data", resolved)
        return
    engine = UpdateEngine()
    if options.role == "guard":
        if engine.snapshot()["busy"]:
            raise SystemExit("Update in progress. Wait for completion before stopping or rebuilding.")
        return
    if options.role == "adopt":
        from deploy.updater.engine import write
        if engine.snapshot()["busy"]:
            raise SystemExit("Update in progress; cannot replace the active deployment.")
        value, _ = engine.capture_current(engine.root / "manual", Path("/opt/qqbot/deploy/compose/compose.yaml"))
        write(engine.active_path, value)
        engine.publish(phase="idle", current_revision=engine.current_revision(), latest_revision="",
                       message="当前部署已记录，可以检查 GitHub 更新。")
        return
    # One worker for a deployment across container restarts. A duplicate worker
    # exits without interpreting the still-running job as an interrupted update.
    if options.role == "worker":
        if len(options.args) != 1:
            raise SystemExit("Missing job")
        with (engine.root / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            engine.execute(options.args[0])
        return
    if options.role == "resume":
        # Used by host launchers after `down`: start the exact active deployment
        # rather than rebuilding an older source checkout over a web update.
        if not engine.active_path.exists():
            raise SystemExit(3)
        status = read(engine.state_path, {})
        if status.get("phase", "idle") not in TERMINAL:
            raise SystemExit("An interrupted update needs recovery; inspect the update service.")
        engine.start_compose(engine.active_path, engine.root / "resume.log")
        return
    # Serialize recovery/startup across old and new API containers.
    with (engine.root / "api.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        engine.recover_orphan()
        server = Server(("0.0.0.0", 5320), engine)
        stopping = threading.Event()

        def monitor():
            while not stopping.wait(15):
                try:
                    engine.monitor_once()
                    engine.cleanup_history()
                except Exception:
                    # A Docker outage is retryable. Keep the existing busy
                    # record rather than declaring a transaction successful.
                    time.sleep(1)
        threading.Thread(target=monitor, daemon=True, name="update-recovery").start()
        try:
            server.serve_forever()
        finally:
            stopping.set()
            server.server_close()


if __name__ == "__main__":
    main()
