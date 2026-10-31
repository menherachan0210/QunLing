import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import time

import pytest

from qunling.resource_gate import ResourceGate, Pressure, FileAdmission, ResourceBusy, pressure, resource_slot, finish_thread
from qunling.scope import Scope
from qunling.tasks import TaskStore, TaskWorker, TaskStatus


def test_pressure_uses_available_and_psi_not_total_swap(tmp_path):
    (tmp_path / "meminfo").write_text("MemTotal: 2097152 kB\nMemAvailable: 700000 kB\nSwapTotal: 2097152 kB\nSwapFree: 0 kB\n")
    (tmp_path / "pressure").mkdir()
    (tmp_path / "pressure/memory").write_text("some avg10=26.00 avg60=2.00 total=1000\nfull avg10=6.00 avg60=0.0 total=100\n")
    value = pressure(tmp_path, tmp_path / "cgroup")
    assert value.total_mib == 2048 and value.available_mib == 683
    assert value.some == 26 and value.full == 6


def test_container_headroom_and_pressure_override_a_healthy_host(tmp_path):
    (tmp_path / "meminfo").write_text("MemTotal: 2097152 kB\nMemAvailable: 1500000 kB\n")
    group = tmp_path / "cgroup"
    group.mkdir()
    (group / "memory.max").write_text(str(1152 * 1024**2))
    (group / "memory.current").write_text(str(900 * 1024**2))
    (group / "memory.pressure").write_text("some avg10=28.00 total=1000\nfull avg10=7.00 total=100\n")
    sampled = pressure(tmp_path, group)
    assert sampled.local_available_mib == 252
    assert sampled.some == 28 and sampled.full == 7
    gate = ResourceGate(tmp_path / "gate", sample=lambda: sampled)
    ticket = gate.ticket("vision")
    assert gate.try_acquire(ticket) is None
    (group / "memory.pressure").write_text("some avg10=0 total=1000\nfull avg10=0 total=100\n")
    sampled = pressure(tmp_path, group)
    assert gate.try_acquire(ticket) is None  # Host free RAM cannot bypass the container cap.
    (group / "memory.current").write_text(str(600 * 1024**2))
    sampled = pressure(tmp_path, group)
    assert gate.try_acquire(ticket) is not None


def test_unlimited_cgroup_has_no_false_zero_capacity(tmp_path):
    (tmp_path / "memory.max").write_text("max")
    (tmp_path / "memory.current").write_text("1")
    assert pressure(tmp_path / "absent", tmp_path).local_available_mib is None


def test_fifo_capacity_and_pressure_across_independent_instances(tmp_path):
    sample = Pressure(2048, 1500)
    gate = ResourceGate(tmp_path, sample=lambda: sample)
    other = ResourceGate(tmp_path, sample=lambda: sample)
    first, second = gate.ticket("file"), other.ticket("image")
    assert other.try_acquire(second) is None  # Cannot overtake oldest ticket.
    sample = Pressure(2048, 600)
    assert gate.try_acquire(first) is None
    sample = Pressure(2048, 1500, full=5)
    assert gate.try_acquire(first) is None
    sample = Pressure(2048, 1500)
    permit = gate.try_acquire(first)
    assert permit is not None and other.try_acquire(second) is None
    gate.release(first)
    assert other.try_acquire(second) is not None


def test_no_oversubscription_under_competing_connections(tmp_path):
    gate = ResourceGate(tmp_path, sample=lambda: Pressure(2048, 2000))
    tokens = [gate.ticket("image") for _ in range(12)]
    def attempt(token):
        return ResourceGate(tmp_path, sample=lambda: Pressure(2048, 2000)).try_acquire(token)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(attempt, tokens))
    assert 1 <= sum(value is not None for value in results) <= 2
    with sqlite3.connect(gate.path) as db:
        assert db.execute("SELECT sum(weight) FROM permits WHERE active=1").fetchone()[0] <= 512


def test_dead_worker_and_abandoned_queue_expire(tmp_path):
    now = 100
    gate = ResourceGate(tmp_path, sample=lambda: Pressure(2048, 2000), clock=lambda: now, max_waiters=1)
    first = gate.ticket("file")
    with pytest.raises(ResourceBusy):
        gate.ticket("image")
    gate.try_acquire(first)
    second = gate.ticket("file")
    now += 31
    with pytest.raises(ResourceBusy):
        gate.renew(first)
    newest = gate.ticket("image")
    assert gate.try_acquire(newest) is not None
    with pytest.raises(ResourceBusy):
        gate.try_acquire(second)


def test_worker_waits_before_claim_and_still_expires_overdue_jobs(tmp_path):
    async def scenario():
        now = 100
        sample = Pressure(2048, 200)
        gate = ResourceGate(tmp_path, clock=lambda: now, sample=lambda: sample)
        store = TaskStore(tmp_path / "tasks.sqlite", clock=lambda: now)
        scope = Scope("workspace", "bot", "group:test", "member")
        first = store.create(scope, "processing", "test", "a", deadline_at=110)
        second = store.create(scope, "processing", "test", "b", deadline_at=200)
        calls = []
        async def handler(*args):
            calls.append(1)
            await asyncio.sleep(.01)
            return {"result_refs": []}
        worker = TaskWorker(store, handler, load_payload=lambda _: {}, admission=FileAdmission(gate))
        assert await worker.run_once() == 0
        assert store.get(scope, first.id).attempts == 0 and not calls
        now = 111
        assert await worker.run_once() == 0
        assert store.get(scope, first.id).status == TaskStatus.FAILED
        sample = Pressure(2048, 2000)
        assert await worker.run_once() == 1
        await worker.wait_idle()
        await worker.close()
        assert store.get(scope, second.id).status == TaskStatus.COMPLETED
        assert store.get(scope, second.id).attempts == 1 and len(calls) == 1
        with sqlite3.connect(gate.path) as db:
            assert db.execute("SELECT count(*) FROM permits").fetchone()[0] == 0
    asyncio.run(scenario())


def test_cancellation_releases_ticket_and_waits_for_cpu_thread(tmp_path, monkeypatch):
    import qunling.resource_gate as module
    original = module.ResourceGate
    monkeypatch.setattr(module, "ResourceGate", lambda root: original(root, sample=lambda: Pressure(2048, 2000)))
    async def scenario():
        entered = asyncio.Event()
        finished = []
        def work():
            time.sleep(.1)
            finished.append(True)
        async def job():
            async with resource_slot("image", root=tmp_path):
                entered.set()
                await finish_thread(work)
        task = asyncio.create_task(job())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished
        with sqlite3.connect(tmp_path / "resource-admission.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM permits").fetchone()[0] == 0
    asyncio.run(scenario())


def test_busy_image_is_not_started_and_queue_timeout_releases_ticket(tmp_path, monkeypatch):
    import qunling.resource_gate as module
    original = module.ResourceGate
    monkeypatch.setattr(module, "ResourceGate", lambda root: original(root, sample=lambda: Pressure(2048, 100)))
    async def scenario():
        with pytest.raises(ResourceBusy):
            async with resource_slot("image", root=tmp_path, wait_seconds=.02):
                pytest.fail("A provider request must not start under pressure")
        with sqlite3.connect(tmp_path / "resource-admission.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM permits").fetchone()[0] == 0
    asyncio.run(scenario())
