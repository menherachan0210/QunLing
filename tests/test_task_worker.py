"""Async durable jobs with synthetic payloads and no external services."""
import asyncio
import json
from pathlib import Path

import pytest

from qunling.assets import AssetError
from qunling.jobs import JobService
from qunling.scope import Scope
from qunling.tasks import ErrorCode, ErrorInfo, TaskFailure, TaskStatus, TaskStore, TaskWorker


SCOPE = Scope("test-workspace", "test-bot", "group:test", "test-actor")


def test_staged_payload_not_claimed_before_activation(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    task = store.create(SCOPE, "processing", "test", "request", ready=False)
    assert store.claim(SCOPE, task.id, "worker") is None
    store.activate(SCOPE, task.id)
    assert store.claim(SCOPE, task.id, "worker") is not None


def test_worker_enforces_concurrency_and_persists_results(tmp_path):
    async def scenario():
        store = TaskStore(tmp_path / "tasks.sqlite")
        for i in range(4):
            store.create(SCOPE, "processing", "test", str(i))
        active, maximum = 0, 0
        gate = asyncio.Event()

        async def handler(scope, payload, progress):
            nonlocal active, maximum
            assert scope == SCOPE and payload == {"synthetic": True}
            active += 1
            maximum = max(active, maximum)
            await progress.save("working", {"done": 1})
            await gate.wait()
            active -= 1
            return {"result_refs": ["asset-test"]}

        worker = TaskWorker(store, handler, load_payload=lambda _: {"synthetic": True}, concurrency=2)
        assert await worker.run_once() == 2
        assert await worker.run_once() == 0
        gate.set()
        await worker.wait_idle()
        assert await worker.run_once() == 2
        await worker.wait_idle()
        assert maximum <= 2
        assert all(task.status == TaskStatus.COMPLETED for task in store.list(SCOPE))
        assert all(task.result_refs == ("asset-test",) for task in store.list(SCOPE))
    asyncio.run(scenario())


def test_worker_recovers_after_restart_and_reuses_checkpoint(tmp_path):
    async def scenario():
        store = TaskStore(tmp_path / "tasks.sqlite", clock=lambda: 100)
        task = store.create(SCOPE, "processing", "test", "r")
        lease = store.claim(SCOPE, task.id, "dead-worker", lease_seconds=1)
        store.checkpoint(lease, "prepared", {"input_refs": ["test"]})
        restarted = TaskStore(store.path, clock=lambda: 102)

        async def handler(scope, payload, progress):
            assert progress.saved["stage"] == "prepared"
            return {"result_refs": ["test-result"]}

        worker = TaskWorker(restarted, handler, load_payload=lambda _: {}, retry_safe_kinds=frozenset({"processing"}))
        assert await worker.run_once() == 1
        await worker.wait_idle()
        assert restarted.get(SCOPE, task.id).status == TaskStatus.COMPLETED
        assert restarted.get(SCOPE, task.id).attempts == 2
    asyncio.run(scenario())


def test_worker_retry_is_explicit_bounded_and_never_for_invalid_input(tmp_path):
    async def scenario():
        store = TaskStore(tmp_path / "tasks.sqlite")
        task = store.create(SCOPE, "processing", "test", "retry", max_attempts=2)
        calls = 0

        async def handler(scope, payload, progress):
            nonlocal calls
            calls += 1
            raise TaskFailure(ErrorInfo(ErrorCode.NETWORK, retryable=True, can_continue=True))

        worker = TaskWorker(store, handler, load_payload=lambda _: {}, retry_delay=0,
                            retry_safe_kinds=frozenset({"processing"}))
        await worker.run_once()
        await worker.wait_idle()
        assert store.get(SCOPE, task.id).status == TaskStatus.WAITING
        await worker.run_once()
        await worker.wait_idle()
        assert calls == 2 and store.get(SCOPE, task.id).status == TaskStatus.FAILED
        assert await worker.run_once() == 0

        invalid = store.create(SCOPE, "processing", "test", "invalid")
        async def bad_handler(*_):
            raise TaskFailure(ErrorInfo(ErrorCode.INVALID_INPUT))
        worker = TaskWorker(store, bad_handler, load_payload=lambda _: {},
                            retry_safe_kinds=frozenset({"processing"}))
        await worker.run_once()
        await worker.wait_idle()
        assert store.get(SCOPE, invalid.id).attempts == 1
        assert store.get(SCOPE, invalid.id).error.code == ErrorCode.INVALID_INPUT
    asyncio.run(scenario())


def test_timeout_cleans_up_before_retry_is_published(tmp_path):
    async def scenario():
        store = TaskStore(tmp_path / "tasks.sqlite")
        task = store.create(SCOPE, "processing", "test", "r")
        cleaned = False
        async def handler(*_):
            nonlocal cleaned
            try:
                await asyncio.sleep(60)
            finally:
                assert store.get(SCOPE, task.id).status == TaskStatus.RUNNING
                cleaned = True
        worker = TaskWorker(store, handler, load_payload=lambda _: {}, max_run_seconds=0.02,
                            retry_safe_kinds=frozenset({"processing"}))
        await worker.run_once()
        await worker.wait_idle()
        assert cleaned and store.get(SCOPE, task.id).status == TaskStatus.WAITING
    asyncio.run(scenario())


def test_lease_heartbeat_extends_and_cancelled_task_stops_handler(tmp_path):
    async def scenario():
        store = TaskStore(tmp_path / "tasks.sqlite")
        task = store.create(SCOPE, "processing", "test", "r")
        started = asyncio.Event()
        stopped = asyncio.Event()
        async def handler(*_):
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                stopped.set()
        worker = TaskWorker(store, handler, load_payload=lambda _: {}, lease_seconds=1)
        await worker.run_once()
        await started.wait()
        await asyncio.sleep(1.05)
        assert store.recover_expired(SCOPE)["pending"] == 0
        store.cancel(SCOPE, task.id)
        await asyncio.wait_for(stopped.wait(), timeout=1.5)
        await worker.wait_idle()
        assert store.get(SCOPE, task.id).status == TaskStatus.CANCELLED
    asyncio.run(scenario())


def test_job_service_create_result_survives_restart_and_is_scoped(tmp_path):
    async def scenario():
        jobs = JobService(tmp_path)
        args = {"format": "txt", "filename": "合成.txt", "payload": {"text": "synthetic content"}}
        initial = jobs.submit(SCOPE, "create", args, "r")
        assert initial["status"] == "pending"
        duplicate = jobs.submit(SCOPE, "create", args, "r")
        assert initial["task_id"] == duplicate["task_id"]
        worker = jobs.worker()
        await worker.run_once()
        await worker.wait_idle()
        final = JobService(tmp_path).snapshot(SCOPE, initial["task_id"])
        assert final["status"] == "completed", final
        assert final["result_refs"]
        assert jobs.assets.read(SCOPE, final["result_refs"][0]) == b"synthetic content"
        wrong = Scope("other", "test-bot", "group:test", "test-actor")
        assert jobs.list(wrong) == []
        with pytest.raises(Exception):
            jobs.snapshot(wrong, final["task_id"])
        with pytest.raises(ValueError):
            await jobs.wait(SCOPE, final["task_id"], seconds=16)
        assert (await jobs.wait(SCOPE, final["task_id"], seconds=0))["status"] == "completed"
    asyncio.run(scenario())


def test_job_payload_and_result_bounded(tmp_path):
    jobs = JobService(tmp_path)
    with pytest.raises(AssetError):
        jobs.submit(SCOPE, "create", {"format": "txt", "filename": "test.txt", "payload": {"text": "x" * 600000}}, "r")
    task = jobs.submit(SCOPE, "create", {"format": "txt", "filename": "test.txt", "payload": {"text": "x"}}, "r")
    with pytest.raises(AssetError):
        jobs._write(SCOPE, task["task_id"], "result", {"large": "x" * 131072})
    payload = jobs._read(SCOPE, task["task_id"], "payload")
    assert payload["operation"] == "create"
    assert not any(".job-" in path.name for path in Path(tmp_path).rglob("*"))
    # A private file with unexpected shape is rejected, not treated as job input.
    jobs._path(SCOPE, task["task_id"], "payload").write_text(json.dumps(["bad"]))
    with pytest.raises(AssetError):
        jobs._read(SCOPE, task["task_id"], "payload")
