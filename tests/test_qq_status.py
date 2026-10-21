"""QQ health regression cases; all transports and accounts are synthetic."""

from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from qunling import qq_status


ROOT = Path(__file__).resolve().parents[1]


def runtime(status=None):
    adapter = NS(
        bot_account_id="cached-account",
        bot=NS(connection=NS(closed=False)),
        call_platform_api=AsyncMock(return_value=status),
    )
    return NS(adapter=adapter, enable=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("online, expected", [(True, "online"), (False, "offline")])
async def test_live_status_round_trip(online, expected, monkeypatch):
    bot = runtime({"online": online, "good": True, "stat": {}})
    monkeypatch.setattr(qq_status.time, "time", lambda: 1000.0)
    result = await qq_status.probe(bot)
    assert result == {"state": expected, "transport_connected": True,
                      "online": online, "checked_at": 1000.0}
    bot.adapter.call_platform_api.assert_awaited_once_with("get_status", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "closed", "disabled", "no_runtime"])
async def test_cached_account_does_not_prove_connection(kind):
    bot = runtime({"online": True, "good": True})
    if kind == "missing":
        bot.adapter.bot.connection = None
    elif kind == "closed":
        bot.adapter.bot.connection.closed = True
    elif kind == "disabled":
        bot.enable = False
    result = await qq_status.probe(None if kind == "no_runtime" else bot)
    assert result["state"] == "disconnected"
    assert result["transport_connected"] is False
    assert result["online"] is None
    bot.adapter.call_platform_api.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    None, {}, [], True, {"online": "true", "good": True},
    {"online": 1, "good": True}, {"online": True},
    {"online": True, "good": False}, {"online": True, "good": "true"},
    {"data": {"online": True, "good": True}},
])
async def test_unconfirmed_status_never_becomes_online(status):
    result = await qq_status.probe(runtime(status))
    assert result["state"] == "unknown"
    assert result["online"] is None
    assert result["transport_connected"] is True


@pytest.mark.asyncio
async def test_platform_failure_is_unknown_without_leaking_error():
    bot = runtime()
    bot.adapter.call_platform_api.side_effect = RuntimeError("private credentials")
    result = await qq_status.probe(bot)
    assert result["state"] == "unknown"
    assert "private" not in repr(result)


@pytest.mark.asyncio
async def test_silent_transport_is_bounded_and_pending_call_cancelled(monkeypatch):
    cancelled = asyncio.Event()

    async def never_answers(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    bot = runtime()
    bot.adapter.call_platform_api.side_effect = never_answers
    monkeypatch.setattr(qq_status, "PROBE_TIMEOUT_SECONDS", 0.01)
    result = await asyncio.wait_for(qq_status.probe(bot), timeout=0.5)
    assert result["state"] == "unknown"
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_converted_to_health_result():
    bot = runtime()
    bot.adapter.call_platform_api.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await qq_status.probe(bot)


@pytest.mark.asyncio
@pytest.mark.parametrize("change, expected, connected", [
    ("closed", "disconnected", False), ("removed", "disconnected", False),
    ("replaced", "unknown", True), ("disabled", "disconnected", False),
])
async def test_response_from_previous_connection_cannot_certify_new_state(change, expected, connected):
    bot = runtime()

    async def disconnect_during_probe(*args):
        if change == "closed":
            bot.adapter.bot.connection.closed = True
        elif change == "removed":
            bot.adapter.bot.connection = None
        elif change == "replaced":
            bot.adapter.bot.connection = NS(closed=False)
        else:
            bot.enable = False
        return {"online": True, "good": True}

    bot.adapter.call_platform_api.side_effect = disconnect_during_probe
    result = await qq_status.probe(bot)
    assert result["state"] == expected
    assert result["transport_connected"] is connected
    assert result["online"] is None


@pytest.mark.asyncio
async def test_reconnection_and_logout_use_current_evidence_each_time():
    bot = runtime()
    bot.adapter.call_platform_api.side_effect = [
        {"online": True, "good": True}, {"online": False, "good": True},
        RuntimeError("temporary error"), {"online": True, "good": True},
    ]
    assert [(await qq_status.probe(bot))["state"] for _ in range(4)] == [
        "online", "offline", "unknown", "online",
    ]


def _service_method():
    path = ROOT / "langbot/src/langbot/pkg/api/http/service/bot.py"
    if not path.is_file():
        pytest.skip("Pinned LangBot checkout is not installed")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BotService")
    method = copy.deepcopy(next(node for node in cls.body if getattr(node, "name", "") == "get_runtime_bot_info"))
    module = ast.Module(body=[method], type_ignores=[])
    namespace = {"TenantContext": object, "WorkspaceNotFoundError": LookupError}
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[method.name]


@pytest.mark.asyncio
async def test_health_is_added_to_existing_workspace_scoped_bot_response():
    bot = runtime({"online": True, "good": True})
    context = object()
    service = NS(
        get_bot=AsyncMock(return_value={"adapter": "onebot-v11", "uuid": "target"}),
        ap=NS(platform_mgr=NS(get_bot_by_uuid=AsyncMock(return_value=bot))),
        _adapter_declares_webhook_url=lambda _: False,
    )
    result = await _service_method()(service, context, "target", include_secret=False)
    service.get_bot.assert_awaited_once_with(context, "target", False)
    service.ap.platform_mgr.get_bot_by_uuid.assert_awaited_once_with(context, "target")
    assert result["adapter_runtime_values"]["qq_status"]["state"] == "online"
    assert result["adapter_runtime_values"]["bot_account_id"] == "cached-account"


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_failure", [False, True])
async def test_unavailable_or_denied_bot_cannot_reach_probe(lookup_failure):
    lookup = AsyncMock(return_value=None)
    if lookup_failure:
        lookup.side_effect = LookupError("workspace denied")
    service = NS(get_bot=lookup, ap=NS(platform_mgr=NS(get_bot_by_uuid=AsyncMock())))
    with pytest.raises(LookupError):
        await _service_method()(service, object(), "foreign")
    service.ap.platform_mgr.get_bot_by_uuid.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_platforms_do_not_receive_qq_probe():
    bot = runtime({"online": True, "good": True})
    service = NS(
        get_bot=AsyncMock(return_value={"adapter": "telegram-omni"}),
        ap=NS(platform_mgr=NS(get_bot_by_uuid=AsyncMock(return_value=bot))),
        _adapter_declares_webhook_url=lambda _: False,
    )
    result = await _service_method()(service, object(), "target")
    assert "qq_status" not in result["adapter_runtime_values"]
    bot.adapter.call_platform_api.assert_not_awaited()


def test_health_patch_is_registered_and_round_trips_independently(tmp_path):
    spec = importlib.util.spec_from_file_location("qq_status_patcher", ROOT / "scripts/Apply-RuntimePatches.py")
    patcher = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = patcher
    spec.loader.exec_module(patcher)
    assert ("host-qq-status.patch", "host", False) in patcher.PATCHES
    name = "host-qq-status.patch"
    source = (ROOT / "patches" / name).read_text(encoding="utf-8")
    _, paths = patcher.runtime_payload(source, "host")
    # No other installed patch changes this target, so upgrade and clean-install
    # preflight see identical context and retain their reverse-check guarantees.
    for other, component, _ in patcher.PATCHES:
        if other == name or component != "host":
            continue
        _, other_paths = patcher.runtime_payload((ROOT / "patches" / other).read_text(encoding="utf-8"), component)
        assert not set(paths) & set(other_paths)
    before = (
        "        if runtime_bot is not None:\n"
        "            adapter_runtime_values['bot_account_id'] = runtime_bot.adapter.bot_account_id\n\n"
        "        # Webhook URL for adapters that declare a generated webhook config item.\n"
        "        # This is manifest-driven so EBA adapters do not need to be mirrored in a\n"
        "        # second hard-coded list.\n"
    )
    target = tmp_path / paths[0]
    target.parent.mkdir(parents=True)
    target.write_text(before, encoding="utf-8")
    plan = patcher.build_plan(name, tmp_path, source, "host")
    assert plan.state == "ready"
    patcher.execute_plans([plan], apply=True)
    second = patcher.build_plan(name, tmp_path, source, "host")
    assert second.state == "already-applied"
    patcher.execute_plans([second], apply=True)
    assert target.read_text(encoding="utf-8").count("await probe_qq_status(runtime_bot)") == 1
    assert patcher.git_apply(tmp_path, plan.payload, "--reverse").returncode == 0
    assert target.read_text(encoding="utf-8") == before
