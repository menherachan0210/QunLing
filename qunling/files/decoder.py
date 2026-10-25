"""Resource-bounded, argument-vector-only native archive decoder."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .archives import archive_password, password_invalid, password_required, validate_native_listing
from .core import FileError, FileLimits, safe_member


def _run(command: list[str], cwd: str, output_limit: int, limits: FileLimits,
         *, password: str | None = None, encrypted: bool = False) -> bytes:
    environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                   "TEMP": cwd, "TMP": cwd, "TMPDIR": cwd}
    for key in ("SYSTEMROOT", "SystemRoot", "WINDIR"):
        if key in os.environ:
            environment[key] = os.environ[key]
    flags = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
             else {"start_new_session": True})
    process = subprocess.Popen(
        [getattr(sys, "_base_executable", sys.executable), "-I", "-S",
         str(Path(__file__).with_name("_limited_decoder_worker.py")),
         str(limits.decoder_memory_bytes), str(limits.decoder_seconds), *command],
        cwd=cwd, shell=False, stdin=subprocess.PIPE if password is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, close_fds=True, **flags,
    )
    def terminate():
        if os.name == "nt":
            if process.poll() is None:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    body, diagnostic = bytearray(), bytearray()
    overflow, failed = threading.Event(), threading.Event()
    def drain():
        try:
            while chunk := process.stdout.read(min(65536, output_limit - len(body) + 1)):
                body.extend(chunk)
                if len(body) > output_limit:
                    overflow.set()
                    terminate()
                    return
        except Exception:
            failed.set()
            terminate()
    reader = threading.Thread(target=drain, daemon=True)
    def drain_errors():
        try:
            while chunk := process.stderr.read(65536):
                diagnostic.extend(chunk[:max(0, 65536 - len(diagnostic))])
        except Exception:
            failed.set()
            terminate()
    errors = threading.Thread(target=drain_errors, daemon=True)
    reader.start()
    errors.start()
    if password is not None:
        try:
            process.stdin.write((password + "\n").encode("utf-8"))
            process.stdin.flush()
        except BrokenPipeError:
            pass
        finally:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
    timed_out = False
    try:
        process.wait(timeout=limits.decoder_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        terminate()
        process.wait(timeout=5)
        reader.join(timeout=5)
        errors.join(timeout=5)
        if not reader.is_alive():
            process.stdout.close()
        if not errors.is_alive():
            process.stderr.close()
    if timed_out:
        raise FileError("decoder_timeout", "压缩包解析超时。")
    if overflow.is_set():
        raise FileError("archive_limit", "压缩包实际输出超过限制。")
    if process.returncode or failed.is_set() or reader.is_alive() or errors.is_alive():
        # Native error output may contain archive names; only inspect fixed signals,
        # never return it, the password, or the child command to callers/loggers.
        detail = (bytes(diagnostic) + bytes(body[:65536])).lower()
        password_error = any(marker in detail for marker in (
            b"wrong password", b"password is incorrect", b"can not open encrypted archive",
            b"cannot open encrypted archive", b"data error in encrypted file",
        ))
        if password_error or (encrypted and process.returncode == 2):
            raise (password_required() if password is None else password_invalid())
        raise FileError("decoder_failed", "压缩包损坏、分卷或超过解析资源限制。")
    return bytes(body)


class NativeArchive:
    def __init__(self, raw: bytes, kind: str, limits: FileLimits,
                 *, password: str | None = None):
        self.raw, self.kind, self.limits = raw, kind, limits
        self._password = archive_password(password)
        configured = limits.seven_zip_path
        found = configured or shutil.which("7zz") or shutil.which("7z")
        if not found or not Path(found).is_absolute() or not Path(found).is_file():
            raise FileError("dependency_missing", "RAR/7z/AES ZIP 需要管理员安装完整的 7-Zip。")
        self.decoder = str(Path(found).resolve())
        listing = self._invoke("l", "-slt", output_limit=8 * 1024 * 1024)
        self._entries = validate_native_listing(listing, kind, len(raw), limits,
                                                password_supplied=password is not None)
        self.entries = [{k: v for k, v in entry.items() if not k.startswith("_")}
                        for entry in self._entries]
        first = next((item for item in self._entries
                      if item["_encrypted"] and not item["directory"]), None)
        if first is not None:
            self.read(first["path"])

    def _invoke(self, operation, *switches, member=None, output_limit, encrypted=False):
        try:
            with tempfile.TemporaryDirectory(prefix="qunling-archive-") as temporary:
                archive = Path(temporary) / "archive.bin"
                archive.write_bytes(self.raw)
                # An omitted -p prompts on stdin; a bare -p supplies an empty
                # password. Never expose a supplied credential in process argv.
                command = [self.decoder, operation, "-sccUTF-8", "-bd", "-bse2", "-bsp0",
                           "-mmt=1", *(["-p"] if self._password is None else []),
                           "-t" + self.kind, *switches,
                           "--", str(archive)]
                if member is not None:
                    command.append(member)
                return _run(command, temporary, output_limit, self.limits,
                            password=self._password, encrypted=encrypted)
        except FileError:
            raise
        except (OSError, subprocess.SubprocessError):
            raise FileError("decoder_failed", "原生解压器无法启动或完成读取。") from None

    def read(self, member):
        name = safe_member(member)
        match = next((item for item in self._entries if item["path"] == name), None)
        if match is None or match["directory"]:
            raise FileError("member_not_found", "未找到指定的普通文件成员。")
        raw = self._invoke("x", "-so", "-spd", "-ssc", "-r-",
                           member=match["_original"], output_limit=match["size"],
                           encrypted=match["_encrypted"])
        if len(raw) != match["size"]:
            raise FileError("invalid_archive", "成员实际大小与目录不一致。")
        return raw
