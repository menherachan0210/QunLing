"""Small SQLite task ledger. No scheduler, model calls, or external side effects.

Every access requires a trusted Scope or a fenced Lease. An expired lease cannot
write or deliver. Recovery schedules safe work; an ambiguous send instead blocks
for reconciliation. Callers must keep credentials and raw documents out of
goals/checkpoints and pass only artifact references and bounded step metadata.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable
import uuid

from qunling.scope import Scope
from .models import ErrorCode, ErrorInfo, Lease, LeaseLost, Receipt, Task, TaskConflict, TaskStatus


_FINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
_SECRET_KEY = re.compile(r"(?:password|secret|credential|authorization|cookie|api.?key|access.?token)", re.I)


def _text(value: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise ValueError("Invalid task field")
    return value


def _number(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid timestamp")
    return float(value)


def _json(value: Any, limit: int = 65536) -> str:
    def check(item, depth=0):
        if depth > 12:
            raise ValueError("Task metadata nesting limit exceeded")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or _SECRET_KEY.search(key):
                    raise ValueError("Unsafe task metadata key")
                check(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check(child, depth + 1)
        elif not isinstance(item, (str, int, float, bool, type(None))):
            raise ValueError("Invalid task metadata")
    check(value)
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError("Task metadata size limit exceeded")
    return encoded


def _refs(values) -> str:
    if not isinstance(values, (tuple, list)) or len(values) > 128:
        raise ValueError("Invalid result references")
    return _json([_text(value) for value in values])


class TaskStore:
    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS ql_tasks (
                    id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, scope_json TEXT NOT NULL,
                    kind TEXT NOT NULL, goal TEXT NOT NULL, request_key TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    deadline_at REAL, retry_at REAL, checkpoint TEXT NOT NULL DEFAULT '{}',
                    result_refs TEXT NOT NULL DEFAULT '[]', error_code TEXT,
                    retryable INTEGER NOT NULL DEFAULT 0, can_continue INTEGER NOT NULL DEFAULT 0,
                    lease_token TEXT, lease_epoch INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT, lease_until REAL,
                    UNIQUE(scope_key, request_key)
                );
                CREATE INDEX IF NOT EXISTS ql_tasks_scope_state ON ql_tasks(scope_key,status);
                CREATE TABLE IF NOT EXISTS ql_task_receipts (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    artifact_ref TEXT NOT NULL, state TEXT NOT NULL, external_ref TEXT,
                    UNIQUE(task_id,idempotency_key),
                    FOREIGN KEY(task_id) REFERENCES ql_tasks(id)
                );
                CREATE TABLE IF NOT EXISTS ql_task_storage (
                    task_id TEXT NOT NULL, slot TEXT NOT NULL, bytes INTEGER NOT NULL,
                    PRIMARY KEY(task_id,slot), FOREIGN KEY(task_id) REFERENCES ql_tasks(id)
                );
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _scope(scope: Scope) -> str:
        if not isinstance(scope, Scope):
            raise ValueError("Trusted scope required")
        return scope.key

    @staticmethod
    def _task(row) -> Task:
        error = None if row["error_code"] is None else ErrorInfo(
            ErrorCode(row["error_code"]), bool(row["retryable"]), bool(row["can_continue"])
        )
        return Task(
            id=row["id"], scope=Scope(**json.loads(row["scope_json"])), kind=row["kind"], goal=row["goal"],
            request_key=row["request_key"], status=TaskStatus(row["status"]), attempts=row["attempts"],
            max_attempts=row["max_attempts"], created_at=row["created_at"], updated_at=row["updated_at"],
            deadline_at=row["deadline_at"], retry_at=row["retry_at"], checkpoint=json.loads(row["checkpoint"]),
            result_refs=tuple(json.loads(row["result_refs"])), error=error,
        )

    def _find(self, db, scope, task_id):
        row = db.execute("SELECT * FROM ql_tasks WHERE id=? AND scope_key=?", (task_id, self._scope(scope))).fetchone()
        if row is None:
            raise TaskConflict("Task unavailable in this scope")
        return row

    def _leased(self, db, lease):
        if not isinstance(lease, Lease):
            raise LeaseLost("Valid task lease required")
        row = self._find(db, lease.scope, lease.task_id)
        now = self.clock()
        if (row["status"] != TaskStatus.RUNNING or row["lease_token"] != lease.token
                or row["lease_epoch"] != lease.epoch or row["worker_id"] != lease.worker_id
                or row["lease_until"] <= now or (row["deadline_at"] is not None and row["deadline_at"] <= now)):
            raise LeaseLost("Task lease is no longer valid")
        return row

    def create(self, scope: Scope, kind: str, goal: str, request_key: str, *,
               max_attempts: int = 3, deadline_at: float | None = None, ready: bool = True,
               max_tasks: int | None = None, scope_active_limit: int | None = None,
               global_active_limit: int | None = None) -> Task:
        key = self._scope(scope)
        _text(kind, 80)
        _text(goal, 8000)
        _text(request_key)
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError("Invalid task attempt budget")
        if type(ready) is not bool:
            raise ValueError("Invalid ready flag")
        for limit in (max_tasks, scope_active_limit, global_active_limit):
            if limit is not None and (type(limit) is not int or limit < 1):
                raise ValueError("Invalid task capacity")
        if deadline_at is not None:
            deadline_at = _number(deadline_at)
        with self._db() as db:
            row = db.execute("SELECT * FROM ql_tasks WHERE scope_key=? AND request_key=?", (key, request_key)).fetchone()
            if row is not None:
                if ((row["kind"], row["goal"], row["max_attempts"]) != (kind, goal, max_attempts)
                        or (deadline_at is not None and row["deadline_at"] != deadline_at)):
                    raise TaskConflict("Request key already belongs to a different task")
                return self._task(row)
            if max_tasks is not None and db.execute("SELECT COUNT(*) FROM ql_tasks").fetchone()[0] >= max_tasks:
                raise TaskConflict("Task storage capacity exhausted")
            active_sql = "status NOT IN ('completed','failed','cancelled')"
            if (scope_active_limit is not None and db.execute(
                f"SELECT COUNT(*) FROM ql_tasks WHERE {active_sql} AND scope_key=?", (key,)
            ).fetchone()[0] >= scope_active_limit):
                raise TaskConflict("Conversation task queue capacity exhausted")
            if (global_active_limit is not None and db.execute(
                f"SELECT COUNT(*) FROM ql_tasks WHERE {active_sql}"
            ).fetchone()[0] >= global_active_limit):
                raise TaskConflict("Global task queue capacity exhausted")
            now = self.clock()
            task_id = uuid.uuid4().hex
            db.execute("""INSERT INTO ql_tasks(id,scope_key,scope_json,kind,goal,request_key,status,
                max_attempts,created_at,updated_at,deadline_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                       (task_id, key, json.dumps(scope.as_dict()), kind, goal, request_key,
                        TaskStatus.PENDING if ready else TaskStatus.WAITING, max_attempts, now, now, deadline_at))
            return self._task(self._find(db, scope, task_id))

    def activate(self, scope: Scope, task_id: str, *, deadline_at: float | None = None) -> Task:
        """Publish a staged task only after its private payload is durable."""
        if deadline_at is not None:
            deadline_at = _number(deadline_at)
        with self._db() as db:
            row = self._find(db, scope, task_id)
            if row["status"] == TaskStatus.PENDING and row["attempts"] == 0:
                return self._task(row)
            if row["status"] != TaskStatus.WAITING or row["attempts"] != 0 or row["error_code"] is not None:
                raise TaskConflict("Task is not awaiting activation")
            if deadline_at is not None:
                if row["deadline_at"] is not None and row["deadline_at"] != deadline_at:
                    raise TaskConflict("Task deadline is already fixed")
                db.execute("UPDATE ql_tasks SET deadline_at=? WHERE id=?", (deadline_at, task_id))
            self._clear_lease(db, task_id, TaskStatus.PENDING, self.clock())
            return self._task(self._find(db, scope, task_id))

    def scopes(self) -> list[Scope]:
        """Host scheduler capability: never expose this method as a model tool."""
        with self._db() as db:
            rows = db.execute("SELECT DISTINCT scope_json FROM ql_tasks ORDER BY scope_key").fetchall()
            return [Scope(**json.loads(row["scope_json"])) for row in rows]

    def reserve_storage(self, scope: Scope, task_id: str, slot: str, size: int, *, total_limit: int) -> None:
        """Reserve private job-file bytes atomically before writing, including failures."""
        if slot not in {"payload", "result"} or type(size) is not int or size < 0:
            raise ValueError("Invalid job storage reservation")
        if type(total_limit) is not int or total_limit < 1:
            raise ValueError("Invalid job storage limit")
        with self._db() as db:
            self._find(db, scope, task_id)
            total = db.execute("SELECT COALESCE(SUM(bytes),0) FROM ql_task_storage").fetchone()[0]
            previous = db.execute("SELECT bytes FROM ql_task_storage WHERE task_id=? AND slot=?", (task_id, slot)).fetchone()
            if total - (previous[0] if previous else 0) + size > total_limit:
                raise TaskConflict("Task storage byte capacity exhausted")
            db.execute("INSERT INTO ql_task_storage(task_id,slot,bytes) VALUES (?,?,?) "
                       "ON CONFLICT(task_id,slot) DO UPDATE SET bytes=excluded.bytes", (task_id, slot, size))

    def delete_final(self, scope: Scope, task_id: str, *, older_than: float) -> bool:
        """Delete an old final task only. Callers remove its private files first."""
        older_than = _number(older_than)
        with self._db() as db:
            row = self._find(db, scope, task_id)
            if row["status"] not in _FINAL or row["updated_at"] >= older_than:
                return False
            db.execute("DELETE FROM ql_task_receipts WHERE task_id=?", (task_id,))
            db.execute("DELETE FROM ql_task_storage WHERE task_id=?", (task_id,))
            db.execute("DELETE FROM ql_tasks WHERE id=?", (task_id,))
            return True

    def get(self, scope: Scope, task_id: str) -> Task:
        with self._db() as db:
            return self._task(self._find(db, scope, task_id))

    def list(self, scope: Scope, *, limit: int = 100) -> list[Task]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid task list limit")
        with self._db() as db:
            rows = db.execute("SELECT * FROM ql_tasks WHERE scope_key=? ORDER BY created_at DESC,id LIMIT ?",
                              (self._scope(scope), limit)).fetchall()
            return [self._task(row) for row in rows]

    def schedulable(self, scope: Scope, *, limit: int = 100) -> list[Task]:
        """FIFO ready work only, so completed history cannot hide queued tasks."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid task list limit")
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM ql_tasks WHERE scope_key=? AND "
                "(status='pending' OR (status='waiting' AND retryable=1 AND retry_at<=?)) "
                "ORDER BY created_at,id LIMIT ?", (self._scope(scope), self.clock(), limit),
            ).fetchall()
            return [self._task(row) for row in rows]

    def expired_final(self, scope: Scope, *, older_than: float, limit: int = 1000) -> list[Task]:
        older_than = _number(older_than)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid task list limit")
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM ql_tasks WHERE scope_key=? "
                "AND status IN ('completed','failed','cancelled') AND updated_at<? "
                "ORDER BY updated_at,id LIMIT ?", (self._scope(scope), older_than, limit),
            ).fetchall()
            return [self._task(row) for row in rows]

    @staticmethod
    def _clear_lease(db, task_id, status, now, *, error=None, retry_at=None):
        db.execute("""UPDATE ql_tasks SET status=?,updated_at=?,retry_at=?,error_code=?,retryable=?,
            can_continue=?,lease_token=NULL,worker_id=NULL,lease_until=NULL WHERE id=?""",
                   (status, now, retry_at, error.code if error else None, int(error.retryable) if error else 0,
                    int(error.can_continue) if error else 0, task_id))

    def claim(self, scope: Scope, task_id: str, worker_id: str, *, lease_seconds: int = 60) -> Lease | None:
        _text(worker_id)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("Invalid lease duration")
        with self._db() as db:
            row = self._find(db, scope, task_id)
            now = self.clock()
            if row["status"] not in {TaskStatus.PENDING, TaskStatus.WAITING}:
                return None
            if row["status"] == TaskStatus.WAITING and (
                not row["retryable"] or row["retry_at"] is None or row["retry_at"] > now
            ):
                return None
            if row["attempts"] >= row["max_attempts"] or (
                row["deadline_at"] is not None and row["deadline_at"] <= now
            ):
                self._clear_lease(db, task_id, TaskStatus.FAILED, now, error=ErrorInfo(ErrorCode.BUDGET))
                return None
            until = min(now + lease_seconds, row["deadline_at"]) if row["deadline_at"] is not None else now + lease_seconds
            token, epoch = uuid.uuid4().hex, row["lease_epoch"] + 1
            db.execute("""UPDATE ql_tasks SET status=?,attempts=attempts+1,updated_at=?,lease_token=?,
                lease_epoch=?,worker_id=?,lease_until=?,retry_at=NULL WHERE id=?""",
                       (TaskStatus.RUNNING, now, token, epoch, worker_id, until, task_id))
            return Lease(task_id, scope, worker_id, token, epoch, until)

    def heartbeat(self, lease: Lease, *, lease_seconds: int = 60) -> Lease:
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("Invalid lease duration")
        with self._db() as db:
            row = self._leased(db, lease)
            now = self.clock()
            until = now + lease_seconds
            if row["deadline_at"] is not None:
                until = min(until, row["deadline_at"])
            db.execute("UPDATE ql_tasks SET lease_until=?,updated_at=? WHERE id=?", (until, now, lease.task_id))
            return Lease(lease.task_id, lease.scope, lease.worker_id, lease.token, lease.epoch, until)

    def checkpoint(self, lease: Lease, stage: str, data: dict[str, Any]) -> None:
        _text(stage, 80)
        if not isinstance(data, dict):
            raise ValueError("Task checkpoint must be an object")
        encoded = _json({"stage": stage, "data": data})
        with self._db() as db:
            self._leased(db, lease)
            db.execute("UPDATE ql_tasks SET checkpoint=?,updated_at=? WHERE id=?", (encoded, self.clock(), lease.task_id))

    def defer(self, lease: Lease, error: ErrorInfo, *, retry_at: float | None = None) -> Task:
        if not isinstance(error, ErrorInfo):
            raise ValueError("Safe error classification required")
        if retry_at is not None:
            retry_at = _number(retry_at)
            if not error.retryable:
                raise ValueError("Only safe transient failures may schedule a retry")
        with self._db() as db:
            row = self._leased(db, lease)
            now = self.clock()
            if self._ambiguous_receipts(db, lease.task_id):
                self._clear_lease(db, lease.task_id, TaskStatus.WAITING, now,
                                  error=ErrorInfo(ErrorCode.DELIVERY_UNCONFIRMED))
            elif error.retryable and (row["attempts"] >= row["max_attempts"] or (
                retry_at is not None and row["deadline_at"] is not None and retry_at >= row["deadline_at"]
            )):
                self._clear_lease(db, lease.task_id, TaskStatus.FAILED, now, error=ErrorInfo(ErrorCode.BUDGET))
            else:
                self._clear_lease(db, lease.task_id, TaskStatus.WAITING, now, error=error, retry_at=retry_at)
            return self._task(self._find(db, lease.scope, lease.task_id))

    def finish(self, lease: Lease, status: TaskStatus = TaskStatus.COMPLETED, *,
               result_refs: tuple[str, ...] = (), error: ErrorInfo | None = None) -> Task:
        if status not in {TaskStatus.COMPLETED, TaskStatus.PARTIAL, TaskStatus.FAILED}:
            raise ValueError("Invalid finish status")
        if error is not None and not isinstance(error, ErrorInfo):
            raise ValueError("Safe error classification required")
        if status == TaskStatus.COMPLETED and error is not None:
            raise ValueError("Completed task cannot have an error")
        encoded = _refs(result_refs)
        with self._db() as db:
            self._leased(db, lease)
            pending = db.execute("SELECT 1 FROM ql_task_receipts WHERE task_id=? AND state!='sent'",
                                 (lease.task_id,)).fetchone()
            if status == TaskStatus.COMPLETED and pending:
                raise TaskConflict("Delivery has not been confirmed")
            self._clear_lease(db, lease.task_id, status, self.clock(), error=error)
            db.execute("UPDATE ql_tasks SET result_refs=? WHERE id=?", (encoded, lease.task_id))
            return self._task(self._find(db, lease.scope, lease.task_id))

    def cancel(self, scope: Scope, task_id: str) -> Task:
        with self._db() as db:
            row = self._find(db, scope, task_id)
            if row["status"] not in _FINAL:
                self._ambiguous_receipts(db, task_id)
                self._clear_lease(db, task_id, TaskStatus.CANCELLED, self.clock(), error=ErrorInfo(ErrorCode.CANCELLED))
            return self._task(self._find(db, scope, task_id))

    def resume(self, scope: Scope, task_id: str) -> Task:
        """Explicit operator/user resume; never resets attempts or deadline."""
        with self._db() as db:
            row = self._find(db, scope, task_id)
            if row["status"] not in {TaskStatus.WAITING, TaskStatus.PARTIAL}:
                raise TaskConflict("Task is not resumable")
            if db.execute("SELECT 1 FROM ql_task_receipts WHERE task_id=? AND state!='sent'", (task_id,)).fetchone():
                raise TaskConflict("Delivery must be reconciled before resuming")
            if row["attempts"] >= row["max_attempts"] or (row["deadline_at"] is not None and row["deadline_at"] <= self.clock()):
                raise TaskConflict("Task budget exhausted")
            self._clear_lease(db, task_id, TaskStatus.PENDING, self.clock())
            return self._task(self._find(db, scope, task_id))

    @staticmethod
    def _ambiguous_receipts(db, task_id) -> bool:
        db.execute("UPDATE ql_task_receipts SET state='unconfirmed' WHERE task_id=? AND state='started'", (task_id,))
        return db.execute("SELECT 1 FROM ql_task_receipts WHERE task_id=? AND state='unconfirmed'", (task_id,)).fetchone() is not None

    def recover_expired(self, scope: Scope) -> dict[str, int]:
        counts = {"pending": 0, "waiting": 0, "failed": 0}
        with self._db() as db:
            now = self.clock()
            rows = db.execute("SELECT * FROM ql_tasks WHERE scope_key=? AND status=? AND (lease_until<=? OR deadline_at<=?)",
                              (self._scope(scope), TaskStatus.RUNNING, now, now)).fetchall()
            for row in rows:
                if self._ambiguous_receipts(db, row["id"]):
                    status, error = TaskStatus.WAITING, ErrorInfo(ErrorCode.DELIVERY_UNCONFIRMED)
                elif row["attempts"] >= row["max_attempts"] or (row["deadline_at"] is not None and row["deadline_at"] <= now):
                    status, error = TaskStatus.FAILED, ErrorInfo(ErrorCode.BUDGET)
                else:
                    status, error = TaskStatus.PENDING, ErrorInfo(ErrorCode.TIMEOUT, retryable=True, can_continue=True)
                self._clear_lease(db, row["id"], status, now, error=error)
                counts[str(status)] += 1
        return counts

    @staticmethod
    def _receipt(row, *, should_send=False):
        return Receipt(row["id"], row["task_id"], row["idempotency_key"], row["artifact_ref"],
                       row["state"], row["external_ref"], should_send)

    def reserve_delivery(self, lease: Lease, idempotency_key: str, artifact_ref: str) -> Receipt:
        _text(idempotency_key)
        _text(artifact_ref)
        with self._db() as db:
            self._leased(db, lease)
            row = db.execute("SELECT * FROM ql_task_receipts WHERE task_id=? AND idempotency_key=?",
                             (lease.task_id, idempotency_key)).fetchone()
            if row:
                if row["artifact_ref"] != artifact_ref:
                    raise TaskConflict("Delivery key already belongs to another artifact")
                return self._receipt(row)
            receipt_id = uuid.uuid4().hex
            db.execute("INSERT INTO ql_task_receipts(id,task_id,idempotency_key,artifact_ref,state) VALUES (?,?,?,?,'started')",
                       (receipt_id, lease.task_id, idempotency_key, artifact_ref))
            row = db.execute("SELECT * FROM ql_task_receipts WHERE id=?", (receipt_id,)).fetchone()
            return self._receipt(row, should_send=True)

    def mark_delivery(self, lease: Lease, receipt_id: str, state: str, *, external_ref: str | None = None) -> Receipt:
        if state not in {"sent", "unconfirmed"}:
            raise ValueError("Invalid delivery state")
        if external_ref is not None:
            _text(external_ref)
        with self._db() as db:
            self._leased(db, lease)
            row = db.execute("SELECT * FROM ql_task_receipts WHERE id=? AND task_id=?", (receipt_id, lease.task_id)).fetchone()
            if row is None:
                raise TaskConflict("Delivery unavailable for this task")
            if row["state"] != "started":
                if row["state"] != state or row["external_ref"] != external_ref:
                    raise TaskConflict("Delivery state already finalized")
                return self._receipt(row)
            db.execute("UPDATE ql_task_receipts SET state=?,external_ref=? WHERE id=?", (state, external_ref, receipt_id))
            return self._receipt(db.execute("SELECT * FROM ql_task_receipts WHERE id=?", (receipt_id,)).fetchone())

    def deliveries(self, scope: Scope, task_id: str) -> list[Receipt]:
        with self._db() as db:
            self._find(db, scope, task_id)
            return [self._receipt(row) for row in db.execute("SELECT * FROM ql_task_receipts WHERE task_id=?", (task_id,))]

    def reconcile_delivery(self, scope: Scope, task_id: str, receipt_id: str, *, external_ref: str) -> Receipt:
        """Trusted transport/operator supplies evidence of delivery; never resends."""
        _text(external_ref)
        with self._db() as db:
            task = self._find(db, scope, task_id)
            if task["status"] == TaskStatus.RUNNING:
                raise TaskConflict("Cannot reconcile while a worker is active")
            row = db.execute("SELECT * FROM ql_task_receipts WHERE id=? AND task_id=?", (receipt_id, task_id)).fetchone()
            if row is None or row["state"] != "unconfirmed":
                raise TaskConflict("Delivery does not require reconciliation")
            db.execute("UPDATE ql_task_receipts SET state='sent',external_ref=? WHERE id=?", (external_ref, receipt_id))
            return self._receipt(db.execute("SELECT * FROM ql_task_receipts WHERE id=?", (receipt_id,)).fetchone())
