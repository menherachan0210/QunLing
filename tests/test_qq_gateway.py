"""Actual Host action registration and query resolution; no platform writes."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / "langbot/src").is_dir():
    sys.path.insert(0, str(ROOT / "langbot/src"))
from langbot.pkg.plugin.handler import RuntimeConnectionHandler
from langbot_plugin.entities.io.actions.enums import PluginToRuntimeAction
from qunling import qq_dispatch
from test_qq_actions import service


@pytest.fixture
def gateway(service, tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    # The real SDK creates a transfer directory relative to its working path.
    # Keep that state outside the read-only source mount used by Linux CI.
    monkeypatch.chdir(tmp_path)
    binding = NS(instance_uuid="i", workspace_uuid="w", placement_generation=1)
    identity = NS(plugin_author="local", plugin_name="QQGroupContext")
    query = service.query
    query.instance_uuid, query.workspace_uuid, query.placement_generation = "i", "w", 1
    app = NS(query_pool=NS(get_query=AsyncMock(return_value=query)))
    query.adapter._runtime = NS(ap=app, adapter=query.adapter, enable=True, bot_entity=NS(enable=True))
    handler = RuntimeConnectionHandler(NS(), None, app)
    handler._require_runtime_action_context = lambda: binding
    handler._require_plugin_action_context = AsyncMock(return_value=(binding, identity))
    handler._require_active_action_context = AsyncMock()
    handler._get_plugin_setting_by_name = AsyncMock(return_value=NS(config={}))
    monkeypatch.setattr(qq_dispatch, "_authorized_notice", AsyncMock())
    return handler, query, identity


@pytest.mark.asyncio
async def test_actual_registered_gateway_returns_catalog_without_raw_adapter_api(gateway):
    handler, query, _ = gateway
    result = await handler.actions[PluginToRuntimeAction.CALL_PLATFORM_API.value]({"bot_uuid": "bot", "action": "qunling_qq",
        "params": {"query_uuid": "query1", "catalog": True, "category": "interaction"}})
    assert result.data["result"]["status"] == "ok", result
    assert any(row["operation"] == "poke" for row in result.data["result"]["operations"])


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["other_plugin", "other_workspace", "other_bot", "finished"])
async def test_gateway_rejects_forged_or_finished_query(gateway, invalid):
    handler, query, identity = gateway
    if invalid == "other_plugin":
        identity.plugin_name = "QQWebSearch"
    if invalid == "other_workspace":
        query.workspace_uuid = "other"
    if invalid == "finished":
        query._qq_task = NS(done=asyncio.Event(), continuation=None)
        query._qq_task.done.set()
    result = await handler.actions[PluginToRuntimeAction.CALL_PLATFORM_API.value]({"bot_uuid": "other" if invalid == "other_bot" else "bot",
        "action": "qunling_qq", "params": {"query_uuid": "query1", "operation": "poke", "arguments": {"user_id": "202"}}})
    assert result.data["result"]["status"] == "error", result
