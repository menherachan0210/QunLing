"""Operational facts and source attribution must survive all conversation paths."""
import asyncio
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from qunling.activity import details, read_activity, tool_start, tool_finish
from qunling.dialogue import Understanding, build_frame
from qunling.dialogue_retrieval import collect
from qunling.media_context import capture
from qunling.proactive.engine import Message
from qunling.request_journal import Journal


def test_plain_question_does_not_acquire_a_peers_image_or_quote_ownership():
    photo = Message("bot", "g", "image", "alice", "", 1, has_media=True,
                    attachments=capture([NS(type="Image")], "image", "alice"))
    target = Message("bot", "g", "question", "bob", "你是不是反复下载？", 2)
    frame = build_frame(bot_id="bot", group_id="g", target=target, messages=[photo, target])
    assert frame["target"]["attachments"] == []
    assert frame["messages"][0]["attachments"] == [{"kind": "image", "source": "message", "message_id": "image", "sender_id": "alice"}]
    quoted = capture([NS(type="Quote", id="image", sender_id="alice", origin=[NS(type="Image")])], "question", "bob")
    frame = build_frame(bot_id="bot", group_id="g", target=replace(target, reply_to="image", attachments=quoted), messages=[photo])
    assert frame["target"]["attachments"][0]["sender_id"] == "alice"
    assert frame["target"]["attachments"][0]["source"] == "quote"
    assert "url" not in json.dumps(frame)


@pytest.mark.asyncio
async def test_banter_skips_readers_and_independent_needed_readers_overlap():
    readers = {key: AsyncMock(return_value=key) for key in ("memory", "web_knowledge", "shared_knowledge")}
    empty = {key: [] for key in readers}
    await collect(Understanding("接一句玩笑"), readers, empty)
    assert all(reader.await_count == 0 for reader in readers.values())
    entered, release = set(), asyncio.Event()
    async def read(key):
        entered.add(key)
        if len(entered) == 3:
            release.set()
        await asyncio.wait_for(release.wait(), .5)
        return key
    result = await collect(Understanding("比较旧讨论与网页", information_needs=("memory", "web")),
        {key: lambda key=key: read(key) for key in readers}, empty)
    assert entered == set(readers)
    assert result["memory"] == "memory"


@pytest.mark.asyncio
async def test_reader_failure_does_not_discard_other_evidence_and_cancel_propagates():
    async def broken():
        raise OSError("private credential")
    result = await collect(None, {"memory": broken, "shared_knowledge": AsyncMock(return_value=["usable"])},
                           {"memory": [], "shared_knowledge": []})
    assert result["shared_knowledge"] == ["usable"] and result["memory"] == []
    async def cancelled():
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await collect(None, {"memory": cancelled}, {"memory": []})


def test_activity_scopes_no_mutation_legacy_and_real_download_distinction(tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    path = tmp_path / "requests" / (hashlib.sha256(b"instance").hexdigest()[:32] + ".sqlite3")
    store = Journal(path, "instance", clock=lambda: 1000)
    for key, workspace, group in [("own", "workspace", "g"), ("foreign", "other", "g"), ("other-group", "workspace", "x")]:
        store.start(NS(query_uuid=key, message_event=NS(message_id="1")), ("instance", workspace, 1, "bot", group, "actor"))
        store.record(key, "tool_wait", tool="browse_group_files")
        store.record(key, "tool", tool="browse_group_files", details={"kind": "tool", "call_id": key,
                     "operation": "import", "outcome": "ok", "cache": "hit", "downloaded_bytes": 0,
                     "url": "https://private/?token=credential", "parameters": "never-store"})
        store.record(key, "ended", status="completed")
    before = path.read_bytes()
    result = read_activity("instance", "workspace", "bot", "g", now=1000)
    assert len(result["operations"]) == 1
    assert result["operations"][0]["downloaded_bytes"] == 0
    assert result["legacy_records_without_operation_detail"] is False
    assert path.read_bytes() == before
    raw = json.dumps(store.listing("workspace", "bot", request_id="own"))
    assert "credential" not in raw and "never-store" not in raw
    assert read_activity("instance", "other", "else", "g", now=1000)["operations"] == []
    with pytest.raises(ValueError):
        read_activity("instance", "workspace", "bot", "g", minutes=True)


def test_diagnostics_do_not_retain_raw_parameters_or_trust_invalid_numbers():
    result = details({"kind": "tool", "downloaded_bytes": float("inf"), "elapsed_ms": True,
                      "purpose": "https://secret.example/x 密码=never-store", "payload": "secret"})
    assert "downloaded_bytes" not in result and "elapsed_ms" not in result
    assert "never-store" not in json.dumps(result)
    assert tool_start("file_workspace", {"operation": "import", "password": "private"}, "request")["operation"] == "import"
    assert "private" not in json.dumps(tool_start("file_workspace", {"password": "private"}, "request"))


def test_tool_return_does_not_turn_pending_jobs_or_unknown_results_into_success():
    import time
    for value in ({"status": "running"}, {"status": {"untrusted": True}}, "plain result", None):
        assert tool_finish(value, time.monotonic())["outcome"] == "unconfirmed"
    assert tool_finish('{"status":"error"}', time.monotonic())["outcome"] == "error"
    assert tool_finish({"status": "ok", "io": {"cache": "hit", "downloaded_bytes": 0}}, time.monotonic())["downloaded_bytes"] == 0


def test_participation_trace_reaches_the_same_scoped_management_journal(tmp_path, monkeypatch):
    from qunling.participation_trace import record
    from qunling.request_journal import get_journal
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    bot = NS(ap=NS(), execution_context=NS(instance_uuid="instance", workspace_uuid="workspace", placement_generation=1),
             bot_entity=NS(uuid="bot"))
    context = NS(group_id="g", reply_candidate_ids=("m1",),
                 messages=(Message("bot", "g", "m1", "member", "private original text", 1),))
    record(bot, context, {"action": "research", "target_message_id": "m1", "understanding": {
        "purpose": "核对自身操作", "context_message_ids": ["m1"], "information_needs": ["activity"],
        "participation": {"role": "addressed", "entry_point": "回答下载质疑", "context_sufficient": True}}}, .25)
    store = get_journal(bot.ap, "instance")
    listing = store.listing("workspace", "bot", group="g")["requests"]
    assert len(listing) == 1 and listing[0]["origin"] == "participation" and listing[0]["status"] == "processed"
    row = store.listing("workspace", "bot", request_id=listing[0]["request_id"])["requests"][0]
    facts = row["events"][-1]["details"]
    assert facts["elapsed_ms"] == 250 and facts["target_message_id"] == "m1"
    assert facts["information_needs"] == ["activity"]
    assert "private original text" not in json.dumps(row)
    assert store.listing("other-workspace", "bot")["requests"] == []
    assert read_activity("instance", "workspace", "bot", "g")["operations"] == []
