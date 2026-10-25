from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from langbot.pkg.api.http.authz import Permission
from qunling.knowledge import host
from qunling.knowledge.config import defaults
from qunling.scope import Scope


@pytest.mark.asyncio
async def test_host_routes_require_manage_permission_and_actual_context_type():
    from langbot.pkg.api.http.context import RequestContext
    registrations = []
    class Router:
        def route(self, path, **kwargs):
            def decorate(function):
                registrations.append((path, kwargs, function))
                return function
            return decorate
    host.register_routes(Router())
    path, options, function = registrations[0]
    assert path == "/<bot_uuid>/knowledge" and options["permission"] == Permission.RESOURCE_MANAGE
    assert function.__annotations__["request_context"] is RequestContext


@pytest.mark.asyncio
async def test_admin_scope_is_checked_before_storage_or_start(monkeypatch):
    from qunling.proactive import host as proactive
    def forbidden(*args, **kwargs):
        raise AssertionError("storage must not open")
    monkeypatch.setattr(proactive, "_authorized_bot", AsyncMock(side_effect=PermissionError))
    monkeypatch.setattr(host.Store, "from_env", forbidden)
    with pytest.raises(PermissionError):
        await host.get_config(NS(), NS(), "other-bot")


@pytest.mark.asyncio
async def test_collector_start_once_stop_cancels_inflight_without_delivery(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    entered = asyncio.Event()
    cancelled = []
    async def collect(bot):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    monkeypatch.setattr(host, "collect", collect)
    bot = NS(bot_entity=NS(adapter="onebot-v11"))
    host.start(bot)
    first = bot._qunling_knowledge_task
    host.start(bot)
    assert bot._qunling_knowledge_task is first
    await entered.wait()
    await host.stop(bot)
    assert cancelled == [True] and first.cancelled()
