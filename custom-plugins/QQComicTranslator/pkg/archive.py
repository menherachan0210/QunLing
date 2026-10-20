"""Validate ZIP/RAR/7z comics and decode one bounded image at a time."""

from __future__ import annotations

import io
import os
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unicodedata
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from PIL import Image

MAX_ARCHIVE_BYTES = 40 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_PAGES = 100
MAX_PIXELS = 40_000_000
MAX_RENDER_EDGE = 2400
SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
SUPPORTED_FORMATS = {"PNG", "JPEG", "WEBP", "BMP"}
SUPPORTED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
MAX_LIST_BYTES = 8 * 1024 * 1024
DECODER_MEMORY_BYTES = 512 * 1024 * 1024
DECODER_TIMEOUT_SECONDS = 120

# This trusted helper applies OS limits before the native decoder sees input.
# It never imports the plugin, reads credentials, or executes archive contents.
_LIMITED_WORKER = r"""
import os, subprocess, sys
limit = int(sys.argv[1])
cpu = int(sys.argv[2])
command = sys.argv[3:]
try:
    if os.name == "nt":
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64),
                ("flags", w.DWORD), ("minimum_working", c.c_size_t),
                ("maximum_working", c.c_size_t), ("active", w.DWORD),
                ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in
                ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(c.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", c.c_size_t),
                ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]
        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        kernel.SetInformationJobObject.restype = w.BOOL
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.AssignProcessToJobObject.restype = w.BOOL
        kernel.GetCurrentProcess.restype = w.HANDLE
        job = kernel.CreateJobObjectW(None, None)
        limits = Extended()
        # JOB_MEMORY | KILL_ON_JOB_CLOSE | ACTIVE_PROCESS | JOB_TIME
        limits.basic.flags = 0x200 | 0x2000 | 0x8 | 0x4
        limits.basic.active = 2  # trusted helper + one decoder
        limits.basic.job_time = cpu * 10_000_000
        limits.job_memory = limit
        if not job or not kernel.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)):
            os._exit(125)
        if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            os._exit(125)
        result = subprocess.run(command, shell=False, stdin=subprocess.DEVNULL,
            stdout=sys.stdout.buffer, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
        os._exit(result.returncode if 0 <= result.returncode <= 255 else 124)
    else:
        import resource
        def bound(kind, requested):
            hard = resource.getrlimit(kind)[1]
            target = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
            resource.setrlimit(kind, (target, target))
        bound(resource.RLIMIT_AS, limit)
        bound(resource.RLIMIT_CPU, cpu)
        bound(resource.RLIMIT_NOFILE, 64)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.execv(command[0], command)
except BaseException:
    os._exit(125)
"""


class ArchiveError(Exception):
    """A fixed error safe to return without revealing archive contents or metadata."""


@dataclass(frozen=True)
class PageInfo:
    index: int
    logical_path: str
    basename: str
    file_size: int


@dataclass(frozen=True)
class PreparedPage:
    index: int
    logical_path: str
    basename: str
    width: int
    height: int
    render_width: int
    render_height: int
    jpeg_bytes: bytes


def _logical_name(info: zipfile.ZipInfo) -> str:
    name = info.orig_filename
    if info.flag_bits & 0x800 or name.isascii():
        return name
    # Recover only recognizable CP437 mojibake, using strict round-trip decoding.
    # Plain Latin filenames stay untouched; ambiguous names are not guessed.
    try:
        raw = name.encode("cp437", errors="strict")
    except UnicodeEncodeError:
        return name
    box_drawing = any("\u2500" <= char <= "\u259f" for char in name)
    for encoding in ("utf-8", "gb18030"):
        try:
            candidate = raw.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
        cjk = any("\u3400" <= char <= "\u9fff" for char in candidate)
        if (
            cjk
            and (encoding == "utf-8" or box_drawing)
            and candidate.encode(encoding) == raw
        ):
            return candidate
    return name


def _validated_path(name: str) -> tuple[str, str]:
    if not isinstance(name, str) or not name or len(name) > 2048:
        raise ArchiveError("压缩包中存在无效或过长的文件名。")
    if any(unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"} for char in name):
        raise ArchiveError("压缩包文件名包含控制字符，无法安全处理。")
    path = name.replace("\\", "/")
    parts = path.rstrip("/").split("/")
    if path.startswith("/") or any(
        part in {"", ".", ".."} or ":" in part for part in parts
    ):
        raise ArchiveError("压缩包包含绝对路径或路径穿越，已拒绝处理。")
    basename = parts[-1]
    if len(basename) > 255 or basename.endswith((" ", ".")):
        raise ArchiveError("压缩包中存在不能可靠匹配的图片文件名。")
    return path, basename


def _natural_key(path: str):
    return tuple(
        (0, int(piece)) if piece.isdigit() else (1, piece.casefold())
        for piece in re.split(r"([0-9]+)", path)
    )


def _preflight_directory(raw: bytes) -> None:
    """Bound the central-directory object count before ZipFile allocates entries."""
    end = raw.rfind(b"PK\x05\x06", max(0, len(raw) - 65557))
    if end < 0 or end + 22 > len(raw):
        raise ArchiveError("ZIP 目录结构无效。")
    _, disk, directory_disk, disk_entries, count, size, offset, comment_size = (
        struct.unpack_from("<4s4H2LH", raw, end)
    )
    if end + 22 + comment_size != len(raw) or disk or directory_disk:
        raise ArchiveError("不支持分卷 ZIP 或不完整的压缩包。")
    if count > MAX_ENTRIES or disk_entries != count:
        raise ArchiveError("ZIP 条目超过 1000 个或目录数量不一致。")
    if offset + size != end or offset < 0:
        raise ArchiveError("ZIP 目录位置无效或使用了不支持的目录扩展。")
    cursor, seen = offset, 0
    while cursor < end:
        if cursor + 46 > end or raw[cursor : cursor + 4] != b"PK\x01\x02":
            raise ArchiveError("ZIP 目录结构无效。")
        name_size, extra_size, entry_comment_size = struct.unpack_from(
            "<3H", raw, cursor + 28
        )
        cursor += 46 + name_size + extra_size + entry_comment_size
        seen += 1
        if seen > MAX_ENTRIES:
            raise ArchiveError("ZIP 条目超过 1000 个，请拆分压缩包。")
    if cursor != end or seen != count:
        raise ArchiveError("ZIP 目录数量或长度不一致。")


def _decoder_path(configured: str | None) -> str:
    if configured is not None and configured != "":
        candidate = Path(configured) if isinstance(configured, str) else None
        if candidate is None or not candidate.is_absolute() or not candidate.is_file():
            raise ArchiveError("管理员配置的 7-Zip 路径无效，无法读取 RAR 或 7z。")
        return str(candidate.resolve())
    for name in ("7zz", "7z"):
        found = shutil.which(name)
        if found:
            return str(Path(found).resolve())
    raise ArchiveError("RAR 和 7z 需要管理员安装完整 7-Zip 并配置 seven_zip_path。")


def _terminate_decoder(process):
    if os.name == "nt":
        if process.poll() is None:
            process.kill()  # Closing the helper's Job Object kills its decoder too.
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _run_limited(command: list[str], cwd: str, output_limit: int) -> bytes:
    """Run trusted decoder arguments with OS memory, time and pipe output limits."""
    environment = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TEMP": cwd,
        "TMP": cwd,
        "TMPDIR": cwd,
    }
    for key in ("SYSTEMROOT", "SystemRoot", "WINDIR"):
        if key in os.environ:
            environment[key] = os.environ[key]
    flags = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = subprocess.Popen(
        [
            # Windows venv python.exe can be a forwarding launcher. Use the
            # real interpreter so this PID owns the Job Object we must kill.
            getattr(sys, "_base_executable", sys.executable),
            "-I",
            "-S",
            "-c",
            _LIMITED_WORKER,
            str(DECODER_MEMORY_BYTES),
            str(max(1, int(DECODER_TIMEOUT_SECONDS))),
            *command,
        ],
        cwd=cwd,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        close_fds=True,
        **flags,
    )
    body = bytearray()
    overflow, failed = threading.Event(), threading.Event()

    def drain():
        try:
            while chunk := process.stdout.read(
                min(65536, output_limit - len(body) + 1)
            ):
                body.extend(chunk)
                if len(body) > output_limit:
                    overflow.set()
                    _terminate_decoder(process)
                    return
        except Exception:
            failed.set()
            _terminate_decoder(process)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        process.wait(timeout=DECODER_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        _terminate_decoder(process)
        process.wait(timeout=5)
        reader.join(timeout=5)
        if not reader.is_alive():
            process.stdout.close()
    if timed_out:
        raise ArchiveError("压缩包解码超过 120 秒，请拆分或重新打包。")
    if overflow.is_set():
        raise ArchiveError("压缩包目录或图片实际输出超过安全限制。")
    if process.returncode != 0 or failed.is_set() or reader.is_alive():
        raise ArchiveError("压缩包解析失败，可能损坏、加密、分卷或超过解码内存限制。")
    return bytes(body)


def _record(lines: list[str]) -> dict[str, str]:
    record = {}
    for line in lines:
        if " = " not in line:
            raise ArchiveError("压缩包目录信息存在歧义，无法安全读取。")
        key, value = line.split(" = ", 1)
        if (
            not key
            or key in record
            or any(
                unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"} for char in line
            )
        ):
            raise ArchiveError("压缩包目录存在重复字段或控制字符。")
        record[key] = value
    return record


def _listing(raw: bytes, archive_type: str, archive_size: int):
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ArchiveError("压缩包文件名编码无法可靠读取。") from None
    # splitlines() treats Unicode path characters as record separators.
    lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
    try:
        start = lines.index("--")
        end = lines.index("----------", start + 1)
    except ValueError:
        raise ArchiveError("压缩包目录结构无效。") from None
    header = _record([line for line in lines[start + 1 : end] if line])
    if header.get("Type") != archive_type or header.get("Physical Size") != str(
        archive_size
    ):
        raise ArchiveError("压缩包类型、长度或完整性不匹配。")
    if (
        header.get("Encrypted") == "+"
        or "Volume Index" in header
        or header.get("Volumes", "1") != "1"
        or "Volume" in header.get("Characteristics", "")
        or header.get("Multivolume", "-") != "-"
        or any(key in header for key in ("Offset", "Tail Size", "ERROR", "WARNING"))
    ):
        raise ArchiveError("不支持加密、分卷或附加异常数据的压缩包。")
    records, pending = [], []
    for line in lines[end + 1 :] + [""]:
        if line:
            pending.append(line)
        elif pending:
            record = _record(pending)
            if next(iter(record), None) != "Path":
                raise ArchiveError("压缩包目录缺少明确的成员路径。")
            records.append(record)
            pending = []
            if len(records) > MAX_ENTRIES:
                raise ArchiveError("压缩包条目超过 1000 个，请拆分压缩包。")
    return records


def _entry_metadata(record: dict[str, str]):
    path, basename = _validated_path(record.get("Path", ""))
    if record.get("Encrypted") != "-":
        raise ArchiveError("不支持加密或加密状态不明确的压缩包。")
    for key in (
        "Symbolic Link",
        "Hard Link",
        "Link",
        "Copy Link",
        "Alternate Stream",
        "Reparse",
    ):
        if record.get(key) not in (None, "", "-"):
            raise ArchiveError("压缩包包含链接或特殊文件，已拒绝处理。")
    if any(
        record.get(key) not in (None, "", "-")
        for key in ("Split Before", "Split After")
    ):
        raise ArchiveError("不支持分卷压缩包，请合并后重新发送。")
    attributes = record.get("Attributes", "")
    # Unix permission type, when provided, must be an ordinary file/directory.
    unix = re.search(r"(?:^|\s)([bcdlps-])[rwxstST-]{9}(?:$|\s)", attributes)
    if unix and unix.group(1) not in {"-", "d"}:
        raise ArchiveError("压缩包包含链接或特殊文件，已拒绝处理。")
    if "L" in attributes.split(" ", 1)[0]:
        raise ArchiveError("压缩包包含链接或特殊文件，已拒绝处理。")
    folder = record.get("Folder")
    if folder not in {None, "+", "-"}:
        raise ArchiveError("压缩包目录属性无效。")
    is_dir = (
        folder == "+"
        or "D" in attributes.split(" ", 1)[0]
        or bool(unix and unix.group(1) == "d")
    )
    value = record.get("Size", "")
    if not value.isascii() or not value.isdecimal() or len(value) > 20:
        raise ArchiveError("压缩包成员大小信息无效。")
    size = int(value)
    if is_dir and size != 0:
        raise ArchiveError("压缩包目录包含异常数据。")
    return path, basename, size, is_dir


class ComicArchive:
    """Own only compressed ZIP bytes; prepare_page never caches decoded images."""

    def __init__(self, raw: bytes):
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_ARCHIVE_BYTES:
            raise ArchiveError("漫画 ZIP 必须非空且不超过 40 MiB。")
        if not raw.startswith((b"PK\x03\x04", b"PK\x05\x06")):
            raise ArchiveError("附件不是有效 ZIP，请发送标准 ZIP 压缩包。")
        _preflight_directory(raw)
        self._buffer = io.BytesIO(raw)
        self._zip = None
        self._infos = []
        self.pages = ()
        self.ignored_entries = 0
        self._closed = False
        try:
            self._zip = zipfile.ZipFile(self._buffer)
            infos = self._zip.infolist()
            if len(infos) > MAX_ENTRIES:
                raise ArchiveError("ZIP 条目超过 1000 个，请拆分压缩包。")
            total = 0
            seen = set()
            candidates = []
            for info in infos:
                if info.flag_bits & (1 | 0x40 | 0x2000):
                    raise ArchiveError("不支持加密 ZIP，请移除密码后发送。")
                # LZMA can request a huge dictionary before output limits apply.
                # Standard stored/deflated ZIPs keep decompressor memory bounded.
                if info.compress_type not in SUPPORTED_COMPRESSION:
                    raise ArchiveError(
                        "ZIP 压缩方式不受支持，请使用标准 ZIP 存储或 Deflate 重新打包。"
                    )
                mode = (info.external_attr >> 16) & 0xFFFF
                kind = stat.S_IFMT(mode)
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise ArchiveError("ZIP 包含符号链接或特殊文件，已拒绝处理。")
                path, basename = _validated_path(_logical_name(info))
                if info.file_size < 0 or info.compress_size < 0:
                    raise ArchiveError("ZIP 文件大小信息无效。")
                total += info.file_size
                if total > MAX_TOTAL_BYTES:
                    raise ArchiveError("ZIP 声明的总解压大小超过 200 MiB，请拆分。")
                if info.is_dir():
                    continue
                if PurePosixPath(basename).suffix.lower() not in SUPPORTED_EXTENSIONS:
                    self.ignored_entries += 1
                    continue
                if not 0 < info.file_size <= MAX_IMAGE_BYTES:
                    raise ArchiveError("ZIP 中图片为空或超过单张 20 MiB 限制。")
                identity = unicodedata.normalize("NFC", basename).casefold()
                if identity in seen:
                    raise ArchiveError(
                        "ZIP 中有同名图片（含大小写或不同目录），无法生成唯一 LabelPlus 匹配。"
                    )
                seen.add(identity)
                candidates.append((path, basename, info))
                if len(candidates) > MAX_PAGES:
                    raise ArchiveError("ZIP 中图片超过 100 张，请拆分压缩包。")
            if not candidates:
                raise ArchiveError("ZIP 中没有 PNG、JPEG、WEBP 或 BMP 图片。")
            candidates.sort(key=lambda item: _natural_key(item[0]))
            self._infos = [item[2] for item in candidates]
            self.pages = tuple(
                PageInfo(index, path, basename, info.file_size)
                for index, (path, basename, info) in enumerate(candidates)
            )
        except ArchiveError:
            self.close()
            raise
        except Exception:
            self.close()
            raise ArchiveError("ZIP 结构无效或不受支持，请重新打包。") from None

    def __enter__(self):
        if self._closed:
            raise ArchiveError("压缩包已关闭，请重新打开任务附件。")
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._zip is not None:
            self._zip.close()
        self._buffer.close()
        self._closed = True

    def _read_member(self, index: int) -> bytes:
        page = self.pages[index]
        body = bytearray()
        with self._zip.open(self._infos[index], "r") as stream:
            while chunk := stream.read(65536):
                body.extend(chunk)
                if len(body) > MAX_IMAGE_BYTES or len(body) > page.file_size:
                    raise ArchiveError("图片实际解压大小超过限制或与声明不符。")
        return bytes(body)

    def prepare_page(self, index: int) -> PreparedPage:
        if self._closed:
            raise ArchiveError("压缩包已关闭，请重新打开任务附件。")
        if type(index) is not int or not 0 <= index < len(self.pages):
            raise ArchiveError("图片页码无效。")
        page = self.pages[index]
        try:
            body = self._read_member(index)
            if len(body) != page.file_size:
                raise ArchiveError("压缩包图片内容不完整，请重新打包。")
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(
                    io.BytesIO(body), formats=list(SUPPORTED_FORMATS)
                ) as image:
                    width, height = image.size
                    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                        raise ArchiveError(
                            "图片像素超过 4000 万或尺寸无效，请缩小原图。"
                        )
                    if getattr(image, "n_frames", 1) != 1:
                        raise ArchiveError("暂不支持多帧或动画图片，请改为单帧图片。")
                    image.load()
                    # Deliberately do not apply EXIF orientation: coordinates must
                    # refer to the same raw pixel geometry as the source file.
                    image.thumbnail(
                        (MAX_RENDER_EDGE, MAX_RENDER_EDGE), Image.Resampling.LANCZOS
                    )
                    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                        rgba = image.convert("RGBA")
                        rgb = Image.new("RGB", rgba.size, "white")
                        try:
                            rgb.paste(rgba, mask=rgba.getchannel("A"))
                        finally:
                            rgba.close()
                    else:
                        rgb = image.convert("RGB")
                    try:
                        output = io.BytesIO()
                        rgb.save(output, format="JPEG", quality=90, optimize=False)
                        jpeg = output.getvalue()
                        render_width, render_height = rgb.size
                    finally:
                        rgb.close()
            return PreparedPage(
                index,
                page.logical_path,
                page.basename,
                width,
                height,
                render_width,
                render_height,
                jpeg,
            )
        except ArchiveError:
            raise
        except Exception:
            raise ArchiveError(
                "压缩包中图片损坏、校验失败或格式不受支持，请重新打包。"
            ) from None


class ExternalComicArchive(ComicArchive):
    """Keep only the original RAR/7z on disk; member bytes go through a pipe."""

    def __init__(self, raw: bytes, archive_type: str, seven_zip_path: str | None):
        self._closed = False
        self._temporary = None
        self.pages = ()
        self.ignored_entries = 0
        self._decoder = _decoder_path(seven_zip_path)
        self._type = archive_type
        try:
            self._temporary = tempfile.TemporaryDirectory(prefix="qqcomic-")
            self._archive_path = Path(self._temporary.name) / "archive.bin"
            self._archive_path.write_bytes(raw)
            listing = _run_limited(
                self._command("l", "-slt"), self._temporary.name, MAX_LIST_BYTES
            )
            entries = _listing(listing, archive_type, len(raw))
            candidates, full_paths, basenames = [], set(), set()
            total = 0
            for record in entries:
                logical_path, basename, size, is_dir = _entry_metadata(record)
                # CLI normalizes CR/LF in names to underscores. Any resulting
                # collision is rejected; extraction must match the exact name.
                identity = unicodedata.normalize(
                    "NFC", logical_path.rstrip("/")
                ).casefold()
                if identity in full_paths:
                    raise ArchiveError("压缩包中有重复或归一化后冲突的成员路径。")
                full_paths.add(identity)
                total += size
                if total > MAX_TOTAL_BYTES:
                    raise ArchiveError("压缩包声明的总解压大小超过 200 MiB，请拆分。")
                if is_dir:
                    continue
                if PurePosixPath(basename).suffix.lower() not in SUPPORTED_EXTENSIONS:
                    self.ignored_entries += 1
                    continue
                if not 0 < size <= MAX_IMAGE_BYTES:
                    raise ArchiveError("压缩包中图片为空或超过单张 20 MiB 限制。")
                identity = unicodedata.normalize("NFC", basename).casefold()
                if identity in basenames:
                    raise ArchiveError(
                        "压缩包中有同名图片（含大小写或不同目录），无法唯一匹配 LabelPlus。"
                    )
                basenames.add(identity)
                candidates.append((logical_path, basename, size, record["Path"]))
                if len(candidates) > MAX_PAGES:
                    raise ArchiveError("压缩包中图片超过 100 张，请拆分。")
            if not candidates:
                raise ArchiveError("压缩包中没有 PNG、JPEG、WEBP 或 BMP 图片。")
            candidates.sort(key=lambda item: _natural_key(item[0]))
            self.pages = tuple(
                PageInfo(index, path, name, size)
                for index, (path, name, size, _) in enumerate(candidates)
            )
            self._members = tuple(item[3] for item in candidates)
        except ArchiveError:
            self.close()
            raise
        except Exception:
            self.close()
            raise ArchiveError("压缩包无法安全读取，请重新打包。") from None

    def _command(self, operation, *switches, member=None):
        # -- also disables @listfile parsing. -spd prevents wildcard matching.
        command = [
            self._decoder,
            operation,
            "-sccUTF-8",
            "-bd",
            "-bse0",
            "-bsp0",
            "-smemx512m",
            "-mmt=1",
            "-p",
            "-t" + self._type,
            *switches,
            "--",
            str(self._archive_path),
        ]
        if member is not None:
            command.append(member)
        return command

    def _read_member(self, index):
        page = self.pages[index]
        return _run_limited(
            self._command(
                "x", "-so", "-spd", "-ssc", "-r-", member=self._members[index]
            ),
            self._temporary.name,
            min(MAX_IMAGE_BYTES, page.file_size),
        )

    def close(self):
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        self._closed = True


def open_archive(raw: bytes, seven_zip_path: str | None = None) -> ComicArchive:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_ARCHIVE_BYTES:
        raise ArchiveError("漫画压缩包必须非空且不超过 40 MiB。")
    if raw.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return ComicArchive(raw)
    for signature, archive_type in (
        (b"7z\xbc\xaf\x27\x1c", "7z"),
        (b"Rar!\x1a\x07\x00", "Rar"),
        (b"Rar!\x1a\x07\x01\x00", "Rar5"),
    ):
        if raw.startswith(signature):
            return ExternalComicArchive(raw, archive_type, seven_zip_path)
    raise ArchiveError("附件不是 ZIP、RAR 或 7z 压缩包；不支持自解压程序。")
