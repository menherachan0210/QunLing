import json
import time
from types import SimpleNamespace as NS
import pytest

from qunling.qq_operation_store import OperationStore, record_native, scope_key
from qunling.qq_event_context import recent


def bot():
    return NS(execution_context=NS(instance_uuid="instance", workspace_uuid="workspace", placement_generation=1),
              bot_entity=NS(uuid="bot"))


@pytest.mark.asyncio
async def test_native_notices_and_requests_are_scoped_and_resettable(tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    runtime = bot()
    adapter = NS(_runtime=runtime)
    record_native(adapter, {"post_type": "notice", "notice_type": "notify", "sub_type": "poke", "group_id": "300",
                            "user_id": "100", "target_id": "200", "time": time.time()})
    record_native(adapter, {"post_type": "request", "request_type": "friend", "user_id": "100",
                            "flag": "private-flag", "comment": "hi", "time": time.time()})
    rows = await recent(runtime, "group", "300")
    assert len(rows) == 1 and rows[0]["sub_type"] == "poke" and rows[0]["target_id"] == "200"
    assert not await recent(runtime, "group", "400")
    assert "private-flag" not in json.dumps(await recent(runtime, "person", "100"))
    scope = scope_key("instance", "workspace", "bot")
    store = OperationStore()
    key, _ = store.reserve(scope, "group:300", "query", "poke", {"user_id": "100"})
    store.finish(key, "confirmed", {})
    store.clear_events(scope, "group:300")
    assert not await recent(runtime, "group", "300")
    assert store.reserve(scope, "group:300", "query", "poke", {"user_id": "100"})[1]["already_attempted"]


@pytest.mark.asyncio
async def test_snapshot_excludes_notices_that_arrived_later(tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    runtime = bot()
    before = time.time() - 1
    store = OperationStore()
    store.event(scope_key("instance", "workspace", "bot"), "group:300", {"type": "poke"})
    assert not await recent(runtime, "group", "300", before)
