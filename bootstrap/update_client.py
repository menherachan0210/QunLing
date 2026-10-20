"""Private, bounded proxy for the deployment-owned update service."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import requests

from bootstrap.api import SetupError

REVISION = re.compile(r"[0-9a-fA-F]{40}\Z")


class UpdateError(SetupError):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class UpdateClient:
    def __init__(self, url, token):
        self.url = (url or "").rstrip("/")
        if self.url:
            parsed = urlsplit(self.url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise SetupError("更新服务地址配置不正确。")
        self.http = requests.Session()
        self.http.trust_env = False
        self.http.headers["Authorization"] = "Bearer " + token

    @staticmethod
    def unavailable(supported=True):
        return {
            "available": False,
            "current_revision": "",
            "latest_revision": "",
            "update_available": False,
            "phase": "unavailable" if supported else "unsupported",
            "message": (
                "更新服务暂时无法连接，请稍后重试。"
                if supported
                else "此部署尚未启用在线更新，请按部署文档升级。"
            ),
            "checked_at": "",
            "repository": "",
            "branch": "",
            "busy": False,
        }

    @staticmethod
    def _status(value):
        if not isinstance(value, dict):
            raise ValueError("Expected update status")
        for key in ("available", "busy", "update_available"):
            if not isinstance(value.get(key), bool):
                raise ValueError("Invalid update status flags")
        result = {
            key: value[key] for key in ("available", "busy", "update_available")
        }
        for key in ("current_revision", "latest_revision"):
            revision = value.get(key, "")
            if not isinstance(revision, str) or (revision and not REVISION.fullmatch(revision)):
                raise ValueError("Invalid revision")
            result[key] = revision.lower()
        for key in ("phase", "message", "checked_at", "repository", "branch"):
            text = value.get(key, "")
            # Never pass through deployment paths, tokens, command logs or other
            # unexpected response fields.
            if not isinstance(text, str):
                raise ValueError("Invalid update description")
            result[key] = text[:1000 if key == "message" else 300]
        if isinstance(value.get("error"), str):
            result["error"] = value["error"][:1000]
        return result

    def _request(self, method, path, body=None):
        if not self.url:
            raise UpdateError("此部署尚未启用在线更新。")
        try:
            response = self.http.request(
                method,
                self.url + path,
                json=body,
                timeout=(3, 35 if path == "/check" else 8),
                allow_redirects=False,
            )
            if not 200 <= response.status_code < 300:
                # A rejected mutation is safe to release; a server error may
                # happen after the worker has already accepted the update.
                raise UpdateError(
                    "更新服务未接受操作，请刷新状态后重试。",
                    uncertain=method == "POST" and response.status_code >= 500,
                )
            return self._status(response.json())
        except requests.RequestException:
            raise UpdateError(
                "暂时无法确认更新服务的状态，请等待重新连接。",
                uncertain=method == "POST",
            ) from None
        except (ValueError, TypeError):
            raise UpdateError(
                "更新服务返回了无法识别的状态，请等待重新连接。",
                uncertain=method == "POST",
            ) from None

    def status(self):
        if not self.url:
            return self.unavailable(False)
        try:
            return self._request("GET", "/status")
        except UpdateError:
            return self.unavailable()

    def check(self):
        return self._request("POST", "/check", {})

    def apply(self, revision):
        if not isinstance(revision, str) or not REVISION.fullmatch(revision):
            raise UpdateError("更新版本不正确，请先检查更新。")
        return self._request("POST", "/apply", {"revision": revision.lower()})
