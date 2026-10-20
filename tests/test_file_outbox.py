"""Durable delivery with synthetic assets and an in-memory platform transport."""
import asyncio
import base64
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qunling.assets import AssetError
from qunling.delivery import DeliveryStopped, DeliveryUnavailable, HostDelivery, resolve_bot
from qunling.jobs import JobService
from qunling.outbox import Outbox
from qunling.scope import Scope
from qunling.tasks import TaskConflict, TaskStatus

ROOT = Path(__file__).resolve().parents[1]
ROUTE = {"instance_uuid": "synthetic-instance", "workspace_uuid": "synthetic-workspace", "placement_generation": 3}
SCOPE = Scope(hashlib.sha256(json.dumps([ROUTE["instance_uuid"], ROUTE["workspace_uuid"]]).encode()).hexdigest(),
              "synthetic-bot", "group:123456", "synthetic-actor")
ARGS = {"format": "txt", "filename": "synthetic.txt", "payload": {"text": "synthetic output"}}


@pytest.fixture
def sdk(monkeypatch):
    message = ModuleType("langbot_plugin.api.entities.builtin.platform.message")
    message.File = SimpleNamespace
    message.MessageChain = list
    connection = ModuleType("langbot_plugin.runtime.io.connection")
    connection.MAX_MESSAGE_BYTES = 32 * 1024 * 1024
    monkeypatch.setitem(sys.modules, message.__name__, message)
    monkeypatch.setitem(sys.modules, connection.__name__, connection)
    return connection


def runtime():
    bot = SimpleNamespace(
        enable=True,
        bot_entity=SimpleNamespace(enable=True, uuid=SCOPE.bot_id, workspace_uuid=ROUTE["workspace_uuid"]),
        execution_context=SimpleNamespace(**ROUTE),
        adapter=SimpleNamespace(bot=SimpleNamespace(connection=SimpleNamespace(closed=False)), send_message=AsyncMock()),
    )
    ap = SimpleNamespace(
        workspace_service=SimpleNamespace(instance_uuid=ROUTE["instance_uuid"]),
        bot_service=SimpleNamespace(get_bot=AsyncMock(return_value={"enable": True, "adapter": "onebot-v11"})),
        platform_mgr=SimpleNamespace(get_bot_by_uuid=AsyncMock(return_value=bot)),
    )
    return ap, bot


def finished(jobs, *, key="request", scope=SCOPE, attach=True, origin="generated", data=b"synthetic output"):
    task = jobs.submit(scope, "create", ARGS, key)
    item = jobs.assets.put(scope, "synthetic.txt", data, origin=origin)
    lease = jobs.tasks.claim(scope, task["task_id"], "synthetic-worker")
    jobs._write(scope, task["task_id"], "result", {"status": "ok", "result_refs": [item["asset_ref"]]})
    jobs.tasks.finish(lease, TaskStatus.COMPLETED, result_refs=[item["asset_ref"]])
    if attach:
        jobs.attach_delivery(scope, task["task_id"], key, ROUTE)
    return task["task_id"], item["asset_ref"]


def test_completed_task_can_gain_delivery_on_same_request_without_reprocessing(tmp_path):
    jobs = JobService(tmp_path)
    task_id, _ = finished(jobs, attach=False)
    repeated = jobs.submit(SCOPE, "create", ARGS, "request")
    assert repeated["task_id"] == task_id and repeated["status"] == "completed"
    assert "delivery_status" not in repeated
    first = jobs.attach_delivery(SCOPE, task_id, "request", ROUTE)
    second = jobs.attach_delivery(SCOPE, task_id, "request", ROUTE)
    assert first == second and first["delivery_status"] == "queued"
    assert jobs.tasks.get(SCOPE, task_id).attempts == 1
    with jobs.tasks._db() as db:
        assert db.execute("SELECT COUNT(*) FROM ql_file_outbox").fetchone()[0] == 1


@pytest.mark.parametrize("mutation", [
    {"instance_uuid": "wrong"}, {"workspace_uuid": "wrong"},
    {"placement_generation": 0}, {"placement_generation": True}, {"target": "999"},
])
def test_route_must_be_exact_trusted_binding(tmp_path, mutation):
    jobs = JobService(tmp_path)
    task_id, _ = finished(jobs, attach=False)
    with pytest.raises(AssetError):
        jobs.attach_delivery(SCOPE, task_id, "request", {**ROUTE, **mutation})
    assert jobs.outbox.state(SCOPE, task_id) is None


def test_only_original_create_extract_request_can_attach(tmp_path):
    jobs = JobService(tmp_path)
    task_id, ref = finished(jobs, attach=False)
    with pytest.raises(AssetError):
        jobs.attach_delivery(SCOPE, task_id, "other-request", ROUTE)
    with pytest.raises(TaskConflict):
        jobs.attach_delivery(replace(SCOPE, actor_id="other"), task_id, "request", ROUTE)
    read = jobs.submit(SCOPE, "read", {"asset_ref": ref}, "read")
    with pytest.raises(AssetError):
        jobs.attach_delivery(SCOPE, read["task_id"], "read", ROUTE)
    extraction = jobs.submit(SCOPE, "extract", {"asset_ref": ref, "member": "synthetic.txt"}, "extract")
    assert jobs.attach_delivery(SCOPE, extraction["task_id"], "extract", ROUTE)["delivery_status"] == "queued"


@pytest.mark.parametrize("conversation", ["group:123456", "person:234567"])
def test_new_host_instance_sends_real_synthetic_bytes_once(tmp_path, sdk, conversation):
    async def scenario():
        scope = replace(SCOPE, conversation_id=conversation)
        jobs = JobService(tmp_path)
        task_id, ref = finished(jobs, scope=scope)
        ap, bot = runtime()
        first = HostDelivery(ap, tmp_path, resolver=AsyncMock(return_value=bot))
        assert await first.tick()
        target_type, target, chain = bot.adapter.send_message.call_args.args
        assert (target_type, target) == tuple(conversation.split(":"))
        assert base64.b64decode(chain[0].base64) == b"synthetic output"
        assert chain[0].name == "synthetic.txt"
        second = HostDelivery(ap, tmp_path, resolver=AsyncMock(return_value=bot))
        assert not await second.tick()
        assert bot.adapter.send_message.await_count == 1
        assert JobService(tmp_path).snapshot(scope, task_id)["delivery_status"] == "sent"
        assert jobs.assets.delivery(scope, "request", ref) == "sent"
    asyncio.run(scenario())


def test_concurrent_hosts_and_foreground_share_content_receipt(tmp_path, sdk):
    async def scenario():
        jobs = JobService(tmp_path)
        task_id, ref = finished(jobs)
        ap, bot = runtime()
        hosts = [HostDelivery(ap, tmp_path, resolver=AsyncMock(return_value=bot)) for _ in range(4)]
        await asyncio.gather(*(host.tick() for host in hosts))
        assert bot.adapter.send_message.await_count == 1
        other_ref = jobs.assets.put(SCOPE, "renamed.txt", b"synthetic output", origin="generated")["asset_ref"]
        assert other_ref != ref
        assert not jobs.assets.reserve_delivery(SCOPE, "request", other_ref)
        assert jobs.assets.delivery(SCOPE, "request", other_ref) == "sent"
        assert jobs.snapshot(SCOPE, task_id)["delivery_status"] == "sent"
    asyncio.run(scenario())


@pytest.mark.parametrize("receipt", ["started", "sent", "unconfirmed"])
def test_foreground_first_prevents_duplicate_and_can_reconcile(tmp_path, sdk, receipt):
    async def scenario():
        jobs = JobService(tmp_path)
        task_id, ref = finished(jobs)
        assert jobs.assets.reserve_delivery(SCOPE, "request", ref)
        if receipt != "started":
            jobs.assets.mark_delivery(SCOPE, "request", ref, receipt)
        ap, bot = runtime()
        host = HostDelivery(ap, tmp_path, resolver=AsyncMock(return_value=bot))
        await host.tick()
        assert bot.adapter.send_message.await_count == 0
        assert jobs.assets.delivery(SCOPE, "request", ref) == receipt
        assert jobs.snapshot(SCOPE, task_id)["delivery_status"] == ("sent" if receipt == "sent" else "unconfirmed")
        if receipt == "started":
            jobs.assets.mark_delivery(SCOPE, "request", ref, "sent")
            await host.tick()
            assert jobs.snapshot(SCOPE, task_id)["delivery_status"] == "sent"
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [RuntimeError("private URL/key must never escape"), TimeoutError()])
def test_transport_failure_is_unconfirmed_never_automatically_retried(tmp_path, sdk, failure):
    async def scenario():
        jobs = JobService(tmp_path)
        task_id, ref = finished(jobs)
        ap, bot = runtime()
        bot.adapter.send_message.side_effect = failure
        host = HostDelivery(ap, tmp_path, resolver=AsyncMock(return_value=bot))
        await host.tick()
        assert jobs.snapshot(SCOPE, task_id)["delivery_status"] == "unconfirmed"
        assert jobs.assets.delivery(SCOPE, "request", ref) == "unconfirmed"
        await HostDelivery(ap, tmp_path, resolver=AsyncMock(return_value=bot)).tick()
        assert bot.adapter.send_message.await_count == 1
    asyncio.run(scenario())


def test_cancel_queued_completed_result_prevents_delivery(tmp_path, sdk):
    async def scenario():
        jobs = JobService(tmp_path)
        task_id, _ = finished(jobs)
        value = jobs.cancel(SCOPE, task_id)
        assert value["status"] == "completed" and value["delivery_status"] == "cancelled"
        ap, bot = runtime()
        assert not await HostDelivery(ap, tmp_path, resolver=AsyncMock(return_value=bot)).tick()
        bot.adapter.send_message.assert_not_called()
    asyncio.run(scenario())


def test_cancel_during_preflight_fences_reserved_claim(tmp_path, sdk):
    async def scenario():
        jobs = JobService(tmp_path)
        task_id, ref = finished(jobs)
        ap, bot = runtime()
        async def resolver(*_):
            jobs.cancel(SCOPE, task_id)
            return bot
        await HostDelivery(ap, tmp_path, resolver=resolver).tick()
        assert jobs.snapshot(SCOPE, task_id)["delivery_status"] == "cancelled"
        assert jobs.assets.delivery(SCOPE, "request", ref) is None
        bot.adapter.send_message.assert_not_called()
    asyncio.run(scenario())


def test_shutdown_cancels_inflight_and_does_not_replay_after_restart(tmp_path, sdk):
    async def scenario():
        jobs = JobService(tmp_path)
        task_id, ref = finished(jobs)
        ap, bot = runtime()
        entered = asyncio.Event()
        async def send(*_):
            entered.set()
            await asyncio.Event().wait()
        bot.adapter.send_message.side_effect = send
        tick = asyncio.create_task(HostDelivery(ap, tmp_path, resolver=AsyncMock(return_value=bot)).tick())
        await entered.wait()
        tick.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tick
        assert jobs.snapshot(SCOPE, task_id)["delivery_status"] == "unconfirmed"
        assert jobs.assets.delivery(SCOPE, "request", ref) == "unconfirmed"
        await HostDelivery(ap, tmp_path, resolver=AsyncMock(return_value=bot)).tick()
        assert bot.adapter.send_message.await_count == 1
    asyncio.run(scenario())


def test_user_cancel_inflight_does_not_retry_and_ack_can_reconcile(tmp_path, sdk):
    async def scenario():
        jobs = JobService(tmp_path)
        task_id, ref = finished(jobs)
        ap, bot = runtime()
        entered, done = asyncio.Event(), asyncio.Event()
        async def send(*_):
            entered.set()
            await done.wait()
        bot.adapter.send_message.side_effect = send
        host = HostDelivery(ap, tmp_path, resolver=AsyncMock(return_value=bot))
        tick = asyncio.create_task(host.tick())
        await entered.wait()
        assert jobs.cancel(SCOPE, task_id)["delivery_status"] == "unconfirmed"
        done.set()
        await tick
        # An already submitted send cannot be recalled. A confirmed existing
        # receipt may be observed, without sending anything again.
        assert jobs.assets.delivery(SCOPE, "request", ref) == "sent"
        await host.tick()
        assert jobs.snapshot(SCOPE, task_id)["delivery_status"] == "sent"
        assert bot.adapter.send_message.await_count == 1
    asyncio.run(scenario())


def test_expired_queue_prunes_even_if_host_was_offline(tmp_path):
    jobs = JobService(tmp_path)
    task_id, _ = finished(jobs)
    old = time.time() - 8 * 86400
    with jobs.tasks._db() as db:
        db.execute("UPDATE ql_tasks SET created_at=?,updated_at=? WHERE id=?", (old, old, task_id))
        db.execute("UPDATE ql_file_outbox SET expires=? WHERE task_id=?", (old, task_id))
    assert jobs.prune() == 1
    assert not jobs._path(SCOPE, task_id, "result").exists()


def test_crash_before_send_recovers_but_started_crash_stops(tmp_path):
    jobs = JobService(tmp_path)
    task_id, ref = finished(jobs)
    now = [time.time()]
    outbox = Outbox(jobs.tasks, clock=lambda: now[0])
    claim = outbox.claim(ROUTE["instance_uuid"])
    assert claim and outbox.claim(ROUTE["instance_uuid"]) is None
    now[0] += 91
    recovered = outbox.claim(ROUTE["instance_uuid"])
    assert recovered and recovered.token != claim.token
    assert not outbox.update(claim, "started")
    assert outbox.update(recovered, "started", asset_ref=ref)
    now[0] += 91
    assert outbox.claim(ROUTE["instance_uuid"]) is None
    assert outbox.state(SCOPE, task_id) == "unconfirmed"


@pytest.mark.parametrize("failure, expected", [(DeliveryUnavailable(), "queued"), (DeliveryStopped(), "cancelled")])
def test_no_bot_or_inactive_bot_never_reserves_send(tmp_path, sdk, failure, expected):
    async def scenario():
        jobs = JobService(tmp_path)
        task_id, ref = finished(jobs)
        ap, bot = runtime()
        host = HostDelivery(ap, tmp_path, resolver=AsyncMock(side_effect=failure))
        await host.tick()
        assert jobs.snapshot(SCOPE, task_id)["delivery_status"] == expected
        assert jobs.assets.delivery(SCOPE, "request", ref) is None
        bot.adapter.send_message.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize("bad_origin,budget", [("upload", 32 * 1024 * 1024), ("generated", 65536)])
def test_bad_origin_or_transport_budget_cannot_send(tmp_path, sdk, bad_origin, budget):
    async def scenario():
        jobs = JobService(tmp_path)
        task_id, _ = finished(jobs, origin=bad_origin)
        sdk.MAX_MESSAGE_BYTES = budget
        ap, bot = runtime()
        host = HostDelivery(ap, tmp_path, resolver=AsyncMock(return_value=bot))
        await host.tick()
        assert jobs.snapshot(SCOPE, task_id)["delivery_status"] == "failed"
        host.resolver.assert_not_called()
        bot.adapter.send_message.assert_not_called()
    asyncio.run(scenario())


def test_pending_job_never_sends_and_prune_preserves_delivery(tmp_path):
    jobs = JobService(tmp_path)
    submitted = jobs.submit(SCOPE, "create", ARGS, "pending")
    jobs.attach_delivery(SCOPE, submitted["task_id"], "pending", ROUTE)
    assert jobs.outbox.claim(ROUTE["instance_uuid"]) is None
    task_id, _ = finished(jobs)
    old = time.time() - 8 * 86400
    with jobs.tasks._db() as db:
        db.execute("UPDATE ql_tasks SET created_at=?,updated_at=? WHERE id=?", (old, old, task_id))
    assert jobs.prune() == 0
    assert jobs._path(SCOPE, task_id, "result").exists()
    jobs.cancel(SCOPE, task_id)
    assert jobs.prune() == 1
    with jobs.tasks._db() as db:
        assert db.execute("SELECT 1 FROM ql_file_outbox WHERE task_id=?", (task_id,)).fetchone() is None


@pytest.fixture
def host_modules(monkeypatch):
    context = ModuleType("langbot.pkg.api.http.context")
    context.ExecutionContext = SimpleNamespace
    boundary = ModuleType("langbot.pkg.core.task_boundary")
    async def uow(ap, workspace, operation):
        assert workspace == ROUTE["workspace_uuid"]
        return await operation()
    boundary.run_in_workspace_uow = AsyncMock(side_effect=uow)
    monkeypatch.setitem(sys.modules, context.__name__, context)
    monkeypatch.setitem(sys.modules, boundary.__name__, boundary)
    return boundary


def test_real_resolver_uses_scoped_persisted_and_runtime_identity(host_modules):
    async def scenario():
        ap, bot = runtime()
        assert await resolve_bot(ap, SCOPE, ROUTE) is bot
        context, bot_id = ap.bot_service.get_bot.call_args.args
        assert vars(context) == {**ROUTE, "bot_uuid": SCOPE.bot_id}
        assert bot_id == SCOPE.bot_id
        assert ap.bot_service.get_bot.call_args.kwargs == {"include_secret": False}
        host_modules.run_in_workspace_uow.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["instance", "persisted-disabled", "adapter", "runtime-disabled",
                                   "bot-id", "workspace", "generation", "missing-bot", "disconnected"])
def test_real_resolver_fails_closed_before_any_send(host_modules, mutation):
    async def scenario():
        ap, bot = runtime()
        if mutation == "instance":
            ap.workspace_service.instance_uuid = "other"
        elif mutation == "persisted-disabled":
            ap.bot_service.get_bot.return_value["enable"] = False
        elif mutation == "adapter":
            ap.bot_service.get_bot.return_value["adapter"] = "other"
        elif mutation == "runtime-disabled":
            bot.enable = False
        elif mutation == "bot-id":
            bot.bot_entity.uuid = "other"
        elif mutation == "workspace":
            bot.bot_entity.workspace_uuid = "other"
        elif mutation == "generation":
            bot.execution_context.placement_generation += 1
        elif mutation == "missing-bot":
            ap.platform_mgr.get_bot_by_uuid.return_value = None
        else:
            bot.adapter.bot.connection.closed = True
        with pytest.raises((DeliveryStopped, DeliveryUnavailable)):
            await resolve_bot(ap, SCOPE, ROUTE)
        bot.adapter.send_message.assert_not_called()
    asyncio.run(scenario())


def test_host_patch_applies_idempotently_and_drains_before_platform_shutdown(tmp_path):
    spec = importlib.util.spec_from_file_location("outbox_patch_installer", ROOT / "scripts/Apply-RuntimePatches.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    target = tmp_path / "src/langbot/pkg/core/app.py"
    target.parent.mkdir(parents=True)
    target.write_text((ROOT / "tests/fixtures/file_outbox/app.py.txt").read_text(encoding="utf-8"), encoding="utf-8")
    patch = (ROOT / "patches/file-outbox-host.patch").read_text(encoding="utf-8")
    plan = module.build_plan("file-outbox-host.patch", tmp_path, patch, "host")
    module.execute_plans([plan], apply=True)
    source = target.read_text(encoding="utf-8")
    compile(source, str(target), "exec")
    assert source.index("await file_outbox.task") < source.index("await self.platform_mgr.shutdown()")
    assert source.count("run_file_outbox(self)") == 1
    assert module.build_plan("file-outbox-host.patch", tmp_path, patch, "host").state == "already-applied"
