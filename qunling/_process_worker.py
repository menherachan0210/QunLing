"""Trusted parser entry point; inputs are data, never Python source."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

# -I deliberately ignores PYTHONPATH and the working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def limits(*, memory_bytes=768 * 1024 * 1024, cpu_seconds=45,
           file_bytes=24 * 1024 * 1024, max_processes=8):
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
        handle = kernel.CreateJobObjectW(None, None)
        value = Extended()
        value.basic.flags = 0x200 | 0x2000 | 0x8 | 0x4
        value.basic.active = max_processes  # includes bounded 7z child helpers
        value.basic.job_time = cpu_seconds * 10_000_000
        value.job_memory = memory_bytes
        if not handle or not kernel.SetInformationJobObject(handle, 9, c.byref(value), c.sizeof(value)):
            raise RuntimeError
        if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
            raise RuntimeError
        return handle
    import resource
    for kind, requested in (
        (resource.RLIMIT_AS, memory_bytes), (resource.RLIMIT_CPU, cpu_seconds),
        (resource.RLIMIT_FSIZE, file_bytes), (resource.RLIMIT_NOFILE, 64),
    ):
        hard = resource.getrlimit(kind)[1]
        cap = requested if hard == resource.RLIM_INFINITY else min(hard, requested)
        resource.setrlimit(kind, (cap, cap))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return None


def main():
    handle = limits()  # retain the Windows Job handle for this process lifetime
    from qunling.files import (FileError, export_file, inspect_file, read_archive_member,
                               read_file, search_file)
    from qunling.assets import safe_name
    from qunling.files.cache import exchange
    root = Path.cwd()
    raw = (root / "request.json").read_bytes()
    if len(raw) > 60 * 1024 * 1024:
        raise ValueError
    request = json.loads(raw)
    operation, args = request["operation"], request["args"]
    password = None
    if request.get("password_supplied"):
        secret_input = sys.stdin.buffer.read(8193)
        if len(secret_input) > 8192:
            raise FileError("invalid_parameters", "密码输入超过限制。")
        password = json.loads(secret_input)["password"]
    candidate = None
    cached = root / "parsed-input.json"
    if request.get("parsed_cache") and password is None and cached.is_file() and cached.stat().st_size <= 4 * 1024 * 1024:
        try:
            candidate = json.loads(cached.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    with exchange(candidate) as parsed:
        result = dispatch(operation, args, request, root, password)
    if request.get("parsed_cache") and password is None and parsed["result"] is not None:
        encoded_cache = json.dumps(parsed["result"], ensure_ascii=False).encode()
        if len(encoded_cache) <= 4 * 1024 * 1024:
            (root / "parsed-output.json").write_bytes(encoded_cache)
    encoded = json.dumps({"ok": True, "result": result, "parse_cache_hit": parsed["hit"]}, ensure_ascii=False).encode()
    if len(encoded) > 128 * 1024:
        raise FileError("result_limit", "结构结果过大，请分页读取文件。")
    (root / "result.json").write_bytes(encoded)
    return handle


def dispatch(operation, args, request, root, password):
    from qunling.files import (FileError, export_file, inspect_file, read_archive_member,
                               read_file, search_file)
    from qunling.assets import safe_name
    if operation == "create":
        if args["format"] == "gif":
            from qunling.animation import render
            inputs = {ref: (root / safe_name(name)).read_bytes()
                      for ref, name in request.get("animation_inputs", {}).items()}
            result = render(args["payload"], inputs, root / "output")
        else:
            sources = {key: root / safe_name(name) for key, name in request.get("zip_inputs", {}).items()}
            result = export_file(args["format"], args["payload"], root / "output", staged_sources=sources)
    else:
        source = root / safe_name(request["input_name"])
        if operation == "inspect":
            result = inspect_file(source, password=password)
        elif operation == "read":
            result = read_file(source, args.get("selector"), password=password)
        elif operation == "search":
            result = search_file(source, args["query"], args.get("selector"),
                                 limit=args.get("limit", 20), password=password)
        elif operation == "extract":
            content = read_archive_member(source, args["member"], password=password)
            (root / "output").write_bytes(content)
            result = {"size": len(content)}
        else:
            raise FileError("invalid_operation", "文件操作无效。")
    return result


if __name__ == "__main__":
    try:
        job_handle = main()
    except BaseException as error:
        from qunling.files import FileError
        from qunling.assets import AssetError
        known = isinstance(error, (FileError, AssetError))
        Path("result.json").write_text(json.dumps({
            "ok": False, "error_code": error.code if known else "processing_failed",
            "error": str(error) if known else "文件处理未完成，可能是损坏、资源限制或不支持的结构。",
        }, ensure_ascii=False), encoding="utf-8")
