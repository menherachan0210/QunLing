import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from pkg.tools import execute
from pkg import tools, group_api
from qunling import activity


@pytest.mark.asyncio
async def test_activity_uses_verified_binding_and_current_group_only(monkeypatch):
    context = NS(bot="bot", group="100", sender="200")
    binding = NS(instance_uuid="instance", workspace_uuid="workspace")
    plugin = NS(contexts=NS(get=Mock(return_value=context)), get_installation_binding=lambda: binding)
    api = NS(get_query_vars=AsyncMock(return_value={}), get_bot_uuid=AsyncMock(return_value="bot"))
    monkeypatch.setattr(tools, "QueryBasedAPIProxy", lambda **kwargs: api)
    plugin.plugin_runtime_handler = None
    access = AsyncMock()
    reader = Mock(return_value={"available": True, "operations": []})
    monkeypatch.setattr(group_api, "validate_group_access", access)
    monkeypatch.setattr(activity, "read_activity", reader)
    tool = NS(plugin=plugin)
    result = json.loads(await execute(tool, "activity", {"minutes": 10}, 1, "query"))
    assert result["available"] is True
    reader.assert_called_once_with("instance", "workspace", "bot", "100", minutes=10, limit=20)
    access.assert_awaited_once_with(plugin, context)
    reader.reset_mock()
    assert json.loads(await execute(tool, "activity", {"group_id": "other"}, 1, "query"))["status"] == "error"
    reader.assert_not_called()
    api.get_bot_uuid.return_value = "other-bot"
    assert json.loads(await execute(tool, "activity", {}, 1, "query"))["status"] == "error"
    reader.assert_not_called()


@pytest.mark.asyncio
async def test_unbound_installation_cannot_read_server_journals(monkeypatch):
    plugin = NS(contexts=NS(get=Mock(return_value=NS(bot="bot"))),
                get_installation_binding=lambda: None, plugin_runtime_handler=None)
    api = NS(get_query_vars=AsyncMock(return_value={}), get_bot_uuid=AsyncMock(return_value="bot"))
    monkeypatch.setattr(tools, "QueryBasedAPIProxy", lambda **kwargs: api)
    reader = Mock()
    monkeypatch.setattr(activity, "read_activity", reader)
    assert json.loads(await execute(NS(plugin=plugin), "activity", {}, 1, "query"))["status"] == "error"
    reader.assert_not_called()
