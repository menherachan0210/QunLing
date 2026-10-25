import asyncio
import threading
from types import SimpleNamespace as NS

import pytest

from qunling.journal_writer import QueuedJournal
from qunling.request_journal import Journal


@pytest.mark.asyncio
async def test_slow_storage_does_not_block_loop_and_preserves_event_order(tmp_path):
    release, entered = threading.Event(), threading.Event()
    def slow_factory(*args):
        entered.set()
        assert release.wait(5)
        return Journal(*args)
    store = QueuedJournal(tmp_path / "journal.db", "instance", factory=slow_factory)
    query = NS(query_uuid="request", message_event=NS(message_id="1"), secret="never-retain")
    store.start(query, ("instance", "workspace", 1, "bot", "group", "actor"))
    store.record("request", "tool", tool="read_web_page")
    store.record("request", "ended", status="completed")
    await asyncio.sleep(0)
    assert not release.is_set()
    release.set()
    result = await asyncio.to_thread(store.listing, "workspace", "bot", request_id="request")
    assert [e["phase"] for e in result["requests"][0]["events"]] == ["received", "tool", "ended"]
    assert result["requests"][0]["status"] == "completed"
    assert not store.write_failed


def test_full_queue_degrades_diagnostics_without_blocking_business(tmp_path):
    release = threading.Event()
    def slow_factory(*args):
        release.wait(5)
        return Journal(*args)
    store = QueuedJournal(tmp_path / "journal.db", "instance", capacity=2, factory=slow_factory)
    for _ in range(10):
        store.record("request", "model")
    assert store.write_failed
    release.set()
    assert store.flush()
