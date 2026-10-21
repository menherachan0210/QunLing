"""Offline contract tests for the trusted Host bridge; never send to QQ."""
from __future__ import annotations

import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import typing
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from qunling.proactive import host
from qunling.proactive.config import ConfigStore, DEFAULT_GROUP, ConfigError
from langbot.pkg.api.http.authz import PermissionDeniedError
from langbot.pkg.api.http.context import ExecutionContext
from langbot_plugin.api.entities.builtin.platform.message import At, Image, MessageChain, Plain
from langbot_plugin.api.entities.builtin.provider.message import Message as ModelMessage

ROOT = Path(__file__).resolve().parents[1]
LANGBOT_ROOT = Path(next(iter(importlib.util.find_spec("langbot").submodule_search_locations))).parents[1]


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    context = ExecutionContext(instance_uuid="instance", workspace_uuid="workspace", placement_generation=1, bot_uuid="bot")
    adapter = NS(bot_account_id="10000", bot=NS(connection=NS(closed=False)),
                 send_message=AsyncMock(return_value=NS(message_id="reply-1")),
                 get_group_list=AsyncMock(return_value=[NS(id="30000", name="测试群")]))
    entity = NS(uuid="bot", workspace_uuid="workspace", adapter="onebot-v11", enable=True)
    bot = NS(adapter=adapter, bot_entity=entity, enable=True, execution_context=context,
             assert_execution_active=AsyncMock(), get_pipeline_target_for_event_type=lambda event: "pipeline")
    adapter._runtime = bot
    provider = NS(invoke_llm=AsyncMock(return_value=ModelMessage(role="assistant", content="自然接话")))
    model = NS(provider=provider)
    pipeline = NS(pipeline_entity=NS(config={"ai": {"runner": {"id": "runner"},
                  "runner_config": {"runner": {"model": {"primary": "chat-model"}}}}}))
    bot.ap = NS(workspace_service=NS(instance_uuid="instance"),
                bot_service=NS(get_bot=AsyncMock(return_value={"enable": True, "adapter": "onebot-v11"})),
                platform_mgr=NS(get_bot_by_uuid=AsyncMock(return_value=bot)),
                pipeline_mgr=NS(get_pipeline_by_uuid=AsyncMock(return_value=pipeline)),
                model_mgr=NS(get_model_by_uuid=AsyncMock(return_value=model)))
    store = ConfigStore("bot", tmp_path / "proactive")
    state = store.read()
    state.update(enabled=True, groups={"30000": {**copy.deepcopy(DEFAULT_GROUP), "mode": "natural"}})
    store.save(state, known_group_ids={"30000"})
    value = host.HostBridge(bot, store=store)
    bot._qunling_proactive = value
    value.engine._debounce_scale = 0
    value.engine._judge = AsyncMock(return_value={"action": "silence", "target_message_id": None})
    value.engine._generate = AsyncMock(return_value="自然接话")
    value.engine.refresh_configuration()
    return value


def event(text="这个话题你怎么看", *, group="30000", sender="20000", explicit=False, message_id="m1"):
    chain = MessageChain(([At(target="10000")] if explicit else []) + ([Plain(text=text)] if text else [Image(url="https://invalid.example/test.png")]))
    return NS(type="message.received", adapter_name="onebot-v11", group=NS(id=group), sender=NS(id=sender, nickname="群友"),
              message_chain=chain, message_id=message_id, timestamp=time.time())


def admin_context(role="admin", workspace="workspace"):
    return NS(workspace_uuid=workspace, workspace=NS(permissions=frozenset({"resource.manage"}) if role == "admin" else frozenset()))


@pytest.mark.asyncio
async def test_unlisted_groups_and_self_messages_have_no_model_or_buffer(bridge):
    for item in (event(group="90000"), event(sender="10000")):
        host.observe(bridge.bot, item, bridge.bot.adapter)
    await bridge.engine.drain()
    assert bridge.engine._groups == {}
    bridge.engine._judge.assert_not_called()
    bridge.bot.adapter.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_image_holds_until_query_removed(bridge):
    request = event(text="", explicit=True)
    host.observe(bridge.bot, request, bridge.bot.adapter)
    host.queued(bridge.bot.adapter, request)
    host.ingress_done(bridge.bot, request)
    host.observe(bridge.bot, event(message_id="other"), bridge.bot.adapter)
    await bridge.engine.drain()
    assert bridge.engine.status()["groups"]["30000"]["explicit_busy"]
    bridge.engine._judge.assert_not_called()
    host.completed(bridge.bot.adapter, request)
    assert not bridge.engine.status()["groups"]["30000"]["explicit_busy"]


@pytest.mark.asyncio
async def test_failed_ingress_releases_explicit_reservation(bridge):
    request = event(explicit=True)
    host.observe(bridge.bot, request, bridge.bot.adapter)
    host.ingress_done(bridge.bot, request)
    assert not bridge.engine.status()["groups"]["30000"]["explicit_busy"]


@pytest.mark.asyncio
async def test_other_person_mention_does_not_trigger_judgment(bridge):
    item = event()
    item.message_chain = MessageChain([At(target="20001"), Plain(text="你的观点呢？")])
    host.observe(bridge.bot, item, bridge.bot.adapter)
    await bridge.engine.drain()
    bridge.engine._judge.assert_not_called()


@pytest.mark.asyncio
async def test_stale_message_is_not_a_new_participation(bridge):
    item = event()
    item.timestamp -= 1000
    host.observe(bridge.bot, item, bridge.bot.adapter)
    await bridge.engine.drain()
    bridge.engine._judge.assert_not_called()


@pytest.mark.asyncio
async def test_completion_reuses_scoped_model_without_tools_or_query(bridge):
    result = await bridge._complete("system rules", {"messages": ["user text"]})
    assert result == "自然接话"
    call = bridge.bot.ap.model_mgr.get_model_by_uuid
    call.assert_awaited_once_with(bridge.bot.execution_context, "chat-model")
    args = call.return_value.provider.invoke_llm.await_args.kwargs
    assert args["query"] is None and args["funcs"] == []
    assert args["execution_context"] is bridge.bot.execution_context
    assert args["extra_args"]["tools"] is None
    assert [item.role for item in args["messages"]] == ["system", "user"]
    bridge.bot.adapter.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_tool_response_is_never_executed_or_sent(bridge):
    model = bridge.bot.ap.model_mgr.get_model_by_uuid.return_value
    model.provider.invoke_llm.return_value = NS(content="done", tool_calls=[{"name": "send_file"}])
    with pytest.raises(host.ProactiveUnavailable):
        await bridge._complete("system", {})
    bridge.bot.adapter.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_actual_pinned_provider_query_none_and_message_content(bridge, monkeypatch):
    from langbot.pkg.provider.modelmgr.requester import RuntimeLLMModel, RuntimeProvider
    from langbot.pkg.provider.modelmgr.requesters import litellmchat

    requester = litellmchat.LiteLLMRequester(bridge.bot.ap, {"custom_llm_provider": "openai"})
    # Only the network boundary is mocked. Both pinned requester argument
    # assembly and RuntimeProvider response/usage conversion execute normally.
    network = AsyncMock(return_value=NS(choices=[NS(message=NS(model_dump=lambda: {
        "role": "assistant", "content": "自然接话",
    }))], usage=None))
    monkeypatch.setattr(litellmchat, "acompletion", network)
    context = bridge.bot.execution_context
    provider = RuntimeProvider(context, NS(uuid="provider", workspace_uuid="workspace", requester="openai-chat-completions"),
                               NS(get_token=lambda: "offline-test-placeholder"), requester)
    entity = NS(uuid="chat-model", workspace_uuid="workspace", provider_uuid="provider", name="offline-model",
                extra_args={"tools": [{"type": "function", "function": {"name": "forbidden"}}]},
                reasoning_config=None, abilities=[])
    model = RuntimeLLMModel(context, entity, provider)
    bridge.bot.ap.model_mgr.get_model_by_uuid.return_value = model
    assert await bridge._complete("rules", {"messages": []}) == "自然接话"
    args = network.await_args.kwargs
    assert args["tools"] is None and args["tool_choice"] is None
    assert args["functions"] is None and args["function_call"] is None
    assert args["messages"][0] == {"role": "system", "content": "rules"}
    bridge.bot.adapter.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_sdk_text_content_elements_are_supported(bridge):
    provider = bridge.bot.ap.model_mgr.get_model_by_uuid.return_value.provider
    provider.invoke_llm.return_value = ModelMessage(role="assistant", content=[{"type": "text", "text": "自然接话"}])
    assert await bridge._complete("rules", {}) == "自然接话"


@pytest.mark.asyncio
async def test_known_other_local_bot_cannot_trigger_reply_loop(bridge):
    bridge.bot.ap.platform_mgr.bots = [NS(execution_context=bridge.bot.execution_context, adapter=NS(bot_account_id="20000"))]
    host.observe(bridge.bot, event(), bridge.bot.adapter)
    await bridge.engine.drain()
    assert bridge.engine._groups == {}
    bridge.engine._judge.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "replaced", "foreign", "offline"])
async def test_stale_or_disabled_runtime_never_sends(bridge, change):
    if change == "disabled":
        bridge.bot.ap.bot_service.get_bot.return_value["enable"] = False
    elif change == "replaced":
        bridge.bot.ap.platform_mgr.get_bot_by_uuid.return_value = NS()
    elif change == "foreign":
        bridge.bot.bot_entity.workspace_uuid = "other"
    else:
        bridge.bot.adapter.bot.connection.closed = True
    with pytest.raises(host.ProactiveUnavailable):
        await bridge.send("30000", "hello")
    bridge.bot.adapter.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_configuration_change_during_send_validation_cancels_old_reply(bridge):
    async def mutate():
        value = bridge.store.read()
        value["groups"]["30000"]["persona"] = "修改了性格"
        bridge.store.save(value, known_group_ids={"30000"})
    bridge._active = mutate
    with pytest.raises(host.ProactiveUnavailable):
        await bridge.send("30000", "准备好的旧回复")
    bridge.bot.adapter.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_send_uses_trusted_group_and_plain_component_without_observing_itself(bridge):
    async def send(kind, group_id, chain):
        host.sent(bridge.bot.adapter, group_id, chain, {"message_id": "sent"})
        return NS(message_id="sent")
    bridge.bot.adapter.send_message.side_effect = send
    await bridge.send("30000", "[CQ:at,qq=all] literal text")
    args = bridge.bot.adapter.send_message.await_args.args
    assert args[:2] == ("group", "30000")
    assert len(args[2]) == 1 and isinstance(args[2][0], Plain)
    assert bridge.engine._groups == {}
    with pytest.raises(host.ProactiveUnavailable):
        await bridge.send("90000", "hello")
    assert bridge.bot.adapter.send_message.await_count == 1


@pytest.mark.asyncio
async def test_successful_explicit_reply_is_available_for_natural_followup(bridge):
    host.sent(bridge.bot.adapter, "30000", MessageChain([Plain(text="我的观点")]), {"message_id": "real-reply"}, "20000")
    state = bridge.engine._groups["30000"]
    assert state.last_bot_text == "我的观点"
    assert state.last_bot_id == "real-reply" and state.last_recipient == "20000"


@pytest.mark.asyncio
async def test_admin_config_requires_permission_and_correct_workspace(bridge):
    with pytest.raises(PermissionDeniedError):
        await host.get_config(bridge.bot.ap, admin_context("viewer"), "bot")
    with pytest.raises(host.ProactiveUnavailable):
        await host.get_config(bridge.bot.ap, admin_context(workspace="other"), "bot")
    state = await host.get_config(bridge.bot.ap, admin_context(), "bot")
    assert state["config"]["groups"]["30000"]["mode"] == "natural"
    assert "messages" not in json.dumps(state["status"])


@pytest.mark.asyncio
async def test_management_uses_real_group_list_and_cannot_enable_unknown_group(bridge):
    assert (await host.list_groups(bridge.bot.ap, admin_context(), "bot"))["groups"] == [{"group_id": "30000", "name": "测试群"}]
    config = bridge.store.read()
    config["groups"]["90000"] = {**copy.deepcopy(DEFAULT_GROUP), "mode": "natural"}
    with pytest.raises(ConfigError):
        await host.save_config(bridge.bot.ap, admin_context(), "bot", config)
    assert "90000" not in bridge.store.read()["groups"]


@pytest.mark.asyncio
async def test_global_pause_is_available_offline_but_enabling_is_not(bridge):
    bridge.bot.adapter.get_group_list.side_effect = TimeoutError()
    config = bridge.store.read()
    config["enabled"] = False
    result = await host.save_config(bridge.bot.ap, admin_context(), "bot", config)
    assert result["config"]["enabled"] is False
    config = bridge.store.read()
    config["enabled"] = True
    with pytest.raises(ConfigError):
        await host.save_config(bridge.bot.ap, admin_context(), "bot", config)


@pytest.fixture
def patched_host(tmp_path):
    spec = importlib.util.spec_from_file_location("proactive_test_patcher", ROOT / "scripts/Apply-RuntimePatches.py")
    patcher = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = patcher
    spec.loader.exec_module(patcher)
    source = (ROOT / "patches/host-proactive-chat.patch").read_text(encoding="utf-8")
    _, paths = patcher.runtime_payload(source, "host")
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((LANGBOT_ROOT / path).read_bytes())
    plan = patcher.build_plan("host-proactive-chat.patch", tmp_path, source, "host")
    patcher.execute_plans([plan], apply=True)
    for path in paths:
        ast.parse((tmp_path / path).read_text(encoding="utf-8"))
    assert patcher.build_plan("host-proactive-chat.patch", tmp_path, source, "host").state == "already-applied"
    return tmp_path


def test_patch_applies_only_to_private_copy_and_contains_authenticated_routes(patched_host):
    router = ast.parse((patched_host / "src/langbot/pkg/api/http/controller/groups/platform/bots.py").read_text(encoding="utf-8"))
    methods = [node for node in ast.walk(router) if isinstance(node, ast.AsyncFunctionDef) and node.name in {"proactive_get", "proactive_save", "proactive_groups"}]
    assert len(methods) == 3
    for method in methods:
        route = method.decorator_list[0]
        options = {keyword.arg: ast.unparse(keyword.value) for keyword in route.keywords}
        assert options["permission"] == "Permission.RESOURCE_MANAGE"
        assert options["auth_type"] == "group.AuthType.USER_TOKEN_OR_API_KEY"


def patched_method(base, path, class_name, name, namespace):
    tree = ast.parse((base / path).read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next(node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(base / path), "exec"), namespace)
    return namespace[name]


@pytest.mark.asyncio
async def test_patched_queue_preserves_message_identity_and_releases_failure(bridge, patched_host):
    from langbot_plugin.api.entities.builtin.platform import entities, events, message
    from langbot_plugin.api.entities.builtin.provider import session

    method = patched_method(patched_host, "src/langbot/pkg/platform/botmgr.py", "RuntimeBot", "_handle_legacy_message_event",
                            {"os": os, "platform_events": events, "platform_message": message, "provider_session": session})
    bridge.bot.logger = NS(info=AsyncMock())
    bridge.bot.PIPELINE_DISCARD = "discard"
    bridge.bot.ap.msg_aggregator = NS(add_message=AsyncMock())
    chain = MessageChain([message.Source(id=123, time=int(time.time())), At(target="10000"), Plain(text="明确提问")])
    incoming = events.MessageReceivedEvent(adapter_name="onebot-v11", message_id="123", timestamp=time.time(),
        message_chain=chain, sender=entities.User(id="20000", nickname="群友"),
        chat_type=entities.ChatType.GROUP, chat_id="30000", group=entities.UserGroup(id="30000", name="群"))
    host.observe(bridge.bot, incoming, bridge.bot.adapter)
    legacy = incoming.to_legacy_event()
    await method(bridge.bot, legacy, bridge.bot.adapter, pipeline_uuid_override="pipeline")
    host.ingress_done(bridge.bot, incoming)
    queued = bridge.bot.ap.msg_aggregator.add_message.await_args.kwargs
    assert queued["message_event"] is legacy and queued["message_chain"] is chain
    assert queued["sender_id"] == "20000" and str(queued["launcher_id"]) == "30000"
    assert bridge.engine.status()["groups"]["30000"]["explicit_busy"]
    host.completed(bridge.bot.adapter, legacy)
    assert not bridge.engine.status()["groups"]["30000"]["explicit_busy"]
    host.observe(bridge.bot, incoming, bridge.bot.adapter)
    bridge.bot.ap.msg_aggregator.add_message.side_effect = RuntimeError("queue unavailable")
    with pytest.raises(RuntimeError, match="queue unavailable"):
        await method(bridge.bot, legacy, bridge.bot.adapter, pipeline_uuid_override="pipeline")
    assert not bridge.queued
    assert not bridge.engine.status()["groups"]["30000"]["explicit_busy"]


@pytest.mark.asyncio
async def test_patched_onebot_send_and_reply_record_actual_success_only(bridge, patched_host):
    import aiocqhttp
    from langbot.pkg.platform.adapters.aiocqhttp.message_converter import AiocqhttpMessageConverter
    from langbot_plugin.api.entities.builtin.platform import events, message

    namespace = {"os": os, "typing": typing, "aiocqhttp": aiocqhttp, "platform_events": events,
                 "platform_message": message, "AiocqhttpMessageConverter": AiocqhttpMessageConverter}
    send = patched_method(patched_host, "src/langbot/pkg/platform/adapters/aiocqhttp/api_impl.py", "AiocqhttpAPIMixin", "send_message", namespace)
    reply = patched_method(patched_host, "src/langbot/pkg/platform/adapters/aiocqhttp/api_impl.py", "AiocqhttpAPIMixin", "reply_message", namespace)
    bridge.bot.adapter.bot.send_group_msg = AsyncMock(return_value={"message_id": 910})
    chain = MessageChain([At(target="20000"), Plain(text="我的具体观点")])
    result = await send(bridge.bot.adapter, "group", "30000", chain)
    state = bridge.engine._groups["30000"]
    assert result.message_id == 910 and state.last_bot_id == "910"
    assert state.last_recipient == "20000" and state.last_bot_text == "我的具体观点"
    bridge.bot.adapter.bot.send_group_msg.side_effect = RuntimeError("transport failed")
    with pytest.raises(RuntimeError):
        await send(bridge.bot.adapter, "group", "30000", MessageChain([Plain(text="不能观察失败消息")]))
    assert state.last_bot_id == "910" and state.last_bot_text == "我的具体观点"
    native = aiocqhttp.Event.from_payload({"post_type": "message", "message_type": "group", "group_id": 30000,
                                         "user_id": 20001, "message_id": 123, "self_id": 10000})
    bridge.bot.adapter.bot.send = AsyncMock(return_value={"message_id": 911})
    result = await reply(bridge.bot.adapter, NS(source_platform_object=native), MessageChain([Plain(text="接另一个人的话")]))
    assert result.message_id == 911 and state.last_bot_id == "911" and state.last_recipient == "20001"
