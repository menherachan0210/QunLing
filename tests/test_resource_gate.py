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


def test_clean_inactive_cache_does_not_permanently_block_file_worker(tmp_path):
    (tmp_path / "meminfo").write_text("MemTotal: 2097152 kB\nMemAvailable: 1700000 kB\n")
    group = tmp_path / "cgroup"
    group.mkdir()
    (group / "memory.max").write_text(str(512 * 1024**2))
    (group / "memory.current").write_text(str(400 * 1024**2))
    (group / "memory.stat").write_text(
        f"inactive_file {352 * 1024**2}\nfile_dirty {8 * 1024**2}\nfile_writeback {8 * 1024**2}\n")
    sample = pressure(tmp_path, group)
    assert sample.local_available_mib == 448
    gate = ResourceGate(tmp_path / "gate", sample=lambda: sample)
    assert gate.try_acquire(gate.ticket("file")) is not None
    # Anonymous allocations and dirty/writeback pages are never discounted.
    (group / "memory.stat").write_text(f"inactive_file {352 * 1024**2}\nfile_dirty {352 * 1024**2}\n")
    assert pressure(tmp_path, group).local_available_mib == 112


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


def test_small_vision_fits_normal_bot_headroom_without_overbooking(tmp_path):
    sample = Pressure(1930, 540, some=.87, full=.75, local_available_mib=312)
    gate = ResourceGate(tmp_path, sample=lambda: sample)
    old = gate.ticket("vision")
    assert gate.try_acquire(old) is None
    gate.release(old)
    assert gate.try_acquire(gate.ticket("vision_small")) is not None
    assert gate.try_acquire(gate.ticket("vision_small")) is not None
    third = gate.ticket("vision_small")
    # Even abundant host RAM cannot let reserved child budgets overbook the
    # remaining memory inside this container before the children allocate it.
    sample = Pressure(1930, 1500, local_available_mib=312)
    assert gate.try_acquire(third) is None


@pytest.mark.parametrize("sample", [
    Pressure(1930, 383, local_available_mib=500),
    Pressure(1930, 1500, local_available_mib=159),
    Pressure(1930, 1500, some=25, local_available_mib=500),
    Pressure(1930, 1500, full=5, local_available_mib=500),
])
def test_small_vision_still_respects_memory_and_pressure_limits(tmp_path, sample):
    gate = ResourceGate(tmp_path, sample=lambda: sample)
    assert gate.try_acquire(gate.ticket("vision_small")) is None


def test_old_ledger_migrates_without_losing_active_reservations(tmp_path):
    path = tmp_path / 'resource-admission.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE permits (seq INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE, weight INTEGER, active INTEGER, expires REAL)')
        db.execute('INSERT INTO permits(token,weight,active,expires) VALUES(?,?,?,?)', ('existing', 384, 1, 200))
    gate = ResourceGate(tmp_path, sample=lambda: Pressure(1930, 1500), clock=lambda: 100)
    assert gate.try_acquire(gate.ticket('model_media_small')) is not None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT weight,active,kind FROM permits WHERE token='existing'").fetchone() == (384, 1, '')
        assert db.execute('SELECT sum(weight) FROM permits WHERE active=1').fetchone()[0] == 416


def test_light_transfers_do_not_wait_behind_unadmittable_file_work(tmp_path):
    gate = ResourceGate(tmp_path, sample=lambda: Pressure(1930, 493, full=5.04, local_available_mib=178))
    file_ticket = gate.ticket('file')
    assert gate.try_acquire(file_ticket) is None
    first, second = gate.ticket('model_media_small'), gate.ticket('model_media_small')
    assert gate.try_acquire(second) is None  # FIFO within transfers remains.
    assert gate.try_acquire(first) is not None
    assert gate.try_acquire(second) is not None
    assert gate.try_acquire(file_ticket) is None


@pytest.mark.parametrize('kind,sample', [
    ('model_media_small', Pressure(1930, 287, local_available_mib=1000)),
    ('model_media_small', Pressure(1930, 1500, local_available_mib=63)),
    ('model_media_medium', Pressure(1930, 319, local_available_mib=1000)),
    ('model_media_medium', Pressure(1930, 1500, local_available_mib=95)),
    ('model_media', Pressure(1930, 1500, full=5, local_available_mib=1000)),
])
def test_transfers_keep_memory_guards_and_large_payloads_keep_pressure_guard(tmp_path, kind, sample):
    gate = ResourceGate(tmp_path, sample=lambda: sample)
    assert gate.try_acquire(gate.ticket(kind)) is None


def test_failure_evidence_is_numeric_bounded_and_excludes_ticket_identity(tmp_path):
    gate = ResourceGate(tmp_path, sample=lambda: Pressure(1930, 100, local_available_mib=40))
    token = gate.ticket('model_media_small')
    assert gate.try_acquire(token) is None
    for _ in range(135):
        gate.record_denial('model_media_small')
    with sqlite3.connect(gate.path) as db:
        rows = db.execute('SELECT kind,evidence FROM denials').fetchall()
    assert len(rows) == 128 and token not in str(rows)
    import json
    evidence = json.loads(rows[-1][1])
    assert evidence['reasons'] == ['container_memory', 'host_memory']
    assert evidence['local_available_mib'] == 40 and evidence['weight_mib'] == 32
