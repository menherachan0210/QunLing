"""Stage progress must protect replies without allowing unbounded execution."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from test_qq_dispatch import managed, put
from test_qq_sessions import app
from qunling import qq_dispatch as dispatch, request_journal as journal


@pytest.mark.parametrize('phase', ['running', 'resuming', 'queued', 'tool', 'tool_wait'])
@pytest.mark.parametrize('finished', [False, True])
def test_only_live_running_work_gets_stage_budget(phase, finished):
    done = asyncio.Event()
    if finished:
        done.set()
    task = NS(phase=phase, changed=1, done=done, terminal='')
    query = NS(_qq_task=task)
    journal.stage_started(query, type('Processor', (), {})())
    assert (task.changed > 1) == (not finished and phase in {'running', 'resuming'})


@pytest.mark.asyncio
async def test_preparation_does_not_spend_model_budget(managed):
    query = await put(managed, 'image-sender', 'inspect images')
    task = query._qq_task
    task.phase = 'running'
    task.changed -= 150
    journal.stage_started(query, type('Processor', (), {})())
    # The next real watchdog tick would have cancelled the old implementation.
    await asyncio.sleep(1.1)
    assert not task.done.is_set() and task.terminal == ''
    task.changed -= 150
    await asyncio.wait_for(task.done.wait(), 2)
    assert task.terminal == 'execution_timeout'


@pytest.mark.asyncio
async def test_progress_cannot_extend_overall_deadline(managed, monkeypatch):
    from qunling import runtime_health
    monkeypatch.setattr(runtime_health, 'wait_available', AsyncMock())
    query = await put(managed, 'image-sender', 'inspect images')
    managed.dispatch.run_timeout = .05
    managed.dispatch.barrier = AsyncMock()

    async def advance(q):
        while True:
            journal.stage_started(q, type('Processor', (), {})())
            await asyncio.sleep(.005)

    await dispatch.run_pipeline(managed, query, NS(run=advance))
    assert query._qq_task.done.is_set()
    assert query._qq_task.terminal == 'execution_timeout'
    assert managed.adapter.reply_message.await_count == 1
