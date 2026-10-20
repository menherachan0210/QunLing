"""SDK-independent, persisted task state and safe error classifications."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from qunling.scope import Scope


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    PARTIAL = "partial"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ErrorCode(StrEnum):
    NETWORK = "network"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SERVICE = "service"
    INVALID_INPUT = "invalid_input"
    PERMISSION = "permission"
    NEEDS_INPUT = "needs_input"
    DELIVERY_UNCONFIRMED = "delivery_unconfirmed"
    BUDGET = "budget"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


_TRANSIENT = {ErrorCode.NETWORK, ErrorCode.TIMEOUT, ErrorCode.RATE_LIMIT, ErrorCode.SERVICE}


@dataclass(frozen=True)
class ErrorInfo:
    code: ErrorCode
    retryable: bool = False
    can_continue: bool = False

    def __post_init__(self):
        if not isinstance(self.code, ErrorCode):
            raise ValueError("Invalid error classification")
        if type(self.retryable) is not bool or type(self.can_continue) is not bool:
            raise ValueError("Invalid error flags")
        if self.retryable and self.code not in _TRANSIENT:
            raise ValueError("This error cannot be retried automatically")
        if self.can_continue and self.code in {
            ErrorCode.PERMISSION, ErrorCode.NEEDS_INPUT, ErrorCode.DELIVERY_UNCONFIRMED,
            ErrorCode.BUDGET, ErrorCode.CANCELLED,
        }:
            raise ValueError("This error requires stopping")


@dataclass(frozen=True)
class Task:
    id: str
    scope: Scope
    kind: str
    goal: str
    request_key: str
    status: TaskStatus
    attempts: int
    max_attempts: int
    created_at: float
    updated_at: float
    deadline_at: float | None
    retry_at: float | None
    checkpoint: dict[str, Any]
    result_refs: tuple[str, ...]
    error: ErrorInfo | None


@dataclass(frozen=True)
class Lease:
    task_id: str
    scope: Scope
    worker_id: str
    token: str
    epoch: int
    expires_at: float


@dataclass(frozen=True)
class Receipt:
    id: str
    task_id: str
    idempotency_key: str
    artifact_ref: str
    state: str
    external_ref: str | None
    # True only on the original reservation. A persisted reservation is never
    # permission to repeat an external side effect.
    should_send: bool = False


class TaskError(Exception):
    """Fixed diagnostic text only; never includes database payloads."""


class LeaseLost(TaskError):
    pass


class TaskConflict(TaskError):
    pass
