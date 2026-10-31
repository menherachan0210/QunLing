"""Cross-process admission for heavy work; ordinary chat never takes this gate.

Only opaque tickets and resource estimates are persisted, never task contents.
All containers use the same private data volume. Expiring, heartbeating leases
recover capacity after process/container death without replaying any operation.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager, closing
from contextvars import ContextVar
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import sqlite3
import time

MIB = 1024 * 1024
WEIGHTS = {"file": 384, "image": 192, "vision": 384, "model_media": 128}
_held = ContextVar("qunling_resource_lease", default=None)


class ResourceBusy(Exception):
    """Admission timed out before the operation started; safe to report as busy."""


async def finish_thread(function, *args):
    """Do not release a memory reservation while a cancelled CPU thread runs."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


@dataclass(frozen=True)
class Pressure:
    total_mib: int = 2048
    available_mib: int | None = None
    some: float = 0
    full: float = 0
    local_available_mib: int | None = None


def pressure(proc=Path("/proc"), cgroup=Path("/sys/fs/cgroup")) -> Pressure:
    total, available, some, full = 2048, None, 0., 0.
    try:
        fields = {line.split(":", 1)[0]: int(line.split()[1]) // 1024
                  for line in (proc / "meminfo").read_text().splitlines()}
        total, available = fields["MemTotal"], fields["MemAvailable"]
    except (OSError, ValueError, KeyError, IndexError):
        # Native Windows still has a bounded global pool; Linux adds PSI and
        # actual available-memory admission. Old swap usage is NOT pressure.
        pass
    for path in (proc / "pressure/memory", cgroup / "memory.pressure"):
        try:
            for line in path.read_text().splitlines():
                parts = line.split()
                value = float(dict(part.split("=", 1) for part in parts[1:])["avg10"])
                if parts[0] == "some":
                    some = max(some, value)
                elif parts[0] == "full":
                    full = max(full, value)
        except (OSError, ValueError, KeyError, IndexError):
            pass
    local_available = None
    try:
        # Docker's MemAvailable describes the host, not this container's limit.
        maximum = int((cgroup / "memory.max").read_text())
        current = int((cgroup / "memory.current").read_text())
        local_available = max(0, maximum - current) // MIB
    except (OSError, ValueError):
        pass
    return Pressure(total, available, some, full, local_available)


class ResourceGate:
    def __init__(self, root, *, sample=pressure, clock=time.time, ttl=30, max_waiters=32):
        self.path = Path(root) / "resource-admission.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("Invalid resource ledger")
        self.sample, self.clock, self.ttl, self.max_waiters = sample, clock, ttl, max_waiters
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS permits (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL,
                weight INTEGER NOT NULL, active INTEGER NOT NULL, expires REAL NOT NULL)""")
        os.chmod(self.path, 0o600)

    @contextmanager
    def _db(self):
        with closing(sqlite3.connect(self.path, timeout=2)) as db:
            db.execute("PRAGMA busy_timeout=2000")
            with db:
                yield db

    def ticket(self, kind, token=None):
        weight = WEIGHTS[kind]
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM permits WHERE expires<=?", (self.clock(),))
            if db.execute("SELECT count(*) FROM permits WHERE active=0").fetchone()[0] >= self.max_waiters:
                raise ResourceBusy("Heavy-work queue is full")
            token = token or secrets.token_hex(16)
            db.execute("INSERT INTO permits(token,weight,active,expires) VALUES(?,?,0,?)",
                       (token, weight, self.clock() + self.ttl))
        return token

    def try_acquire(self, token):
        sample, now = self.sample(), self.clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM permits WHERE expires<=?", (now,))
            row = db.execute("SELECT seq,weight,active FROM permits WHERE token=?", (token,)).fetchone()
            if row is None or row[2]:
                raise ResourceBusy("Resource ticket expired")
            db.execute("UPDATE permits SET expires=? WHERE token=?", (now + self.ttl, token))
            first = db.execute("SELECT seq FROM permits WHERE active=0 ORDER BY seq LIMIT 1").fetchone()[0]
            used = db.execute("SELECT coalesce(sum(weight),0) FROM permits WHERE active=1").fetchone()[0]
            budget = max(512, min(2048, sample.total_mib // 4))
            if (row[0] != first or used + row[1] > budget or sample.some >= 25 or sample.full >= 5
                    or (sample.local_available_mib is not None and sample.local_available_mib < 32 + row[1])
                    or (sample.available_mib is not None and sample.available_mib < 256 + used + row[1])):
                return None
            db.execute("UPDATE permits SET active=1 WHERE token=?", (token,))
        return Permit(self, token, row[1])

    def renew(self, token):
        now = self.clock()
        with self._db() as db:
            changed = db.execute("UPDATE permits SET expires=? WHERE token=? AND active=1 AND expires>?",
                                 (now + self.ttl, token, now)).rowcount
        if not changed:
            raise ResourceBusy("Resource lease lost")

    def release(self, token):
        with self._db() as db:
            db.execute("DELETE FROM permits WHERE token=?", (token,))


class Permit:
    def __init__(self, gate, token, weight):
        self.gate, self.token, self.weight = gate, token, weight
        self.active = False

    async def __aenter__(self):
        owner = asyncio.current_task()

        async def heartbeat():
            try:
                while True:
                    await asyncio.sleep(self.gate.ttl / 5)
                    await asyncio.to_thread(self.gate.renew, self.token)
            except Exception:
                # Stop work if its reservation is no longer valid. Never allow
                # an expired worker and its replacement to keep allocating.
                owner.cancel()

        self.context = _held.set(self)
        self.active = True
        self.heartbeat = asyncio.create_task(heartbeat())
        return self

    async def __aexit__(self, *exc):
        self.active = False
        self.heartbeat.cancel()
        await asyncio.gather(self.heartbeat, return_exceptions=True)
        _held.reset(self.context)
        await asyncio.to_thread(self.gate.release, self.token)


class FileAdmission:
    """A worker has one FIFO ticket, acquired BEFORE claiming a durable task."""
    def __init__(self, gate):
        self.gate, self.token = gate, None

    def __call__(self, task):
        try:
            if self.token is None:
                self.token = self.gate.ticket("file")
            permit = self.gate.try_acquire(self.token)
        except ResourceBusy:
            self.token = None
            return None
        if permit is not None:
            self.token = None
        return permit

    def close(self):
        if self.token is not None:
            self.gate.release(self.token)
            self.token = None


@asynccontextmanager
async def resource_slot(kind, *, wait_seconds=45, root=None):
    root = root or os.environ.get("QUNLING_DATA_DIR")
    held = _held.get()
    if not root or (held is not None and held.active and held.weight >= WEIGHTS[kind]
                    and held.gate.path.parent == Path(root)):
        yield
        return
    gate = await asyncio.to_thread(ResourceGate, root)
    token = secrets.token_hex(16)
    try:
        await finish_thread(gate.ticket, kind, token)
        deadline = time.monotonic() + wait_seconds
        while True:
            permit = await finish_thread(gate.try_acquire, token)
            if permit is not None:
                async with permit:
                    yield
                return
            if time.monotonic() >= deadline:
                raise ResourceBusy("Heavy-work admission timed out")
            await asyncio.sleep(min(.5, max(0, deadline - time.monotonic())))
    finally:
        await asyncio.to_thread(gate.release, token)
