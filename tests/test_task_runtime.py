"""Task lifecycle regressions use synthetic identities, local SQLite, no SDK."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from qunling.scope import Scope
from qunling.tasks import ErrorCode, ErrorInfo, LeaseLost, TaskConflict, TaskStatus, TaskStore


@pytest.fixture
def task_env(tmp_path):
    clock = [1000.0]
    scope = Scope("synthetic-workspace", "synthetic-bot", "group:synthetic", "synthetic-actor")
    store = TaskStore(tmp_path / "tasks.sqlite", clock=lambda: clock[0])
    task = store.create(scope, "attachment", "整理合成附件并交付", "request-1")
    return store, scope, task, clock


def claim(env, **kwargs):
    store, scope, task, _ = env
    return store.claim(scope, task.id, "worker-a", **kwargs)


def test_checkpoint_result_and_idempotency_survive_restart(task_env):
    store, scope, task, _ = task_env
    assert store.create(scope, task.kind, task.goal, task.request_key).id == task.id
    with pytest.raises(TaskConflict):
        store.create(scope, task.kind, "another goal", task.request_key)
    lease = claim(task_env)
    store.checkpoint(lease, "converted", {"input_refs": ["asset-a"], "output_refs": ["asset-b"], "pages": 4})
    restarted = TaskStore(store.path)
    saved = restarted.get(scope, task.id)
    assert saved.checkpoint["stage"] == "converted"
    assert saved.checkpoint["data"]["output_refs"] == ["asset-b"]
    result = store.finish(lease, result_refs=("asset-b",))
    assert result.status == TaskStatus.COMPLETED
    assert restarted.get(scope, task.id).result_refs == ("asset-b",)
    assert store.claim(scope, task.id, "worker-b") is None


@pytest.mark.parametrize("field", ["workspace_id", "bot_id", "conversation_id", "actor_id"])
def test_every_scope_dimension_isolated(task_env, field):
    store, scope, task, _ = task_env
    wrong = replace(scope, **{field: "unrelated"})
    assert store.list(wrong) == []
    for action in (store.get, store.cancel, store.resume, store.deliveries):
        with pytest.raises(TaskConflict):
            action(wrong, task.id)
    with pytest.raises(TaskConflict):
        store.claim(wrong, task.id, "worker-b")
    assert store.recover_expired(wrong) == {"pending": 0, "waiting": 0, "failed": 0}
    assert store.create(wrong, task.kind, task.goal, task.request_key).id != task.id


def test_only_one_concurrent_worker_claims(task_env):
    store, scope, task, _ = task_env
    with ThreadPoolExecutor(max_workers=8) as pool:
        leases = list(pool.map(lambda i: store.claim(scope, task.id, f"worker-{i}"), range(8)))
    assert sum(lease is not None for lease in leases) == 1
    assert store.get(scope, task.id).attempts == 1


def test_expired_worker_is_fenced_and_recovery_preserves_checkpoint(task_env):
    store, scope, task, clock = task_env
    old = claim(task_env, lease_seconds=10)
    store.checkpoint(old, "downloaded", {"input_refs": ["asset-a"]})
    clock[0] += 11
    with pytest.raises(LeaseLost):
        store.checkpoint(old, "stale", {})
    assert store.recover_expired(scope)["pending"] == 1
    new = store.claim(scope, task.id, "worker-b")
    assert new.epoch == old.epoch + 1 and new.token != old.token
    for action in (lambda: store.finish(old), lambda: store.heartbeat(old),
                   lambda: store.reserve_delivery(old, "send-a", "asset-a")):
        with pytest.raises(LeaseLost):
            action()
    assert store.get(scope, task.id).checkpoint["stage"] == "downloaded"
    store.finish(new)


def test_heartbeat_and_deadline_do_not_extend_budget(task_env):
    store, scope, _, clock = task_env
    task = store.create(scope, "web", "synthetic lookup", "deadline", deadline_at=1020)
    lease = store.claim(scope, task.id, "worker", lease_seconds=10)
    clock[0] += 5
    renewed = store.heartbeat(lease, lease_seconds=60)
    assert renewed.expires_at == 1020
    clock[0] = 1021
    with pytest.raises(LeaseLost):
        store.finish(renewed)
    assert store.recover_expired(scope)["failed"] == 1
    assert store.get(scope, task.id).error.code == ErrorCode.BUDGET


def test_bounded_safe_retry_and_no_early_claim(task_env):
    store, scope, task, clock = task_env
    transient = ErrorInfo(ErrorCode.NETWORK, retryable=True, can_continue=True)
    first = claim(task_env)
    store.defer(first, transient, retry_at=1005)
    assert store.claim(scope, task.id, "worker") is None
    clock[0] = 1005
    second = store.claim(scope, task.id, "worker")
    store.defer(second, transient, retry_at=1005)
    third = store.claim(scope, task.id, "worker")
    done = store.defer(third, transient, retry_at=1005)
    assert done.status == TaskStatus.FAILED and done.error.code == ErrorCode.BUDGET
    assert done.attempts == 3
    assert store.claim(scope, task.id, "worker") is None


def test_permission_input_and_ambiguous_delivery_never_auto_retry(task_env):
    store, scope, task, _ = task_env
    for code in (ErrorCode.PERMISSION, ErrorCode.NEEDS_INPUT, ErrorCode.DELIVERY_UNCONFIRMED):
        with pytest.raises(ValueError):
            ErrorInfo(code, retryable=True)
        with pytest.raises(ValueError):
            ErrorInfo(code, can_continue=True)
    lease = claim(task_env)
    store.defer(lease, ErrorInfo(ErrorCode.NEEDS_INPUT))
    assert store.claim(scope, task.id, "worker") is None
    store.resume(scope, task.id)
    assert store.claim(scope, task.id, "worker") is not None


def test_cancel_invalidates_lease_and_cannot_resurrect(task_env):
    store, scope, task, clock = task_env
    lease = claim(task_env)
    receipt = store.reserve_delivery(lease, "send", "asset")
    store.cancel(scope, task.id)
    with pytest.raises(LeaseLost):
        store.finish(lease)
    with pytest.raises(LeaseLost):
        store.mark_delivery(lease, receipt.id, "sent")
    clock[0] += 1000
    store.recover_expired(scope)
    assert store.get(scope, task.id).status == TaskStatus.CANCELLED
    assert store.deliveries(scope, task.id)[0].state == "unconfirmed"
    assert store.claim(scope, task.id, "worker") is None
    with pytest.raises(TaskConflict):
        store.resume(scope, task.id)


def test_delivery_reservation_is_at_most_once_and_completion_requires_receipt(task_env):
    store, scope, task, _ = task_env
    lease = claim(task_env)
    first = store.reserve_delivery(lease, "send", "asset")
    assert first.should_send and first.state == "started"
    duplicate = store.reserve_delivery(lease, "send", "asset")
    assert duplicate.id == first.id and not duplicate.should_send
    with pytest.raises(TaskConflict):
        store.reserve_delivery(lease, "send", "different-asset")
    with pytest.raises(TaskConflict):
        store.finish(lease)
    sent = store.mark_delivery(lease, first.id, "sent", external_ref="synthetic-receipt")
    assert sent.state == "sent"
    assert not store.reserve_delivery(lease, "send", "asset").should_send
    store.finish(lease)
    assert not store.deliveries(scope, task.id)[0].should_send


def test_recovery_with_ambiguous_send_blocks_until_reconciliation(task_env):
    store, scope, task, clock = task_env
    lease = claim(task_env)
    receipt = store.reserve_delivery(lease, "send", "asset")
    clock[0] += 100
    assert store.recover_expired(scope)["waiting"] == 1
    assert store.get(scope, task.id).error.code == ErrorCode.DELIVERY_UNCONFIRMED
    assert store.claim(scope, task.id, "worker") is None
    with pytest.raises(TaskConflict):
        store.resume(scope, task.id)
    store.reconcile_delivery(scope, task.id, receipt.id, external_ref="transport-proof")
    store.resume(scope, task.id)
    new = store.claim(scope, task.id, "worker")
    assert not store.reserve_delivery(new, "send", "asset").should_send
    store.finish(new)


def test_network_failure_after_send_started_does_not_schedule_retry(task_env):
    store, scope, task, _ = task_env
    lease = claim(task_env)
    store.reserve_delivery(lease, "send", "asset")
    waiting = store.defer(lease, ErrorInfo(ErrorCode.NETWORK, retryable=True, can_continue=True), retry_at=1000)
    assert waiting.error.code == ErrorCode.DELIVERY_UNCONFIRMED
    assert store.claim(scope, task.id, "worker") is None


def test_partial_preserves_outputs_and_needs_explicit_resume(task_env):
    store, scope, task, _ = task_env
    lease = claim(task_env)
    partial = store.finish(lease, TaskStatus.PARTIAL, result_refs=("asset-a",))
    assert partial.result_refs == ("asset-a",)
    assert store.claim(scope, task.id, "worker") is None
    store.resume(scope, task.id)
    assert store.get(scope, task.id).attempts == 1
    assert store.claim(scope, task.id, "worker") is not None


@pytest.mark.parametrize("payload", [
    {"api_key": "never-store"}, {"nested": {"authorization": "never-store"}},
    {"body": "x" * 65536}, {"number": float("nan")},
])
def test_checkpoint_bounds_and_credential_fields(task_env, payload):
    store, _, _, _ = task_env
    with pytest.raises(ValueError):
        store.checkpoint(claim(task_env), "step", payload)


def test_invalid_timestamps_attempts_and_lease_bounds(task_env):
    store, scope, task, _ = task_env
    for deadline in (float("inf"), float("nan"), -1, True):
        with pytest.raises(ValueError):
            store.create(scope, "kind", "goal", "bad", deadline_at=deadline)
    for attempts in (0, 11, True):
        with pytest.raises(ValueError):
            store.create(scope, "kind", "goal", "bad", max_attempts=attempts)
    for seconds in (0, 301, True):
        with pytest.raises(ValueError):
            store.claim(scope, task.id, "worker", lease_seconds=seconds)
