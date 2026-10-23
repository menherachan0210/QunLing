"""Finite retries for explicitly sourced read-only plugin operations only."""
import asyncio

from qunling import request_journal as journal

READ_OPERATIONS = frozenset({
    ('local/QQGroupContext', 'read_recent_group_context'),
    ('local/QQGroupContext', 'query_group_members'),
    ('local/QQTextReader', 'read_text_attachment'),
    ('local/QQWebSearch', 'read_web_page'),
    ('local/QQWebSearch', 'list_web_files'),
})
TRANSIENT = frozenset({'runtime_unavailable', 'tool_timeout', 'upstream_timeout', 'upstream_unavailable', 'network'})
ATTEMPT_TIMEOUT = 60.0
RETRY_DELAY = .5


async def invoke_plugin(ap, query, invoke, name, source_id):
    task = getattr(query, '_qq_task', None)
    eligible = task is not None and (source_id, name) in READ_OPERATIONS
    for attempt in range(1, 3 if eligible else 2):
        if task is not None and task.done.is_set():
            raise asyncio.CancelledError()
        try:
            if eligible:
                from qunling.qq_dispatch import _authorized_notice
                from qunling.runtime_health import wait_available
                async with asyncio.timeout(ATTEMPT_TIMEOUT):
                    await wait_available(ap.plugin_connector)
                    await _authorized_notice(ap, query)
                    if task.done.is_set():
                        raise asyncio.CancelledError()
                    journal.record(query, 'tool', tool=name, attempt=attempt, uncertain=False)
                    return await invoke()
            return await invoke()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = journal.classify(exc, phase='tool')
            journal.failure(query, exc)
            if not eligible or attempt == 2 or reason not in TRANSIENT:
                object.__setattr__(query, '_request_tool_failed', True)
                raise
            journal.record(query, 'retry_wait', reason=reason, tool=name, attempt=2)
            await asyncio.sleep(RETRY_DELAY)
    raise AssertionError('Unreachable retry state')
