"""Private durable file jobs and opt-in delivery through the trusted Host."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

from qunling.assets import AssetError, AssetStore
from qunling.processing import run_operation, validate_operation
from qunling.outbox import Outbox
from qunling.scope import Scope
from qunling.tasks import ErrorCode, ErrorInfo, TaskConflict, TaskFailure, TaskStatus, TaskStore, TaskWorker

MAX_PAYLOAD_BYTES = 528 * 1024  # Validated args are <=512 KiB; envelope overhead only.
MAX_RESULT_BYTES = 128 * 1024
_TASK_ID = re.compile(r"[0-9a-f]{32}")
_ERROR_TEXT = {
    "network": "网络暂不可用。", "timeout": "文件处理超时。",
    "rate_limit": "服务暂时限流。", "service": "处理服务暂不可用。",
    "invalid_input": "文件或参数不受支持，请检查输入。",
    "permission": "当前会话无权读取该文件。", "needs_input": "任务需要补充输入。",
    "delivery_unconfirmed": "发送结果尚未确认。", "budget": "任务已达到执行预算。",
    "cancelled": "任务已取消。", "internal": "文件处理未完成。",
}


class JobService:
    def __init__(self, root: str | Path, *, max_jobs=1024, max_bytes=128 * 1024 * 1024,
                 scope_active_limit=4, global_active_limit=32, retention_seconds=7 * 86400):
        for value in (max_jobs, max_bytes, scope_active_limit, global_active_limit, retention_seconds):
            if type(value) is not int or value < 1:
                raise ValueError("Invalid job service budget")
        self.root = Path(root).absolute()
        self.assets = AssetStore(self.root / "assets")
        self.tasks = TaskStore(self.root / "tasks.sqlite")
        self.outbox = Outbox(self.tasks)
        self.max_jobs, self.max_bytes = max_jobs, max_bytes
        self.scope_active_limit, self.global_active_limit = scope_active_limit, global_active_limit
        self.retention_seconds = retention_seconds
        self.jobs = self.root / "jobs"
        self.jobs.mkdir(mode=0o700, exist_ok=True)
        if self.jobs.is_symlink():
            raise AssetError("invalid_storage", "任务存储目录不可用。")

    def _path(self, scope: Scope, task_id: str, kind: str) -> Path:
        if not isinstance(scope, Scope) or not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise AssetError("invalid_task", "任务编号无效。")
        if kind not in {"payload", "result"}:
            raise ValueError("Invalid job file kind")
        folder = self.jobs / scope.key
        if folder.is_symlink():
            raise AssetError("invalid_storage", "任务存储目录不可用。")
        folder.mkdir(mode=0o700, exist_ok=True)
        target = folder / f"{task_id}.{kind}.json"
        if target.is_symlink() or folder.resolve().parent != self.jobs.resolve():
            raise AssetError("invalid_storage", "任务存储目录不可用。")
        return target

    @staticmethod
    def _encode(value: dict, limit: int) -> bytes:
        try:
            raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        except (TypeError, ValueError, RecursionError):
            raise AssetError("invalid_parameters", "任务数据需要有限的 JSON。") from None
        if len(raw) > limit:
            raise AssetError("input_limit", "任务数据超过大小限制。")
        return raw

    def _write(self, scope, task_id, kind, value):
        path = self._path(scope, task_id, kind)
        raw = self._encode(value, MAX_RESULT_BYTES if kind == "result" else MAX_PAYLOAD_BYTES)
        try:
            self.tasks.reserve_storage(scope, task_id, kind, len(raw), total_limit=self.max_bytes)
        except TaskConflict:
            raise AssetError("task_storage_limit", "任务存储空间已满，请清理旧任务或稍后重试。") from None
        descriptor, temporary = tempfile.mkstemp(prefix=".job-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _read(self, scope, task_id, kind):
        path = self._path(scope, task_id, kind)
        maximum = MAX_RESULT_BYTES if kind == "result" else MAX_PAYLOAD_BYTES
        try:
            with path.open("rb") as stream:
                raw = stream.read(maximum + 1)
            if len(raw) > maximum:
                raise ValueError
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (OSError, ValueError, RecursionError):
            raise AssetError("invalid_task_data", "任务数据不可用或超过限制。") from None

    def submit(self, scope: Scope, operation: str, args: dict, request_key: str) -> dict:
        validate_operation(operation, args)
        if "password" in args:
            raise AssetError("ephemeral_input_required", "带密码的压缩包操作应使用即时处理，不写入持久任务。")
        self.prune()
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 256:
            raise AssetError("invalid_request", "任务请求编号无效。")
        payload = {"operation": operation, "args": args}
        raw = self._encode(payload, MAX_PAYLOAD_BYTES)
        request_digest = hashlib.sha256(request_key.encode() + b"\x00" + raw).hexdigest()
        # The absolute deadline derives from persisted creation time and is
        # fixed when the staged payload becomes runnable.
        try:
            task = self.tasks.create(
                scope, "processing", f"文件操作：{operation}", request_digest,
                max_attempts=2, ready=False, max_tasks=self.max_jobs,
                scope_active_limit=self.scope_active_limit, global_active_limit=self.global_active_limit,
            )
        except TaskConflict:
            raise AssetError("task_capacity_limit", "任务数量已达上限，请等待已有任务完成或取消不再需要的任务。") from None
        if task.status == TaskStatus.WAITING and task.attempts == 0 and task.error is None:
            try:
                self._write(scope, task.id, "payload", payload)
            except (OSError, AssetError, TaskConflict):
                self.tasks.cancel(scope, task.id)
                raise
            self.tasks.activate(scope, task.id, deadline_at=task.created_at + 600)
        return self.snapshot(scope, task.id)

    def _request_matches(self, task, request_key) -> bool:
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 256:
            return False
        try:
            payload = self._read(task.scope, task.id, "payload")
        except AssetError:
            return False  # Concurrent staging must neither leak nor hide other jobs.
        raw = self._encode(payload, MAX_PAYLOAD_BYTES)
        return hashlib.sha256(request_key.encode() + b"\x00" + raw).hexdigest() == task.request_key

    def _visible(self, task, before, request_key) -> bool:
        if before is None:
            return True
        if isinstance(before, bool) or not isinstance(before, (int, float)) or not math.isfinite(before):
            raise ValueError("Invalid task visibility boundary")
        return task.created_at <= before or self._request_matches(task, request_key)

    def _visible_task(self, scope, task_id, before, request_key):
        task = self.tasks.get(scope, task_id)
        if not self._visible(task, before, request_key):
            raise AssetError("task_unavailable", "该任务不属于本轮可访问的任务范围。")
        return task

    def attach_delivery(self, scope: Scope, task_id: str, request_key: str, route: dict) -> dict:
        task = self.tasks.get(scope, task_id)
        payload = self._read(scope, task_id, "payload")
        if payload.get("operation") not in {"create", "extract"} or not self._request_matches(task, request_key):
            raise AssetError("delivery_unavailable", "只有本轮生成或导出的文件可以登记后台交付。")
        self.outbox.attach(scope, task_id, request_key, route)
        return self.snapshot(scope, task_id)

    def snapshot(self, scope: Scope, task_id: str, *, before=None, request_key=None) -> dict:
        task = self._visible_task(scope, task_id, before, request_key)
        value = {
            "task_id": task.id, "status": task.status.value,
            "attempts": task.attempts, "max_attempts": task.max_attempts,
            "result_refs": list(task.result_refs), "stage": task.checkpoint.get("stage"),
        }
        delivery = self.outbox.state(scope, task_id)
        if delivery is not None:
            value["delivery_status"] = delivery
        if task.error is not None:
            value.update(
                error_code=task.error.code.value, error=_ERROR_TEXT[task.error.code.value],
                retryable=task.error.retryable,
            )
        if task.status == TaskStatus.COMPLETED:
            value["result"] = self._read(scope, task_id, "result")
        elif task.status == TaskStatus.FAILED and self._path(scope, task_id, "result").is_file():
            value["result"] = self._read(scope, task_id, "result")
        return value

    def list(self, scope: Scope, *, limit: int = 20, before=None, request_key=None) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid job list limit")
        # Apply visibility before LIMIT so newer invisible jobs cannot hide old
        # visible ones. The durable store already bounds the total job count.
        with self.tasks._db() as db:
            rows = db.execute("SELECT * FROM ql_tasks WHERE scope_key=? ORDER BY created_at DESC,id",
                              (self.tasks._scope(scope),)).fetchall()
        result = []
        for row in rows:
            task = self.tasks._task(row)
            if self._visible(task, before, request_key):
                result.append(self.snapshot(scope, task.id, before=before, request_key=request_key))
                if len(result) == limit:
                    break
        return result

    async def wait(self, scope: Scope, task_id: str, *, seconds: float = 10, before=None, request_key=None) -> dict:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= seconds <= 15:
            raise ValueError("Invalid job wait duration")
        deadline = time.monotonic() + seconds
        while True:
            value = await asyncio.to_thread(self.snapshot, scope, task_id, before=before, request_key=request_key)
            if value["status"] in {"completed", "partial", "failed", "cancelled"}:
                return value
            if value["status"] == "waiting" and not value.get("retryable", False):
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return value
            await asyncio.sleep(min(0.2, remaining))

    def cancel(self, scope: Scope, task_id: str, *, before=None, request_key=None) -> dict:
        self._visible_task(scope, task_id, before, request_key)
        self.outbox.cancel(scope, task_id)
        self.tasks.cancel(scope, task_id)
        return self.snapshot(scope, task_id, before=before, request_key=request_key)

    def prune(self) -> int:
        """Only final jobs older than retention; active payloads are never removed."""
        cutoff = time.time() - self.retention_seconds
        removed = 0
        for scope in self.tasks.scopes():
            for task in self.tasks.expired_final(scope, older_than=cutoff):
                def remove_files():
                    for kind in ("payload", "result"):
                        self._path(scope, task.id, kind).unlink(missing_ok=True)
                removed += self.outbox.prune_final(scope, task.id, older_than=cutoff, remove_files=remove_files)
            folder = self.jobs / scope.key
            if folder.is_dir() and not folder.is_symlink():
                try:
                    folder.rmdir()  # Only an empty scoped directory; never recursive.
                except OSError:
                    pass
        return removed

    async def _handle(self, scope, payload, progress):
        result_path = self._path(scope, progress.task_id, "result")
        if result_path.is_file():
            # Crash after durable result but before ledger completion: reuse it.
            result = self._read(scope, progress.task_id, "result")
            if result.get("status") == "error":
                raise TaskFailure(ErrorInfo(ErrorCode.INVALID_INPUT))
            for ref in result.get("result_refs", []):
                self.assets.get(scope, ref)
            return result
        try:
            await progress.save("processing", {"operation": payload.get("operation")})
            result = await run_operation(self.assets, scope, payload["operation"], payload["args"])
            # Fence cancellation/lease expiry before publishing a result.
            await progress.save("processed", {"result_refs": result.get("result_refs", [])})
            self._write(scope, progress.task_id, "result", result)
            return result
        except AssetError as exc:
            code = ErrorCode.INVALID_INPUT
            if exc.code in {"scope_mismatch", "access_denied"}:
                code = ErrorCode.PERMISSION
            elif exc.code in {"task_storage_limit", "task_capacity_limit"}:
                code = ErrorCode.BUDGET
            # AssetError is a fixed, controlled application diagnostic. Never
            # serialize arbitrary exception text, provider bodies or tracebacks.
            try:
                self._write(scope, progress.task_id, "result", {
                    "status": "error", "error_code": exc.code, "error": str(exc), "result_refs": [],
                })
            except (AssetError, OSError):
                pass  # Even a full disk must still yield a fenced terminal error.
            raise TaskFailure(ErrorInfo(code)) from None
        except (KeyError, TypeError):
            raise TaskFailure(ErrorInfo(ErrorCode.INVALID_INPUT)) from None

    def worker(self, *, concurrency: int = 2) -> TaskWorker:
        return TaskWorker(
            self.tasks, self._handle,
            load_payload=lambda task: self._read(task.scope, task.id, "payload"),
            concurrency=concurrency, max_run_seconds=100, retry_delay=2,
            retry_safe_kinds=frozenset({"processing"}),
        )
