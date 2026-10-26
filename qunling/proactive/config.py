"""Validated, bot-scoped administrator settings for optional group participation."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from qunling.assets import data_root
from qunling.dialogue import validate_examples

MAX_BYTES = 256 * 1024
MAX_GROUPS = 100
MAX_PERSONA_CHARACTERS = 6000
GROUP_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
REVISION = re.compile(r"(?:0|[0-9a-f]{32})\Z")
TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
DEFAULT_GROUP = {
    "mode": "off", "display_name": "群灵", "persona": "",
    "quiet_hours": {"enabled": False, "start": "23:00", "end": "08:00", "timezone": "Asia/Shanghai"},
}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class ConfigError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def defaults() -> dict:
    return {"version": 1, "revision": "0", "enabled": False, "groups": {}}


def _text(value, limit, name, *, multiline=False):
    if (not isinstance(value, str) or len(value) > limit
            or any((ord(c) < 32 and not (multiline and c in "\n\t"))
                   or 0xD800 <= ord(c) <= 0xDFFF or ord(c) == 127 for c in value)):
        raise ConfigError("invalid_config", f"{name}格式无效或超过长度限制。")
    return value.strip()


def validate(values: object) -> dict:
    if (not isinstance(values, dict) or set(values) - {"default_style", "default_examples"} != {"version", "revision", "enabled", "groups"}
            or type(values["version"]) is not int or values["version"] != 1
            or type(values["enabled"]) is not bool
            or not isinstance(values["revision"], str) or not REVISION.fullmatch(values["revision"])
            or not isinstance(values["groups"], dict) or len(values["groups"]) > MAX_GROUPS):
        raise ConfigError("invalid_config", "自由对话配置无效，最多保存 100 个群的设置。")
    result = {**values, "groups": {}}
    if "default_style" in values:
        result["default_style"] = _text(values["default_style"], 6000, "默认语气", multiline=True)
    if "default_examples" in values:
        try:
            result["default_examples"] = validate_examples(values["default_examples"])
        except ValueError as exc:
            raise ConfigError("invalid_config", str(exc)) from None
    for group_id, entry in values["groups"].items():
        if not isinstance(group_id, str) or not GROUP_ID.fullmatch(group_id):
            raise ConfigError("invalid_config", "群号格式无效。")
        if (not isinstance(entry, dict) or set(entry) - {"examples"} != set(DEFAULT_GROUP)
                or not isinstance(entry.get("mode"), str)
                or entry.get("mode") not in {"off", "quiet", "natural", "active"}):
            raise ConfigError("invalid_config", "请选择有效的群参与模式。")
        quiet = entry["quiet_hours"]
        if (not isinstance(quiet, dict) or set(quiet) != set(DEFAULT_GROUP["quiet_hours"])
                or type(quiet["enabled"]) is not bool
                or not isinstance(quiet["start"], str) or not TIME.fullmatch(quiet["start"])
                or not isinstance(quiet["end"], str) or not TIME.fullmatch(quiet["end"])
                or not isinstance(quiet["timezone"], str) or len(quiet["timezone"]) > 80):
            raise ConfigError("invalid_config", "安静时段需要有效的起止时间和时区。")
        try:
            ZoneInfo(quiet["timezone"])
        except (ValueError, ZoneInfoNotFoundError):
            raise ConfigError("invalid_config", "安静时段时区不可用。") from None
        if quiet["enabled"] and quiet["start"] == quiet["end"]:
            raise ConfigError("invalid_config", "安静时段的开始与结束不能相同；全天静默请选择仅 @ 回复。")
        name = _text(entry["display_name"], 40, "机器人称呼")
        if not name:
            raise ConfigError("invalid_config", "机器人称呼不能为空。")
        result["groups"][group_id] = {
            "mode": entry["mode"], "display_name": name,
            "persona": _text(entry["persona"], MAX_PERSONA_CHARACTERS, "人设", multiline=True),
            "quiet_hours": dict(quiet),
        }
        if "examples" in entry:
            try:
                result["groups"][group_id]["examples"] = validate_examples(entry["examples"])
            except ValueError as exc:
                raise ConfigError("invalid_config", str(exc)) from None
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
        raise ConfigError("invalid_config", "自由对话配置过大，请减少群数量或缩短人设。")
    return result


class ConfigStore:
    def __init__(self, bot_uuid: str, root: Path | None = None):
        if (not isinstance(bot_uuid, str) or not 1 <= len(bot_uuid) <= 100
                or any(0xD800 <= ord(c) <= 0xDFFF for c in bot_uuid)):
            raise ConfigError("invalid_bot", "机器人身份无效。")
        self.root = Path(root) if root is not None else data_root() / "proactive"
        if not self.root.is_absolute():
            raise ConfigError("storage_unavailable", "自由对话配置需要绝对存储目录。")
        self.key = hashlib.sha256(bot_uuid.encode("utf-8")).hexdigest()
        self.path = self.root / (self.key + ".json")
        self.read_valid = True
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(str(self.path), threading.RLock())

    def read(self) -> dict:
        self.read_valid = True
        try:
            if self.root.is_symlink() or self.path.is_symlink():
                raise ValueError()
            if not self.path.exists():
                return defaults()
            if not self.path.is_file() or self.path.stat().st_size > MAX_BYTES:
                raise ValueError()
            return validate(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
            self.read_valid = False
            return defaults()

    @contextmanager
    def _write_lock(self):
        with self.lock:
            if self.root.is_symlink():
                raise ConfigError("storage_unavailable", "自由对话配置目录不可用。")
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock_path = self.root / (self.key + ".lock")
            if lock_path.is_symlink():
                raise ConfigError("storage_unavailable", "自由对话配置锁不可用。")
            with lock_path.open("a+b") as stream:
                if os.name == "nt":
                    import msvcrt
                    if stream.seek(0, os.SEEK_END) == 0:
                        stream.write(b"0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if os.name == "nt":
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def save(self, values: object, known_group_ids: set[str] | None) -> dict:
        candidate = validate(values)
        try:
            with self._write_lock():
                current = self.read()
                if not self.read_valid:
                    raise ConfigError("storage_unavailable", "自由对话配置损坏或不可读，请恢复配置文件后重试。")
                if candidate["revision"] != current["revision"]:
                    raise ConfigError("revision_conflict", "设置已在其他页面修改，请重新载入后再保存。")
                # Older consoles do not know this field. Saving group settings
                # from one must not erase an administrator's default style.
                if "default_style" not in candidate and "default_style" in current:
                    candidate["default_style"] = current["default_style"]
                if "default_examples" not in candidate and "default_examples" in current:
                    candidate["default_examples"] = current["default_examples"]
                for group_id, policy in candidate["groups"].items():
                    before = current["groups"].get(group_id, {})
                    if "examples" not in policy and "examples" in before:
                        policy["examples"] = before["examples"]
                candidate = validate(candidate)
                # An offline emergency pause is allowed only if policies are
                # unchanged (or removed/turned off), never as an authorization path.
                for group_id, policy in candidate["groups"].items():
                    before = current["groups"].get(group_id)
                    if known_group_ids is None:
                        if candidate["enabled"] or (policy != before and policy["mode"] != "off"):
                            raise ConfigError("groups_unavailable", "群列表暂不可用，当前只能暂停或关闭自由对话。")
                    elif group_id not in known_group_ids:
                        unchanged_pause = not candidate["enabled"] and policy == before
                        if not unchanged_pause and (policy["mode"] != "off" or before is None):
                            raise ConfigError("group_not_joined", "只能为当前 QQ 已加入的群开启自由对话。")
                if candidate["enabled"] and known_group_ids is None:
                    raise ConfigError("groups_unavailable", "开启前需要成功读取当前 QQ 的群列表。")
                candidate["revision"] = secrets.token_hex(16)
                temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(6) + ".tmp")
                try:
                    with temporary.open("x", encoding="utf-8") as stream:
                        os.chmod(temporary, 0o600)
                        json.dump(candidate, stream, ensure_ascii=False)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.path)
                finally:
                    temporary.unlink(missing_ok=True)
                self.read_valid = True
                return copy.deepcopy(candidate)
        except ConfigError:
            raise
        except OSError:
            raise ConfigError("storage_unavailable", "自由对话设置暂未保存，请检查存储后重试。") from None
