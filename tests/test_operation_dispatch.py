import asyncio
from contextlib import suppress
import json
from unittest.mock import AsyncMock

import pytest

from test_qq_dispatch import app, managed, put, until
from langbot.pkg.pipeline.controller import Controller
from qunling import qq_dispatch as dispatch


@pytest.mark.asyncio
async def test_identical_history_view_is_suppressed_but_pages_and_new_requests_are_not(managed):
    fetch = AsyncMock(return_value=json.dumps({"messages": [{"message_ref": "old", "text": "actual history"}]}))
    results = []

    async def run(query):
        query.pipeline_config = managed.pipeline.pipeline_entity.config
        async def read(parameters):
            return await dispatch.invoke_tool(managed, query, fetch, "read_recent_group_context",
                                              source="plugin", parameters=parameters)
        results.append([await read({}), await read({"limit": 60, "min_age_seconds": 0}),
                        await read({"before_ref": "old"})])

    managed.pipeline.run = run
    consumer = asyncio.create_task(Controller(managed).consumer())
    try:
        await put(managed, "a", "read history")
        await until(lambda: len(results) == 1)
        await put(managed, "a", "another request")
        await until(lambda: len(results) == 2)
        await asyncio.gather(*managed.workers)
        assert fetch.await_count == 4
        for first, repeated, older in results:
            assert json.loads(repeated)["status"] == "unchanged"
            assert "actual history" in first and "actual history" in older
        assert managed.dispatch.parked == 0
    finally:
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_failed_history_reads_are_retryable_and_mutations_are_never_deduplicated(managed):
    fetch = AsyncMock(return_value='{"status":"error"}')
    done = asyncio.Event()
    async def run(query):
        query.pipeline_config = managed.pipeline.pipeline_entity.config
        for _ in range(2):
            await dispatch.invoke_tool(managed, query, fetch, "read_recent_group_context", source="plugin", parameters={})
        for _ in range(2):
            await dispatch.invoke_tool(managed, query, fetch, "file_workspace", source="plugin", parameters={"operation": "create"})
        done.set()
    managed.pipeline.run = run
    consumer = asyncio.create_task(Controller(managed).consumer())
    try:
        await put(managed, "a", "request")
        await asyncio.wait_for(done.wait(), 3)
        await asyncio.gather(*managed.workers)
        assert fetch.await_count == 4
    finally:
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer
