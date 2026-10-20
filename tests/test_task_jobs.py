"""Persistent job quotas and retention; all payloads are synthetic and local."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
import time

import pytest

from qunling.assets import AssetError, AssetStore
from qunling.jobs import JobService
from qunling.scope import Scope
from qunling.tasks import TaskConflict, TaskStatus

SCOPE = Scope("synthetic-workspace", "synthetic-bot", "group:synthetic", "synthetic-actor")
ARGS = {"format": "txt", "filename": "synthetic.txt", "payload": {"text": "synthetic"}}


def age_task(service, task_id, *, days=8):
    old = time.time() - days * 86400
    with sqlite3.connect(service.tasks.path) as db:
        db.execute("UPDATE ql_tasks SET created_at=?,updated_at=? WHERE id=?", (old, old, task_id))


def test_scope_queue_quota_counts_active_but_duplicate_is_idempotent(tmp_path):
    jobs = JobService(tmp_path, scope_active_limit=2)
    first = jobs.submit(SCOPE, "create", ARGS, "first")
    jobs.submit(SCOPE, "create", ARGS, "second")
    assert jobs.submit(SCOPE, "create", ARGS, "first")["task_id"] == first["task_id"]
    with pytest.raises(AssetError, match="数量"):
        jobs.submit(SCOPE, "create", ARGS, "third")
    # Other actors do not consume this scope's own quota.
    other = replace(SCOPE, actor_id="another-synthetic-actor")
    assert jobs.submit(other, "create", ARGS, "third")["status"] == "pending"
    jobs.cancel(SCOPE, first["task_id"])
    assert jobs.submit(SCOPE, "create", ARGS, "third")["status"] == "pending"


def test_global_queue_quota_is_atomic_under_concurrent_submit(tmp_path):
    jobs = JobService(tmp_path, global_active_limit=3)
    def submit(index):
        try:
            return jobs.submit(replace(SCOPE, actor_id=f"actor-{index}"), "create", ARGS, str(index))
        except AssetError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(submit, range(12)))
    assert sum(value is not None for value in outcomes) == 3
    assert sum(len(jobs.tasks.schedulable(scope)) for scope in jobs.tasks.scopes()) == 3


def test_terminal_jobs_still_count_until_retention_prune(tmp_path):
    jobs = JobService(tmp_path, max_jobs=2)
    first = jobs.submit(SCOPE, "create", ARGS, "first")
    jobs.cancel(SCOPE, first["task_id"])
    jobs.submit(SCOPE, "create", ARGS, "second")
    with pytest.raises(AssetError, match="数量"):
        jobs.submit(SCOPE, "create", ARGS, "third")
    age_task(jobs, first["task_id"])
    # submit triggers a bounded retention pass and frees old terminal rows.
    assert jobs.submit(SCOPE, "create", ARGS, "third")["status"] == "pending"
    with pytest.raises(TaskConflict):
        jobs.snapshot(SCOPE, first["task_id"])


def test_byte_quota_reservation_is_atomic_and_overwrite_does_not_double_count(tmp_path):
    jobs = JobService(tmp_path, max_bytes=220)
    first = jobs.submit(SCOPE, "create", ARGS, "first")
    task_id = first["task_id"]
    payload = jobs._read(SCOPE, task_id, "payload")
    jobs._write(SCOPE, task_id, "payload", payload)
    jobs._write(SCOPE, task_id, "payload", payload)
    jobs._write(SCOPE, task_id, "result", {"status": "ok"})
    with pytest.raises(AssetError, match="存储空间"):
        jobs._write(SCOPE, task_id, "result", {"status": "ok", "text": "x" * 200})
    assert jobs._read(SCOPE, task_id, "result") == {"status": "ok"}
    with pytest.raises(AssetError, match="存储空间"):
        jobs.submit(SCOPE, "create", ARGS, "second")
    # Failed staging must not remain in the active queue.
    assert len(jobs.tasks.schedulable(SCOPE)) == 1
    assert sum(task.status == TaskStatus.CANCELLED for task in jobs.tasks.list(SCOPE)) == 1


def test_prune_preserves_every_active_state_and_removes_only_old_final_files(tmp_path):
    jobs = JobService(tmp_path, scope_active_limit=10)
    completed = jobs.submit(SCOPE, "create", ARGS, "completed")
    failed = jobs.submit(SCOPE, "create", ARGS, "failed")
    cancelled = jobs.submit(SCOPE, "create", ARGS, "cancelled")
    pending = jobs.submit(SCOPE, "create", ARGS, "pending")
    running = jobs.submit(SCOPE, "create", ARGS, "running")
    waiting = jobs.tasks.create(SCOPE, "processing", "staged", "waiting", ready=False)
    partial = jobs.submit(SCOPE, "create", ARGS, "partial")
    recent = jobs.submit(SCOPE, "create", ARGS, "recent")
    for snapshot, status in ((completed, TaskStatus.COMPLETED), (failed, TaskStatus.FAILED),
                             (partial, TaskStatus.PARTIAL)):
        lease = jobs.tasks.claim(SCOPE, snapshot["task_id"], "test")
        jobs._write(SCOPE, snapshot["task_id"], "result", {"status": "synthetic"})
        jobs.tasks.finish(lease, status)
    jobs.cancel(SCOPE, cancelled["task_id"])
    jobs.cancel(SCOPE, recent["task_id"])
    jobs.tasks.claim(SCOPE, running["task_id"], "test")
    jobs._write(SCOPE, waiting.id, "payload", {"operation": "create", "args": ARGS})
    for item in (completed, failed, cancelled, pending, running, partial):
        age_task(jobs, item["task_id"])
    age_task(jobs, waiting.id)
    assert jobs.prune() == 3
    for item in (completed, failed, cancelled):
        assert not jobs._path(SCOPE, item["task_id"], "payload").exists()
        assert not jobs._path(SCOPE, item["task_id"], "result").exists()
        with pytest.raises(TaskConflict):
            jobs.tasks.get(SCOPE, item["task_id"])
    for task_id in (pending["task_id"], running["task_id"], partial["task_id"], waiting.id, recent["task_id"]):
        assert jobs._path(SCOPE, task_id, "payload").is_file()
        assert jobs.tasks.get(SCOPE, task_id).id == task_id


def test_prune_releases_byte_reservations(tmp_path):
    jobs = JobService(tmp_path, max_bytes=160)
    first = jobs.submit(SCOPE, "create", ARGS, "first")
    jobs.cancel(SCOPE, first["task_id"])
    age_task(jobs, first["task_id"])
    assert jobs.prune() == 1
    assert jobs.submit(SCOPE, "create", ARGS, "second")["status"] == "pending"


def test_queue_and_prune_queries_do_not_hide_work_behind_newer_final_history(tmp_path):
    jobs = JobService(tmp_path)
    oldest = jobs.submit(SCOPE, "create", ARGS, "oldest")
    queued = jobs.submit(SCOPE, "create", ARGS, "queued")
    jobs.cancel(SCOPE, oldest["task_id"])
    age_task(jobs, oldest["task_id"])
    for index in range(1001):
        task = jobs.tasks.create(SCOPE, "processing", "synthetic", f"history-{index}")
        jobs.tasks.cancel(SCOPE, task.id)
    assert jobs.tasks.schedulable(SCOPE)[0].id == queued["task_id"]
    assert jobs.prune() == 1


def test_job_asset_storage_matches_shared_assets_and_preserves_safe_processing_error(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
        jobs = JobService(tmp_path)
        shared = AssetStore.from_env()
        assert shared.root == jobs.assets.root == tmp_path / "assets"
        item = shared.put(SCOPE, "synthetic.txt", b"synthetic")
        created = jobs.submit(SCOPE, "read", {"asset_ref": item["asset_ref"]}, "read")
        async def fail(*_):
            raise AssetError("needs_ocr", "该页面需要 OCR。")
        monkeypatch.setattr("qunling.jobs.run_operation", fail)
        worker = jobs.worker()
        await worker.run_once()
        await worker.wait_idle()
        final = jobs.snapshot(SCOPE, created["task_id"])
        assert final["status"] == "failed" and final["attempts"] == 1
        assert final["result"]["error_code"] == "needs_ocr"
        assert final["result"]["error"] == "该页面需要 OCR。"
    asyncio.run(scenario())


def test_result_quota_error_reaches_terminal_budget_without_retry_loop(tmp_path, monkeypatch):
    async def scenario():
        jobs = JobService(tmp_path, max_bytes=140)
        submitted = jobs.submit(SCOPE, "create", ARGS, "quota")
        async def large_result(*_):
            return {"result_refs": [], "text": "x" * 500}
        monkeypatch.setattr("qunling.jobs.run_operation", large_result)
        worker = jobs.worker()
        await worker.run_once()
        await worker.wait_idle()
        final = jobs.snapshot(SCOPE, submitted["task_id"])
        assert final["status"] == "failed"
        assert final["error_code"] == "budget" and final["attempts"] == 1
        assert await worker.run_once() == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("keyword", ["max_jobs", "max_bytes", "scope_active_limit", "global_active_limit", "retention_seconds"])
def test_invalid_service_budgets_rejected(tmp_path, keyword):
    with pytest.raises(ValueError):
        JobService(tmp_path, **{keyword: 0})
