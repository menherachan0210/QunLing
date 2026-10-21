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


def archive_password(value: str | None) -> str | None:
    """Validate a transient decoder credential without including it in errors."""
    if value is None:
        return None
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n"):
        raise FileError("invalid_archive_password", "压缩包密码须为非空单行文本。")
    try:
        if len(value.encode("utf-8")) > 1024:
            raise ValueError
    except (UnicodeError, ValueError):
        raise FileError("invalid_archive_password", "压缩包密码编码或长度无效。") from None
    return value


def password_required():
    return FileError("archive_password_required", "压缩包已加密，请提供此压缩包的密码后重试。")


def password_invalid():
    return FileError("archive_password_invalid", "密码不正确或加密内容已损坏，请确认密码后重试。")


def _aes_compression(info):
    extra, position, methods = info.extra, 0, []
    while position < len(extra):
        if position + 4 > len(extra):
            raise FileError("invalid_archive", "ZIP 扩展字段不完整。")
        tag, size = struct.unpack_from("<HH", extra, position)
        position += 4
        value = extra[position:position + size]
        position += size
        if len(value) != size:
            raise FileError("invalid_archive", "ZIP 扩展字段不完整。")
        if tag == 0x9901:
            if len(value) != 7:
                raise FileError("unsupported_compression", "ZIP AES 参数无效。")
            version, vendor, strength, method = struct.unpack("<H2sBH", value)
            if version not in {1, 2} or vendor != b"AE" or strength not in {1, 2, 3}:
                raise FileError("unsupported_compression", "ZIP AES 参数不受支持。")
            methods.append(method)
    if len(methods) != 1 or not info.flag_bits & 1:
        raise FileError("unsupported_compression", "ZIP AES 参数缺失或存在歧义。")
    return methods[0]


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
    def __init__(self, raw: bytes, limits: FileLimits, *, password: str | None = None):
        self.limits = limits
        password = archive_password(password)
        self._password = password.encode("utf-8") if password is not None else None
        self._native = None
        self.encrypted = False
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
                self.encrypted |= bool(info.flag_bits & 1)
                if info.flag_bits & 0x40:
                    raise FileError("unsupported_compression", "ZIP 强加密格式不受支持。")
                method = (_aes_compression(info) if info.compress_type == 99
                          else info.compress_type)
                if method not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
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
            if self.encrypted and password is None:
                raise password_required()
            if any(info.compress_type == 99 for info in self._members.values()):
                from .decoder import NativeArchive
                self._native = NativeArchive(raw, "zip", limits, password=password)
            elif self.encrypted:
                first = next((name for name, info in self._members.items()
                              if info.flag_bits & 1 and not info.is_dir()), None)
                if first is not None:
                    self.read(first)
        except FileError:
            raise
        except (ValueError, OSError, zipfile.BadZipFile):
            raise FileError("invalid_archive", "ZIP 无法安全解析。") from None

    def read(self, member: str) -> bytes:
        name = safe_member(member)
        info = self._members.get(name)
        if info is None or info.is_dir():
            raise FileError("member_not_found", "未找到指定的普通文件成员。")
        if self._native is not None:
            return self._native.read(name)
        try:
            with self.zip.open(info, pwd=self._password) as stream:
                raw = stream.read(min(info.file_size, self.limits.max_member_bytes) + 1)
            if len(raw) != info.file_size:
                raise FileError("member_limit", "成员实际大小与目录不一致。")
            return raw
        except FileError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile, ValueError):
            if info.flag_bits & 1:
                raise password_invalid() from None
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


def open_archive(raw: bytes, limits: FileLimits, *, password: str | None = None):
    kind = archive_type(raw)
    if kind == "zip":
        return ZipArchive(raw, limits, password=password)
    if kind:
        from .decoder import NativeArchive
        return NativeArchive(raw, kind, limits, password=password)
    raise FileError("unsupported_archive", "仅支持 ZIP、RAR、7z；不支持自解压程序。")


def validate_native_listing(raw: bytes, kind: str, size: int, limits: FileLimits,
                            *, password_supplied: bool = False):
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
    if (header.get("Volumes", "1") != "1"
            or header.get("Multivolume", "-") != "-"
            or "Volume" in header.get("Characteristics", "")
            or any(key in header for key in ("Volume Index", "Offset", "Tail Size", "ERROR", "WARNING"))):
        raise FileError("unsupported_archive", "不支持分卷或附加异常数据的压缩包。")
    if header.get("Encrypted") == "+" and not password_supplied:
        raise password_required()
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
        encrypted = item.get("Encrypted")
        if encrypted not in {"-", "+"}:
            raise FileError("encrypted_file", "成员加密状态不明确。")
        if encrypted == "+" and not password_supplied:
            raise password_required()
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
                        "_original": item["Path"], "_encrypted": encrypted == "+"})
    if total > max(1, size) * limits.max_ratio:
        raise FileError("archive_limit", "压缩包总压缩比超过限制。")
    return entries
