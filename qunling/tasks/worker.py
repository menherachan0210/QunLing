"""Bounded async job execution; transport delivery is deliberately absent."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
from typing import Any, Awaitable, Callable
import uuid

from qunling.scope import Scope
from .models import ErrorCode, ErrorInfo, Lease, LeaseLost, Task, TaskConflict, TaskStatus
from .store import TaskStore


class TaskFailure(Exception):
    """A handler reports a safe category, never a raw exception or request."""
    def __init__(self, error: ErrorInfo):
        self.error = error
        super().__init__(error.code.value)


@dataclass
class Progress:
    """Handler checkpoint handle. Payload remains in the caller's private store."""
    task_id: str
    saved: dict[str, Any]
    _store: TaskStore
    _lease: Lease

    async def save(self, stage: str, data: dict[str, Any]) -> None:
        await asyncio.to_thread(self._store.checkpoint, self._lease, stage, data)
        self.saved = {"stage": stage, "data": data}


Handler = Callable[[Scope, dict[str, Any], Progress], Awaitable[dict[str, Any]]]


class TaskWorker:
    def __init__(
        self, store: TaskStore, handler: Handler, *, load_payload: Callable[[Task], Any],
        worker_id: str | None = None, concurrency: int = 2, lease_seconds: int = 60,
        max_run_seconds: float = 300, retry_delay: float = 2,
        retry_safe_kinds: frozenset[str] = frozenset(),
    ):
        if type(concurrency) is not int or not 1 <= concurrency <= 8:
            raise ValueError("Invalid worker concurrency")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("Invalid worker lease duration")
        if not 0 < max_run_seconds <= 3600 or not 0 <= retry_delay <= 300:
            raise ValueError("Invalid worker time budget")
        self.store, self.handler, self.load_payload = store, handler, load_payload
        self.worker_id = worker_id or "worker-" + uuid.uuid4().hex
        self.concurrency, self.lease_seconds = concurrency, lease_seconds
        self.max_run_seconds, self.retry_delay = max_run_seconds, retry_delay
        self.retry_safe_kinds = frozenset(retry_safe_kinds)
        self._active: set[asyncio.Task] = set()
        self._pump_lock = asyncio.Lock()

    async def _heartbeat(self, lease: Lease) -> None:
        while True:
            await asyncio.sleep(min(5, self.lease_seconds / 3))
            await asyncio.to_thread(self.store.heartbeat, lease, lease_seconds=self.lease_seconds)

    async def _perform(self, task: Task, lease: Lease) -> dict:
        if inspect.iscoroutinefunction(self.load_payload):
            payload = await self.load_payload(task)
        else:
            payload = await asyncio.to_thread(self.load_payload, task)
            if inspect.isawaitable(payload):
                payload = await payload
        if not isinstance(payload, dict):
            raise TaskFailure(ErrorInfo(ErrorCode.INVALID_INPUT))
        progress = Progress(task.id, task.checkpoint, self.store, lease)
        result = await self.handler(task.scope, payload, progress)
        if not isinstance(result, dict) or not isinstance(result.get("result_refs", []), (list, tuple)):
            raise TaskFailure(ErrorInfo(ErrorCode.INTERNAL))
        return result

    async def _record_error(self, task: Task, lease: Lease, error: ErrorInfo) -> None:
        safe = task.kind in self.retry_safe_kinds and error.retryable
        try:
            if safe:
                await asyncio.to_thread(
                    self.store.defer, lease, error, retry_at=self.store.clock() + self.retry_delay,
                )
            elif error.code in {ErrorCode.NEEDS_INPUT, ErrorCode.PERMISSION, ErrorCode.DELIVERY_UNCONFIRMED}:
                await asyncio.to_thread(self.store.defer, lease, ErrorInfo(error.code))
            else:
                await asyncio.to_thread(self.store.finish, lease, TaskStatus.FAILED, error=ErrorInfo(error.code))
        except (LeaseLost, TaskConflict):
            pass  # Cancellation/new lease wins. Never write an unfenced fallback.

    async def _run(self, task: Task, lease: Lease) -> None:
        execution = asyncio.create_task(self._perform(task, lease))
        heartbeat = asyncio.create_task(self._heartbeat(lease))
        timeout = self.max_run_seconds
        if task.deadline_at is not None:
            timeout = min(timeout, max(0, task.deadline_at - self.store.clock()))
        try:
            done, _ = await asyncio.wait({execution, heartbeat}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                heartbeat.result()  # A lost lease aborts/cancels the operation.
                raise LeaseLost("Task lease is no longer valid")
            if execution not in done:
                raise TimeoutError
            result = execution.result()
            await asyncio.to_thread(
                self.store.finish, lease, result_refs=tuple(result.get("result_refs", ())),
            )
        except (LeaseLost, TaskConflict):
            pass
        except TaskFailure as exc:
            await self._record_error(task, lease, exc.error)
        except TimeoutError:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            await self._record_error(task, lease, ErrorInfo(ErrorCode.TIMEOUT, retryable=True, can_continue=True))
        except asyncio.CancelledError:
            # Shutdown is not user cancellation. Leave the lease to expire;
            # recovery respects checkpoints, receipts and original budgets.
            raise
        except Exception:
            await self._record_error(task, lease, ErrorInfo(ErrorCode.INTERNAL))
        finally:
            execution.cancel()
            heartbeat.cancel()
            # Handler must propagate cancellation and stop its subprocess. No
            # second attempt starts inside this worker while cleanup is active.
            await asyncio.gather(execution, heartbeat, return_exceptions=True)

    async def run_once(self) -> int:
        """Recover persisted scopes and schedule at most the available slots."""
        async with self._pump_lock:
            self._active = {job for job in self._active if not job.done()}
            count = 0
            for scope in await asyncio.to_thread(self.store.scopes):
                await asyncio.to_thread(self.store.recover_expired, scope)
                for task in await asyncio.to_thread(self.store.schedulable, scope, limit=1000):
                    if len(self._active) >= self.concurrency:
                        return count
                    if task.status not in {TaskStatus.PENDING, TaskStatus.WAITING}:
                        continue
                    # Only safe registered job kinds may be recovered after a
                    # lease loss; explicit foreground retry remains available.
                    if task.attempts and task.kind not in self.retry_safe_kinds:
                        continue
                    lease = await asyncio.to_thread(
                        self.store.claim, scope, task.id, self.worker_id, lease_seconds=self.lease_seconds,
                    )
                    if lease is not None:
                        job = asyncio.create_task(self._run(task, lease))
                        self._active.add(job)
                        count += 1
            return count

    async def wait_idle(self) -> None:
        """Wait for currently running work, without silently starting new jobs."""
        if self._active:
            await asyncio.gather(*tuple(self._active), return_exceptions=True)
        self._active = {job for job in self._active if not job.done()}

    async def close(self) -> None:
        for job in self._active:
            job.cancel()
        await self.wait_idle()

    async def serve(self, stop: asyncio.Event, *, poll_seconds: float = 1) -> None:
        if not 0.05 <= poll_seconds <= 60:
            raise ValueError("Invalid worker poll interval")
        try:
            while not stop.is_set():
                await self.run_once()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass
        finally:
            await self.close()
