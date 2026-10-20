"""Shared, SDK-independent file types and bounded input handling."""

from __future__ import annotations

import hashlib
import importlib
import math
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path


class FileError(ValueError):
    """Stable machine-readable failure; messages never contain file contents."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FileLimits:
    max_input_bytes: int = 40 * 1024 * 1024
    max_output_bytes: int = 20 * 1024 * 1024
    max_member_bytes: int = 20 * 1024 * 1024
    max_expanded_bytes: int = 200 * 1024 * 1024
    max_entries: int = 1000
    max_ratio: int = 1000
    max_xml_bytes: int = 8 * 1024 * 1024
    max_xml_total_bytes: int = 40 * 1024 * 1024
    max_xml_nodes: int = 200_000
    max_xml_depth: int = 128
    max_sections: int = 1000
    max_rows: int = 100_000
    max_cells: int = 200_000
    max_text_chars: int = 1_000_000
    max_result_chars: int = 24_000
    max_page_items: int = 100
    decoder_seconds: int = 60
    decoder_memory_bytes: int = 512 * 1024 * 1024
    seven_zip_path: str | None = None

    def __post_init__(self):
        for name, value in vars(self).items():
            if name != "seven_zip_path" and (type(value) is not int or value < 1):
                raise ValueError("File limits must be positive integers")


DEFAULT_LIMITS = FileLimits()


def dependency(name: str):
    try:
        return importlib.import_module(name)
    except ImportError:
        raise FileError("dependency_missing", "该格式的解析依赖尚未安装。") from None


def load_bytes(path, limits: FileLimits) -> bytes:
    try:
        with Path(path).open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise FileError("invalid_input", "仅允许读取普通文件。")
            if info.st_size > limits.max_input_bytes:
                raise FileError("input_limit", "文件超过输入大小限制。")
            raw = stream.read(limits.max_input_bytes + 1)
    except OSError:
        raise FileError("input_unavailable", "文件不可读取。") from None
    if len(raw) > limits.max_input_bytes:
        raise FileError("input_limit", "文件超过输入大小限制。")
    return raw


def safe_member(name: str) -> str:
    """Canonical member identity, never a host filesystem destination."""
    if not isinstance(name, str) or not name or len(name) > 2048:
        raise FileError("unsafe_member", "压缩包成员名称无效。")
    if any(unicodedata.category(c) in {"Cc", "Cs", "Zl", "Zp"} for c in name):
        raise FileError("unsafe_member", "压缩包成员名称含控制字符。")
    normalized = name.replace("\\", "/")
    parts = normalized.rstrip("/").split("/")
    if normalized.startswith("/") or any(
        part in {"", ".", ".."} or ":" in part or len(part) > 255
        or part.endswith((" ", ".")) for part in parts
    ):
        raise FileError("unsafe_member", "压缩包含不安全或歧义成员路径。")
    # Prevent Windows device names even when a future consumer writes this member.
    devices = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
               *(f"lpt{i}" for i in range(1, 10))}
    if any(part.split(".", 1)[0].casefold() in devices for part in parts):
        raise FileError("unsafe_member", "压缩包成员使用保留文件名。")
    return normalized


def identity(name: str) -> str:
    return unicodedata.normalize("NFC", name.rstrip("/")).casefold()


def strict_keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise FileError("invalid_schema", "参数结构不符合该操作的要求。")


def integer(value, minimum=0, maximum=1_000_000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise FileError("invalid_selector", "分页或范围参数无效。")
    return value


def scalar(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise FileError("invalid_schema", "单元格仅允许文本、有限数值、布尔值或空值。")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
