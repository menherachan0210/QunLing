"""Bounded plugin transport probes; cached health never certifies a replacement."""
import asyncio
import time

PING_TIMEOUT = 5.0
HEALTH_TTL = 50.0


async def ping(connector):
    handler = connector._runtime_handler()
    try:
        # The SDK's response timer starts after send. Cover lock acquisition,
        # serialization and pipe backpressure as well as the peer's response.
        async with asyncio.timeout(PING_TIMEOUT):
            result = await handler.ping()
    except BaseException:
        connector._qunling_probe = (handler, time.monotonic(), False)
        raise
    connector._qunling_probe = (handler, time.monotonic(), True)
    return result


def snapshot(connector):
    if connector is None:
        return {"state": "unknown", "healthy": False}
    if not connector.is_enable_plugin:
        return {"state": "disabled", "healthy": False}
    handler, checked, ok = getattr(connector, "_qunling_probe", (None, 0, False))
    connected = getattr(connector, "_connected", None)
    fresh = handler is not None and 0 <= time.monotonic() - checked <= HEALTH_TTL
    healthy = bool(fresh and ok and connected and connected.is_set()
                   and getattr(connector, "handler", None) is handler)
    return {"state": "ready" if healthy else "recovering" if fresh else "unknown",
            "healthy": healthy}
