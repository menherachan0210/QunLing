"""Run real query binding, dispatch, host normalization and summary publication."""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain, Source

from pkg.context import ContextStore, KEY
from pkg import group_members, tools
from qunling.group_knowledge import recall
from qunling.integration import CONTEXT_KEY, descriptor, workspace_key
from qunling.qq_group_api import read_group_api
from qunling.network import Fetched

PIXELS = "data:image/jpeg;base64,c3ludGhldGlj"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    binding = NS(instance_uuid="instance", workspace_uuid="workspace", installation_uuid="installation",
                 runtime_revision="revision", placement_generation=1)
    plugin = NS(contexts=ContextStore(), get_installation_binding=lambda: binding,
                get_config=lambda: {"model_uuid": "vision"}, plugin_runtime_handler=object(),
                invoke_llm=AsyncMock(return_value=NS(content="头像中是一只戴眼镜的白猫。", tool_calls=[])))
    source = NS(launcher_type="group", launcher_id="1001", sender_id="201", bot_uuid="bot-a",
                message_chain=MessageChain([Source(id=-990, time=datetime.now()), Plain(text="查看小明的头像和群资料")]))
    event_ctx = NS(event=source, query_id=10, query_uuid="query-a", **vars(binding))
    token = plugin.contexts.capture(plugin, event_ctx, "bot-a")
    variables = {KEY: token, "launcher_type": "group", "launcher_id": "1001", "sender_id": "201",
                 CONTEXT_KEY: descriptor(plugin, source, "query-a")}
    context = plugin.contexts.get(plugin, variables, 10, "query-a")
    context.current_question = "查看小明的头像和群资料"
    proxy = NS(get_query_vars=AsyncMock(return_value=variables), get_bot_uuid=AsyncMock(return_value="bot-a"),
               query_uuid="query-a")
    monkeypatch.setattr(tools, "QueryBasedAPIProxy", lambda **kwargs: proxy)

    def member(user):
        return {"group_id": "1001", "user_id": str(user), "nickname": "小明" if str(user) == "301" else "提问者",
                "card": "", "role": "admin" if str(user) == "301" else "member", "private_profile": "never-forward"}

    async def napcat(action, **params):
        if action == "get_group_member_info":
            return member(params["user_id"])
        if action == "get_group_member_list":
            return [member("201"), member("301")]
        if action == "get_group_root_files":
            return {"folders": [], "files": [{"file_id": "raw-file-id", "file_name": "翻译规范.txt",
                                               "file_size": 4, "busid": 102}]}
        if action == "get_group_file_url":
            return {"url": "https://example.test/file?private=signed"}
        if action == "get_msg":
            return {"message_type": "group", "group_id": "1001", "message_id": -990, "time": context.timestamp,
                    "message": [{"type": "forward", "data": {"id": "raw-forward-resource"}}]}
        if action == "get_forward_msg":
            return {"messages": [{"sender": {"user_id": 700, "nickname": "转发者声称的名字"},
                                  "content": [{"type": "text", "data": {"text": "转发中的蓝色猫是什么"}},
                                              {"type": "image", "data": {"url": "https://example.test/cat?private=signature"}}]}]}
        raise AssertionError(action)

    onebot = NS(call_action=AsyncMock(side_effect=napcat))
    async def platform(bot, api, payload):
        assert bot == "bot-a" and api == "call_platform_api"
        return await read_group_api(onebot, payload["action"], payload["params"])
    plugin.call_platform_api = AsyncMock(side_effect=platform)
    load = AsyncMock(return_value=PIXELS)
    monkeypatch.setattr(group_members, "load_image", load)
    monkeypatch.setattr(tools, "load_image", load)
    return NS(plugin=plugin, context=context, variables=variables, proxy=proxy, binding=binding,
              source=source, event_ctx=event_ctx, onebot=onebot, load=load)


async def call(runtime, action, params=None, *, query_id=10, query_uuid="query-a"):
    return json.loads(await tools.execute(NS(plugin=runtime.plugin), action, params or {}, query_id, query_uuid))


def summaries(runtime, question="小明的头像"):
    return recall(workspace_id=workspace_key(runtime.plugin), bot_id="bot-a", group_id="1001", question=question)


@pytest.mark.asyncio
async def test_member_output_survives_real_host_bridge_and_publishes_metadata_only(runtime):
    result = await call(runtime, "members", {"action": "get", "member_id": "301"})
    assert result["status"] == "ok" and result["member"]["role"] == "admin"
    assert result["member"]["member_ref"].startswith("member_")
    assert "private_profile" not in json.dumps(result)
    records = summaries(runtime, "小明是什么群身份")
    assert len(records) == 1 and records[0]["source"] == "member_info"
    assert "admin" in records[0]["text"] and "member_" not in records[0]["text"]
    assert runtime.context.group_data_inflight == 0
    assert runtime.context.group_data["claimed"] is True


@pytest.mark.asyncio
async def test_avatar_reuses_member_ref_real_scope_and_shared_summary(runtime):
    member = (await call(runtime, "members", {"action": "get", "member_id": "301"}))["member"]
    result = await call(runtime, "avatar", {"target": member["member_ref"], "question": "小明的头像是什么图案"})
    assert result["status"] == "ok" and "白猫" in result["analysis"]
    assert runtime.context.vision_used and runtime.context.group_data_inflight == 0
    runtime.load.assert_awaited_once_with("https://thirdqq.qlogo.cn/g?b=sdk&s=640&nk=301")
    records = summaries(runtime)
    assert any(row["source"] == "avatar_analysis" and "白猫" in row["text"] for row in records)
    serialized = json.dumps(records, ensure_ascii=False)
    assert member["member_ref"] not in serialized and "qlogo" not in serialized
    second = await call(runtime, "avatar", {"target": "self", "question": "再看看"})
    assert second["status"] == "error" and runtime.plugin.invoke_llm.await_count == 1
    assert runtime.context.group_data_inflight == 0


@pytest.mark.asyncio
async def test_group_file_list_then_import_preserves_host_busid_and_asset_scope(runtime, monkeypatch):
    from pkg import group_files
    from qunling.assets import AssetStore, AssetError
    from qunling.scope import Scope

    runtime.context.current_question = "找群文件目录中的翻译规范"
    listed = await call(runtime, "group_files")
    assert listed["status"] == "ok" and listed["entries"][0]["name"] == "翻译规范.txt"
    assert "raw-file-id" not in json.dumps(listed)
    records = summaries(runtime, "群文件目录翻译规范")
    assert len(records) == 1 and records[0]["source"] == "group_file_list"
    fetch = AsyncMock(return_value=Fetched("https://example.test/file", b"data", "text/plain"))
    monkeypatch.setattr(group_files, "fetch_public_file", fetch)
    imported = await call(runtime, "group_files", {"action": "import", "file_ref": listed["entries"][0]["file_ref"]})
    assert imported["status"] == "ok"
    scope = Scope(**runtime.variables[CONTEXT_KEY]["scope"])
    store = AssetStore.from_env()
    assert store.read(scope, imported["asset"]["asset_ref"]) == b"data"
    store.require_visible(scope, imported["asset"]["asset_ref"], runtime.variables[CONTEXT_KEY])
    with pytest.raises(AssetError):
        store.read(Scope(scope.workspace_id, scope.bot_id, scope.conversation_id, "301"), imported["asset"]["asset_ref"])
    assert runtime.onebot.call_action.await_args.args == ("get_group_file_url",)
    assert runtime.onebot.call_action.await_args.kwargs["busid"] == 102
    assert summaries(runtime, "群文件目录翻译规范") == records
    assert runtime.context.group_data_inflight == 0


@pytest.mark.asyncio
async def test_forward_real_host_result_can_be_read_and_visually_analyzed_without_publishing_body(runtime):
    result = await call(runtime, "forwarded")
    assert result["status"] == "ok" and len(result["messages"]) == 1
    row = result["messages"][0]
    assert row["claimed_display_name"] == "转发者声称的名字" and row["speaker_is_verified"] is False
    serialized = json.dumps(result, ensure_ascii=False)
    assert "raw-forward-resource" not in serialized and "signature" not in serialized
    runtime.plugin.invoke_llm.return_value.content = "转发图中是一只蓝色猫。"
    viewed = await call(runtime, "forwarded", {"action": "images", "image_refs": row["image_refs"],
                                             "message_refs": [row["message_ref"]], "question": "解释转发中的蓝色猫"})
    assert viewed["status"] == "ok" and "蓝色猫" in viewed["analysis"]
    sent = runtime.plugin.invoke_llm.await_args.kwargs["messages"]
    assert "未经核验" in sent[0].content or "可能被编辑或伪造" in sent[0].content
    visual_payload = json.dumps([message.model_dump() for message in sent], ensure_ascii=False)
    assert PIXELS in visual_payload and "转发者声称的名字" in visual_payload
    assert "signature" not in visual_payload and "raw-forward-resource" not in visual_payload
    assert summaries(runtime, "转发中的蓝色猫") == []
    assert runtime.context.group_data_inflight == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["query", "query_uuid", "bot", "group", "sender", "installation"])
async def test_scope_rejection_precedes_network_lookup_and_publish(runtime, fault):
    kwargs = {}
    if fault == "query":
        kwargs["query_id"] = 11
    elif fault == "query_uuid":
        kwargs["query_uuid"] = "other-query"
    elif fault == "bot":
        runtime.proxy.get_bot_uuid.return_value = "other-bot"
    elif fault == "group":
        runtime.variables["launcher_id"] = "1002"
    elif fault == "sender":
        runtime.variables["sender_id"] = "301"
    else:
        runtime.binding.runtime_revision = "revoked"
    result = await call(runtime, "members", {"action": "get", "member_id": "301"}, **kwargs)
    assert result["status"] == "error"
    runtime.onebot.call_action.assert_not_awaited()
    assert summaries(runtime) == [] and runtime.context.group_data_inflight == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["foreign_group", "wrong_member", "failed"])
async def test_membership_failure_releases_claim_and_does_not_publish(runtime, fault):
    if fault == "failed":
        runtime.onebot.call_action.side_effect = RuntimeError("private token and endpoint")
    else:
        runtime.onebot.call_action.side_effect = None
        runtime.onebot.call_action.return_value = {"group_id": "999" if fault == "foreign_group" else "1001",
                                                 "user_id": "999" if fault == "wrong_member" else "201", "role": "member"}
    result = await call(runtime, "members")
    assert result["status"] == "error" and "private token" not in json.dumps(result)
    assert runtime.onebot.call_action.await_count == 1
    assert runtime.context.group_data_inflight == 0 and summaries(runtime) == []


@pytest.mark.asyncio
async def test_context_data_budget_evicts_idle_and_never_evicts_inflight(runtime):
    store = runtime.plugin.contexts
    first = runtime.context
    store.claim_group_data(first)
    first.group_data_inflight -= 1
    for index in range(16):
        runtime.event_ctx.query_id = 100 + index
        runtime.event_ctx.query_uuid = f"other-{index}"
        token = store.capture(runtime.plugin, runtime.event_ctx, "bot-a")
        item = store.entries[token]
        store.claim_group_data(item)
        # Sixteen live contexts remain protected. The idle original is evicted.
    assert first not in store.entries.values()
    assert sum(bool(item.group_data) for item in store.entries.values()) == 16
    runtime.event_ctx.query_id = 999
    runtime.event_ctx.query_uuid = "full"
    token = store.capture(runtime.plugin, runtime.event_ctx, "bot-a")
    runtime.variables[KEY] = token
    result = await call(runtime, "members", query_id=999, query_uuid="full")
    assert result["status"] == "error" and "较多" in result["error"]
    assert store.entries[token].group_data_inflight == 0
    runtime.onebot.call_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_releases_dispatch_group_data_claim(runtime):
    entered = asyncio.Event()
    async def pending(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    runtime.onebot.call_action.side_effect = pending
    task = asyncio.create_task(call(runtime, "members"))
    await asyncio.wait_for(entered.wait(), 1)
    assert runtime.context.group_data_inflight == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.context.group_data_inflight == 0
