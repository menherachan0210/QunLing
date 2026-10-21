"""Private, bounded proxy for the deployment-owned update service."""

from __future__ import annotations

from datetime import datetime
import re
from urllib.parse import urlsplit

import requests

from bootstrap.api import SetupError

REVISION = re.compile(r"[0-9a-fA-F]{40}\Z")
CLEANUP_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z"
)
CLEANUP_STATUSES = {"completed", "partial", "failed", "skipped"}
CLEANUP_WARNINGS = frozenset({
    "invalid_retention_setting_cleanup_disabled",
    "unmanaged_or_unresolved_history_preserved",
    "worker_removal_failed",
    "backup_removal_failed",
    "image_cleanup_skipped_incomplete_history",
    "image_removal_failed",
    "cleanup_inventory_failed",
})


def _cleanup_summary(value):
    """Expose a bounded maintenance summary, never private inventory or logs."""
    if not isinstance(value, dict):
        return None
    checked_at, status = value.get("checked_at"), value.get("status")
    if (not isinstance(checked_at, str) or not CLEANUP_TIME.fullmatch(checked_at)
            or not isinstance(status, str) or status not in CLEANUP_STATUSES):
        return None
    try:
        datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    result = {"checked_at": checked_at, "status": status}
    limits = {
        "keep_backups": (1, 10),
        # The browser must be able to display byte counts without rounding.
        "reclaimed_backup_bytes": (0, 2**53 - 1),
        "removed_jobs": (0, 1_000_000),
        "removed_images": (0, 1_000_000),
        "removed_workers": (0, 1_000_000),
    }
    for key, (minimum, maximum) in limits.items():
        number = value.get(key)
        if type(number) is int and minimum <= number <= maximum:
            result[key] = number
    warnings = value.get("warnings")
    if isinstance(warnings, list):
        # Do not scan an arbitrarily large list or echo unknown error text.
        result["warnings"] = list(dict.fromkeys(
            warning for warning in warnings[:100]
            if isinstance(warning, str) and warning in CLEANUP_WARNINGS
        ))
    return result


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
        cleanup = _cleanup_summary(value.get("cleanup"))
        if cleanup is not None:
            result["cleanup"] = cleanup
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
