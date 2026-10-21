"""Local onboarding page and background provisioning for the public distribution."""

from __future__ import annotations

import argparse
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from bootstrap.api import LangBotAPI, NapCatAPI, SetupError
from bootstrap import proactive
from bootstrap.configure import (
    configure,
    ensure_plugins,
    link_napcat,
    public_settings,
    settings_from_input,
)
from bootstrap.state import State
from bootstrap.update_client import REVISION, UpdateClient, UpdateError

ROOT = Path(__file__).resolve().parents[1]
QQ_STATUS_TTL = 30


class SetupManager:
    def __init__(self, state, langbot_url, napcat_url, packages, archive):
        self.state = state
        self.secrets = state.read("secrets.json")
        if not self.secrets:
            raise SetupError("请先运行部署初始化服务。")
        self.api = LangBotAPI(langbot_url, state)
        self.napcat = NapCatAPI(napcat_url, self.secrets["napcat_token"])
        self.langbot_url = langbot_url
        self.packages, self.archive = Path(packages), archive
        self.lock = threading.RLock()
        self.updater = UpdateClient(
            os.environ.get("QQBOT_UPDATER_URL", ""), self.secrets["setup_token"]
        )
        self.update_pending = False
        self.update_inflight = False
        self.pending = None
        self.stopping = threading.Event()
        self.qq_sampled_at = None
        self.status = {
            "stage": "starting",
            "message": "正在启动服务，首次安装依赖可能需要数分钟。",
            "plugins_ready": False,
            "configured": False,
            "qq_logged_in": False,
            "qq_connected": False,
            "qq_state": "unknown",
            "qq_checked_at": None,
            "qq_status_fresh": False,
            "qr_image": "",
            "settings": {},
        }

    def update(self, **values):
        with self.lock:
            self.status.update(values)

    def snapshot(self):
        with self.lock:
            result = dict(self.status)
            fresh = (self.qq_sampled_at is not None
                     and 0 <= time.monotonic() - self.qq_sampled_at <= QQ_STATUS_TTL)
            result["qq_status_fresh"] = fresh
            if not fresh:
                result.update(qq_state="unknown", qq_logged_in=False,
                              qq_connected=False, qr_image="")
                if result["stage"] == "ready":
                    result.update(stage="waiting_qq", message="QQ 状态检查已过期，正在重新确认连接。")
            return result

    def _record_qq(self, state, logged_in=False, connected=False, image="", *, sampled_at=None):
        messages = {
            "online": "机器人已连接。群聊 @ 机器人或输入 /ai，私聊可直接发送。",
            "offline": "QQ 已离线，请检查 QQ 登录状态并重新登录。",
            "disconnected": "QQ 消息通道已断开，正在等待重新连接。",
            "unknown": "暂时无法确认 QQ 在线状态，正在自动重试。",
            "connecting": "QQ 登录已确认，正在等待消息通道连接。",
            "waiting_login": "等待 QQ 登录并连接；二维码失效时可以刷新。",
        }
        with self.lock:
            self.qq_sampled_at = time.monotonic() if sampled_at is None else sampled_at
            self.status.update(qq_state=state, qq_logged_in=logged_in,
                               qq_connected=connected, qr_image=image,
                               qq_checked_at=time.time(), qq_status_fresh=True)
            if self.status["stage"] in {"ready", "waiting_qq"}:
                self.status.update(
                    stage="ready" if connected and self.status["configured"] else "waiting_qq",
                    message=messages[state],
                )

    def submit(self, values):
        config = settings_from_input(values, self.state.read("ai.json", {}))
        with self.lock:
            if self.update_inflight or self.update_status()["busy"]:
                raise SetupError("版本更新正在进行，完成后再保存 AI 配置。")
            if (
                not self.status["plugins_ready"]
                or self.status["stage"] == "configuring"
            ):
                raise SetupError("初始化或配置仍在进行，请稍候。")
            self.pending = config
            self.status.update(
                stage="configuring", message="正在统一配置模型、工具和记忆。"
            )

    def proactive_settings(self):
        return proactive.settings(self)

    def proactive_groups(self):
        return proactive.groups(self)

    def save_proactive(self, values):
        return proactive.save(self, values)

    def update_status(self):
        with self.lock:
            value = self.updater.status()
            if self.update_inflight:
                value.update(busy=True, message="正在提交更新任务。")
                return value
            if value["available"]:
                self.update_pending = value["busy"]
            elif self.update_pending:
                value.update(
                    busy=True,
                    message="更新状态暂不可用，正在等待服务恢复；尚未确认更新完成。",
                )
            return value

    def check_update(self, values):
        if values != {}:
            raise SetupError("检查更新不接受额外参数。")
        with self.lock:
            if self.update_status()["busy"]:
                raise SetupError("更新正在进行，请等待完成。")
            return self.updater.check()

    def apply_update(self, values):
        if (
            not isinstance(values, dict)
            or set(values) != {"revision"}
            or not isinstance(values["revision"], str)
            or not REVISION.fullmatch(values["revision"])
        ):
            raise SetupError("请先检查更新，再选择页面显示的版本。")
        with self.lock:
            if self.status["stage"] in {"starting", "configuring"} or self.pending:
                raise SetupError("初始化或配置正在进行，完成后再更新。")
            current = self.update_status()
            if current["busy"]:
                raise SetupError("更新正在进行，请等待完成。")
            if not current["available"]:
                raise SetupError(current["message"])
            if (
                not current["update_available"]
                or current["latest_revision"] != values["revision"].lower()
            ):
                raise SetupError("可更新版本已变化，请重新检查更新。")
            self.update_pending = True
            self.update_inflight = True
        # The updater rechecks this wizard's readiness before accepting a job.
        # Do not hold the snapshot lock across that callback round trip.
        try:
            value = self.updater.apply(values["revision"])
        except UpdateError as exc:
            with self.lock:
                self.update_pending = exc.uncertain
                self.update_inflight = False
            raise
        except Exception:
            with self.lock:
                self.update_inflight = False
                self.update_pending = True
            raise
        with self.lock:
            self.update_pending = value["busy"]
            self.update_inflight = False
            return value

    def refresh_qr(self):
        # Recheck the login service, not a possibly stale dashboard snapshot.
        # Network I/O must not hold the snapshot lock and block its TTL check.
        status = self.napcat.call("/QQLogin/CheckLoginStatus")
        if (isinstance(status, dict) and status.get("isLogin") is True
                and status.get("isOffline") is not True):
            return
        self.napcat.call("/QQLogin/RefreshQRcode")
        self._record_qq("waiting_login")

    def _apply(self, config):
        record = self.state.read(default={})
        result = configure(
            self.api,
            config,
            record,
            self.state.write,
            self.secrets["onebot_token"],
            self.langbot_url,
            self.archive,
        )
        self.state.write(config, "ai.json")
        self.update(
            configured=True,
            settings=public_settings(config),
            stage="waiting_qq",
            message="AI 和全部插件已配置，请用 QQ 扫码登录。",
        )
        return result

    def _qq_status(self, record):
        import qrcode

        sampled_at = time.monotonic()
        status = self.napcat.call("/QQLogin/CheckLoginStatus")
        if not isinstance(status, dict) or not isinstance(status.get("isLogin"), bool):
            raise SetupError("QQ 登录状态无效。")
        offline = status.get("isOffline") is True
        logged_in = status["isLogin"] and not offline
        state = "offline" if offline else "connecting" if logged_in else "waiting_login"
        if status.get("qrLoginAccepted") is True and not offline:
            state = "connecting"
        image = ""
        if (
            state == "waiting_login"
            and isinstance(status.get("qrcodeurl"), str)
            and len(status["qrcodeurl"]) <= 4096
        ):
            if status["qrcodeurl"]:
                buffer = io.BytesIO()
                qrcode.make(status["qrcodeurl"]).save(buffer, format="PNG")
                image = (
                    "data:image/png;base64,"
                    + base64.b64encode(buffer.getvalue()).decode()
                )
        connected = False
        if logged_in and record.get("configured"):
            link_napcat(self.napcat, record["onebot_url"], self.secrets["onebot_token"])
            bot = self.api.call("GET", "/api/v1/platform/bots/" + record["bot_uuid"])[
                "bot"
            ]
            probe = bot.get("adapter_runtime_values", {}).get("qq_status", {})
            checked = probe.get("checked_at") if isinstance(probe, dict) else None
            # Account IDs survive disconnects. Only a fresh, successful OneBot
            # round trip proves the transport and QQ account are both online.
            if (not isinstance(checked, (int, float)) or isinstance(checked, bool)
                    or not -5 <= time.time() - checked <= QQ_STATUS_TTL):
                state, logged_in = "unknown", False
            elif probe.get("state") == "online" and probe.get("online") is True and probe.get("transport_connected") is True:
                state, connected = "online", True
            elif probe.get("state") == "offline" and probe.get("online") is False:
                state, logged_in = "offline", False
            elif probe.get("state") == "disconnected" and probe.get("transport_connected") is False:
                state = "disconnected"
            else:
                state, logged_in = "unknown", False
        self._record_qq(state, logged_in, connected, image, sampled_at=sampled_at)

    def run(self):
        # Retrying readiness is safe; API/model requests that cost money are not
        # made by this wizard.
        while not self.stopping.is_set():
            try:
                self.api.login()
                self.api.call("GET", "/api/v1/plugins")
                break
            except SetupError as exc:
                self.update(message=str(exc))
                self.stopping.wait(5)
        if self.stopping.is_set():
            return
        try:
            ensure_plugins(
                self.api, self.packages, lambda text: self.update(message=text)
            )
            self.update(
                plugins_ready=True,
                stage="waiting_ai",
                message="插件已安装，填写 AI 配置即可启用。",
            )
        except Exception as exc:
            self.update(
                stage="error",
                message=str(exc)
                if isinstance(exc, SetupError)
                else "初始化失败，请查看部署日志并重启设置服务。",
            )
            return
        saved = self.state.read("ai.json")
        if saved:
            # A recoverable API failure on restart must not disable the worker
            # and make all later submissions wait forever.
            try:
                self.update(stage="configuring", settings=public_settings(saved))
                self._apply(saved)
            except Exception as exc:
                self.update(
                    stage="error",
                    message=str(exc)
                    if isinstance(exc, SetupError)
                    else "恢复配置未完成，请重新保存配置重试。",
                )
        while not self.stopping.is_set():
            with self.lock:
                pending, self.pending = self.pending, None
            if pending is not None:
                try:
                    self._apply(pending)
                except Exception as exc:
                    self.update(
                        stage="error",
                        message=str(exc)
                        if isinstance(exc, SetupError)
                        else "配置未完成，已保留进度；检查服务后重新保存即可继续。",
                    )
            try:
                self._qq_status(self.state.read(default={}))
            except Exception:
                self._record_qq("unknown")
            self.stopping.wait(4)


class SetupHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, manager):
        super().__init__(address, Handler)
        self.manager = manager


class Handler(BaseHTTPRequestHandler):
    server_version = "QQBotSetup/1"

    def log_message(self, format, *args):
        # URLs may contain a user-pasted token; do not log request paths.
        pass

    def _trusted_host(self):
        try:
            host = urlsplit("//" + self.headers.get("Host", "")).hostname
            allowed = {"localhost", "127.0.0.1", "::1"}
            allowed.update(
                filter(None, os.environ.get("QQBOT_ALLOWED_HOSTS", "").split(","))
            )
            return host in allowed
        except ValueError:
            return False

    def _authorized(self):
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.manager.secrets["setup_token"]
        return secrets.compare_digest(supplied.encode(), expected.encode())

    def _reject_post(self, status, value):
        # Closing a socket with an unread POST body can replace the HTTP error
        # with a TCP reset on Windows. Consume only a small, declared body with
        # a total deadline; authorization has already failed and no data is
        # parsed or passed to the manager.
        previous_timeout = self.connection.gettimeout()
        try:
            lengths = self.headers.get_all("Content-Length", [])
            size = int(lengths[0]) if len(lengths) == 1 else 0
            if 0 < size <= 64 * 1024 and not self.headers.get("Transfer-Encoding"):
                deadline = time.monotonic() + .25
                while size:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.connection.settimeout(remaining)
                    chunk = self.rfile.read1(min(size, 16 * 1024))
                    if not chunk:
                        break
                    size -= len(chunk)
        except (OSError, ValueError):
            pass
        finally:
            self.connection.settimeout(previous_timeout)
        return self._send(status, value)

    def _send(self, status, value, content_type="application/json; charset=utf-8"):
        raw = (
            value
            if isinstance(value, bytes)
            else json.dumps(value, ensure_ascii=False).encode()
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self._trusted_host():
            return self._send(403, {"error": "请通过本机地址或 SSH 隧道访问设置页。"})
        path = urlsplit(self.path).path
        static = {
            "/": ("index.html", "text/html"),
            "/app.js": ("app.js", "text/javascript"),
            "/style.css": ("style.css", "text/css"),
        }
        if path in static:
            filename, kind = static[path]
            return self._send(
                200,
                (ROOT / "bootstrap/static" / filename).read_bytes(),
                kind + "; charset=utf-8",
            )
        if path == "/healthz":
            return self._send(200, {"status": "ok"})
        if not self._authorized():
            return self._send(
                401, {"error": "请使用启动命令显示的专属设置链接打开此页面。"}
            )
        if path == "/api/status":
            return self._send(200, self.server.manager.snapshot())
        if path == "/api/update":
            return self._send(200, self.server.manager.update_status())
        if path in {"/api/proactive", "/api/proactive/groups"}:
            try:
                method = (self.server.manager.proactive_settings if path == "/api/proactive"
                          else self.server.manager.proactive_groups)
                return self._send(200, method())
            except SetupError as exc:
                return self._send(getattr(exc, "status", 503), {"error": str(exc)})
            except Exception:
                return self._send(503, {"error": "自由对话设置暂不可用，请稍后重试。"})
        if path == "/api/admin":
            return self._send(
                200, self.server.manager.state.read("langbot-admin.json", {})
            )
        return self._send(404, {"error": "Not found"})

    def do_POST(self):
        if not self._trusted_host() or not self._authorized():
            return self._reject_post(403, {"error": "未授权。"})
        origin = self.headers.get("Origin")
        if origin != "http://" + self.headers.get(
            "Host"
        ) and origin != "https://" + self.headers.get("Host"):
            return self._reject_post(403, {"error": "来源校验失败，请从设置页操作。"})
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            return self._reject_post(415, {"error": "需要 JSON 请求。"})
        try:
            size = int(self.headers.get("Content-Length", ""))
            maximum = 256 * 1024 if self.path == "/api/proactive" else 16 * 1024
            if not 0 < size <= maximum:
                return self._reject_post(413, {"error": "请求过大。"})
            self.connection.settimeout(10)
            body = json.loads(self.rfile.read(size))
            if self.path == "/api/configure":
                self.server.manager.submit(body)
            elif self.path == "/api/proactive":
                return self._send(200, self.server.manager.save_proactive(body))
            elif self.path == "/api/qr/refresh":
                self.server.manager.refresh_qr()
            elif self.path == "/api/update/check":
                return self._send(200, self.server.manager.check_update(body))
            elif self.path == "/api/update/apply":
                return self._send(202, self.server.manager.apply_update(body))
            else:
                return self._send(404, {"error": "Not found"})
            return self._send(202, {"status": "accepted"})
        except (ValueError, UnicodeError):
            return self._send(400, {"error": "请求格式不正确。"})
        except UpdateError as exc:
            return self._send(503 if exc.uncertain else 409, {"error": str(exc)})
        except SetupError as exc:
            return self._send(getattr(exc, "status", 400), {"error": str(exc)})
        except Exception:
            return self._send(500, {"error": "设置暂未完成，请稍后再试。"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url", action="store_true", help="Print the private local setup link"
    )
    args = parser.parse_args()
    state = State(os.environ.get("QQBOT_STATE_DIR", str(ROOT / ".local/public-setup")))
    if args.url:
        values = state.read("secrets.json", {})
        if not values.get("setup_token"):
            raise SystemExit("初始化尚未完成，请稍后重试。")
        print("http://127.0.0.1:5310/#token=" + values["setup_token"])
        return
    manager = SetupManager(
        state,
        os.environ.get("QQBOT_LANGBOT_URL", "http://app:5300"),
        os.environ.get("QQBOT_NAPCAT_URL", "http://napcat:6099"),
        os.environ.get("QQBOT_PACKAGES_DIR", str(ROOT / "packages")),
        os.environ.get("QQBOT_ARCHIVE_PATH", "/usr/local/bin/7zz"),
    )
    threading.Thread(target=manager.run, daemon=True, name="qqbot-onboarding").start()
    server = SetupHTTPServer(
        (
            os.environ.get("QQBOT_BIND", "127.0.0.1"),
            int(os.environ.get("QQBOT_PORT", "5310")),
        ),
        manager,
    )
    try:
        server.serve_forever()
    finally:
        manager.stopping.set()
        server.server_close()


if __name__ == "__main__":
    main()
