"""Read-only OneBot health evidence, independent of cached account identity."""

from __future__ import annotations

import asyncio
import time


PROBE_TIMEOUT_SECONDS = 3.0


def _connection(runtime_bot):
    if runtime_bot is None or getattr(runtime_bot, "enable", False) is not True:
        return None
    adapter = getattr(runtime_bot, "adapter", None)
    connection = getattr(getattr(adapter, "bot", None), "connection", None)
    if connection is None or getattr(connection, "closed", True) is not False:
        return None
    return connection


def _result(state, transport_connected, online=None):
    return {
        "state": state,
        "transport_connected": transport_connected,
        "online": online,
        "checked_at": time.time(),
    }


async def probe(runtime_bot):
    """Verify a live socket and a bounded QQ status round trip; never send chat.

    The caller must resolve the runtime through LangBot's authorized workspace
    lookup. Account IDs are deliberately ignored because they survive logout.
    """
    connection = _connection(runtime_bot)
    if connection is None:
        return _result("disconnected", False)
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            status = await runtime_bot.adapter.call_platform_api("get_status", {})
    except Exception:
        # Do not expose platform/provider errors, credentials or account details.
        status = None
    current = _connection(runtime_bot)
    if current is None:
        return _result("disconnected", False)
    if current is not connection:
        # An answer from the previous socket cannot certify its replacement.
        return _result("unknown", True)
    if not isinstance(status, dict) or type(status.get("online")) is not bool:
        return _result("unknown", True)
    if status["online"] is False:
        return _result("offline", True, False)
    if status.get("good") is not True:
        return _result("unknown", True)
    return _result("online", True, True)
