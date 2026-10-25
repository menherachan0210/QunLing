import asyncio
from dataclasses import replace

import pytest

from qunling.assets import AssetError, AssetStore
from qunling.scope import Scope
from qunling.tasks import TaskStore, TaskWorker, TaskStatus


def test_old_task_cannot_be_starved_by_lower_scope_hash(tmp_path):
    async def scenario():
        clock = [10.0]
        store = TaskStore(tmp_path / "tasks.db", clock=lambda: clock[0])
        low, high = sorted([Scope("ws", "bot", "group:1", "a"), Scope("ws", "bot", "group:1", "b")], key=lambda s: s.key)
        old = store.create(high, "processing", "old", "old")
        clock[0] += 1
        store.create(low, "processing", "new", "new")
        started = []
        async def handle(scope, payload, progress):
            started.append(scope.actor_id)
            return {}
        worker = TaskWorker(store, handle, load_payload=lambda _: {}, concurrency=1)
        await worker.run_once()
        await worker.wait_idle()
        assert started == [high.actor_id]
        assert store.get(high, old.id).status == TaskStatus.COMPLETED
        await worker.close()
    asyncio.run(scenario())


def test_member_and_group_quotas_leave_capacity_for_other_conversations(tmp_path):
    store = AssetStore(tmp_path, member_bytes=4, conversation_bytes=6)
    alice = Scope("ws", "bot", "group:1", "alice")
    bob = replace(alice, actor_id="bob")
    other = replace(alice, conversation_id="group:2")
    store.put(alice, "one.txt", b"1234")
    with pytest.raises(AssetError, match="你在当前会话"):
        store.put(alice, "two.txt", b"x")
    store.put(bob, "one.txt", b"12")
    with pytest.raises(AssetError, match="当前会话的文件总额度"):
        store.put(bob, "two.txt", b"x")
    assert store.put(other, "one.txt", b"1234")["size"] == 4
