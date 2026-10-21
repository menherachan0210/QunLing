"""The same nonblocking file lock used by update and maintenance workers."""
from contextlib import contextmanager
import os


@contextmanager
def worker_lock(directory):
    path = directory / "worker.lock"
    if path.is_symlink():
        yield False
        return
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if not path.stat().st_size:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            acquire = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: (stream.seek(0), msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            import fcntl
            acquire = lambda: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(stream, fcntl.LOCK_UN)
        try:
            acquire()
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            release()
