# Adapted unchanged from QQComicTranslator: apply OS limits before native decoding.
import os
import subprocess
import sys
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
