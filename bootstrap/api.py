"""Bounded management clients. Endpoints are deployment-owned, never model inputs."""

from __future__ import annotations

import hashlib
import secrets
import time
import threading

import requests


class SetupError(RuntimeError):
    pass


class ProactiveAPIError(SetupError):
    def __init__(self, message, status):
        self.status = status
        super().__init__(message)


class LangBotAPI:
    def __init__(self, url, state):
        self.url = url.rstrip("/")
        self.state = state
        self.http = requests.Session()
        self.http.trust_env = False

    def runtime_health(self):
        """Read the cached probe without waiting on plugin RPC or Workspace locks."""
        try:
            response = self.http.get(self.url + "/healthz", timeout=(2, 3))
            response.raise_for_status()
            value = response.json().get("plugin_runtime", {})
            if (isinstance(value, dict) and value.get("healthy") is True
                    and value.get("state") == "ready"):
                return "ready"
            return "recovering" if isinstance(value, dict) and value.get("state") == "recovering" else "unknown"
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            return "unknown"

    def call(self, method, path, data=None, _refresh=True):
        if not path.startswith("/api/v1/"):
            raise SetupError("无效的管理接口。")
        try:
            response = self.http.request(method, self.url + path, json=data, timeout=45)
            if response.status_code == 401 and _refresh and "/user/" not in path:
                self.login()
                return self.call(method, path, data, _refresh=False)
            if "/proactive" in path and not response.ok:
                # Only fixed public errors cross this management boundary;
                # never echo provider responses or private adapter details.
                try:
                    reason = response.json().get("error_code")
                except (ValueError, AttributeError):
                    reason = None
                messages = {
                    "revision_conflict": (409, "设置已在其他页面修改，请重新载入后再保存。"),
                    "group_not_joined": (400, "只能为当前 QQ 已加入的群开启自由对话。"),
                    "groups_unavailable": (503, "群列表暂不可用，请确认 QQ 已连接后重试。"),
                    "invalid_config": (400, "自由对话配置无效，请检查群模式、人设和安静时段。"),
                }
                status, message = messages.get(reason, (503, "自由对话管理暂不可用，请稍后重试。"))
                raise ProactiveAPIError(message, status)
            response.raise_for_status()
            body = response.json()
            if body.get("code") != 0:
                raise SetupError(f"LangBot 未接受配置：{method} {path}")
            return body.get("data") or {}
        except requests.RequestException as exc:
            status = getattr(exc.response, "status_code", None)
            raise SetupError(
                f"LangBot 接口暂不可用（HTTP {status or '连接失败'}）。"
            ) from None
        except (ValueError, TypeError):
            raise SetupError("LangBot 返回了无法识别的结果。") from None

    def login(self):
        initialized = self.call("GET", "/api/v1/user/init")["initialized"]
        account = self.state.read("langbot-admin.json")
        if account is None:
            if initialized:
                raise SetupError(
                    "已有 LangBot 账号但缺少向导凭据，请恢复原部署的设置数据卷。"
                )
            account = {
                "user": "admin@local.invalid",
                "password": secrets.token_urlsafe(28),
            }
            self.state.write(account, "langbot-admin.json")
        if not initialized:
            self.call("POST", "/api/v1/user/init", account)
        result = self.call("POST", "/api/v1/user/auth", account)
        self.http.headers["Authorization"] = "Bearer " + result["token"]

    def install(self, path):
        try:
            with path.open("rb") as stream:
                response = self.http.post(
                    self.url + "/api/v1/plugins/install/local",
                    files={"file": (path.name, stream, "application/octet-stream")},
                    timeout=60,
                )
            response.raise_for_status()
            body = response.json()
            if body.get("code") != 0:
                raise SetupError("插件安装请求失败。")
            return body["data"]["task_id"]
        except (requests.RequestException, ValueError, KeyError):
            raise SetupError(f"无法提交插件安装：{path.name}") from None


class NapCatAPI:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token
        self.http = requests.Session()
        self.http.trust_env = False
        self.authenticated_at = 0.0
        self.lock = threading.RLock()

    def _post(self, path, data=None):
        try:
            response = self.http.post(
                self.url + "/api" + path, json=data or {}, timeout=12
            )
            response.raise_for_status()
            body = response.json()
            if body.get("code") != 0:
                raise SetupError("NapCat 尚未准备好，请稍后重试。")
            return body.get("data") or {}
        except (requests.RequestException, ValueError, TypeError):
            raise SetupError("暂时无法连接 NapCat，请检查容器状态。") from None

    def login(self):
        if self.authenticated_at and time.monotonic() - self.authenticated_at < 3000:
            return
        digest = hashlib.sha256((self.token + ".napcat").encode()).hexdigest()
        result = self._post("/auth/login", {"hash": digest})
        credential = result.get("Credential")
        if not credential:
            raise SetupError("NapCat 管理认证失败，请核对设置数据卷与 QQ 配置卷。")
        self.http.headers["Authorization"] = "Bearer " + credential
        self.authenticated_at = time.monotonic()

    def call(self, path, data=None):
        with self.lock:
            self.login()
            try:
                return self._post(path, data)
            except SetupError:
                # Invalidate on restart/expiry. Retry on the next poll, never
                # replay a configuration mutation with an ambiguous outcome.
                self.authenticated_at = 0.0
                raise
