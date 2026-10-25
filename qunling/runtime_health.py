"""Bounded plugin transport probes; cached health never certifies a replacement."""
import asyncio
import os
import time

PING_TIMEOUT = 5.0
HEALTH_TTL = 50.0
WORKER_READY_TIMEOUT = 120.0
AVAILABILITY_TIMEOUT = 90.0
OPTIONAL_PLUGINS = {('local', 'QQTextReader'), ('local', 'QQGroupContext'),
                    ('local', 'QQWebSearch'), ('local', 'QQComicTranslator'),
                    ('langbot-team', 'AIImagePlugin'), ('langbot-team', 'LongTermMemory')}


def core_worker(plugin):
    """In managed deployments the wizard checks selected feature readiness.

    Optional worker initialization must not deadlock base chat or the management
    API needed to deselect a failed feature. Unknown/third-party workers retain
    the original strict barrier.
    """
    metadata = plugin.get('manifest', {}).get('manifest', {}).get('metadata', {})
    return not os.environ.get('QUNLING_DATA_DIR') or (metadata.get('author'), metadata.get('name')) not in OPTIONAL_PLUGINS


def transport_closed(handler):
    return (getattr(handler, '_closed', False) is True
            or getattr(getattr(handler, 'conn', None), '_closed', False) is True)


def recover_closed(connector, handler):
    """Only the current connection may initiate recovery; never replay work."""
    if getattr(connector, 'handler', None) is handler and not connector._closing:
        connector._connected.clear()
        connector._qunling_workers_ready = None
        connector.schedule_reconnect()


async def wait_workers_ready(connector, handler):
    """A control-plane ping does not certify asynchronously launched workers."""
    connector._qunling_workers_ready = None
    async with asyncio.timeout(WORKER_READY_TIMEOUT):
        while True:
            if getattr(connector, 'handler', None) is not handler or connector._closing:
                raise RuntimeError('Plugin runtime changed during worker initialization')
            states = tuple(getattr(connector, '_known_desired_states', {}).values())
            ready = True
            for state in states:
                if not state.enabled:
                    continue
                # Retain exact installation authority, including Workspace and revision.
                with handler.installation_scope(state.binding):
                    plugins = await handler.list_plugins()
                if not plugins or any(p.get('status') != 'initialized' for p in plugins if core_worker(p)):
                    ready = False
                    break
            if ready:
                if getattr(connector, 'handler', None) is not handler or connector._closing:
                    raise RuntimeError('Plugin runtime changed during worker initialization')
                connector._qunling_workers_ready = handler
                return
            await asyncio.sleep(.5)


async def wait_available(connector):
    """Hold new events during recovery, before any plugin side effects occur."""
    if getattr(connector, '_transport_task', None) is None or not connector.is_enable_plugin:
        return
    async with asyncio.timeout(AVAILABILITY_TIMEOUT):
        while True:
            handler = getattr(connector, 'handler', None)
            if handler is not None and transport_closed(handler):
                recover_closed(connector, handler)
            if (handler is not None and connector._connected.is_set()
                    and getattr(connector, '_qunling_workers_ready', None) is handler):
                return
            if connector._closing:
                raise RuntimeError('Plugin runtime is shutting down')
            await asyncio.sleep(.1)


async def ping(connector):
    handler = connector._runtime_handler()
    try:
        # The SDK's response timer starts after send. Cover lock acquisition,
        # serialization and pipe backpressure as well as the peer's response.
        async with asyncio.timeout(PING_TIMEOUT):
            result = await handler.ping()
    except BaseException as exc:
        connector._qunling_probe = (handler, time.monotonic(), False)
        # Closed channels cannot heal after another three heartbeat attempts.
        from langbot_plugin.entities.io.errors import ConnectionClosedError
        if isinstance(exc, ConnectionClosedError) or transport_closed(handler):
            recover_closed(connector, handler)
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
                   and getattr(connector, "handler", None) is handler
                   and getattr(connector, '_qunling_workers_ready', None) is handler
                   and not transport_closed(handler))
    return {"state": "ready" if healthy else "recovering" if fresh else "unknown",
            "healthy": healthy, **({"probe_id": str(checked)} if healthy else {})}
