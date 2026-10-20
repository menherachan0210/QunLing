"""Bounded mixed-content archive navigation, without filesystem extraction.

ZIP preflight and 7-Zip guards follow QunLing's QQComicTranslator archive checks.
Resource-limited decoding is not a security sandbox; the service must also isolate
untrusted parsers from credentials, host mounts and network access.
"""

from __future__ import annotations

import io
import re
import stat
import struct
import zipfile

from .core import FileError, FileLimits, identity, safe_member


def zip_preflight(raw: bytes, limits: FileLimits):
    end = raw.rfind(b"PK\x05\x06", max(0, len(raw) - 65557))
    if end < 0 or end + 22 > len(raw):
        raise FileError("invalid_archive", "ZIP 目录无效。")
    _, disk, directory_disk, disk_entries, count, size, offset, comment = (
        struct.unpack_from("<4s4H2LH", raw, end)
    )
    if end + 22 + comment != len(raw) or disk or directory_disk or disk_entries != count:
        raise FileError("unsupported_archive", "不支持分卷、不完整或附加异常数据的 ZIP。")
    if count > limits.max_entries or offset + size != end:
        raise FileError("archive_limit", "ZIP 目录超限或使用不支持的 ZIP64 扩展。")
    cursor, seen = offset, 0
    while cursor < end:
        if cursor + 46 > end or raw[cursor:cursor + 4] != b"PK\x01\x02":
            raise FileError("invalid_archive", "ZIP 目录结构无效。")
        a, b, c = struct.unpack_from("<3H", raw, cursor + 28)
        cursor += 46 + a + b + c
        seen += 1
        if seen > limits.max_entries:
            raise FileError("archive_limit", "压缩包成员过多。")
    if cursor != end or seen != count:
        raise FileError("invalid_archive", "ZIP 目录数量不一致。")


class ZipArchive:
    def __init__(self, raw: bytes, limits: FileLimits):
        self.limits = limits
        zip_preflight(raw, limits)
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(raw))
            self.entries, self._members = [], {}
            seen, total = set(), 0
            for info in self.zip.infolist():
                # ZipInfo truncates filenames at NUL; inspect the original.
                name = safe_member(info.orig_filename)
                key = identity(name)
                if key in seen:
                    raise FileError("ambiguous_member", "压缩包含重复或归一化后冲突的成员路径。")
                seen.add(key)
                mode = info.external_attr >> 16
                if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise FileError("unsafe_member", "压缩包含链接或特殊文件。")
                if info.flag_bits & 1:
                    raise FileError("encrypted_file", "暂不读取加密压缩包。")
                if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise FileError("unsupported_compression", "ZIP 仅支持 Store 或 Deflate 压缩。")
                if info.file_size > limits.max_member_bytes:
                    raise FileError("member_limit", "压缩包单个成员超过大小限制。")
                if info.file_size > max(1, info.compress_size) * limits.max_ratio:
                    raise FileError("archive_limit", "压缩包成员压缩比超过限制。")
                total += info.file_size
                if total > limits.max_expanded_bytes:
                    raise FileError("archive_limit", "压缩包展开总大小超过限制。")
                is_dir = info.is_dir()
                if is_dir and info.file_size:
                    raise FileError("invalid_archive", "目录成员包含异常数据。")
                self.entries.append({"path": name, "size": info.file_size, "directory": is_dir})
                self._members[name] = info
        except FileError:
            raise
        except (ValueError, OSError, zipfile.BadZipFile):
            raise FileError("invalid_archive", "ZIP 无法安全解析。") from None

    def read(self, member: str) -> bytes:
        name = safe_member(member)
        info = self._members.get(name)
        if info is None or info.is_dir():
            raise FileError("member_not_found", "未找到指定的普通文件成员。")
        try:
            with self.zip.open(info) as stream:
                raw = stream.read(min(info.file_size, self.limits.max_member_bytes) + 1)
            if len(raw) != info.file_size:
                raise FileError("member_limit", "成员实际大小与目录不一致。")
            return raw
        except FileError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile, ValueError):
            raise FileError("invalid_archive", "成员损坏或校验失败。") from None


def archive_type(raw):
    for signature, kind in (
        (b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"),
        (b"7z\xbc\xaf\x27\x1c", "7z"), (b"Rar!\x1a\x07\x00", "Rar"),
        (b"Rar!\x1a\x07\x01\x00", "Rar5"),
    ):
        if raw.startswith(signature):
            return kind
    return None


def open_archive(raw: bytes, limits: FileLimits):
    kind = archive_type(raw)
    if kind == "zip":
        return ZipArchive(raw, limits)
    if kind:
        from .decoder import NativeArchive
        return NativeArchive(raw, kind, limits)
    raise FileError("unsupported_archive", "仅支持 ZIP、RAR、7z；不支持自解压程序。")


def validate_native_listing(raw: bytes, kind: str, size: int, limits: FileLimits):
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        raise FileError("invalid_archive", "成员名编码无法可靠读取。") from None
    lines = [s.removesuffix("\r") for s in text.split("\n")]
    def record(rows):
        result = {}
        for row in rows:
            if " = " not in row:
                raise FileError("invalid_archive", "压缩包目录格式不明确。")
            key, value = row.split(" = ", 1)
            if not key or key in result or any(ord(c) < 32 for c in row):
                raise FileError("invalid_archive", "压缩包目录字段无效。")
            result[key] = value
        return result
    try:
        start = lines.index("--")
        end = lines.index("----------", start + 1)
    except ValueError:
        raise FileError("invalid_archive", "压缩包目录结构无效。") from None
    header = record([line for line in lines[start + 1:end] if line])
    if header.get("Type") != kind or header.get("Physical Size") != str(size):
        raise FileError("invalid_archive", "压缩包类型或长度不匹配。")
    if (header.get("Encrypted") == "+" or header.get("Volumes", "1") != "1"
            or header.get("Multivolume", "-") != "-"
            or "Volume" in header.get("Characteristics", "")
            or any(key in header for key in ("Volume Index", "Offset", "Tail Size", "ERROR", "WARNING"))):
        raise FileError("unsupported_archive", "不支持加密、分卷或附加异常数据的压缩包。")
    records, pending = [], []
    for line in lines[end + 1:] + [""]:
        if line:
            pending.append(line)
        elif pending:
            item = record(pending)
            if next(iter(item), None) != "Path":
                raise FileError("invalid_archive", "成员缺少明确路径。")
            records.append(item)
            pending = []
            if len(records) > limits.max_entries:
                raise FileError("archive_limit", "压缩包成员过多。")
    entries, seen, total = [], set(), 0
    for item in records:
        name = safe_member(item.get("Path", ""))
        key = identity(name)
        if key in seen:
            raise FileError("ambiguous_member", "压缩包成员路径冲突。")
        seen.add(key)
        if item.get("Encrypted") != "-":
            raise FileError("encrypted_file", "成员已加密或加密状态不明确。")
        for field in ("Symbolic Link", "Hard Link", "Link", "Copy Link", "Alternate Stream",
                      "Reparse", "Split Before", "Split After"):
            if item.get(field) not in (None, "", "-"):
                raise FileError("unsafe_member", "压缩包含链接、特殊文件或分卷成员。")
        attributes = item.get("Attributes", "")
        unix = re.search(r"(?:^|\s)([bcdlps-])[rwxstST-]{9}(?:$|\s)", attributes)
        if (unix and unix.group(1) not in {"-", "d"}) or "L" in attributes.split(" ", 1)[0]:
            raise FileError("unsafe_member", "压缩包含链接或特殊文件。")
        if item.get("Folder") not in (None, "+", "-"):
            raise FileError("invalid_archive", "目录属性无效。")
        directory = (item.get("Folder") == "+" or "D" in attributes.split(" ", 1)[0]
                     or bool(unix and unix.group(1) == "d"))
        value = item.get("Size", "")
        if not value.isascii() or not value.isdecimal() or len(value) > 20:
            raise FileError("invalid_archive", "成员大小无效。")
        length = int(value)
        total += length
        if length > limits.max_member_bytes or total > limits.max_expanded_bytes:
            raise FileError("archive_limit", "压缩包展开大小超过限制。")
        if directory and length:
            raise FileError("invalid_archive", "目录成员包含异常数据。")
        entries.append({"path": name, "size": length, "directory": directory,
                        "_original": item["Path"]})
    if total > max(1, size) * limits.max_ratio:
        raise FileError("archive_limit", "压缩包总压缩比超过限制。")
    return entries
