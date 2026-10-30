"""Supervise LangBot and the SDK-free file worker as one service lifetime."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def windows_job():
    """Kill descendant processes if a hidden Windows supervisor is force-stopped."""
    if os.name != "nt":
        return None
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
    handle, value = kernel.CreateJobObjectW(None, None), Extended()
    value.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if (not handle or not kernel.SetInformationJobObject(handle, 9, c.byref(value), c.sizeof(value))
            or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess())):
        raise RuntimeError("Could not establish managed Windows process ownership")
    return handle  # Retained until supervisor exits; do not close while children live.


def _start(command, cwd, env):
    options = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
               else {"start_new_session": True})
    return subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, **options)


def _stop(process, grace=15):
    if process is None:
        return
    if os.name == "nt" and process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    except ProcessLookupError:
        pass


def supervise(app_command, daemon_command, *, cwd, env, stop=None, install_signals=True,
              daemon_restart_delay=1, grace=15):
    """Fixed child commands only; exit with Host, restart an unexpectedly lost daemon."""
    stop = stop or threading.Event()
    previous = {}
    if install_signals:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
    app = daemon = None
    try:
        if daemon_command is not None:
            daemon = _start(daemon_command, cwd, env)
        app = _start(app_command, cwd, env)
        failures = []
        while not stop.wait(0.1):
            if app.poll() is not None:
                return app.returncode
            if daemon is not None and daemon.poll() is not None:
                now = time.monotonic()
                failures = [t for t in failures if now-t < 60]
                failures.append(now)
                if len(failures) >= 5:
                    print("File task worker repeatedly exited; restarting the managed service.", flush=True)
                    return 1
                print("File task worker exited; restarting with persisted task recovery.", flush=True)
                if stop.wait(daemon_restart_delay):
                    break
                daemon = _start(daemon_command, cwd, env)
        return 0
    finally:
        # Stop work first so no task writes outlive the managed Host.
        _stop(daemon, grace)
        _stop(app, grace)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    os.umask(0o077)
    root = ROOT.resolve()
    app = root / "langbot"
    env = os.environ.copy()
    env.setdefault("QUNLING_DATA_DIR", str(app / "data/qunling"))
    # Independent group members use independent sessions. Bound foreground
    # work and its queue so a busy group cannot start one pipeline per person.
    # Lightweight participation retains its own bounded model slots.
    env.setdefault("CONCURRENCY__PIPELINE", "2")
    env.setdefault("CONCURRENCY__PENDING_QUERIES", "64")
    env.setdefault("CONCURRENCY__PENDING_QUERIES_PER_WORKSPACE", "32")
    if not Path(env["QUNLING_DATA_DIR"]).is_absolute():
        raise SystemExit("QUNLING_DATA_DIR must be an absolute private data directory")
    env["PYTHONPATH"] = str(root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    decoder_dir = root / ".local/tools/7zip" / ("windows-x64" if os.name == "nt" else "linux-x64")
    if decoder_dir.is_dir():
        env["PATH"] = str(decoder_dir) + os.pathsep + env.get("PATH", os.defpath)
    handle = windows_job()
    # The Host and plugin Runtime have not started: no memory writer can race
    # an index conversion. Failure stops startup with the old data retained.
    sys.path.insert(0, str(root))
    from qunling.memory_upgrade import before_start
    migrated = before_start(app)
    if migrated:
        print(f"Verified Chinese memory indexes: {len(migrated)} collections.", flush=True)
    # Keep the Job object handle alive for all descendants on Windows.
    _ = handle
    daemon = None if env.get("QUNLING_EXTERNAL_FILE_WORKER") == "1" else [sys.executable, "-u", "-m", "qunling.daemon"]
    return supervise([sys.executable, "-u", "main.py"], daemon,
                     cwd=app, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
